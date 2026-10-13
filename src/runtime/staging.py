"""Review snapshot staging — the RAW OBJECT EXPORTER (docs/mxr-review-context-design.md D1).

Stages an ephemeral, de-linked, non-writable snapshot of a repo at a reviewed tip, to
become a CONFINED reviewer's cwd (kilabz/lobster; runner validates the path — D2/D3).
The snapshot is additive "verify against real code"; the inlined fenced diff stays the
review's source of truth, so every staging failure has an inline-only fallback (§4).

WHY a raw exporter and not git's own machinery (all three verified during design review):
- `git archive` honors in-tree `.gitattributes export-ignore` — a hostile PR can silently
  HIDE whole directories from the snapshot.
- `git checkout-index` (and any checkout path) runs attribute processing: `* text eol=crlf`
  / `ident` mutate staged bytes (silent divergence from the fenced diff), and a `filter=`
  driver reference executes HOST-side during staging (SSRF/RCE class on the orchestrator,
  outside any agent sandbox). It also materializes committed symlinks LIVE.
The raw walk (`ls-tree -r -z` + per-blob `cat-file blob`) kills all three by construction:
nothing consults .gitattributes, symlink blobs are written as regular files CONTAINING the
target string (inert), gitlinks are skipped, and no exec bits are set.

HOSTILE TREE PATHS (kilabz r3 CRITICAL): bypassing checkout also bypassed git's
`verify_path()` — so this module REIMPLEMENTS it. ls-tree paths come from an UNTRUSTED
tree object; a hand-crafted tree can contain `..`, `.git` (any case / HFS-ignorable-char
variants), duplicates, or case-colliding names (silent overwrite on case-insensitive APFS).
Per entry, BEFORE writing: reject absolute paths, empty/`.`/`..` components, any component
that case-insensitively normalizes to `.git` (or the NTFS `git~1` alias); create files
O_EXCL|O_NOFOLLOW (duplicates and case collisions fail LOUDLY, never collapse); verify the
final path is strictly under the run dir. ANY violation raises StagingError — the caller's
§4 policy decides (gate mode fails closed; the human loop degrades loudly to inline-only).

Every git step's exit is asserted independently (no pipeline), each under a hard timeout,
and a final count check (files written == ls-tree blob count) makes partial exports loud.

PR-2 seam: `python3 -m runtime.staging stage|teardown|reap ...` gives play-review.sh the
same exporter without a bash reimplementation.
"""
from __future__ import annotations

import os
import re
import secrets
import select
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional

_GIT_TIMEOUT_S = 120          # per git step — mirrors workspace._git's hang ceiling
_TIP_RE = re.compile(r"[0-9a-f]{40}")
# HFS+-ignorable code points git strips in is_hfs_dotgit (a name like ".g‌it" reaches
# the filesystem as ".git" under HFS normalization). Same set, same purpose.
_HFS_IGNORABLE = {
    0x200C, 0x200D, 0x200E, 0x200F,
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F,
    0xFEFF,
}
# extraction caps — a hostile tree must not disk-fill the host before the count check runs.
# Generous vs any real review repo (this one: ~200 files / ~3 MB); env-overridable for a
# legitimately bigger repo, never silently exceeded.
_MAX_FILES_DEFAULT = 20_000
_MAX_BYTES_DEFAULT = 512 * 1024 * 1024
# ls-tree METADATA cap — the in-memory budget for the entry list, INDEPENDENT of the blob
# byte budget (kilabz r5: raising MAX_BYTES for a big-blob repo must not enlarge the
# ls-tree buffer a hostile deep-path tree can amplify). One tree path is capped too.
_MAX_META_BYTES_DEFAULT = 64 * 1024 * 1024
_MAX_PATH_LEN = 8192


class StagingError(RuntimeError):
    """Any staging failure — validation, hostile path, git error, cap, timeout. The
    message is operator-facing; callers apply the §4 fallback policy."""


