#!/usr/bin/env python3
"""Strict `config.env` parser for the MyndAIX two-machine substrate.

SECURITY BOUNDARY. This program NEVER executes `config.env`. It parses a strict
`KEY=value` dotenv subset — no `source`, no shell, no `$(...)`/backtick command
substitution, no `${VAR}` interpolation — validates every key against a whitelist
and a per-key type, and fails CLOSED on anything unexpected. A compromised or
malformed `config.env` must never execute code or silently widen FACTORY's
behavior (e.g. an empty author allowlist must DENY, not allow-all).

Usage
-----
  config_parse.py <config.env>            validate; print the resolved config as JSON
  config_parse.py <config.env> --get KEY  validate; print exactly one resolved value

Exit 0 on success. Exit 2 (reason on stderr) on any validation failure — reconcile
treats a nonzero exit as a hard ALARM and performs no restart.

Design refs: docs/two-machine-system-design.md §2.4 (validated-not-sourced),
§2.5 (MACHINE_ROLE), §5 (config never printed / fail-closed).
"""
from __future__ import annotations

import json
import re
import sys
from typing import NoReturn

# ---------------------------------------------------------------------------
# Whitelist. Every key config.env may carry is declared here with a validator.
# An unknown key is a hard error (a typo must not silently no-op a security value). So REMOVING a
# key is a migration: strip it from every live config.env BEFORE this parser ships, or reconcile and
# both canaries fail validation (DEV_TREE / DEV_TREE_DRIFT_THRESHOLD, removed 2026-09-22, were).
# ---------------------------------------------------------------------------
ROLES = ("lab", "factory")

_DSN_RE = re.compile(r"^postgres(?:ql)?://\S+$")
# GitHub login: alphanumeric + single hyphens, no leading/trailing hyphen, <=39 chars.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
# Pragmatic strict email (local@domain.tld) — catches typos/garbage without attempting
# RFC 5322; the Gmail API is the real authority at runtime.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _err(msg: str) -> NoReturn:
    sys.stderr.write(f"config_parse: {msg}\n")
    raise SystemExit(2)


def _abspath(key: str, val: str) -> str:
    """An absolute path, no traversal, no NUL/control chars. Existence NOT required
    (config may be validated before the tree is provisioned)."""
    if not val.startswith("/"):
        _err(f"{key}: must be an absolute path (got {val!r})")
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in val):  # C0 + DEL + C1 controls
        _err(f"{key}: control character in path")
    parts = val.split("/")
    if ".." in parts:
        _err(f"{key}: path traversal '..' is not allowed")
    return val


def _dsn(key: str, val: str) -> str:
    if not _DSN_RE.match(val):
        _err(f"{key}: not a postgres DSN (must match postgres[ql]://…, no whitespace)")
    return val


def _role(key: str, val: str) -> str:
    if val not in ROLES:
        _err(f"{key}: must be one of {ROLES} (got {val!r})")
    return val


def _allowlist(key: str, val: str) -> list[str]:
    """Comma-separated GitHub logins. FAIL-CLOSED: an empty list is a hard error on
    the key being present — the caller decides whether the key is required per role,
    but a present-but-empty allowlist (which would allow-all downstream) is rejected."""
    logins = [x.strip() for x in val.split(",") if x.strip()]
    if not logins:
        _err(f"{key}: present but empty — refusing (an empty author allowlist is fail-OPEN)")
    bad = [g for g in logins if not _LOGIN_RE.match(g)]
    if bad:
        _err(f"{key}: invalid GitHub login(s): {bad}")
    return logins


def _cli_path(key: str, val: str) -> list[str]:
    """Colon-separated absolute directories to prepend to PATH for agent CLIs."""
    dirs = [x for x in val.split(":") if x]
    for d in dirs:
        _abspath(key, d)
    return dirs


def _poll(key: str, val: str) -> int:
    # base-10 explicit: a leading-zero value must not be read as octal (the global
    # bash rule ported to Python defensively — int(val) already base-10, but reject
    # non-digit junk loudly).
    if not re.fullmatch(r"[0-9]+", val):
        _err(f"{key}: must be a base-10 integer (got {val!r})")
    n = int(val)
    if n < 60:
        _err(f"{key}: poll floor is 60s (got {n})")
    return n


def _email_list(key: str, val: str) -> list[str]:
    """Comma-separated email addresses (Inbox Assistant accounts). OPTIONAL feature
    switch: absent OR present-but-empty = component OFF. Unlike AUTHOR_ALLOWLIST an
    empty list here fails SAFE downstream (nothing is pulled), so it is accepted."""
    emails = [x.strip() for x in val.split(",") if x.strip()]
    bad = [e for e in emails if not _EMAIL_RE.match(e)]
    if bad:
        _err(f"{key}: invalid email address(es): {bad}")
    return emails


