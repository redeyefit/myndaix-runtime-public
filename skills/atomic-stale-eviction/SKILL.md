---
name: atomic-stale-eviction
description: Prefer kernel flock; mv-evict stale locks, never rm-recreate
path_trigger: "tools/*.sh substrate/*.sh substrate/lab/*.sh orchestrator/*.sh orchestrator/phone/*.sh"
---

Shell advisory locks come in three strengths — the phone surface walked all three in
one PR, so pin the ladder:

1. rm-then-recreate stale eviction: BROKEN (TOCTOU — two waiters both observe stale,
   both rm, the second rm destroys the first waiter's fresh lock; both enter the
   critical section; caps bypass, counters lose increments).
2. mv-eviction (`mv "$lock" "$lock.reaped.$$"` — one-winner rename, staleness re-checked
   adjacent to the mv): strictly better, but still check-then-act. Three review rounds
   each found a NARROWER race in it (mxr-phone r1 M-4/M-5 → r2 MED-4 → r3 HIGH-1) —
   the staleness check and the rename are separate ops, so the class never fully closes.
   If you ship this shape, state which residual it accepts.
3. kernel flock on a permanent lock FILE: ELIMINATES the class. A crashed holder's fd
   dies with its process, so there is no stale state to evict — no reaper, no eviction
   threshold to tune or drift. macOS has no flock(1); perl (always present) is the
   house route. The lock lives on the OPEN FILE DESCRIPTION and releases only when the
   LAST fd referencing it closes — which cuts both ways:
   - bash-held session lock (`exec 8>>file` + perl fdopen ">>&=" + LOCK_NB): bash keeps
     a reference, so children exec-closing THEIR copies never releases it. Safe.
   - perl-opens-then-execs wrapper (perl is the ONLY holder): execve closes perl's
     CLOEXEC'd fd = the last reference = lock RELEASED, section runs unlocked
     (mxr-phone r4 MAJOR-1, probe-proven both directions). Must CLEAR FD_CLOEXEC so
     the exec'd child inherits the fd and carries the lock.

Flag: any `rm -rf <lockpath>` on a staleness branch; mv-eviction presented as fully
atomic; NEW lock code choosing mkdir+reaper where flock fits; a perl flock wrapper
where perl is the SOLE fd holder execing without clearing FD_CLOEXEC (a second live
holder makes the clear unnecessary — check who holds the description before flagging);
pid files written with `|| true` where write failure still counts as acquisition.
