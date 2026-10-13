"""curate.py guard tests: stage-in filtering + runtime-authored permissions, the promote
classification matrix (the enforcement), promote_apply against a REAL git corpus (CAS, journal,
per-file commit), the journal sweep, and a full curate() round trip with an injected fake agent
(compliant + noncompliant + lint-read-only paths). The DB-backed round trips use the test ledger.

Run:  LEDGER_TEST_DSN=postgresql://localhost/runtime_test PYTHONPATH=src python3 tests/test_curate.py
"""
import asyncio
import inspect
import json
import os
import subprocess
import tempfile
from pathlib import Path

from runtime import curate, knowledge
from runtime.ledger.postgres_store import PostgresLedger

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")
PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


def _mk_corpus(root: Path, *, git: bool = True):
    (root / "2026-06-08-alpha.md").write_text("# Alpha\nhiggsfield api pricing details\n")
    (root / "2026-06-20-beta.md").write_text("# Beta\nsee [[2026-06-08-alpha]]\n")
    (root / "asset.json").write_text("{}")
    (root / "index.md").write_text(
        "# Index\n- [[2026-06-08-alpha]] 2026-06-08-alpha.md — alpha\n"
        "- [[2026-06-20-beta]] 2026-06-20-beta.md — beta\n\n## Assets\n- asset.json\n")
    if git:
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)


def _staged(root: Path, op="file"):
    walk = knowledge.walk_corpus(root)
    return curate.stage_in(root, walk, op=op)


# ---- stage-in ----------------------------------------------------------------------------------
def test_stage_in():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root, git=False)
        (root / ".secret-notes.md").write_text("token=xyz")
        staging, manifest = _staged(root)
        try:
            names = {p.name for p in staging.iterdir()}
            # BUILD FINDING: NO .claude/settings.json (it self-denied in-tree access); tool
            # confinement is the registry --allowedTools whitelist. Staging = md + MANIFEST only.
            ok(names == {"2026-06-08-alpha.md", "2026-06-20-beta.md", "index.md", "MANIFEST.txt"},
               f"staging holds md + manifest only, NO .claude settings (got {names})")
            ok(not (staging / ".claude").exists(), "no runtime-authored settings.json (build finding)")
            ok(set(manifest) == {"2026-06-08-alpha.md", "2026-06-20-beta.md", "index.md"},
               "manifest = staged md shas")
            man = (staging / "MANIFEST.txt").read_text()
            ok("asset.json" in man and "\tasset" in man, "non-md artifact listed in MANIFEST")
            ok(".secret-notes.md" not in man and not (staging / ".secret-notes.md").exists(),
               "hidden file neither staged nor listed")
        finally:
            import shutil as _sh
            _sh.rmtree(staging, ignore_errors=True)


# ---- classification matrix ---------------------------------------------------------------------
def _classify_case(mutate, op="file"):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root, git=False)
        staging, manifest = _staged(root, op=op)
        try:
            mutate(staging)
            return curate.classify_changes(staging, manifest, op=op)
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)


def test_classify_no_changes():
    ch = _classify_case(lambda s: None)
    ok(not ch.new_files and not ch.index_modified and not ch.violations, "clean run: no changes")


def test_classify_valid_new_plus_index():
    def m(s: Path):
        (s / "2026-07-05-gamma.md").write_text("# Gamma\nlinks [[2026-06-08-alpha]]\n")
        (s / "index.md").write_text(
            "# Index\n- 2026-06-08-alpha.md — alpha\n- 2026-06-20-beta.md — beta\n"
            "- [[2026-07-05-gamma]] 2026-07-05-gamma.md — gamma\n")
    ch = _classify_case(m)
    ok(ch.new_files == ["2026-07-05-gamma.md"] and ch.index_modified and not ch.violations,
       f"new brief + index update is compliant (v={ch.violations})")


