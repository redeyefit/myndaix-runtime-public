"""Raw-object-exporter proofs (docs/mxr-review-context-design.md §9) — the design's
security pins VERIFIED against real git, including hand-crafted hostile tree objects
(git mktree/hash-object --literally bypass git's own verify_path exactly like an
attacker's tree would, so the exporter's reimplementation is what's under test).

Run: PYTHONPATH=src python3 tests/test_staging.py
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from runtime import review, staging
from runtime.staging import StagingError

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(repo, *argv, data: bytes = None) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), *argv], input=data,
                       capture_output=True, env=_GIT_ENV)
    assert p.returncode == 0, f"git {argv}: {p.stderr.decode(errors='replace')}"
    return p.stdout


def _mkrepo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="mdx-test-stagerepo."))
    _git(d, "init", "-q")
    return d


def _commit_all(repo: Path, msg="c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").decode().strip()


def _hash_blob(repo: Path, content: bytes) -> str:
    return _git(repo, "hash-object", "-w", "--stdin", data=content).decode().strip()


def _raw_tree(repo: Path, entries) -> str:
    """Hand-craft a tree OBJECT byte-by-byte (mode SP name NUL sha20) and write it with
    --literally — the attacker path: no verify_path, no mktree validation."""
    data = b"".join(mode.encode() + b" " + name + b"\0" + bytes.fromhex(sha)
                    for mode, name, sha in entries)
    return _git(repo, "hash-object", "-t", "tree", "-w", "--stdin", "--literally",
                data=data).decode().strip()


def _commit_tree(repo: Path, tree: str) -> str:
    return _git(repo, "commit-tree", tree, "-m", "hostile").decode().strip()


def _stage(repo: Path, tip: str, root: Path) -> Path:
    return staging.stage_snapshot(repo, tip, root=root)


def _tmproot() -> Path:
    return Path(tempfile.mkdtemp(prefix="mdx-test-stageroot."))


def _fs_case_insensitive(where: Path) -> bool:
    probe = where / "CaSeProbe"
    probe.write_text("x")
    try:
        return (where / "caseprobe").exists()
    finally:
        probe.unlink()


# ---- the exporter's happy path + the three verified design pins -------------------

def test_export_basic_verbatim_and_locked_down():
    repo, root = _mkrepo(), _tmproot()
    try:
        (repo / "a.txt").write_bytes(b"alpha\n")
        (repo / "sub").mkdir()
        (repo / "sub" / "b.bin").write_bytes(bytes(range(256)))
        (repo / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
        (repo / "run.sh").chmod(0o755)
        tip = _commit_all(repo)
        snap = _stage(repo, tip, root)

        # file set == ls-tree blob set (§9), bytes VERBATIM
        listed = set(_git(repo, "ls-tree", "-r", "--name-only", tip).decode().splitlines())
        staged = {str(p.relative_to(snap)) for p in snap.rglob("*") if p.is_file()}
        assert staged == listed, (staged, listed)
        assert (snap / "a.txt").read_bytes() == b"alpha\n"
        assert (snap / "sub" / "b.bin").read_bytes() == bytes(range(256))
        # no .git, no exec bits (100755 deliberately not reproduced), non-writable
        assert not (snap / ".git").exists()
        mode = (snap / "run.sh").stat().st_mode
        assert not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)), oct(mode)
        # OWNER-ONLY + non-writable (kilabz r2 HIGH — no group/other read on a snapshot
        # of possibly-private repo content): files 0400, dirs 0500, incl. the run dir.
        for p in snap.rglob("*"):
            perm = p.stat().st_mode & 0o777
            assert perm == (0o500 if p.is_dir() else 0o400), f"{p}: {oct(perm)}"
        assert snap.stat().st_mode & 0o777 == 0o500, oct(snap.stat().st_mode)
        for p in [snap, *snap.rglob("*")]:
            assert not (p.stat().st_mode & 0o277), f"group/other or writable: {p}"
        # teardown removes it (chmod-before-remove — a-w would wedge a naive rmtree)
        assert staging.teardown_snapshot(snap, root=root) is True
        assert not snap.exists()
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_export_ignore_files_ARE_present():
    # the `git archive` rejection pin (oracle r1 CRITICAL, verified): export-ignore must
    # not hide anything from the raw exporter.
    repo, root = _mkrepo(), _tmproot()
    try:
        (repo / ".gitattributes").write_text("hidden/** export-ignore\n")
        (repo / "hidden").mkdir()
        (repo / "hidden" / "secret-logic.py").write_text("x = 1\n")
        tip = _commit_all(repo)
        snap = _stage(repo, tip, root)
        assert (snap / "hidden" / "secret-logic.py").read_text() == "x = 1\n"
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_attributes_never_mutate_bytes_and_no_filter_executes():
    # the `checkout-index` rejection pin (kilabz r2 HIGH + oracle r2 CRITICAL, convergent):
    # eol/ident/filter must not alter snapshot bytes; the bogus filter must never run
    # (checkout WOULD invoke it — the raw exporter never consults attributes at all).
    repo, root = _mkrepo(), _tmproot()
    try:
        (repo / ".gitattributes").write_text(
            "*.txt text eol=crlf\n*.py ident\n*.dat filter=bogus\n")
        (repo / "eol.txt").write_bytes(b"line1\nline2\n")           # LF committed
        (repo / "id.py").write_bytes(b"# $Id$\n")                   # ident UNexpanded
        (repo / "f.dat").write_bytes(b"raw\n")
        tip = _commit_all(repo)
        # a checkout would need filter.bogus.smudge and would CRLF eol.txt; the exporter:
        snap = _stage(repo, tip, root)
        assert (snap / "eol.txt").read_bytes() == b"line1\nline2\n"   # still LF
        assert (snap / "id.py").read_bytes() == b"# $Id$\n"           # still unexpanded
        assert (snap / "f.dat").read_bytes() == b"raw\n"
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_committed_symlink_is_inert_regular_file():
    # kilabz r2 pin: a committed symlink materializes as a REGULAR FILE containing the
    # target string; nothing in the snapshot is a symlink (nothing to traverse).
    repo, root = _mkrepo(), _tmproot()
    try:
        os.symlink("/etc/hosts", repo / "escape-link")
        tip = _commit_all(repo)
        snap = _stage(repo, tip, root)
        p = snap / "escape-link"
        assert p.is_file() and not p.is_symlink()
        assert p.read_bytes() == b"/etc/hosts"
        assert not any(q.is_symlink() for q in snap.rglob("*"))
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_gitlink_skipped_not_fatal():
    repo, root = _mkrepo(), _tmproot()
    try:
        (repo / "real.txt").write_text("keep\n")
        _git(repo, "add", "-A")
        # a gitlink entry (mode 160000) without cloning a submodule
        _git(repo, "update-index", "--add", "--cacheinfo",
             f"160000,{'1' * 40},vendored")
        _git(repo, "commit", "-q", "-m", "with gitlink")
        tip = _git(repo, "rev-parse", "HEAD").decode().strip()
        snap = _stage(repo, tip, root)
        assert (snap / "real.txt").read_text() == "keep\n"
        assert not (snap / "vendored").exists()          # skipped, silently
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


# ---- hostile trees (kilabz r3 CRITICAL pin): the verify_path reimplementation -----

def _hostile_case(repo: Path, root: Path, entries) -> None:
    """Craft a raw tree with `entries`, commit it, and require staging to FAIL CLOSED
    with nothing left behind (no partial run dir, nothing outside)."""
    tip = _commit_tree(repo, _raw_tree(repo, entries))
    before = {p.name for p in root.iterdir()} if root.is_dir() else set()
    try:
        staging.stage_snapshot(repo, tip, root=root)
    except StagingError:
        after = {p.name for p in root.iterdir()} if root.is_dir() else set()
        assert after == before, f"partial staging left behind: {after - before}"
        return
    raise AssertionError(f"hostile tree staged without error: {entries}")


def test_hostile_tree_paths_fail_closed():
    repo, root = _mkrepo(), _tmproot()
    try:
        blob = _hash_blob(repo, b"evil\n")
        inner = _raw_tree(repo, [("100644", b"escape", blob)])
        hook_tree = _raw_tree(repo, [("100644", b"hook", blob)])

        # `..` as a tree component → would write outside the run dir
        _hostile_case(repo, root, [("40000", b"..", inner)])
        # `.git` as a tree component (ls-tree -r path ".git/hook")
        _hostile_case(repo, root, [("40000", b".git", hook_tree)])
        # nested x/.git/config
        x_tree = _raw_tree(repo, [("40000", b".git", hook_tree)])
        _hostile_case(repo, root, [("40000", b"x", x_tree)])
        # case variants + NTFS alias + HFS-ignorable-char smuggling
        _hostile_case(repo, root, [("40000", b".GIT", hook_tree)])
        _hostile_case(repo, root, [("40000", b"GIT~1", hook_tree)])
        _hostile_case(repo, root, [("40000", ".g‌it".encode(), hook_tree)])
        # absolute path component
        _hostile_case(repo, root, [("100644", b"/etc/cron.d/x", blob)])
        # duplicate names → O_EXCL must refuse the silent double-write
        _hostile_case(repo, root, [("100644", b"dup", blob), ("100644", b"dup", blob)])
        # weird raw modes: git ls-tree CANONICALIZES on read (verified: 100600→100644,
        # 100777→100755), so the exporter's unknown-mode check is an unreachable belt —
        # this stages fine, as a plain non-exec file.
        tip = _commit_tree(repo, _raw_tree(repo, [("100600", b"weird", blob)]))
        snap = staging.stage_snapshot(repo, tip, root=root)
        assert (snap / "weird").read_bytes() == b"evil\n"
        assert not (snap / "weird").stat().st_mode & 0o111
        staging.teardown_snapshot(snap, root=root)
        # ensure nothing ever escaped NEXT TO the root either
        assert not (root.parent / "escape").exists()
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_case_colliding_names():
    # on a case-INSENSITIVE fs (APFS default) FOO/foo collide → O_EXCL fails closed;
    # on a case-sensitive fs (Linux CI) they are two files and staging succeeds.
    repo, root = _mkrepo(), _tmproot()
    try:
        blob = _hash_blob(repo, b"x\n")
        tip = _commit_tree(repo, _raw_tree(
            repo, [("100644", b"FOO", blob), ("100644", b"foo", blob)]))
        if _fs_case_insensitive(root):
            try:
                staging.stage_snapshot(repo, tip, root=root)
                raise AssertionError("case collision must fail closed on case-insensitive fs")
            except StagingError:
                pass
        else:
            snap = staging.stage_snapshot(repo, tip, root=root)
            assert (snap / "FOO").exists() and (snap / "foo").exists()
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_case_colliding_DIRECTORY_prefixes():
    # kilabz r2 HIGH: O_EXCL catches file collisions, but makedirs(exist_ok=True) would
    # SILENTLY MERGE FOO/a and foo/b into one dir on a case-insensitive fs (count check
    # still passes → snapshot diverges from the tree). The dir_owner realpath guard must
    # fail closed on case-insensitive; on case-sensitive the two dirs legitimately coexist.
    repo, root = _mkrepo(), _tmproot()
    try:
        ba = _hash_blob(repo, b"a\n")
        bb = _hash_blob(repo, b"b\n")
        foo_up = _raw_tree(repo, [("100644", b"a", ba)])       # FOO/a
        foo_lo = _raw_tree(repo, [("100644", b"b", bb)])       # foo/b
        tip = _commit_tree(repo, _raw_tree(
            repo, [("40000", b"FOO", foo_up), ("40000", b"foo", foo_lo)]))
        if _fs_case_insensitive(root):
            try:
                staging.stage_snapshot(repo, tip, root=root)
                raise AssertionError("dir-prefix case collision must fail closed")
            except StagingError:
                pass
            assert not list(root.iterdir())                    # nothing left behind
        else:
            snap = staging.stage_snapshot(repo, tip, root=root)
            assert (snap / "FOO" / "a").exists() and (snap / "foo" / "b").exists()
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_case_colliding_INTERMEDIATE_prefixes():
    # oracle r3 HIGH: the collision is at an INTERMEDIATE level (FOO vs foo) while the
    # leaf files sit in DIFFERENT subdirs (FOO/a/x, foo/c/y) — an immediate-parent-only
    # check misses it (FOO/a and foo/c are distinct inodes). The walk-up must catch it.
    repo, root = _mkrepo(), _tmproot()
    try:
        bx = _hash_blob(repo, b"x\n")
        by = _hash_blob(repo, b"y\n")
        a_tree = _raw_tree(repo, [("100644", b"x", bx)])          # a/x
        c_tree = _raw_tree(repo, [("100644", b"y", by)])          # c/y
        foo_up = _raw_tree(repo, [("40000", b"a", a_tree)])        # FOO/a/x
        foo_lo = _raw_tree(repo, [("40000", b"c", c_tree)])        # foo/c/y
        tip = _commit_tree(repo, _raw_tree(
            repo, [("40000", b"FOO", foo_up), ("40000", b"foo", foo_lo)]))
        if _fs_case_insensitive(root):
            try:
                staging.stage_snapshot(repo, tip, root=root)
                raise AssertionError("intermediate case collision must fail closed")
            except StagingError:
                pass
            assert not list(root.iterdir())
        else:
            snap = staging.stage_snapshot(repo, tip, root=root)
            assert (snap / "FOO" / "a" / "x").exists() and (snap / "foo" / "c" / "y").exists()
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_tip_validation_and_caps():
    repo, root = _mkrepo(), _tmproot()
    try:
        (repo / "f").write_text("xx")        # 2 bytes — over the 1-byte cap below
        tip = _commit_all(repo)
        for bad in ("HEAD", "main", tip[:12], tip.upper(), f"--upload-pack={tip}", ""):
            try:
                staging.stage_snapshot(repo, bad, root=root)
                raise AssertionError(f"tip {bad!r} accepted")
            except StagingError:
                pass
        # byte cap (hostile disk-fill guard) fails closed and cleans up
        os.environ["MYNDAIX_STAGING_MAX_BYTES"] = "1"
        try:
            staging.stage_snapshot(repo, tip, root=root)
            raise AssertionError("byte cap not enforced")
        except StagingError:
            pass
        finally:
            os.environ.pop("MYNDAIX_STAGING_MAX_BYTES", None)
        assert not list(root.iterdir())                  # nothing left behind
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


# ---- teardown + reaper --------------------------------------------------------------

def test_ls_tree_stream_parse_and_caps():
    # kilabz r4+r5 HIGH: ls-tree is stream-parsed into a bounded entry list with
    # file-count + metadata caps enforced DURING the read (no full-buffer split/decode).
    repo = _mkrepo()
    try:
        for i in range(5):
            (repo / f"f{i}.txt").write_text("x\n")
        tip = _commit_all(repo)
        # full read returns parsed (mode,type,sha,path) tuples
        entries = staging._read_ls_tree_entries(repo, tip, 100, 1 << 20)
        paths = {e[3] for e in entries}
        assert paths == {f"f{i}.txt" for i in range(5)}
        assert all(e[1] == "blob" for e in entries)
        # a tiny METADATA cap is exceeded → StagingError (not a truncated silent result)
        try:
            staging._read_ls_tree_entries(repo, tip, 100, 4)
            raise AssertionError("metadata cap not enforced")
        except StagingError:
            pass
        # a file-count cap of 2 aborts before the 6th record
        try:
            staging._read_ls_tree_entries(repo, tip, 2, 1 << 20)
            raise AssertionError("file cap not enforced")
        except StagingError:
            pass
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_ls_tree_metadata_cap_independent_of_byte_cap():
    # the metadata cap must NOT scale with MYNDAIX_STAGING_MAX_BYTES (kilabz r5): raising
    # the blob budget for a big-blob repo must not enlarge the ls-tree buffer.
    repo, root = _mkrepo(), _tmproot()
    os.environ["MYNDAIX_STAGING_MAX_BYTES"] = str(1 << 30)     # 1 GiB blob budget
    os.environ["MYNDAIX_STAGING_MAX_META_BYTES"] = "8"         # 8-byte metadata budget
    try:
        for i in range(4):
            (repo / f"f{i}.txt").write_text("x\n")
        tip = _commit_all(repo)
        try:
            staging.stage_snapshot(repo, tip, root=root)
            raise AssertionError("metadata cap should fail closed regardless of byte cap")
        except StagingError:
            pass
        assert not list(root.iterdir())
    finally:
        os.environ.pop("MYNDAIX_STAGING_MAX_BYTES", None)
        os.environ.pop("MYNDAIX_STAGING_MAX_META_BYTES", None)
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_blob_over_cap_rejected_before_read():
    # the DoS fix: a blob whose SIZE exceeds the remaining budget is rejected by the
    # cheap cat-file -s probe BEFORE its content is read, and nothing is staged.
    repo, root = _mkrepo(), _tmproot()
    os.environ["MYNDAIX_STAGING_MAX_BYTES"] = "16"
    try:
        (repo / "big.txt").write_bytes(b"y" * 4096)      # 4 KiB > 16 B cap
        tip = _commit_all(repo)
        try:
            staging.stage_snapshot(repo, tip, root=root)
            raise AssertionError("over-cap blob not rejected")
        except StagingError:
            pass
        assert not list(root.iterdir())                  # partial export removed
        # a normal blob under the cap still streams correctly (verbatim)
        os.environ["MYNDAIX_STAGING_MAX_BYTES"] = "1048576"
        (repo / "ok.txt").write_bytes(b"z" * 100)
        tip2 = _commit_all(repo, "c2")
        snap = staging.stage_snapshot(repo, tip2, root=root)
        assert (snap / "ok.txt").read_bytes() == b"z" * 100
        assert (snap / "big.txt").read_bytes() == b"y" * 4096
    finally:
        os.environ.pop("MYNDAIX_STAGING_MAX_BYTES", None)
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_dir_check_memoized_stat_count():
    # oracle r6 HIGH: the case-collision walk must be O(distinct dirs), not O(files*depth)
    # — a deep tree with many files must not storm the orchestrator with stats. Count the
    # os.stat calls the export makes and assert it equals the number of distinct dirs.
    import runtime.staging as S
    repo, root = _mkrepo(), _tmproot()
    files, depth = 10, 20
    try:
        # `files` files under ONE shared `depth`-deep dir. The collision walk, if
        # memoized, stats each distinct dir ONCE (~depth); unmemoized it would stat
        # files*depth = 200 times. (Total also includes makedirs' O(files) isdir checks.)
        deep = repo.joinpath(*[f"d{i}" for i in range(depth)])
        deep.mkdir(parents=True)
        for i in range(files):
            (deep / f"f{i}.txt").write_text("x\n")
        tip = _commit_all(repo)
        real_stat = os.stat
        calls = {"n": 0}

        def counting_stat(*a, **k):
            calls["n"] += 1
            return real_stat(*a, **k)

        S.os.stat = counting_stat
        try:
            snap = staging.stage_snapshot(repo, tip, root=root)
        finally:
            S.os.stat = real_stat
        # memoized: ~depth (walk) + ~files (makedirs) ≈ 30; unmemoized would be > 200.
        assert calls["n"] < files * depth // 2, \
            f"stat count {calls['n']} suggests the walk is NOT memoized (quadratic)"
        assert all((deep_snap := snap.joinpath(*[f"d{i}" for i in range(depth)]))
                   .joinpath(f"f{i}.txt").exists() for i in range(files))
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_batch_stages_many_blobs_one_process():
    # oracle r6 HIGH: all blobs stream through ONE cat-file --batch (verbatim), not a
    # fork per blob. Verify content correctness across several files + a symlink + binary.
    repo, root = _mkrepo(), _tmproot()
    try:
        for i in range(6):
            (repo / f"f{i}.dat").write_bytes(bytes([i]) * (i * 50 + 1))
        os.symlink("/etc/hosts", repo / "lnk")
        (repo / "sub").mkdir()
        (repo / "sub" / "deep.txt").write_text("nested\n")
        tip = _commit_all(repo)
        snap = staging.stage_snapshot(repo, tip, root=root)
        for i in range(6):
            assert (snap / f"f{i}.dat").read_bytes() == bytes([i]) * (i * 50 + 1)
        assert (snap / "lnk").read_bytes() == b"/etc/hosts" and not (snap / "lnk").is_symlink()
        assert (snap / "sub" / "deep.txt").read_text() == "nested\n"
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_teardown_refuses_non_review_paths():
    root = _tmproot()
    os.environ["MYNDAIX_STAGING_ROOT"] = str(root)
    try:
        victim = root / "curate-20260101-aaaa"           # curator's dir — NOT ours to remove
        victim.mkdir()
        outside = Path(tempfile.mkdtemp(prefix="mdx-test-outside."))
        assert staging.teardown_snapshot(victim) is False and victim.exists()
        assert staging.teardown_snapshot(outside) is False and outside.exists()
        assert staging.teardown_snapshot(root) is False and root.exists()
        # nested review-* (not a DIRECT child) refused too
        nested = victim / "review-x"
        nested.mkdir()
        assert staging.teardown_snapshot(nested) is False and nested.exists()
        ours = root / "review-20260101000000-abcd"
        ours.mkdir()
        assert staging.teardown_snapshot(ours) is True and not ours.exists()
        shutil.rmtree(outside, ignore_errors=True)
    finally:
        os.environ.pop("MYNDAIX_STAGING_ROOT", None)
        shutil.rmtree(root, ignore_errors=True)


def test_reaper_ttl_derived_and_scoped():
    root = _tmproot()
    os.environ["MYNDAIX_STAGING_ROOT"] = str(root)
    try:
        ttl = staging._review_ttl_s()
        # derived, not hand-set: must cover kilabz 900s × ledger MAX_ATTEMPTS 3 + margin
        assert ttl >= 900 * 3 + 3600, ttl
        old = root / "review-old"
        old.mkdir()
        fresh = root / "review-fresh"
        fresh.mkdir()
        foreign = root / "curate-old"                    # out of scope — curate reaps its own
        foreign.mkdir()
        # an a-w snapshot must not wedge the reaper (chmod-before-remove)
        (old / "f").write_text("x")
        os.chmod(old / "f", 0o444)
        os.chmod(old, 0o555)
        past = time.time() - ttl - 60
        os.utime(old, (past, past))          # LAST — writing into the dir bumps its mtime
        os.utime(foreign, (past, past))
        assert staging.reap_old_review_staging(set()) == 1   # empty set = reap freely
        assert not old.exists() and fresh.exists() and foreign.exists()
    finally:
        os.environ.pop("MYNDAIX_STAGING_ROOT", None)
        shutil.rmtree(root, ignore_errors=True)


def test_reaper_never_reaps_in_use_dir():
    # adversarial-review MED: an AGED dir still referenced by a live job must NOT be
    # reaped (liveness by job state, not mtime — the terminal-state-gate defeat).
    root = _tmproot()
    os.environ["MYNDAIX_STAGING_ROOT"] = str(root)
    try:
        ttl = staging._review_ttl_s()
        past = time.time() - ttl - 60
        live = root / "review-live"          # old AND in use → kept
        dead = root / "review-dead"          # old AND not referenced → reaped
        live.mkdir()
        dead.mkdir()
        os.utime(live, (past, past))
        os.utime(dead, (past, past))
        # in_use compares by realpath, so a symlinked/relative spelling still protects
        n = staging.reap_old_review_staging({str(live)})
        assert n == 1 and dead.exists() is False and live.exists()
    finally:
        os.environ.pop("MYNDAIX_STAGING_ROOT", None)
        shutil.rmtree(root, ignore_errors=True)


def test_reaper_none_liveness_reaps_nothing():
    # kilabz r2 MED: in_use=None means "liveness UNKNOWN" (e.g. ledger down) → reap
    # NOTHING, never blind mtime-reap (the round-1 bug class).
    root = _tmproot()
    os.environ["MYNDAIX_STAGING_ROOT"] = str(root)
    try:
        d = root / "review-old"
        d.mkdir()
        past = time.time() - staging._review_ttl_s() - 60
        os.utime(d, (past, past))
        assert staging.reap_old_review_staging(None) == 0 and d.exists()
    finally:
        os.environ.pop("MYNDAIX_STAGING_ROOT", None)
        shutil.rmtree(root, ignore_errors=True)


def test_module_main_seam():
    # the PR-2 seam: stage prints the dir, teardown removes it, failures are rc!=0
    repo, root = _mkrepo(), _tmproot()
    os.environ["MYNDAIX_STAGING_ROOT"] = str(root)
    try:
        (repo / "f").write_text("x")
        tip = _commit_all(repo)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert staging.main(["staging", "stage", str(repo), tip]) == 0
        snap = buf.getvalue().strip()
        assert Path(snap).is_dir()
        assert staging.main(["staging", "teardown", snap]) == 0
        assert not Path(snap).exists()
        assert staging.main(["staging", "stage", str(repo), "not-a-sha"]) == 1
        assert staging.main(["staging", "bogus"]) == 2
    finally:
        os.environ.pop("MYNDAIX_STAGING_ROOT", None)
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


# ---- the `mxr review` verb: tip/range/repo resolution (no pool needed) ---------------

def test_verb_tip_range_coherence():
    repo = _mkrepo()
    try:
        (repo / "f").write_text("one\n")
        c1 = _commit_all(repo, "c1")
        (repo / "f").write_text("two\n")
        c2 = _commit_all(repo, "c2")

        # tip derived from the range end
        tip, base_sha, head_sha = review._resolve_tip(repo, None, f"{c1}..{c2}")
        assert (tip, base_sha, head_sha) == (c2, c1, c2)
        # explicit tip that agrees passes; one that disagrees fails closed
        assert review._resolve_tip(repo, c2, f"{c1}..{c2}")[0] == c2
        for bad_call in (
            lambda: review._resolve_tip(repo, c1, f"{c1}..{c2}"),   # tip != range end
            lambda: review._resolve_tip(repo, "HEAD", None),        # ref name, not sha
            lambda: review._resolve_tip(repo, c2[:12], None),       # short sha
            lambda: review._resolve_tip(repo, None, f"-u..{c2}"),   # leading-dash injection
            lambda: review._resolve_tip(repo, None, f"{c1}...{c2}"),  # three-dot form
            lambda: review._resolve_tip(repo, None, "nosuch..ref"),
        ):
            try:
                bad_call()
                raise AssertionError("expected SystemExit")
            except SystemExit:
                pass
        # tip not local → inline-only signal (None), not an error
        assert review._resolve_tip(repo, "d" * 40, None) == (None, None, None)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_verb_diff_no_textconv_no_driver_exec():
    # kilabz r3 HIGH: a hostile in-tree .gitattributes selecting a host-configured diff
    # driver must NOT run that driver during `mxr review --range`'s git diff. Configure a
    # LOCAL textconv+command driver whose command drops a sentinel, then confirm the diff
    # (built with --no-ext-diff --no-textconv) never fires it.
    repo = _mkrepo()
    sentinel = repo / "DRIVER_RAN"
    try:
        # a driver that would leave a trace if git ever invoked it
        _git(repo, "config", "diff.danger.textconv", f"touch {sentinel};cat")
        _git(repo, "config", "diff.danger.command", f"touch {sentinel};true")
        (repo / ".gitattributes").write_text("*.bin diff=danger\n")
        (repo / "payload.bin").write_bytes(b"before\n")
        c1 = _commit_all(repo, "c1")
        (repo / "payload.bin").write_bytes(b"after\n")
        c2 = _commit_all(repo, "c2")
        # drive the exact diff the verb builds
        out = review._git(repo, ["diff", "--no-ext-diff", "--no-textconv", c1, c2, "--"])
        assert out is not None
        assert not sentinel.exists(), "a hostile diff driver EXECUTED on the host"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_verb_repo_resolution():
    repo = _mkrepo()
    other = Path(tempfile.mkdtemp(prefix="mdx-test-notrepo."))
    rj = Path(tempfile.mkdtemp(prefix="mdx-test-repomap.")) / "repos.json"
    rj.write_text(json.dumps({
        "_comment": "test map",
        "goodrepo": {"path": str(repo)},
        "relpath": {"path": "code/x"},
        "norepo": {"path": str(other)},
    }))
    os.environ["MYNDAIX_REPOS_JSON"] = str(rj)
    try:
        p, rid = review._resolve_repo(str(repo))         # absolute path arg
        assert p == repo.resolve() and rid == repo.name
        p, rid = review._resolve_repo("goodrepo")        # basename via the trusted map
        assert p == repo.resolve() and rid == "goodrepo"
        for bad in ("missing", "relpath", "norepo", "_comment"):
            try:
                review._resolve_repo(bad)
                raise AssertionError(f"{bad!r} resolved")
            except SystemExit:
                pass
    finally:
        os.environ.pop("MYNDAIX_REPOS_JSON", None)
        shutil.rmtree(other, ignore_errors=True)
        shutil.rmtree(rj.parent, ignore_errors=True)
        shutil.rmtree(repo, ignore_errors=True)


def test_verb_warning_strips_cr_lf():
    # kilabz HIGH: the degradation-warning cleaner must strip \r AND \n (a single-line
    # warning; a \r rewrites the line, a \n injects a fake follow-up → log forging).
    dirty = "staging failed: \rreview APPROVE with snapshot\ninjected line\x1b[2K\x9b31m"
    cleaned = review._clean(dirty)
    assert "\r" not in cleaned and "\n" not in cleaned
    assert "\x1b" not in cleaned and "\x9b" not in cleaned
    assert "review APPROVE with snapshot" in cleaned      # content kept, controls gone


def test_export_canonical_parent_guard_present():
    # the MED fix must be a REAL canonical check, not lexical: a happy export still
    # works (the guard never false-rejects a legitimate nested path).
    repo, root = _mkrepo(), _tmproot()
    try:
        (repo / "deep").mkdir()
        (repo / "deep" / "nested").mkdir()
        (repo / "deep" / "nested" / "f.txt").write_text("ok\n")
        tip = _commit_all(repo)
        snap = _stage(repo, tip, root)
        assert (snap / "deep" / "nested" / "f.txt").read_text() == "ok\n"
        # and the resolved snapshot root has no symlink components
        assert os.path.realpath(snap) == str(snap.resolve())
    finally:
        for d in (repo, root):
            shutil.rmtree(d, ignore_errors=True)


def test_verb_prompt_contract():
    # snapshot block ONLY when staged (a reviewer is never told it has a snapshot it
    # doesn't have); diff nonce-fenced as UNTRUSTED; objective above the fence.
    tip = "a" * 40
    with_snap = review._build_prompt("OBJ-TEXT", staged_tip=tip, diff="+ x = 1")
    without = review._build_prompt("OBJ-TEXT", staged_tip=None, diff="+ x = 1")
    no_diff = review._build_prompt("OBJ-TEXT", staged_tip=tip, diff=None)
    assert with_snap.startswith("OBJECTIVE: OBJ-TEXT")
    assert "non-writable snapshot" in with_snap and tip in with_snap
    assert "pointer stubs" in with_snap                          # the LFS note rides along
    assert "non-writable snapshot" not in without
    assert "===BEGIN UNTRUSTED pushed-diff nonce=" in with_snap
    assert "+ x = 1" in with_snap
    assert "BEGIN UNTRUSTED" not in no_diff
    assert with_snap.index("OBJECTIVE") < with_snap.index("BEGIN UNTRUSTED")
    # the two fence markers carry the SAME nonce
    import re as _re
    nonces = _re.findall(r"nonce=([0-9a-f]{32})", with_snap)
    assert len(nonces) == 3 and len(set(nonces)) == 1            # intro + begin + end


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