def _vault(key: str, val: str) -> str:
    """A 1Password vault name — conservative charset (the value is interpolated into
    op://<VAULT>/... secret references downstream)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", val):
        _err(f"{key}: must match [A-Za-z0-9_-]+ (got {val!r})")
    return val


def _days(key: str, val: str) -> int:
    # Same base-10 discipline as _poll: digits only, a leading zero is never octal.
    if not re.fullmatch(r"[0-9]+", val):
        _err(f"{key}: must be a base-10 integer (got {val!r})")
    return int(val, 10)


def _email_or_empty(key: str, val: str) -> str:
    """A single email address, or empty = feature off. INBOX_DRIVE_ACCOUNT membership
    in INBOX_ACCOUNTS is checked by the component at runtime, not here."""
    if val and not _EMAIL_RE.match(val):
        _err(f"{key}: not an email address (got {val!r})")
    return val


def _notion_db(key: str, val: str) -> str:
    """A Notion database id (32 hex chars, optionally dashed), or empty = mirror off."""
    if val and not re.fullmatch(r"[A-Fa-f0-9-]{32,36}", val):
        _err(f"{key}: not a Notion database id (32-36 hex/dash chars, got {val!r})")
    return val


def _e164(key: str, val: str) -> str:
    """An E.164-ish phone number (+ then 7-15 digits), or empty = ping off."""
    if val and not re.fullmatch(r"\+[0-9]{7,15}", val):
        _err(f"{key}: not an E.164 phone number like +13105551234 (got {val!r})")
    return val


# key -> (validator, required_always, required_on_factory)
_SCHEMA = {
    "MACHINE_ROLE":     (_role,      True,  True),
    "MYNDAIX_HOME":     (_abspath,   True,  True),
    "MYNDAIX_DSN":      (_dsn,       True,  True),
    "MYNDAIX_WORK_DSN": (_dsn,       False, False),
    "OPERATOR_INBOX":   (_abspath,   False, True),
    "AUTHOR_ALLOWLIST": (_allowlist, False, True),
    "AGENT_CLI_PATH":   (_cli_path,  False, False),
    "POLL_INTERVAL_S":  (_poll,      False, False),
    "DEPLOY_CLONE":     (_abspath,   False, False),  # override; default derived below
    # Inbox Assistant — ALL optional on every role (labs and unconfigured factories
    # must keep converging). Absent INBOX_ACCOUNTS = component off, never an error.
    "INBOX_ACCOUNTS":      (_email_list,     False, False),
    "INBOX_OP_VAULT":      (_vault,          False, False),
    "INBOX_BACKFILL_DAYS": (_days,           False, False),
    "INBOX_DRIVE_ACCOUNT": (_email_or_empty, False, False),
    "INBOX_NOTION_DB":     (_notion_db,      False, False),
    "INBOX_IMESSAGE_TO":   (_e164,           False, False),
}

_DEFAULTS = {"POLL_INTERVAL_S": 900, "INBOX_OP_VAULT": "Automation", "INBOX_BACKFILL_DAYS": 90}


def _unquote(key: str, raw: str) -> str:
    """Strip ONE layer of matching single or double quotes. No expansion of any kind
    happens on the inner text — it is an opaque literal."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    # An unbalanced leading/trailing quote is a config typo — reject loudly rather
    # than guess.
    if v[:1] in ("'", '"') or v[-1:] in ("'", '"'):
        _err(f"{key}: unbalanced quote in value")
    return v


def parse(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        _err(f"config file not found: {path}")
    except OSError as e:
        _err(f"cannot read {path}: {e}")

    raw: dict[str, str] = {}
    for lineno, line in enumerate(lines, 1):
        s = line.rstrip("\n").rstrip("\r")
        stripped = s.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in s:
            _err(f"line {lineno}: not KEY=value")
        key, _, rest = s.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            _err(f"line {lineno}: invalid key {key!r} (must be UPPER_SNAKE)")
        if key not in _SCHEMA:
            _err(f"line {lineno}: unknown key {key!r} (fail-closed on unrecognized config)")
        if key in raw:
            _err(f"line {lineno}: duplicate key {key!r}")
        raw[key] = _unquote(key, rest)

    # Determine role first (drives which keys are required).
    if "MACHINE_ROLE" not in raw:
        _err("MACHINE_ROLE is required")
    role = _role("MACHINE_ROLE", raw["MACHINE_ROLE"])

    resolved: dict = {}
    for key, (validator, req_always, req_factory) in _SCHEMA.items():
        if key in raw:
            resolved[key] = validator(key, raw[key])
        elif req_always or (role == "factory" and req_factory):
            _err(f"{key} is required" + ("" if req_always else " on a factory machine"))
        elif key in _DEFAULTS:
            resolved[key] = _DEFAULTS[key]

    # Derived: DEPLOY_CLONE convention if not overridden.
    resolved.setdefault("DEPLOY_CLONE", resolved["MYNDAIX_HOME"] + "/deploy/myndaix-runtime")
    return resolved


def _emit_value(resolved: dict, key: str) -> None:
    if key not in _SCHEMA:
        _err(f"--get: unknown key {key!r}")
    # An absent OPTIONAL key prints an empty string with exit 0 BY DESIGN — callers (lib.sh
    # substrate_load_config) read optional keys like OPERATOR_INBOX unconditionally and an empty
    # value is the correct "not set" signal. No security decision keys off an empty --get here
    # (required keys already fail validation above; the automerge author gate reads its own env).
    val = resolved.get(key, "")
    if isinstance(val, list):
        # AUTHOR_ALLOWLIST / INBOX_ACCOUNTS / AGENT_CLI_PATH re-serialize naturally.
        sep = ":" if key == "AGENT_CLI_PATH" else ","
        sys.stdout.write(sep.join(val))
    else:
        sys.stdout.write(str(val))
    sys.stdout.write("\n")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _err("usage: config_parse.py <config.env> [--get KEY]")
    path = argv[1]
    resolved = parse(path)
    if len(argv) >= 4 and argv[2] == "--get":
        _emit_value(resolved, argv[3])
    elif len(argv) == 2:
        json.dump(resolved, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        _err("usage: config_parse.py <config.env> [--get KEY]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