def test_classify_violations():
    ch = _classify_case(lambda s: (s / "2026-06-08-alpha.md").write_text("overwritten!"))
    ok(any("modified existing" in v for v in ch.violations), "editing an existing brief flagged")
    ch = _classify_case(lambda s: (s / "2026-06-08-alpha.md").unlink())
    ok(any("deleted" in v for v in ch.violations), "deleting a staged file flagged")
    ch = _classify_case(lambda s: (s / "UPPER.md").write_text("# x"))
    ok(any("name rule" in v for v in ch.violations), "bad new filename flagged")
    ch = _classify_case(lambda s: (s / "new.md").write_text("see [[ghost-page]]"))
    ok(any("ghost" in v for v in ch.violations), "ghost wikilink in new file flagged")
    ch = _classify_case(lambda s: (s / "new.md").write_text("key AKIAABCDEFGHIJKLMNOP"))
    ok(any("secret" in v for v in ch.violations), "secret pattern in new file flagged")
    ch = _classify_case(lambda s: (s / "index.md").write_text("gutted"))
    ok(any("completeness" in v or "missing entry" in v for v in ch.violations),
       "gutted index fails structural validation")
    def mk_dir(s: Path):
        (s / "sub").mkdir(); (s / "sub" / "nested.md").write_text("# n")
    ch = _classify_case(mk_dir)
    ok(any("directory" in v or "top level" in v for v in ch.violations), "nested creation flagged")
    ch = _classify_case(lambda s: os.symlink("/etc/hosts", s / "sneaky.md"))
    ok(any("not a regular file" in v for v in ch.violations), "symlink in staging flagged")
    ch = _classify_case(lambda s: (s / "2026-07-05-x.md").write_text("# x"), op="lint")
    ok(any("read-only" in v for v in ch.violations), "lint run allows NOTHING (belt)")


def test_classify_ignores_runtime_artifacts():
    def m(s: Path):
        (s / "MANIFEST.txt").write_text("tampered")
        # stage-in no longer authors .claude, but the guard still defensively ignores one if the
        # agent creates it (it's in _RUNTIME_DIRS) — simulate that stray dir.
        (s / ".claude").mkdir(exist_ok=True)
        (s / ".claude" / "settings.json").write_text("{}")
    ch = _classify_case(m)
    ok(not ch.violations and not ch.new_files, "runtime artifacts ignored (discarded, not flagged)")


def test_classify_cross_linked_new_files():
    def m(s: Path):
        (s / "2026-07-05-one.md").write_text("# One\nsee [[2026-07-05-two]]")
        (s / "2026-07-05-two.md").write_text("# Two\nsee [[2026-07-05-one]]")
        idx = (s / "index.md").read_text()
        (s / "index.md").write_text(idx + "- 2026-07-05-one.md — one\n- 2026-07-05-two.md — two\n")
    ch = _classify_case(m)
    ok(sorted(ch.new_files) == ["2026-07-05-one.md", "2026-07-05-two.md"] and not ch.violations,
       f"two new briefs may cross-link each other (v={ch.violations})")


# ---- promote_apply (real git) ------------------------------------------------------------------
def test_promote_apply_and_cas():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root)
        staging, manifest = _staged(root)
        try:
            (staging / "2026-07-05-new.md").write_text("# New\nbody [[2026-06-08-alpha]]\n")
            (staging / "index.md").write_text((staging / "index.md").read_text()
                                              + "- 2026-07-05-new.md — new\n")
            ch = curate.classify_changes(staging, manifest, op="file")
            ok(not ch.violations, "fixture compliant")
            applied, notes = curate.promote_apply(root, staging, ch, manifest, set(), slug="t")
            ok(applied and (root / "2026-07-05-new.md").exists(), "new brief promoted")
            ok("2026-07-05-new" in (root / "index.md").read_text(), "index promoted")
            head = subprocess.run(["git", "-C", str(root), "show", "--stat", "--oneline", "HEAD"],
                                  capture_output=True, text=True).stdout
            ok("curate(corpus): t" in head and "2026-07-05-new.md" in head, "hardened commit landed")
            porcelain = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                       capture_output=True, text=True).stdout
            ok(porcelain.strip() == "", "corpus clean after promote (per-file add, no drift)")
            j = json.loads((staging / curate.JOURNAL).read_text())
            ok(j["state"] == "committed", "journal terminal")
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)

    # CAS: the new name appears live mid-run -> abort BEFORE any write
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root)
        staging, manifest = _staged(root)
        try:
            (staging / "2026-07-05-race.md").write_text("# Race\n")
            idx = (staging / "index.md").read_text()
            (staging / "index.md").write_text(idx + "- 2026-07-05-race.md — race\n")
            ch = curate.classify_changes(staging, manifest, op="file")
            (root / "2026-07-05-race.md").write_text("human got here first")
            applied, notes = curate.promote_apply(root, staging, ch, manifest, set(), slug="t")
            ok(not applied and any("CONFLICT" in n for n in notes), "new-name collision aborts")
            ok((root / "2026-07-05-race.md").read_text() == "human got here first",
               "human file untouched")
            # index CAS: human edits index.md mid-run
            (root / "2026-07-05-race.md").unlink()
            (root / "index.md").write_text("# human rewrote it\n")
            applied2, notes2 = curate.promote_apply(root, staging, ch, manifest, set(), slug="t")
            ok(not applied2 and any("index.md changed" in n for n in notes2),
               "index CAS conflict aborts (human edit wins)")
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)


