#!/usr/bin/env python3
"""Render a launchd plist from a JSON descriptor + resolved config, via `plistlib`.

Why plistlib and not sed/envsubst (design §2.4): a `&`, `<`, `>` or `]]>` in any
config value (a DSN password, an inbox path) corrupts hand-templated XML and makes
`launchctl bootstrap` silently reject the plist — a tick then just stops firing.
`plistlib.dump` escapes every value correctly, so config values are always safe.

A descriptor (`substrate/plists/<label>.json`) declares a job abstractly:

    {
      "label": "ai.myndaix.controller",
      "roles": ["factory"],                 # MACHINE_ROLEs this job installs on
      "program": ["/bin/bash", "{DEPLOY_CLONE}/orchestrator/controller-tick.sh"],
      "schedule": {"StartInterval": 3600},  # or StartCalendarInterval, or "{POLL_INTERVAL_S}"
      "run_at_load": true,
      "abandon_process_group": false,
      "stdout": "{MYNDAIX_HOME}/orchestrator/controller.out",
      "env": {"MYNDAIX_DSN": "MYNDAIX_DSN", "MYNDAIX_HOME": "MYNDAIX_HOME"}
    }

Placeholders `{KEY}` in string values are replaced with the resolved config value
(validated upstream by config_parse). `env` maps an ENV_VAR name -> a config KEY.

Usage
-----
  render_plist.py role-check <descriptor.json> <role>       exit 0 iff job installs on <role>
  render_plist.py render     <descriptor.json> <config.env>  emit plist XML to stdout

Exit 0 success; 2 on a descriptor/config error; 1 = role-check "does not apply".
"""
from __future__ import annotations

import json
import plistlib
import re
import sys
from typing import NoReturn

import config_parse  # sibling module; strict validator


def _err(msg: str) -> NoReturn:
    sys.stderr.write(f"render_plist: {msg}\n")
    raise SystemExit(2)


_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def _subst(value, resolved: dict):
    """Recursively replace {KEY} placeholders in strings with resolved config values.
    A referenced list config value (AUTHOR_ALLOWLIST) is not substitutable into a
    string placeholder — descriptors never do that (env handles lists)."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in resolved:
                _err(f"placeholder {{{key}}} not in resolved config")
            v = resolved[key]
            if not isinstance(v, (str, int)):
                _err(f"placeholder {{{key}}} resolves to a non-scalar; not substitutable")
            return str(v)
        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, list):
        return [_subst(v, resolved) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v, resolved) for k, v in value.items()}
    return value


def _load_descriptor(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            desc = json.load(fh)
    except FileNotFoundError:
        _err(f"descriptor not found: {path}")
    except json.JSONDecodeError as e:
        _err(f"descriptor is not valid JSON: {e}")
    # Validate TYPES, not just key presence (cross-family review MAJOR): a non-list `roles` (int, str)
    # would make `role in roles` raise TypeError -> Python exits rc1, which reconcile maps to "skip
    # this role" and then orphan-prunes the label. Any schema error must exit 2 (a hard fail), so it
    # is never confused with rc1 = "valid descriptor for another role".
    if not isinstance(desc, dict):
        _err("descriptor must be a JSON object")
    label = desc.get("label")
    roles = desc.get("roles")
    if not isinstance(label, str) or not label:
        _err("descriptor 'label' must be a non-empty string")
    if not isinstance(roles, list) or not roles or not all(isinstance(r, str) and r for r in roles):
        _err("descriptor 'roles' must be a non-empty list of non-empty strings")
    return desc


def role_check(desc_path: str, role: str) -> int:
    desc = _load_descriptor(desc_path)   # validates label/roles types; exits 2 on any schema error
    return 0 if role in desc["roles"] else 1


def _env_dict(desc: dict, resolved: dict) -> dict:
    """Build EnvironmentVariables. `env` maps ENV_VAR -> config KEY; `env_literal` maps
    ENV_VAR -> a template string whose {KEY} placeholders resolve against config (for
    derived values like PLAY_SELF = {DEPLOY_CLONE}/orchestrator/play-review.sh).
    List-valued config (AUTHOR_ALLOWLIST) serializes with commas; AGENT_CLI_PATH is
    prepended to a minimal base PATH when the target env var is PATH."""
    out: dict[str, str] = {}
    base_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    for env_var, cfg_key in desc.get("env", {}).items():
        if cfg_key not in resolved:
            # optional config key absent -> skip injecting it (script keeps its own default)
            continue
        v = resolved[cfg_key]
        if env_var == "PATH":
            prefix = ":".join(v) if isinstance(v, list) else str(v)
            out["PATH"] = f"{prefix}:{base_path}" if prefix else base_path
        elif isinstance(v, list):
            out[env_var] = ",".join(v)
        else:
            out[env_var] = str(v)
    for env_var, template in desc.get("env_literal", {}).items():
        out[env_var] = _subst(template, resolved)
    return out


def build_plist(desc: dict, resolved: dict) -> dict:
    """Build the plist dict from a descriptor + resolved config (no I/O)."""
    program = _subst(desc.get("program"), resolved)
    if not isinstance(program, list) or not program:
        _err("descriptor 'program' must be a non-empty array")

    plist: dict = {
        "Label": desc["label"],
        "ProgramArguments": program,
    }

    sched = desc.get("schedule")
    if sched is not None:
        sched = _subst(sched, resolved)
        if isinstance(sched, dict):
            for k, v in sched.items():
                # A poll interval placeholder resolves to a str-int; coerce.
                if k == "StartInterval" and isinstance(v, str):
                    if not re.fullmatch(r"[0-9]+", v):
                        _err(f"StartInterval resolved to non-integer {v!r}")
                    v = int(v)
                plist[k] = v
        else:
            _err("descriptor 'schedule' must be an object")

    if desc.get("run_at_load"):
        plist["RunAtLoad"] = True
    if desc.get("keep_alive"):
        plist["KeepAlive"] = True
    if desc.get("abandon_process_group"):
        plist["AbandonProcessGroup"] = True

    if "stdout" in desc:
        out = _subst(desc["stdout"], resolved)
        plist["StandardOutPath"] = out
        plist["StandardErrorPath"] = _subst(desc.get("stderr", desc["stdout"]), resolved)

    env = _env_dict(desc, resolved)
    if env:
        plist["EnvironmentVariables"] = env
    return plist


def render_bytes(desc_path: str, config_path: str) -> bytes:
    """Resolve a descriptor against config and return the plist XML bytes.
    Raises SystemExit(2) via _err on a role mismatch or bad descriptor."""
    desc = _load_descriptor(desc_path)
    resolved = config_parse.parse(config_path)
    role = resolved["MACHINE_ROLE"]
    if role not in desc.get("roles", []):
        _err(f"job {desc['label']} does not install on role {role!r} — reconcile should not render it")
    plist = build_plist(desc, resolved)
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)


def render(desc_path: str, config_path: str) -> int:
    # plistlib writes bytes; emit XML to stdout.
    sys.stdout.buffer.write(render_bytes(desc_path, config_path))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "role-check" and len(argv) == 4:
        return role_check(argv[2], argv[3])
    if len(argv) >= 2 and argv[1] == "render" and len(argv) == 4:
        return render(argv[2], argv[3])
    _err("usage: render_plist.py role-check <descriptor.json> <role>\n"
         "                     | render <descriptor.json> <config.env>")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
