#!/usr/bin/env python3
"""Artifact manifest for the two-machine substrate — the drift detector's core.

A SHA alone is weak evidence (design §2.6, kilabz M-1): origin/main can match while
a stale/hand-edited *installed plist* or an unloaded label silently diverges. This
tool records and compares the LIVE ARTIFACTS: deploy SHA, per-managed-script hashes,
installed-vs-freshly-rendered plist hashes, which managed labels are actually loaded,
the migration head object, the dep-source hash, and a secret-stripped config hash.

DB-free and network-free by design (like staging.py): the live migration probe is a
psql one-liner owned by reconcile's restart WAIT; this tool only records the EXPECTED
head object name from the pinned `substrate/migration_head.txt`.

Usage
-----
  manifest.py build <config.env>   emit the live manifest as JSON (the receipt)
  manifest.py check <config.env>   emit the manifest + a "drift" list; exit 1 on any drift

Drift conditions (check): deploy SHA behind origin; an installed plist != its freshly
rendered form; a managed label not loaded. (git-working-tree drift + the live migration
probe are checked by reconcile itself.)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

# Robust sibling resolution: reconcile invokes us as `python3 .../substrate/manifest.py`,
# so substrate/ is sys.path[0] — but make it explicit so the tool works from any CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_parse  # noqa: E402  (sibling module, path set above)
import render_plist  # noqa: E402  # pyright: ignore[reportMissingImports]

# Code files launchd + the pre-push hook resolve directly from the deploy clone (Option A).
# Hashing them gives forensic drift evidence beyond `git status`.
MANAGED_SCRIPTS = [
    "orchestrator/controller-tick.sh",
    "orchestrator/automerge-tick.sh",
    "orchestrator/fix-sweep.sh",
    "orchestrator/play-review.sh",
    "orchestrator/play-fix.sh",
    "substrate/bootstrap-fetch.sh",
    "substrate/reconcile.sh",
    "substrate/drift-canary.sh",
    "substrate/config_parse.py",
    "substrate/render_plist.py",
    "substrate/manifest.py",
]


def _err(msg: str) -> NoReturn:
    sys.stderr.write(f"manifest: {msg}\n")
    raise SystemExit(2)


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str | None:
    try:
        return _sha256_bytes(p.read_bytes())
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def _git(repo: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo, *args],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _label_loaded(uid: int, label: str) -> bool:
    try:
        r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _strip_dsn_userinfo(v: str) -> str:
    """postgres://user:pass@host/db -> postgres://host/db (never hash a secret, §2.6)."""
    return re.sub(r"(postgres(?:ql)?://)[^@/]*@", r"\1", v)


def _config_hash(resolved: dict) -> str:
    """Hash the resolved config with any DSN userinfo stripped — the manifest carries
    no secrets."""
    scrubbed = {}
    for k, val in resolved.items():
        if isinstance(val, str) and "://" in val:
            scrubbed[k] = _strip_dsn_userinfo(val)
        else:
            scrubbed[k] = val
    return _sha256_bytes(json.dumps(scrubbed, sort_keys=True).encode())


def _descriptor_dir(deploy: str) -> Path:
    return Path(deploy) / "substrate" / "plists"