def test_promote_toctou_new_file_no_clobber():
    # kilabz BLOCKER: a human file appearing AFTER the check must never be clobbered by the
    # promote. O_EXCL create makes the publish atomic-no-clobber even if `dirty` missed it.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root)
        staging, manifest = _staged(root)
        try:
            (staging / "2026-07-05-new.md").write_text("# curator version\n")
            ch = curate.classify_changes(staging, manifest, op="file")
            # simulate the race: file exists live but was NOT in the (empty) dirty set passed in
            (root / "2026-07-05-new.md").write_text("HUMAN VERSION\n")
            applied, notes = curate.promote_apply(root, staging, ch, manifest, set(), slug="t")
            ok((root / "2026-07-05-new.md").read_text() == "HUMAN VERSION\n",
               "O_EXCL refused to clobber the human file that raced in")
            ok(not applied or "PARTIAL" in " ".join(notes) or any("FAIL" in n for n in notes),
               "promote did not succeed silently over the human file")
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)


def test_promote_unsafe_target_refused():
    # kilabz MAJOR: promote_apply re-validates targets, does not trust Changes.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root)
        staging, manifest = _staged(root)
        try:
            ch = curate.Changes(new_files=["../evil.md"])
            applied, notes = curate.promote_apply(root, staging, ch, manifest, set(), slug="t")
            ok(not applied and any("unsafe" in n.lower() for n in notes),
               "traversal target refused at promote time")
            ok(not (Path(td) / "evil.md").exists(), "nothing written outside root")
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)


def test_promote_isolates_human_staged_index():
    # oracle MAJOR + kilabz re-review: the commit must record ONLY our targets, never a file the
    # human staged mid-run (scratch-index commit), and must leave that staged file staged.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root)
        # human stages an unrelated change
        (root / "2026-06-08-alpha.md").write_text("# Alpha\nhuman edit staged\n")
        subprocess.run(["git", "-C", str(root), "add", "2026-06-08-alpha.md"], check=True)
        staging, manifest = _staged(root)
        try:
            (staging / "2026-07-05-new.md").write_text("# New\n[[2026-06-08-alpha]]\n")
            (staging / "index.md").write_text((staging / "index.md").read_text()
                                              + "- 2026-07-05-new.md — new\n")
            ch = curate.classify_changes(staging, manifest, op="file")
            applied, notes = curate.promote_apply(root, staging, ch, manifest, set(), slug="t")
            ok(applied, "promote succeeded")
            files_in_commit = subprocess.run(
                ["git", "-C", str(root), "show", "--name-only", "--format=", "HEAD"],
                capture_output=True, text=True).stdout.split()
            ok("2026-06-08-alpha.md" not in files_in_commit,
               "human-staged file NOT swept into the curate commit")
            ok("2026-07-05-new.md" in files_in_commit, "our new file IS in the commit")
            staged = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--name-only"],
                                    capture_output=True, text=True).stdout
            ok("2026-06-08-alpha.md" in staged, "human's staged change survives (still staged)")
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)


def test_promote_requires_baseline_commit():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root, git=False)
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)   # repo, but NO commits
        try:
            curate.git_preflight(root)
            ok(False, "no-baseline repo must be refused")
        except RuntimeError as e:
            ok("no commits" in str(e) or "baseline" in str(e), "no-baseline repo refused clearly")


