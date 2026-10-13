"""proposer-review fold regressions — sha canonicalization at the storage boundary +
the is_unauthored_stub empty-blob fail-closed guard. DB-free (the ledger is stubbed).

The failure class under test: the write side was permissive (is_hex_sha normalizes
before matching, so "DEADBEE1\\n" VALIDATES) but stored the RAW value, and the read
side (capture_provenance's `commit_sha ~ '^[0-9a-f]{7,40}$'`) silently drops every
noncanonical stored form — a class can go 'ready' on recurrence yet render its draft
with finding_ids=n/a because the provenance the human needs was stranded.

Run:  PYTHONPATH=src python3 tests/test_capture_fold.py
"""
import asyncio
import os
import sys
import types

import runtime.capture as C

# capturerecord imports the asyncpg-backed PostgresLedger at module level; this suite never
# touches a DB (PostgresLedger is monkeypatched per-test), so when asyncpg is absent from the
# bare interpreter a placeholder module keeps the suite runnable zero-dep. postgres_store only
# dereferences asyncpg attributes inside function bodies (annotations are lazy via
# `from __future__ import annotations`), so an empty module survives the import.
try:
    import asyncpg  # noqa: F401
except ModuleNotFoundError:
    sys.modules["asyncpg"] = types.ModuleType("asyncpg")

import runtime.capturerecord as R  # noqa: E402

PASS = [0]
FAIL = [0]

# _th() reads live $CAPTURE_* at call time inside record(); ambient exports (they are the
# documented live tuning knobs on the armed box) would make these tests machine-dependent.
_CAPTURE_ENV = ["CAPTURE_MIN_RECUR", "CAPTURE_MIN_EVENTS", "CAPTURE_MIN_AUTHORS",
                "CAPTURE_REPROPOSE_MULT", "CAPTURE_MAX_OPEN", "CAPTURE_TTL_DAYS"]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


class _StubLedger:
    """Captures what record() would store; commit_sha is the 4th positional in
    record()'s record_capture(repo_id, tag, path_glob, commit_sha, ...) call."""
    def __init__(self):
        self.shas = []

    async def record_capture(self, *args, **kw):
        self.shas.append(args[3])
        return None

    async def close(self):
        pass


def _run_record(*args):
    """Drive R.record with a stubbed ledger + scrubbed CAPTURE_* env; returns
    (rc, stored_shas, connect_count)."""
    led = _StubLedger()
    connects = [0]

    class _PL:
        @staticmethod
        async def connect(dsn):
            connects[0] += 1
            return led

    saved_pl = R.PostgresLedger
    saved_env = {k: os.environ.pop(k) for k in _CAPTURE_ENV if k in os.environ}
    try:
        R.PostgresLedger = _PL
        rc = asyncio.run(R.record(*args))
    finally:
        R.PostgresLedger = saved_pl
        os.environ.update(saved_env)
    return rc, led.shas, connects[0]


# ---- normalize_sha: the shared normalizer the validator and the storage boundary both use ----
def test_normalize_sha_pure():
    ok(C.normalize_sha("  DEADBEE1 \n") == "deadbee1", "strip + lower -> canonical form")
    ok(C.normalize_sha("deadbee1") == "deadbee1", "canonical input is a fixed point")
    ok(C.normalize_sha("") == "", "empty -> empty")
    ok(C.normalize_sha(None) == "", "None -> empty (mirrors is_hex_sha's `or ''`)")
    ok("normalize_sha" in C.__all__, "normalize_sha is exported")


def test_storable_invariant():
    # is_hex_sha(s) is True  ⟺  normalize_sha(s) fullmatches [0-9a-f]{7,40}  ⟺  the stored
    # form passes capture_provenance's strict SQL hex filter. All accepted variants must
    # normalize to something the read side can see again.
    for s in ["deadbee1", "DEADBEE1", " deadbee1 ", "deadbee1\n", "  DEADBEEF  "]:
        ok(C.is_hex_sha(s), f"{s!r} validates (write-side predicate unchanged)")
        ok(C._HEX_SHA_RE.fullmatch(C.normalize_sha(s)) is not None,
           f"normalize_sha({s!r}) is storable (matches the read-side hex filter's form)")
    for s in ["HEAD", "g1234567", "abcdef", "", None]:
        ok(not C.is_hex_sha(s), f"{s!r} still rejected (predicate behavior identical)")


# ---- the storage boundary: record() must store the canonical form ----
def test_record_normalizes_at_storage_boundary():
    rc, shas, connects = _run_record("repoA", "DEADBEE1\n", "e1", "a1", ["fail-open"], "src/*.py")
    ok(rc == 0, "record returns 0 (fail-open instrumentation contract)")
    ok(shas == ["deadbee1"],
       f"stored sha is CANONICAL (got {shas!r}) — a raw 'DEADBEE1\\n' row is stranded behind "
       "the read-side hex filter and the draft renders finding_ids=n/a")
    rc2, shas2, _ = _run_record("repoA", "  DeadBee2  ", "e1", "a1", ["fail-open"], None)
    ok(rc2 == 0 and shas2 == ["deadbee2"],
       f"whitespace+mixed-case variant also stored canonical (got {shas2!r}) — case variants "
       "of one commit must dedupe on the (fingerprint, commit_sha) PK, never double-count")


def test_record_rejects_non_hex_before_ledger():
    # amendment belt: main() validates, but a direct record() caller (backfill script, future
    # hook) can skip it — a stored non-hex sha counts toward recurrence (count(*) has no hex
    # filter) yet is unmatchable at read time: the exact stranded-provenance failure one layer
    # down. record() must no-op (fail-open, rc 0) without touching the ledger.
    for bad in ["HEAD", "c1", "not-a-sha!"]:
        rc, shas, connects = _run_record("repoA", bad, "e1", "a1", ["fail-open"], None)
        ok(rc == 0, f"non-hex {bad!r} -> rc 0 (fail-open no-op, never breaks the caller)")
        ok(connects == 0, f"non-hex {bad!r} -> ledger never contacted")
        ok(shas == [], f"non-hex {bad!r} -> nothing recorded")


def test_record_main_routed_values_still_record():
    # main()-validated values must keep recording: is_hex_sha(raw) True implies the normalized
    # form re-validates (normalize is idempotent), so the new belt can never turn a
    # previously-accepted call into a no-op.
    rc, shas, connects = _run_record("repoA", "deadbee1", "e1", "a1",
                                     ["fail-open", "toctou-race"], "src/*.py")
    ok(rc == 0 and connects == 1 and shas == ["deadbee1", "deadbee1"],
       f"canonical sha records one occurrence per tag (got shas={shas!r} connects={connects})")


# ---- is_unauthored_stub: degenerate content must count as unauthored (fail-closed) ----
def test_stub_gate_fail_closed_on_degenerate_content():
    ok(C.is_unauthored_stub(""), "empty blob counts as unauthored (a truncated/empty SKILL.md "
                                 "must never pass a gate that uses this as its SOLE check)")
    ok(C.is_unauthored_stub("   \n\t  "), "whitespace-only counts as unauthored")
    ok(C.is_unauthored_stub(None), "None counts as unauthored")
    ok(C.is_unauthored_stub(f"body\n{C.STUB_MARKER}: fill me in"),
       "marker-bearing content still detected (original behavior kept)")
    ok(not C.is_unauthored_stub("---\nname: x\n---\nreal lesson"),
       "authored content still passes (guards against an over-broad fix)")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                FAIL[0] += 1
                print(f"  FAIL (exception): {name}: {type(e).__name__}: {e}")
                continue
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