def staging_root() -> Path:
    """The ONE namespace the runner accepts a context.workdir under. Must agree with
    runner._staging_root / curate.STAGING_ROOT — all three read the same env override."""
    return Path(os.environ.get("MYNDAIX_STAGING_ROOT")
                or Path.home() / ".myndaix" / "orchestrator" / "staging")


def _int_env(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    return int(v) if re.fullmatch(r"[1-9][0-9]{0,9}", v) else default


def _git(repo: Path, argv: list[str]) -> bytes:
    """One git step: exit asserted, stderr surfaced, hard timeout, prompts disabled.
    Raises StagingError on ANY failure — never returns partial output."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *argv],
            capture_output=True, timeout=_GIT_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise StagingError(f"git {argv[0]}: {e}") from e
    if p.returncode != 0:
        err = p.stderr.decode(errors="replace").strip()[:300]
        raise StagingError(f"git {argv[0]} exited {p.returncode}: {err}")
    return p.stdout


def _parse_record(rec: bytes) -> tuple[str, str, str, str]:
    """Parse ONE `ls-tree -z` record (`mode SP type SP sha TAB path`) → (mode, type, sha,
    path). Path decoded strictly as UTF-8 (a non-UTF-8 path can't be safely validated
    against the dot-git/traversal rules → fail closed) and length-capped so one absurd
    path can't bloat the entry list."""
    try:
        meta_part, path_b = rec.split(b"\t", 1)
        mode, otype, sha = meta_part.split(b" ", 2)
        path = path_b.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise StagingError(f"unparseable ls-tree entry: {rec[:100]!r} ({e})") from e
    if len(path.encode("utf-8")) > _MAX_PATH_LEN:
        raise StagingError(f"tree path too long for {path[:60]!r} ({len(path)} chars)")
    return mode.decode(), otype.decode(), sha.decode(), path


def _read_ls_tree_entries(repo: Path, tip: str, max_files: int,
                          max_meta: int) -> list[tuple[str, str, str, str]]:
    """Stream `git ls-tree -r -z <tip>` and parse it INCREMENTALLY into a bounded entry
    list, enforcing the file-count, metadata-byte, and per-path-length caps DURING the
    read — so a hostile tree can't OOM by amplifying metadata (many tiny blobs under very
    long/deep paths) via a full-output buffer + split()/decode copies (kilabz r5 HIGH).
    The metadata cap is INDEPENDENT of the blob byte budget (raising MAX_BYTES for a
    big-blob repo must not enlarge the in-memory ls-tree buffer). The WHOLE stream is read
    and git closed BEFORE any blob is written, so slow blob writes can't trip this read's
    timeout. select() = hang safety; git's nonzero exit fails closed."""
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(repo), "ls-tree", "-r", "-z", tip],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except OSError as e:
        raise StagingError(f"git ls-tree: {e}") from e
    entries: list[tuple[str, str, str, str]] = []
    buf = bytearray()
    meta = 0
    deadline = time.monotonic() + _GIT_TIMEOUT_S
    try:
        eof = False
        while not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                raise StagingError("git ls-tree timed out")
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                proc.kill()
                raise StagingError("git ls-tree timed out")
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                eof = True
            else:
                meta += len(chunk)
                if meta > max_meta:
                    proc.kill()
                    raise StagingError(f"ls-tree metadata exceeds cap {max_meta}")
                buf += chunk
            nul = buf.find(b"\0")                       # drain complete records
            while nul != -1:
                rec = bytes(buf[:nul])
                del buf[:nul + 1]
                entries.append(_parse_record(rec))
                if len(entries) > max_files:
                    proc.kill()
                    raise StagingError(f"tree exceeds file cap {max_files}")
                nul = buf.find(b"\0")
            if len(buf) > _MAX_PATH_LEN + 512:          # one unterminated record too long
                proc.kill()
                raise StagingError("ls-tree record too long")
        if buf:
            raise StagingError("ls-tree ended mid-record (no trailing NUL)")
    finally:
        # kill BEFORE wait (oracle r6 MED): if _parse_record raised, the cap kills were
        # skipped — a git blocked on a full stderr pipe would never see SIGPIPE on stdout,
        # so proc.wait() could deadlock forever. Kill an un-exited child first.
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            err = proc.stderr.read().decode(errors="replace").strip()[:300]
        except OSError:
            err = ""
        finally:
            proc.stderr.close()
        rc = proc.wait()
    if rc != 0:
        raise StagingError(f"git ls-tree exited {rc}: {err}")
    return entries