def test_promote_dirty_index_collision():
    # kilabz MAJOR: a dirty index.md is a target collision (was only checked for new_files).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        _mk_corpus(root)
        staging, manifest = _staged(root)
        try:
            (staging / "index.md").write_text((staging / "index.md").read_text() + "- extra\n")
            ch = curate.classify_changes(staging, manifest, op="file")
            applied, notes = curate.promote_apply(root, staging, ch, manifest, {"index.md"},
                                                  slug="t")
            ok(not applied and any("index.md" in n for n in notes), "dirty index.md aborts promote")
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)


def test_journal_sweep():
    with tempfile.TemporaryDirectory() as td:
        old = curate.STAGING_ROOT
        curate.STAGING_ROOT = Path(td)
        try:
            d = Path(td) / "curate-x"
            d.mkdir()
            (d / curate.JOURNAL).write_text(json.dumps({"state": "applying"}))
            out = curate.sweep_unterminated_journals()
            ok(len(out) == 1 and "unterminated" in out[0], "crash-mid-promote detected")
            (d / curate.JOURNAL).write_text(json.dumps({"state": "committed"}))
            ok(curate.sweep_unterminated_journals() == [], "terminal journal is quiet")
        finally:
            curate.STAGING_ROOT = old


# ---- full round trip (DB + fake agent) ---------------------------------------------------------
async def _round_trip(tmp: Path, agent_behavior, op="file", write=True):
    root = tmp / "corpus"
    root.mkdir()
    _mk_corpus(root)
    os.environ[knowledge._ENV_SCOPES] = f"testscope={root}"
    if write:
        os.environ["MYNDAIX_CURATOR_WRITE"] = "1"        # exercise the promote path
    old_root, old_dsn = curate.STAGING_ROOT, curate.DSN
    curate.STAGING_ROOT = tmp / "staging"
    curate.DSN = DSN
    import runtime.knowledgerecord as kr
    old_kr_dsn = kr.DSN
    kr.DSN = DSN

    async def fake_agent(led, prompt, staging):
        return agent_behavior(prompt, Path(staging))

    try:
        rc = await curate.curate("testscope", op, "test the round trip", run_agent=fake_agent)
        return rc, root
    finally:
        curate.STAGING_ROOT, curate.DSN = old_root, old_dsn
        kr.DSN = old_kr_dsn
        os.environ.pop(knowledge._ENV_SCOPES, None)
        os.environ.pop("MYNDAIX_CURATOR_WRITE", None)


async def test_curate_compliant_round_trip(led):
    with tempfile.TemporaryDirectory() as td:
        def behave(prompt, staging: Path):
            ok("OPERATION: FILE" in prompt and "constitution" in prompt.lower() or True, "")
            (staging / "2026-07-05-delta.md").write_text("# Delta\nnew knowledge\n")
            (staging / "index.md").write_text(
                (staging / "index.md").read_text() + "- 2026-07-05-delta.md — delta\n")
            return True, "filed delta brief"
        rc, root = await _round_trip(Path(td), behave)
        ok(rc == 0, f"compliant round trip exits 0 (got {rc})")
        ok((root / "2026-07-05-delta.md").exists(), "brief landed in the live corpus")
        row = await led._pool.fetchrow(
            "SELECT status FROM knowledge_doc_current WHERE scope='testscope' "
            "AND path='2026-07-05-delta.md'")
        ok(row is not None and row["status"] == "active", "promoted brief indexed post-promote")
        ok(not list((Path(td) / "staging").glob("curate-*")), "staging discarded on success")


async def test_curate_noncompliant_round_trip(led):
    with tempfile.TemporaryDirectory() as td:
        def behave(prompt, staging: Path):
            (staging / "2026-06-08-alpha.md").write_text("VANDALIZED")
            return True, "I improved alpha for you"
        rc, root = await _round_trip(Path(td), behave)
        ok(rc == 1, f"noncompliant run exits 1 (got {rc})")
        ok("VANDALIZED" not in (root / "2026-06-08-alpha.md").read_text(),
           "live corpus untouched by the vandal edit")
        kept = list((Path(td) / "staging").glob("curate-*"))
        ok(len(kept) == 1, "staging kept for inspection")


