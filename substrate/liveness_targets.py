#!/usr/bin/env python3
"""liveness_targets.py — emit the declared watch-set for liveness-canary.sh.

Usage
-----
  liveness_targets.py <config.env> <descriptor.json> [<descriptor.json> ...]

One TAB-delimited line per job whose descriptor `roles` includes this machine's
MACHINE_ROLE (resolved from the validated config — same role semantics as
render_plist.py role-check):

    <label> TAB <liveness_max_gap_seconds> TAB <requires_sentinel|-> TAB <resolved stdout path>

Descriptors for OTHER roles are silently skipped (excluded, not missing). A
corrupt or schema-invalid descriptor emits

    ERR TAB <basename> TAB <reason>

and processing CONTINUES — one bad file must never sink the batch (design build
note: the canary counts every ERR line as a divergence; fail-closed per file).
A watched-role descriptor MISSING liveness_max_gap_seconds is an ERR too: an
unwatchable job is the exact omission class this kills (opt-in would recreate
the hole).

Exit 0 whenever the batch was processed; nonzero only when the CONFIG itself is
unreadable/invalid (config_parse fails closed) — the canary treats that as a
whole-run divergence.
"""
from __future__ import annotations

import json
import os
import re
import sys

import config_parse           # sibling strict validator (fails closed on bad config)
from render_plist import _subst  # the exact placeholder resolution plists get

_LABEL_RE = re.compile(r"^ai\.myndaix\.[A-Za-z0-9._-]+$")
_SENTINEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _one_line(msg: str) -> str:
    """ERR reasons are embedded in a TAB-delimited line; strip framing chars."""
    return msg.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def _emit_err(name: str, reason: str) -> None:
    sys.stdout.write(f"ERR\t{_one_line(name)}\t{_one_line(reason)}\n")


def _target_line(desc: dict, resolved: dict) -> str:
    """Validate a watched descriptor and build its output line (raises ValueError)."""
    label = desc["label"]
    if not _LABEL_RE.fullmatch(label):
        raise ValueError(f"label {label!r} fails the ai.myndaix.* validation regex")
    max_gap = desc.get("liveness_max_gap_seconds")
    if not isinstance(max_gap, int) or isinstance(max_gap, bool) or max_gap <= 0:
        raise ValueError("missing/invalid liveness_max_gap_seconds (required on every "
                         "watched-role descriptor — an unwatchable job is the omission "
                         "class this canary exists to kill)")
    sentinel = desc.get("requires_sentinel", "-") or "-"
    if sentinel != "-" and not _SENTINEL_RE.fullmatch(str(sentinel)):
        raise ValueError(f"requires_sentinel {sentinel!r} is not a plain token")
    stdout_tpl = desc.get("stdout")
    if not isinstance(stdout_tpl, str) or not stdout_tpl:
        raise ValueError("missing 'stdout' — no execution evidence to watch")
    out_path = _subst(stdout_tpl, resolved)   # SystemExit(2) on a bad placeholder
    if "\t" in out_path or "\n" in out_path:
        raise ValueError("resolved stdout path contains framing characters")
    return f"{label}\t{max_gap}\t{sentinel}\t{out_path}\n"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("usage: liveness_targets.py <config.env> <descriptor.json> [...]\n")
        return 2
    resolved = config_parse.parse(argv[1])   # invalid config -> SystemExit (whole-run failure)
    role = resolved["MACHINE_ROLE"]
    for path in argv[2:]:
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                desc = json.load(fh)
            if not isinstance(desc, dict):
                raise ValueError("descriptor must be a JSON object")
            label = desc.get("label")
            roles = desc.get("roles")
            if not isinstance(label, str) or not label:
                raise ValueError("'label' must be a non-empty string")
            if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
                raise ValueError("'roles' must be a list of strings")
            if role not in roles:
                continue                      # excluded by role-check: skipped, not missing
            sys.stdout.write(_target_line(desc, resolved))
        except SystemExit:
            _emit_err(name, "placeholder resolution failed (unknown {KEY} in stdout)")
        except (OSError, ValueError, json.JSONDecodeError) as e:
            _emit_err(name, str(e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