class _CatFileBatch:
    """One long-running `git cat-file --batch` for the WHOLE tree — feed a blob sha on
    stdin, read a `<sha> <type> <size>\\n` header, then <size> content bytes + a trailing
    `\\n`. This replaces a per-blob `cat-file -s` + `cat-file blob` fork pair (oracle r6
    HIGH: 2 forks × 20k files = 40k fork/exec ≈ 200s of blocking wall-clock — an
    asymmetric DoS on the single-threaded orchestrator). The header carries the size, so
    an over-budget blob is rejected BEFORE its content is read, and content streams
    straight to the dest fd (never buffered in Python — the r4 OOM property is kept).
    stderr → DEVNULL so a chatty git can't fill an unread pipe and deadlock; per-object
    status ('missing') comes on stdout and process failure via a short/closed stream."""

    def __init__(self, repo: Path):
        try:
            self._p = subprocess.Popen(
                ["git", "-C", str(repo), "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except OSError as e:
            raise StagingError(f"git cat-file --batch: {e}") from e
        self._fd = self._p.stdout.fileno()

    def _read(self, n: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StagingError("git cat-file --batch timed out")
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                raise StagingError("git cat-file --batch timed out")
            chunk = os.read(self._fd, min(65536, n - len(buf)))
            if not chunk:
                raise StagingError("git cat-file --batch closed early")
            buf += chunk
        return bytes(buf)

    def _read_header(self, deadline: float) -> bytes:
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StagingError("git cat-file --batch timed out")
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                raise StagingError("git cat-file --batch timed out")
            ch = os.read(self._fd, 1)
            if not ch:
                raise StagingError("git cat-file --batch closed early")
            if ch == b"\n":
                return bytes(buf)
            buf += ch
            if len(buf) > 4096:
                raise StagingError("git cat-file --batch header too long")

    def write_blob(self, sha: str, fileobj, budget: int) -> int:
        """Stream blob <sha> into fileobj, rejecting closed if its size exceeds `budget`
        (the remaining byte cap). Returns the blob size. A once-per-blob deadline keeps
        the read hang-safe without coupling to the whole export's duration."""
        deadline = time.monotonic() + _GIT_TIMEOUT_S
        try:
            self._p.stdin.write(sha.encode() + b"\n")
            self._p.stdin.flush()
        except OSError as e:
            raise StagingError(f"git cat-file --batch write: {e}") from e
        header = self._read_header(deadline).decode("utf-8", "replace")
        parts = header.split(" ")
        if len(parts) == 2 and parts[1] in ("missing", "ambiguous"):
            raise StagingError(f"blob {parts[1]} in batch: {sha}")
        if len(parts) != 3 or parts[1] != "blob":
            raise StagingError(f"unexpected cat-file --batch header: {header!r}")
        try:
            size = int(parts[2])
        except ValueError:
            raise StagingError(f"bad size in cat-file --batch header: {header!r}")
        if size > budget:                              # reject BEFORE reading content
            raise StagingError(f"snapshot exceeds byte cap (blob {size} > remaining {budget})")
        remaining = size
        while remaining > 0:
            dl = deadline - time.monotonic()
            if dl <= 0:
                raise StagingError("git cat-file --batch timed out")
            ready, _, _ = select.select([self._fd], [], [], dl)
            if not ready:
                raise StagingError("git cat-file --batch timed out")
            chunk = os.read(self._fd, min(65536, remaining))
            if not chunk:
                raise StagingError("git cat-file --batch closed early")
            try:
                fileobj.write(chunk)
            except OSError as e:
                raise StagingError(f"blob write failed for {sha}: {e}") from e
            remaining -= len(chunk)
        self._read(1, deadline)                        # consume the trailing newline
        return size

    def close(self) -> None:
        try:
            if self._p.stdin:
                self._p.stdin.close()
        except OSError:
            pass
        if self._p.poll() is None:                     # kill BEFORE wait — never deadlock
            try:
                self._p.kill()
            except OSError:
                pass
        self._p.wait()
        try:
            self._p.stdout.close()
        except OSError:
            pass


def _is_dotgit(component: str) -> bool:
    """git's is_hfs_dotgit + NTFS alias, reimplemented: strip HFS-ignorable code points,
    casefold, compare against '.git' and 'git~1'. NFC-normalize first so a decomposed
    spelling can't dodge the comparison on a normalizing filesystem."""
    comp = unicodedata.normalize("NFC", component)
    comp = "".join(ch for ch in comp if ord(ch) not in _HFS_IGNORABLE).casefold()
    return comp in (".git", "git~1")


def _verify_entry_path(path: str) -> None:
    """The verify_path() reimplementation (D1). Raises StagingError on any hostile shape;
    a clean return means the path is a safe RELATIVE path whose every component is a
    plain name."""
    if not path:
        raise StagingError("tree entry with empty path")
    if path.startswith("/") or os.path.isabs(path):
        raise StagingError(f"tree entry with absolute path: {path!r}")
    if "\\" in path:
        # a backslash is a legal POSIX filename byte, but it is ALSO a path separator to
        # a checkout on Windows/some tools — nothing in a reviewed repo needs one; reject
        # rather than reason about it.
        raise StagingError(f"tree entry with backslash: {path!r}")
    for comp in path.split("/"):
        if comp in ("", ".", ".."):
            raise StagingError(f"tree entry with traversal component: {path!r}")
        if _is_dotgit(comp):
            raise StagingError(f"tree entry with .git component: {path!r}")


def stage_snapshot(repo_path: str | os.PathLike, tip: str, *,
                   root: Optional[Path] = None) -> Path:
    """Export <repo>@<tip> into a fresh run dir under the staging root and return it.
    Raises StagingError on ANY failure (the partial dir is removed first). The returned
    snapshot is non-writable (a-w), contains no .git, no symlinks, no exec bits."""
    repo = Path(repo_path)
    # tip is the RESOLVED sha — never a ref name, so a branch named `-u` can't inject
    # flags into git argv (oracle r2). fullmatch, lowercase-only.
    if not isinstance(tip, str) or not _TIP_RE.fullmatch(tip):
        raise StagingError(f"tip must be a 40-hex sha, got {tip!r}")
    if not (repo / ".git").exists():
        raise StagingError(f"not a git repo: {repo}")

    sroot = root or staging_root()
    sroot.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d%H%M%S")
    rundir = (sroot / f"review-{ts}-{secrets.token_hex(4)}").resolve()
    try:
        rundir.mkdir(mode=0o700)          # must NOT pre-exist — FileExistsError is a failure
    except OSError as e:
        raise StagingError(f"staging dir create failed: {e}") from e

    try:
        _export_tree(repo, tip, rundir)
    except StagingError:
        _force_remove(rundir)             # a partial export must never look staged
        raise
    except Exception as e:                # noqa: BLE001 — same guarantee for the unforeseen
        _force_remove(rundir)
        raise StagingError(f"staging failed: {type(e).__name__}: {e}") from e
    return rundir


def _export_tree(repo: Path, tip: str, rundir: Path) -> None:
    max_files = _int_env("MYNDAIX_STAGING_MAX_FILES", _MAX_FILES_DEFAULT)
    max_bytes = _int_env("MYNDAIX_STAGING_MAX_BYTES", _MAX_BYTES_DEFAULT)
    max_meta = _int_env("MYNDAIX_STAGING_MAX_META_BYTES", _MAX_META_BYTES_DEFAULT)
    # ls-tree is stream-parsed into a bounded entry list (file-count + metadata + per-path
    # caps enforced DURING the read), fully read + git closed BEFORE any blob is written.
    entries = _read_ls_tree_entries(repo, tip, max_files, max_meta)

    expected = sum(1 for _, otype, _, _ in entries if otype == "blob")

    written = 0
    total_bytes = 0
    rd = str(rundir)
    rd_real = os.path.realpath(rd)                  # rundir is fresh + not a symlink (mkdir)
    # dir-prefix case-collision guard (kilabz r2 HIGH): O_EXCL catches FILE collisions,
    # but `makedirs(exist_ok=True)` SILENTLY MERGES case-colliding DIRECTORY prefixes on
    # a case-insensitive fs (APFS): tree entries `FOO/a` then `foo/b` land in ONE physical
    # dir, the count check still passes, and the snapshot diverges from the tree instead of
    # failing closed. Keyed on the parent's (st_dev, st_ino) INODE — NOT realpath, which on
    # macOS preserves the requested case spelling rather than the on-disk one: on a
    # case-insensitive fs the two rel-parents share ONE inode; on a case-sensitive fs they
    # are distinct dirs with distinct inodes.
    dir_owner: dict[tuple, str] = {}
    checked_dirs: set[str] = set()                  # dirs whose full ancestor chain is validated
    batch = _CatFileBatch(repo)                     # ONE git process for ALL blobs (oracle r6 HIGH)
    try:
        for mode, otype, sha, path in entries:
            if otype == "commit" and mode == "160000":
                continue                               # gitlink (submodule) — skip
            if otype != "blob":
                raise StagingError(f"unexpected ls-tree entry type {otype!r} for {path!r}")
            if mode not in ("100644", "100755", "120000"):
                raise StagingError(f"unexpected blob mode {mode!r} for {path!r}")
            _verify_entry_path(path)

            dst = os.path.normpath(os.path.join(rd, path))
            # lexical belt: components are already validated (no ..,/.git), so normpath
            # can't escape — but assert anyway.
            if os.path.commonpath([dst, rd]) != rd or dst == rd:
                raise StagingError(f"entry escapes run dir: {path!r}")

            parent = os.path.dirname(dst)
            if parent != rd:
                try:
                    os.makedirs(parent, exist_ok=True)  # a file/dir name collision raises
                except OSError as e:
                    raise StagingError(f"mkdir failed for {path!r}: {e}") from e
            # CANONICAL check (kilabz MED): the lexical commonpath above does not PROVE the
            # design's "final canonical path strictly under the run dir" — a symlinked
            # intermediate dir would satisfy it while resolving outside. Components are
            # validated and dirs are created fresh so no symlink can exist here, but the
            # trust boundary must actually verify what it claims. (O_NOFOLLOW below covers
            # the final component.)
            try:
                real_parent = os.path.realpath(parent)
            except OSError as e:
                raise StagingError(f"realpath failed for {path!r}: {e}") from e
            if real_parent != rd_real and os.path.commonpath([real_parent, rd_real]) != rd_real:
                raise StagingError(f"entry parent resolves outside run dir: {path!r}")
            # Walk EVERY intermediate dir from the leaf parent up to the run dir, not just
            # the immediate parent (oracle r3 HIGH: `FOO/a/b` + `foo/c/d` have distinct leaf
            # parents so an immediate-parent-only check never stats the FOO/foo merge one
            # level up). MEMOIZED on rel-path (oracle r6 HIGH): a dir whose chain is already
            # validated is skipped, so this is O(distinct dirs), not O(files × depth) — a
            # deep hostile tree can't storm the orchestrator with tens of millions of stats.
            cur, cur_rel = dst, path
            while True:
                cur = os.path.dirname(cur)
                cur_rel = os.path.dirname(cur_rel)
                if cur == rd or not cur_rel or cur_rel in checked_dirs:
                    break
                try:
                    pst = os.stat(cur)
                except OSError as e:
                    raise StagingError(f"stat failed for {path!r}: {e}") from e
                owner = dir_owner.setdefault((pst.st_dev, pst.st_ino), cur_rel)
                if owner != cur_rel:
                    raise StagingError(f"case-colliding directory prefix for {path!r} "
                                       f"(collides with {owner!r} on this filesystem)")
                checked_dirs.add(cur_rel)

            if not _TIP_RE.fullmatch(sha):             # blob sha feeds git argv — validate
                raise StagingError(f"non-hex blob sha for {path!r}: {sha!r}")
            try:
                # O_EXCL: duplicate / case-colliding FILE names fail LOUDLY (never a silent
                # overwrite on case-insensitive APFS; dir-prefix collisions caught above).
                # O_NOFOLLOW: belt — no symlinks exist by construction. mode 0600: owner-
                # only from birth (kilabz r2 HIGH — a snapshot of possibly-private repo
                # content must not be group/other-readable).
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except OSError as e:
                raise StagingError(f"create failed (duplicate/collision?) for {path!r}: {e}") from e
            try:
                with os.fdopen(fd, "wb") as f:
                    # the batch header carries the size, so an over-budget blob is rejected
                    # BEFORE its content is read (r4 OOM property); content streams straight
                    # to the fd (no Python buffer). 120000 (symlink): the blob bytes ARE the
                    # target string, written as inert regular-file content. 100755: exec bit
                    # deliberately NOT reproduced (reviewers read; nothing runs).
                    blob_size = batch.write_blob(sha, f, max_bytes - total_bytes)
            except OSError as e:
                raise StagingError(f"write failed for {path!r}: {e}") from e
            total_bytes += blob_size
            written += 1
    finally:
        batch.close()

    if written != expected:
        raise StagingError(f"count check failed: wrote {written}, ls-tree lists {expected}")

    # make the snapshot GENUINELY non-writable AND owner-only ("read-only" is otherwise
    # only an agent-sandbox property; kilabz r2 HIGH — do NOT widen to group/other read).
    # Files → 0400 (owner read, no write), dirs → 0500 (owner read+traverse, no write),
    # INCLUDING the rundir itself (it was 0700 — 0500 removes owner write without adding
    # any group/other bit). Bottom-up so parents stay traversable while chmod-ing children;
    # never follows symlinks (none exist — belt).
    for dirpath, dirnames, filenames in os.walk(rundir, topdown=False):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if not os.path.islink(p):
                os.chmod(p, 0o400)
        os.chmod(dirpath, 0o500)


def _force_remove(rundir: Path) -> None:
    """Remove a staging dir, restoring write permission first (the a-w snapshot would
    wedge a naive rmtree: unlinking needs a writable parent dir). Never follows symlinks;
    never raises (best-effort — the age-reaper is the backstop)."""
    try:
        if rundir.is_dir() and not rundir.is_symlink():
            for dirpath, dirnames, _ in os.walk(rundir):
                try:
                    os.chmod(dirpath, 0o700)
                except OSError:
                    pass
        shutil.rmtree(rundir, ignore_errors=True)
    except OSError:
        pass


def teardown_snapshot(path: str | os.PathLike, *, root: Optional[Path] = None) -> bool:
    """Remove ONE review staging dir. Refuses anything that is not a `review-*` dir
    strictly inside the staging root (this function must never become an arbitrary
    rm -rf). Returns True if it removed something."""
    sroot = (root or staging_root()).resolve()
    p = Path(path)
    try:
        real = p.resolve()
    except OSError:
        return False
    if (not real.name.startswith("review-") or real.parent != sroot
            or not real.is_dir() or p.is_symlink()):
        return False
    _force_remove(real)
    return True


def _review_ttl_s() -> int:
    """Reaper TTL for leaked review-* dirs: must exceed WORST-CASE job lifetime, derived
    (not hand-set): max profile timeout among staging-eligible agents × the ledger's
    poison ceiling + an hour of queue margin. A stranded sync wait deliberately leaves
    the dir for this reaper (teardown is gated on terminal job state — never yank a
    RUNNING reviewer's cwd)."""
    try:
        from runtime.ledger.postgres_store import PostgresLedger
        attempts = PostgresLedger.MAX_ATTEMPTS
    except Exception:                                   # noqa: BLE001 — reaper must not crash
        attempts = 3
    try:
        from runtime.registry import REGISTRY
        timeouts = [s.profile.timeout_s for s in REGISTRY.values()
                    if s.adapter.get("staging_cwd") == "optional"]
    except Exception:                                   # noqa: BLE001
        timeouts = []
    return max(timeouts or [900]) * attempts + 3600


def reap_old_review_staging(in_use: Optional[set[str]]) -> int:
    """Remove leaked review-* staging dirs older than the derived TTL (crash-leak
    backstop, same shape as curate.reap_old_staging). Restores write permission first —
    the a-w snapshot would otherwise wedge the reaper. Returns the count removed.

    `in_use` is the REQUIRED fail-safe denylist of workdir paths a live (non-terminal)
    job still references — NEVER reaped whatever their age. Liveness is decided by JOB
    STATE, not mtime: a reviewer reading the chmod'd-a-w snapshot never refreshes its
    mtime, so age alone would let a concurrent reap yank a still-running reviewer's cwd
    (adversarial-review MED, 2026-07-09). Pass an (empty) set to reap freely; pass
    **None** ONLY when liveness is UNKNOWN (e.g. the ledger is unreachable) — then this
    reaps NOTHING and returns 0, because blind mtime-reaping is the exact bug class
    (kilabz r2 MED). The mtime cutoff still gates the reap of a dir no live job
    references. Paths compared by realpath so a symlink/relative spelling can't dodge it."""
    if in_use is None:
        return 0                                           # liveness unknown → never blind-reap
    sroot = staging_root()
    if not sroot.is_dir():
        return 0
    protected = set()
    for p in in_use:
        try:
            protected.add(os.path.realpath(p))
        except OSError:
            continue
    cutoff = time.time() - _review_ttl_s()
    reaped = 0
    for d in sroot.glob("review-*"):
        try:
            if not (d.is_dir() and not d.is_symlink() and d.stat().st_mtime < cutoff):
                continue
            if os.path.realpath(d) in protected:
                continue                                   # a live reviewer's cwd — keep
            _force_remove(d)
            reaped += 1
        except OSError:
            continue
    return reaped


async def ledger_active_workdirs() -> set[str]:
    """Live-job cwds from the ledger, for the reaper's in-use denylist. Raises on any
    ledger error (the caller decides whether to skip the reap — NEVER reap blind)."""
    from runtime.ledger.postgres_store import PostgresLedger
    dsn = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
    led = await PostgresLedger.connect(dsn)
    try:
        return await led.active_workdirs()
    finally:
        await led.close()


def main(argv: list[str]) -> int:
    """The PR-2 seam for play-review.sh: stage/teardown/reap without a bash
    reimplementation of the exporter. Prints the staged dir on stdout; all diagnostics
    on stderr; nonzero exit on any failure (the caller applies the §4 policy)."""
    args = argv[1:]
    if len(args) == 3 and args[0] == "stage":
        try:
            print(stage_snapshot(args[1], args[2]))
            return 0
        except StagingError as e:
            print(f"staging failed: {e}", file=sys.stderr)
            return 1
    if len(args) == 2 and args[0] == "teardown":
        return 0 if teardown_snapshot(args[1]) else 1
    if len(args) == 1 and args[0] == "reap":
        # query live cwds from the ledger and FAIL CLOSED if it's unreachable — a
        # standalone reap must never fall back to blind mtime-reaping (kilabz r2 MED).
        import asyncio
        try:
            in_use = asyncio.run(ledger_active_workdirs())
        except Exception as e:                             # noqa: BLE001
            print(f"reap: cannot load live workdirs, refusing to reap: {e}", file=sys.stderr)
            return 1
        print(reap_old_review_staging(in_use))
        return 0
    print("usage: python3 -m runtime.staging stage <repo> <tip40> | teardown <dir> | reap",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(["staging", *sys.argv[1:]]))