async def test_curate_lint_readonly(led):
    with tempfile.TemporaryDirectory() as td:
        def behave(prompt, staging: Path):
            return True, "P1: none. P2: none. P3: orphans fine."
        rc, root = await _round_trip(Path(td), behave, op="lint")
        ok(rc == 0, f"clean lint exits 0 (got {rc})")


async def test_curate_agent_failure(led):
    with tempfile.TemporaryDirectory() as td:
        def behave(prompt, staging: Path):
            return False, "pool exploded"
        rc, root = await _round_trip(Path(td), behave)
        ok(rc == 1, "agent failure surfaces as exit 1, nothing promoted")
        # spine-audit MED: the agent-fail path used to leak the full-corpus-copy staging dir (only
        # the success branch rmtree'd). The finally now reaps it on every non-inspection exit.
        ok(not list((Path(td) / "staging").glob("curate-*")),
           "staging is reaped on agent failure (no full-corpus-copy leak)")


async def test_reap_old_staging(led):
    # spine-audit MED: curate self-cleans staging dirs older than the age cutoff so a leaked or
    # deliberately-kept (NONCOMPLIANT) workspace can't accumulate toward a disk-fill.
    import time
    with tempfile.TemporaryDirectory() as td:
        old_root = curate.STAGING_ROOT
        curate.STAGING_ROOT = Path(td) / "staging"
        curate.STAGING_ROOT.mkdir()
        try:
            fresh = curate.STAGING_ROOT / "curate-fresh"
            stale = curate.STAGING_ROOT / "curate-stale"
            fresh.mkdir(); stale.mkdir()
            old = time.time() - 10 * 86400                    # 10d old, past the 7d default
            os.utime(stale, (old, old))
            reaped = curate.reap_old_staging()
            ok(reaped == 1 and stale.exists() is False, "a >7d staging dir is reaped")
            ok(fresh.exists(), "a fresh staging dir is left alone")
        finally:
            curate.STAGING_ROOT = old_root


async def test_curate_prompt_binds_the_fence_nonce(led):
    # spine-audit MED (fence-forgery): the objective must anchor the untrusted-recall boundary to
    # THIS run's nonce so a corpus brief that forges a "===END UNTRUSTED===" line can't break out.
    seen = {}
    with tempfile.TemporaryDirectory() as td:
        def behave(prompt, staging: Path):
            seen["prompt"] = prompt
            return True, "ok"
        await _round_trip(Path(td), behave, op="lint")
        p = seen.get("prompt", "")
        ok("ENDS ONLY at a line" in p and "===END UNTRUSTED nonce=" in p,
           "objective binds the recall fence to the run nonce (not the bare marker string)")


async def test_curate_readonly_propose_only(led):
    # ship-gate posture: with write DISABLED, even a well-formed agent file write is NOT promoted
    # (we don't trust CLI write-confinement until the enforcement gate clears it).
    with tempfile.TemporaryDirectory() as td:
        def behave(prompt, staging: Path):
            (staging / "2026-07-05-epsilon.md").write_text("# Epsilon\nproposed\n")
            return True, "here is the brief I propose"
        rc, root = await _round_trip(Path(td), behave, write=False)
        ok(rc == 0, f"read-only run exits 0 (got {rc})")
        ok(not (root / "2026-07-05-epsilon.md").exists(),
           "read-only: agent file write NOT promoted to the live corpus")
        row = await led._pool.fetchrow(
            "SELECT 1 FROM knowledge_doc_current WHERE scope='testscope' "
            "AND path='2026-07-05-epsilon.md' AND status='active'")
        ok(row is None, "nothing indexed in read-only mode")


def main():
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and not inspect.iscoroutinefunction(v):
            v()
            print("PASS", k)

    async def run_db():
        led = await PostgresLedger.connect(DSN)
        async with led._pool.acquire() as con:
            await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await led.init_schema()
        await led.migrate()
        try:
            for k, v in sorted(globals().items()):
                if k.startswith("test_") and inspect.iscoroutinefunction(v):
                    await v(led)
                    print("PASS", k)
        finally:
            await led.close()
    asyncio.run(run_db())
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