def build(config_path: str) -> dict:
    resolved = config_parse.parse(config_path)
    role = resolved["MACHINE_ROLE"]
    deploy = resolved["DEPLOY_CLONE"]
    uid = os.getuid()
    la = Path.home() / "Library" / "LaunchAgents"

    m: dict = {
        "role": role,
        "deploy_sha": _git(deploy, "rev-parse", "HEAD"),
        "origin_sha": _git(deploy, "rev-parse", "origin/main"),
        "scripts": {},
        "plists_installed": {},
        "plists_expected": {},
        "labels_loaded": {},
    }

    for rel in MANAGED_SCRIPTS:
        h = _sha256_file(Path(deploy) / rel)
        if h is not None:
            m["scripts"][rel] = h

    for desc_path in sorted(_descriptor_dir(deploy).glob("*.json")):
        try:
            desc = json.loads(desc_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _err(f"bad descriptor {desc_path}: {e}")
        # Fail CLOSED on a schema-broken descriptor (missing/typed-wrong label or roles) rather than
        # silently skipping it — a silent skip drops it from plists_expected and it reads as an orphan
        # (cross-family review MAJOR). Only a well-formed descriptor for another role is skipped.
        label = desc.get("label")
        roles = desc.get("roles")
        if not isinstance(label, str) or not isinstance(roles, list):
            _err(f"descriptor missing/invalid 'label' or 'roles': {desc_path}")
        if role not in roles:
            continue  # well-formed, but not a job this role installs
        # A sentinel-gated job (the reconcile poll, requires_sentinel=RECONCILE_ARMED) is only
        # EXPECTED when its sentinel exists — else an unarmed, deliberately-not-installed poll would
        # read as drift (§2.8). reconcile applies the identical gate at install time.
        sentinel = desc.get("requires_sentinel")
        if sentinel and not (Path(resolved["MYNDAIX_HOME"]) / sentinel).exists():
            m.setdefault("disarmed", []).append(label)   # transitionally being disarmed — NOT an orphan
            if _label_loaded(uid, label):                # ...but a STILL-LOADED disarmed job IS drift
                m.setdefault("disarmed_loaded", []).append(label)  #   (full check only) so the poll
            continue                                     #   auto-deployer actually gets unloaded (r2 CRIT #2)
        m["plists_expected"][label] = _sha256_bytes(
            render_plist.render_bytes(str(desc_path), config_path))
        m["plists_installed"][label] = _sha256_file(la / f"{label}.plist") or "absent"
        m["labels_loaded"][label] = _label_loaded(uid, label)

    head_txt = Path(deploy) / "substrate" / "migration_head.txt"
    m["migration_head"] = (head_txt.read_text().strip() if head_txt.exists() else None)
    m["venv_source_hash"] = _sha256_file(Path(deploy) / "pyproject.toml")
    # venv health so the dry-run gate SEES a missing/corrupt in-tree venv (cross-family review MAJOR):
    # otherwise a same-SHA machine with a deleted .venv reads "no drift" and is never repaired.
    _pip = Path(deploy) / ".venv" / "bin" / "pip"
    m["venv_ok"] = _pip.is_file() and os.access(_pip, os.X_OK)
    m["config_hash"] = _config_hash(resolved)

    # Orphan detection (cross-family review CRITICAL): a label reconcile PREVIOUSLY managed but that
    # is no longer expected (its descriptor was removed, or its role no longer matches) yet remains
    # installed/loaded. SCOPED to the recorded managed set (state/managed_labels) — NEVER a bare
    # ai.myndaix.* glob, which would treat unrelated jobs (audio-player, deadman, …) as orphans.
    m["orphans"] = {}
    _disarmed = set(m.get("disarmed", []))
    managed_rec = Path(resolved["MYNDAIX_HOME"]) / "state" / "managed_labels"
    if managed_rec.exists():
        for label in managed_rec.read_text().split():
            # Skip a label that is transitionally DISARMED (sentinel-gated + currently unarmed) — it is
            # deliberately being torn down this converge (reconcile bootouts it), not a stray orphan
            # (cross-family review CRITICAL #2 — else the disarmed-but-still-loaded poll reads as drift
            # and the disarm converge fails before it can unload the poll).
            if not label or label in m["plists_expected"] or label == "ai.myndaix.runtime" or label in _disarmed:
                continue
            inst = _sha256_file(la / f"{label}.plist")
            loaded = _label_loaded(uid, label)
            if inst is not None or loaded:
                m["orphans"][label] = {"installed": inst is not None, "loaded": loaded}
    return m


def drift_list(m: dict, health_only: bool = False) -> list[str]:
    drift: list[str] = []
    # deploy-vs-origin SHA currency is a DRIFT-DETECTOR concern (are we behind origin?), NOT a
    # post-converge HEALTH concern. health_gate's verify passes health_only=True so an AUTO-REVERT
    # (deploy=last-good while origin=bad) isn't reported as a failed converge (cross-family review
    # CRITICAL #1). The dry-run + drift-canary keep the full check (they WANT the SHA-currency signal).
    if not health_only:
        if not m["deploy_sha"] or not m["origin_sha"]:
            drift.append(f"unresolvable SHA (deploy={m['deploy_sha']}, origin={m['origin_sha']}) — "
                         "treat as drift")
        elif m["deploy_sha"] != m["origin_sha"]:
            drift.append(f"deploy behind origin: {m['deploy_sha'][:8]} != {m['origin_sha'][:8]}")
        # A sentinel-disarmed job that is STILL LOADED is drift in the FULL check (so the drift-canary /
        # a poll's dry-run forces a converge whose final step boots it out) — but NOT under --health-only
        # (so the disarming converge's own verify can pass and reach that bootout). (r2 CRITICAL #2)
        for label in m.get("disarmed_loaded", []):
            drift.append(f"sentinel-disarmed job still loaded: {label} — converge to bootout it")
    for label, expected in m["plists_expected"].items():
        installed = m["plists_installed"].get(label)
        if installed != expected:
            drift.append(f"plist drift: {label} (installed {str(installed)[:8]} != expected {expected[:8]})")
        if not m["labels_loaded"].get(label):
            drift.append(f"label not loaded: {label}")
    for label, o in m.get("orphans", {}).items():
        drift.append(f"orphaned managed label still present: {label} "
                     f"(installed={o['installed']} loaded={o['loaded']})")
    if not m.get("venv_ok", True):
        drift.append("venv missing/invalid (.venv/bin/pip not executable) — converge to repair")
    return drift


def main(argv: list[str]) -> int:
    # manifest.py build <config> | check [--health-only] <config>
    args = argv[1:]
    health_only = False
    if "--health-only" in args:
        health_only = True
        args = [a for a in args if a != "--health-only"]
    if len(args) != 2 or args[0] not in ("build", "check"):
        _err("usage: manifest.py build|check [--health-only] <config.env>")
    m = build(args[1])
    if args[0] == "build":
        json.dump(m, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    # check
    drift = drift_list(m, health_only=health_only)
    out = {"manifest": m, "drift": drift}
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
