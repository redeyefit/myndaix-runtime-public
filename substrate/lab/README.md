# substrate/lab — jobs that run on the LAB (MacBook), hand-managed

The factory's substrate (reconcile + rogue sweep + liveness-canary) governs the Mini only.
Anything here is installed BY HAND on the MacBook and is deliberately outside that loop.

## tailnet-watch

The lab watches the factory: probes the Mini's tailnet node (ssh port via pinned
`/usr/bin/nc`, then `tailscale ping`) every 10 min. Three consecutive **observed** failures
→ macOS notification, re-armed every 4h while dark, recovery notice on heal. Ticks are
**skipped, not counted** when this Mac itself is off the tailnet (plane, captive portal,
wake race) — the watcher only asserts what it observed, and a fail streak with a >30 min
observation gap restarts rather than lying about continuity.

Exists because of the 2026-07-18→08-09 incident: the Mini ran dark off the tailnet for 22
days while its own liveness-canary stayed green (every LOCAL job was healthy — reachability
had no watcher). The Mini is now also the offsite backup mirror, so a dark Mini = silently
single-copy.

Runtime-audit-driven guards: observer-health gate; per-signal latch that only arms when the
notification DELIVERED (failed osascript retries next tick, stderr goes to the log — no
silent suppression on the alert path); 4h re-alert cadence (de-anchors retries from daily
Focus windows); staleness window; atomic state writes; digit-capped `10#` parsing with
future-timestamp clamps; runs from `~/.myndaix/bin`, NOT the git tree (a branch switch must
never kill the monitor). macOS-only by design (`nc -G`, `date -r`).

### Install (once, on the MacBook — every step required)

First edit `substrate/lab/ai.myndaix.tailnet-watch.plist` — it is SITE CONFIG: set `TW_HOST`
(your factory node's tailnet IP; the script fails fast without it) and replace `/Users/you`
with your home directory (launchd does not expand `$HOME`). Then:

```
mkdir -p ~/.myndaix/state ~/.myndaix/bin    # launchd opens the plist stdout paths BEFORE the script runs
cp substrate/lab/tailnet-watch.sh ~/.myndaix/bin/tailnet-watch.sh
cp substrate/lab/ai.myndaix.tailnet-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.myndaix.tailnet-watch.plist
```

The CRITICAL alert also opens a dialog WINDOW (auto-dismisses after 10 min) — Focus modes and
notification-style settings cannot suppress it, so the alert path carries no drifting Settings
dependency. Optional polish (nicer banners; recovery notices visible inside Focus): System
Settings → Notifications → **Script Editor** → style **Alerts**; add Script Editor to Focus
allow-lists.

**Mandatory delivery drill — you must SEE the banner before trusting the watcher:**

```
MYNDAIX_HOME="$(mktemp -d)" TW_HOST=192.0.2.1 TW_FAIL_THRESHOLD=1 bash ~/.myndaix/bin/tailnet-watch.sh
```

(The scratch `MYNDAIX_HOME` keeps the drill out of the live state file — without it the real
agent's next tick would fire a spurious recovery notice.)

### Update flow

The installed copy at `~/.myndaix/bin/tailnet-watch.sh` is a HAND-COPIED deploy artifact
(this consciously extends the known hand-copied set — see runtime-deploy-topology): after
changing `substrate/lab/tailnet-watch.sh` on main, re-run the `cp` line. `diff` them if
unsure which is newer.

**Script and plist ship TOGETHER.** The script takes `TW_HOST` from the plist's
`EnvironmentVariables`; if it is missing (a script-only update against an old plist) or still
the unedited placeholder, every tick logs and raises a "misconfigured — the factory is
UNWATCHED" alert until the plist is fixed — loud, but the watcher still is not watching. After
any update, verify a live tick: `tail ~/.myndaix/state/tailnet-watch.log` shows a clean entry
within 10 min.

### Verify / operate

```
bash substrate/lab/test.sh                      # hermetic suite (repo copy)
launchctl list | grep tailnet-watch             # loaded?
tail ~/.myndaix/state/tailnet-watch.log         # transitions, alerts, notify failures, skips
```

Notes: `tailscale ping -c 1` is verified against the installed GUI-app CLI (1.90.x) — docs
list variants; trust the binary. Flap behavior: an alert+recovery pair fires per distinct
outage (the 4h cap applies within one outage) — accepted; revisit only if flap noise appears.

Residuals (accepted): detection requires the MacBook awake and on the tailnet — worst case
is hours, not weeks. A notification can still be missed if macOS suppresses it after the
one-time settings above drift (OS update resets, new Focus modes) — the log is the forensic
trail. The 24/7 upgrade path is a healthchecks.io dead-man ping from the Mini (needs a Jefe
account); deliberately not built.
