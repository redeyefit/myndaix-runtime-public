"""CLI flag parsing - the --image/--application flags populate Job.context, no DB
needed (cli.submit is stubbed so we assert only what main() threads through).

Run: PYTHONPATH=src python3 tests/test_cli.py
"""
from runtime import cli


def _capture_submit():
    """Replace cli.submit with an async stub that records its kwargs; returns
    (captured_dict, restore_fn)."""
    captured: dict = {}

    async def fake_submit(agent, task, *, context=None, repo_id=None,
                          base_ref=None, timeout_s=180.0):
        captured.update(agent=agent, task=task, context=context,
                        repo_id=repo_id, base_ref=base_ref)
        return 0

    orig = cli.submit
    cli.submit = fake_submit
    return captured, (lambda: setattr(cli, "submit", orig))


def test_cli_image_and_application_build_context():
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["higgsfield", "gentle push-in",
                       "--image", "http://example.com/cat.png",
                       "--application", "/higgsfield-ai/dop/lite"])
        assert rc == 0
        assert captured["agent"] == "higgsfield" and captured["task"] == "gentle push-in"
        assert captured["context"] == {"image_url": "http://example.com/cat.png",
                                       "application": "/higgsfield-ai/dop/lite"}
    finally:
        restore()


def test_cli_no_media_flags_gives_empty_context():
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["recon", "what is rust"])
        assert rc == 0 and captured["context"] == {}      # empty, not None
    finally:
        restore()


def test_cli_image_only_omits_application_key():
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["higgsfield", "spin", "--image", "http://example.com/a.png"])
        assert rc == 0 and captured["context"] == {"image_url": "http://example.com/a.png"}
    finally:
        restore()


def test_cli_repo_and_base_ref_thread_through():
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["kilabz", "review this", "--repo", "fieldvision",
                       "--base-ref", "abc123"])
        assert rc == 0
        assert captured["repo_id"] == "fieldvision" and captured["base_ref"] == "abc123"
    finally:
        restore()


def test_cli_no_scope_flags_are_none():
    # omitted -> None -> NULL repo_id -> cap-exempt (never a shared bucket)
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["recon", "what is rust"])
        assert rc == 0 and captured["repo_id"] is None and captured["base_ref"] is None
    finally:
        restore()


def test_cli_get_routes_to_get_job():
    # `mxr get <id>` is special-cased above the flat submit parser (D1 artifact read)
    captured = {}

    async def fake_get(job_id, reply=False):
        captured["job_id"] = job_id
        captured["reply"] = reply
        return 0

    orig = cli.get_job
    cli.get_job = fake_get
    try:
        rc = cli.main(["get", "11111111-2222-3333-4444-555555555555"])
        assert rc == 0
        assert captured["job_id"] == "11111111-2222-3333-4444-555555555555"
        assert captured["reply"] is False                    # default: status JSON
        rc = cli.main(["get", "--reply", "11111111-2222-3333-4444-555555555555"])
        assert rc == 0
        assert captured["reply"] is True
    finally:
        cli.get_job = orig


def test_cli_get_rejects_non_uuid_before_db():
    # a malformed id fails closed (rc 2) at parse time — never touches the ledger
    import asyncio
    assert asyncio.run(cli.get_job("not-a-uuid")) == 2


# ---- `mxr get` short-id prefix resolver (PR-3 quick win 1) -----------------

class _FakeLedger:
    """Stands in for PostgresLedger past the parse gate: resolve_job_prefix over a
    fixed id list (same hyphen-stripped LIKE semantics as the SQL), get_status a
    canned dict."""
    def __init__(self, ids):
        self.ids = ids
        self.prefix_seen = None
        self.status_asked = None

    async def resolve_job_prefix(self, prefix):
        self.prefix_seen = prefix
        return [i for i in self.ids if i.replace("-", "").startswith(prefix)]

    async def get_status(self, jid):
        self.status_asked = str(jid)
        return {"id": str(jid), "status": "done", "to_agent": "recon"}

    async def close(self):
        pass


def _patch_ledger(fake):
    class _FakePL:
        @staticmethod
        async def connect(dsn):
            return fake
    orig = cli.PostgresLedger
    cli.PostgresLedger = _FakePL
    return lambda: setattr(cli, "PostgresLedger", orig)


def test_cli_get_short_prefix_too_short_rejected_before_db():
    # <8 hex chars fails closed at parse time — the ledger is never dialed
    import asyncio

    class _Boom:
        @staticmethod
        async def connect(dsn):
            raise AssertionError("must not touch the ledger for a too-short prefix")
    orig = cli.PostgresLedger
    cli.PostgresLedger = _Boom
    try:
        assert asyncio.run(cli.get_job("abc123")) == 2       # 6 hex chars
        assert asyncio.run(cli.get_job("abcd-12")) == 2      # 6 after hyphen strip
    finally:
        cli.PostgresLedger = orig


def test_cli_get_unique_prefix_resolves():
    import asyncio
    full = "deadbeef-0000-4000-8000-000000000001"
    fake = _FakeLedger([full, "11111111-2222-3333-4444-555555555555"])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job("deadbeef")) == 0
        assert fake.prefix_seen == "deadbeef"
        assert fake.status_asked == full                     # resolved to the FULL id
    finally:
        restore()


def test_cli_get_prefix_hyphens_and_case_stripped():
    # a hyphen-spanning, mixed-case slice of the full JOB_ID works (12 hex after strip)
    import asyncio
    full = "deadbeef-0000-4000-8000-000000000001"
    fake = _FakeLedger([full])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job("DEAD-beef-0000")) == 0
        assert fake.prefix_seen == "deadbeef0000"
        assert fake.status_asked == full
    finally:
        restore()


def test_cli_get_ambiguous_prefix_fails_closed():
    import asyncio
    fake = _FakeLedger(["deadbeef-0000-4000-8000-000000000001",
                        "deadbeef-1111-4111-8111-000000000002"])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job("deadbeef")) == 2     # refuse, don't guess
        assert fake.status_asked is None                     # no status read happened
    finally:
        restore()


def test_cli_get_prefix_no_match():
    import asyncio
    fake = _FakeLedger(["deadbeef-0000-4000-8000-000000000001"])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job("abcd1234")) == 1     # same rc as unknown full id
    finally:
        restore()


def test_cli_get_full_uuid_skips_resolver():
    import asyncio
    full = "11111111-2222-3333-4444-555555555555"
    fake = _FakeLedger([full])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job(full)) == 0
        assert fake.prefix_seen is None                      # resolver never consulted
        assert fake.status_asked == full
    finally:
        restore()


# ---- `mxr get --reply` (phone-tailnet surface: recover a late reply) --------

class _FakeLedgerReply(_FakeLedger):
    def __init__(self, ids, outbound):
        super().__init__(ids)
        self.outbound = outbound

    async def get_status(self, jid):
        self.status_asked = str(jid)
        return {"id": str(jid), "status": "done", "to_agent": "librarian",
                "outbound": self.outbound}


def test_cli_get_reply_prints_latest_body():
    import asyncio, contextlib, io
    full = "11111111-2222-3333-4444-555555555555"
    fake = _FakeLedgerReply([full], [{"body": "first"}, {"body": "the cited answer"}])
    restore = _patch_ledger(fake)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = asyncio.run(cli.get_job(full, reply=True))
        assert rc == 0
        assert buf.getvalue().strip() == "the cited answer"   # LATEST outbound wins
    finally:
        restore()


def test_cli_get_reply_none_yet_exits_3():
    # job known, no outbound body yet -> exit 3 (distinct from 1 unknown / 2 malformed),
    # so the phone wrapper can say "still thinking" instead of lying or erroring
    import asyncio
    full = "11111111-2222-3333-4444-555555555555"
    for empty in ([], None, [{"body": None}]):
        fake = _FakeLedgerReply([full], empty)
        restore = _patch_ledger(fake)
        try:
            assert asyncio.run(cli.get_job(full, reply=True)) == 3
        finally:
            restore()


# ---- --staged-workdir + `mxr review` routing (mxr-review-context PR-1) ------

def test_cli_staged_workdir_threads_to_context():
    # pass-through ONLY — the runner is the trust boundary (staging-cwd adapters,
    # strictly inside $MYNDAIX_STAGING_ROOT, fail-closed TERMINAL there)
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["kilabz", "review this", "--repo", "myndaix-runtime",
                       "--staged-workdir", "/tmp/staging/review-x"])
        assert rc == 0
        assert captured["context"] == {"workdir": "/tmp/staging/review-x"}
    finally:
        restore()


def test_cli_staged_workdir_empty_string_still_propagates():
    # kilabz r2 MED: an EXPLICIT empty --staged-workdir "" must reach context.workdir
    # (→ runner rejects it TERMINAL), never be dropped by a truthy check to a silent
    # scratch downgrade. Omitting the flag entirely leaves workdir unset.
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["kilabz", "t", "--repo", "x", "--staged-workdir", ""])
        assert rc == 0 and captured["context"] == {"workdir": ""}
        rc = cli.main(["kilabz", "t", "--repo", "x"])
        assert rc == 0 and "workdir" not in captured["context"]
    finally:
        restore()


def test_cli_review_routes_to_review_main():
    from runtime import review as review_mod
    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    orig = review_mod.main
    review_mod.main = fake_main
    try:
        rc = cli.main(["review", "kilabz", "--repo", "x", "--tip", "a" * 40])
        assert rc == 0
        assert captured["argv"][0] == "review" and captured["argv"][1] == "kilabz"
    finally:
        review_mod.main = orig


def test_cli_review_staging_verbs_route_to_staging_main():
    # PR-2: the staging primitives play-review.sh calls each map to staging.main's
    # subcommand (stage/teardown/reap), inheriting the runtime venv via mxr.
    from runtime import staging as staging_mod
    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    orig = staging_mod.main
    staging_mod.main = fake_main
    try:
        assert cli.main(["review-stage", "/some/repo", "a" * 40]) == 0
        assert captured["argv"] == ["staging", "stage", "/some/repo", "a" * 40]
        assert cli.main(["review-teardown", "/stage/review-x"]) == 0
        assert captured["argv"] == ["staging", "teardown", "/stage/review-x"]
        assert cli.main(["review-reap"]) == 0
        assert captured["argv"] == ["staging", "reap"]
    finally:
        staging_mod.main = orig


# ---- sync-wait derivation (PR-3 quick win 2) --------------------------------

def _with_env(key, value, fn):
    import os
    had, orig = key in os.environ, os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        return fn()
    finally:
        if had:
            os.environ[key] = orig
        else:
            os.environ.pop(key, None)


def test_profile_sync_wait_derived_from_timeout():
    from runtime.contracts import Profile
    assert Profile().sync_wait() == 360.0                    # 300 default + 60 margin
    assert Profile(timeout_s=900).sync_wait() == 960.0       # kilabz: scales, not flat 180
    assert Profile(timeout_s=900, sync_wait_s=42).sync_wait() == 42.0   # explicit pin wins


def test_resolve_sync_wait_env_always_wins():
    assert _with_env("MXR_TIMEOUT_S", "77", lambda: cli._resolve_sync_wait("kilabz")) == 77.0


def test_resolve_sync_wait_unset_env_uses_profile():
    # kilabz profile timeout_s=900 -> 960; the 180s flat default that stranded a DONE
    # reply behind a 900s exec cap is gone for registry agents
    assert _with_env("MXR_TIMEOUT_S", None, lambda: cli._resolve_sync_wait("kilabz")) == 960.0
    assert _with_env("MXR_TIMEOUT_S", None, lambda: cli._resolve_sync_wait("recon")) >= 180.0


def test_resolve_sync_wait_no_profile_falls_back_180():
    # defensive: an agent missing from the registry (submit rejects it earlier anyway)
    assert _with_env("MXR_TIMEOUT_S", None,
                     lambda: cli._resolve_sync_wait("no-such-agent")) == 180.0


def test_resolve_sync_wait_malformed_env_falls_to_profile():
    # a broken export must not crash NOR silently pin 180 under a 900s exec cap
    assert _with_env("MXR_TIMEOUT_S", "not-a-number",
                     lambda: cli._resolve_sync_wait("kilabz")) == 960.0


def test_get_reply_marker_carries_terminality():
    # Marker contract (phone-audit fold): rc stays 3 for EVERY no-body case; the stderr
    # MARKER is what tells a script caller whether the job can still answer. failed/dead
    # -> MXR_JOB_FAILED/DEAD, done-with-no-body -> MXR_DONE_EMPTY, genuinely pending ->
    # no marker at all.
    import asyncio
    import contextlib
    import io
    full = "deadbeef-0000-4000-8000-000000000001"
    for status, marker in (("failed", "MXR_JOB_FAILED"), ("dead", "MXR_JOB_DEAD"),
                           ("done", "MXR_DONE_EMPTY"), ("queued", None)):
        fake = _FakeLedger([full])

        async def _st(jid, _s=status):
            return {"id": str(jid), "status": _s, "outbound": []}
        fake.get_status = _st
        restore = _patch_ledger(fake)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                assert asyncio.run(cli.get_job(full, reply=True)) == 3
            lines = err.getvalue().splitlines()
            if marker:
                assert marker in lines, f"{status}: missing {marker} in {lines}"
            else:
                assert not any(ln.startswith("MXR_") for ln in lines), \
                    f"pending must emit NO marker, got {lines}"
        finally:
            restore()


def test_get_unknown_job_emits_no_such_job_marker():
    # rc=1 alone is ambiguous (a down ledger also exits 1) — the marker is the ONLY
    # authoritative "this id does not exist" signal a script caller may trust.
    import asyncio
    import contextlib
    import io
    full = "deadbeef-0000-4000-8000-000000000001"
    fake = _FakeLedger([full])

    async def _none(jid):
        return None
    fake.get_status = _none
    restore = _patch_ledger(fake)
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert asyncio.run(cli.get_job(full, reply=True)) == 1
        assert "MXR_NO_SUCH_JOB" in err.getvalue().splitlines()
    finally:
        restore()


def test_run_job_empty_body_is_done_empty():
    # r6 P2: `body text NOT NULL` permits "" — an empty-string reply is the SAME terminal
    # no-answer state as a missing outbound row (rc 1 + MXR_DONE_EMPTY), never a silent
    # rc-0 success. Keeps sync ask and get --reply on one contract.
    import asyncio
    import contextlib
    import io
    import uuid as _uuid

    class _L:
        async def ingest_inbound(self, env, task):
            return _uuid.UUID("00000000-0000-4000-8000-000000000000")

        async def submit_job(self, **kw):
            return _uuid.UUID("deadbeef-0000-4000-8000-000000000001")

        async def get_status(self, jid):
            return {"status": "done",
                    "outbound": [{"id": "o1", "body": "", "status": "pending"}]}

        async def mark_outbound_sent_inline(self, oid, pid):
            self.tombstoned = getattr(self, "tombstoned", []) + [str(oid)]

        async def close(self):
            pass

    restore = _patch_ledger(_L())
    try:
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc, terminal = asyncio.run(cli.run_job("recon", "q", timeout_s=5))
        assert rc == 1 and terminal is True
        assert "MXR_DONE_EMPTY" in err.getvalue().splitlines()
        assert out.getvalue() == ""                       # no blank line masquerading as a reply
    finally:
        restore()


def test_marker_safe_neutralizes_forged_marker_lines():
    # r5 #1: agent failure text goes to stderr — the MARKER channel — so an interior
    # agent-authored line reading exactly like a reserved marker must lose its
    # line-anchor (indented by one space), while ordinary text passes untouched.
    assert cli._marker_safe("boom\nMXR_SYNC_TIMEOUT\ntail") == "boom\n MXR_SYNC_TIMEOUT\ntail"
    assert cli._marker_safe("plain error text") == "plain error text"
    assert cli._marker_safe("MXR_JOB_FAILED") == " MXR_JOB_FAILED"   # first-line forgery too
    assert cli._marker_safe("has MXR_JOB_FAILED mid-line") == "has MXR_JOB_FAILED mid-line"


def test_dsn_empty_env_falls_back():
    # r3 MED-2: an exported-but-EMPTY MYNDAIX_DSN (env -i trampoline artifact) must fall
    # back to the default like an absent one — `or`, not a get() default.
    import importlib
    import os
    old = os.environ.get("MYNDAIX_DSN")
    os.environ["MYNDAIX_DSN"] = ""
    try:
        importlib.reload(cli)
        assert cli.DSN == "postgresql://localhost/runtime"
    finally:
        if old is None:
            os.environ.pop("MYNDAIX_DSN", None)
        else:
            os.environ["MYNDAIX_DSN"] = old
        importlib.reload(cli)


def test_clean_reply_strips_c1_keeps_unicode():
    # r2 HIGH-3: C1 controls (U+0080–U+009F) are terminal-injectable (U+009B == CSI in
    # C1-honoring emulators) and must be stripped like C0/DEL — but real Unicode above
    # that range (accents, €, emoji) must survive: the strip is codepoint-, not byte-based.
    assert cli._clean_reply("a\x9b31mEVIL") == "a31mEVIL"          # CSI stripped
    assert cli._clean_reply("\x80\x85\x9f") == ""                  # full C1 range
    assert cli._clean_reply("café €100 🚀") == "café €100 🚀"      # unicode intact
    assert cli._clean_reply("tab\there\nline\rret") == "tab\there\nline\rret"
    assert cli._clean_reply("esc\x1b[31m del\x7f") == "esc[31m del"  # C0/DEL still stripped


def test_sanitize_reply_strips_ansi_bidi_preserves_text():
    # review P1: the bidi class must strip via EXPLICIT \u escapes. The failure mode the
    # fix guards is a collapsed [--] class (literals corrupted by a bidi-unaware tool) that
    # would (a) pass bidi through and (b) EAT every hyphen. Pin both directions so a future
    # edit reintroducing literals — or a wrong escape — is caught. chr() keeps this source
    # ASCII (the bidi controls are invisible if typed literally).
    rlo, pdi = chr(0x202e), chr(0x2069)                 # RIGHT-TO-LEFT OVERRIDE, POP DIR ISO
    all_bidi = "".join(chr(c) for c in (0x200e, 0x200f, 0x061c, 0x2066))
    assert cli._sanitize_reply(f"safe{rlo}RTL{pdi}here") == "safeRTLhere"    # bidi stripped
    assert cli._sanitize_reply(all_bidi) == ""                              # full set gone
    assert cli._sanitize_reply("well-formed - text") == "well-formed - text"  # hyphens kept
    assert cli._sanitize_reply("x\x1b[31mred\x1b[0m") == "xred"               # CSI stripped
    assert cli._sanitize_reply("café €100 🚀") == "café €100 🚀"             # unicode intact


def _capture_discuss_main():
    """Replace cli._discuss_main with an async stub recording its args; returns
    (captured_dict, restore_fn). Isolates main()'s preflight/timeout-resolution logic
    from the real dispatch (DB/SSH) the same way _capture_submit isolates cli.submit."""
    captured: dict = {}

    async def fake_discuss_main(agents_ordered, topic, timeout_s):
        captured.update(agents_ordered=agents_ordered, topic=topic, timeout_s=timeout_s)
        return 0

    orig = cli._discuss_main
    cli._discuss_main = fake_discuss_main
    return captured, (lambda: setattr(cli, "_discuss_main", orig))


def test_discuss_preflight_rejects_unknown_agent():
    assert cli.main(["discuss", "topic", "--with", "no-such-agent"]) == 2


def test_discuss_preflight_rejects_non_responder_agent():
    # mack is WORKSPACE_ACTOR — discuss is RESPONDER-only (a workspace-mutating agent must
    # never be fanned into a read-only multi-agent gut-check).
    assert cli.main(["discuss", "topic", "--with", "mack"]) == 2


def test_discuss_preflight_accepts_responders_and_dedups():
    captured, restore = _capture_discuss_main()
    try:
        rc = cli.main(["discuss", "topic", "--with", "kilabz", "kilabz", "librarian"])
        assert rc == 0
        assert captured["agents_ordered"] == ["kilabz", "librarian"]
    finally:
        restore()


def test_discuss_unset_timeout_flows_through_as_none():
    # review MED (discuss-flat-300-ignores-profile-syncwait): main() must NOT pre-resolve a
    # flat 300s default — None has to reach _discuss_main so it can derive a per-agent wait.
    captured, restore = _capture_discuss_main()
    try:
        cli.main(["discuss", "topic", "--with", "kilabz"])
        assert captured["timeout_s"] is None
    finally:
        restore()


def test_discuss_explicit_timeout_flows_through_unchanged():
    captured, restore = _capture_discuss_main()
    try:
        cli.main(["discuss", "topic", "--with", "kilabz", "--timeout", "42"])
        assert captured["timeout_s"] == 42.0
    finally:
        restore()


def test_discuss_timeout_rejects_nan_inf_negative_zero():
    for bad in ("nan", "inf", "-5", "0"):
        try:
            cli.main(["discuss", "topic", "--with", "kilabz", "--timeout", bad])
        except SystemExit as e:
            assert e.code == 2, f"--timeout {bad} exited {e.code!r}, want 2"
        else:
            assert False, f"--timeout {bad} should have exited"


def test_discuss_main_resolves_per_agent_timeout_when_unset():
    # review MED: a flat default shorter than a slow agent's exec cap strands its DONE
    # reply behind a false TIMEOUT. kilabz (profile timeout_s=900 -> sync_wait 960) and
    # librarian (timeout_s=120 -> sync_wait 180) must each get THEIR OWN derived wait, not
    # a shared flat value, when no --timeout/MXR_TIMEOUT_S is given.
    import asyncio as _asyncio
    seen = {}

    async def fake_submit_and_fetch(agent, topic, timeout_s):
        seen[agent] = timeout_s
        return cli.DiscussResult(agent=agent, reply="ok", job_id=None,
                                 ledger="local", error=None)

    orig = cli._submit_and_fetch
    cli._submit_and_fetch = fake_submit_and_fetch
    try:
        _with_env("MXR_TIMEOUT_S", None, lambda: _asyncio.run(
            cli._discuss_main(["kilabz", "librarian"], "t", None)))
        assert seen["kilabz"] == 960.0
        assert seen["librarian"] == 180.0
    finally:
        cli._submit_and_fetch = orig


def test_discuss_main_explicit_timeout_applies_uniformly():
    import asyncio as _asyncio
    seen = {}

    async def fake_submit_and_fetch(agent, topic, timeout_s):
        seen[agent] = timeout_s
        return cli.DiscussResult(agent=agent, reply="ok", job_id=None,
                                 ledger="local", error=None)

    orig = cli._submit_and_fetch
    cli._submit_and_fetch = fake_submit_and_fetch
    try:
        _asyncio.run(cli._discuss_main(["kilabz", "librarian"], "t", 42.0))
        assert seen["kilabz"] == 42.0 and seen["librarian"] == 42.0
    finally:
        cli._submit_and_fetch = orig


class _FakeProc:
    """Duck-types asyncio.subprocess.Process closely enough for _remote_fetch: real
    StreamReaders (so _drain's .read(4096) works unmodified) + an async wait()."""

    def __init__(self, returncode, stdout_bytes=b"", stderr_bytes=b""):
        self.returncode = returncode
        self.stdout = __import__("asyncio").StreamReader()
        self.stdout.feed_data(stdout_bytes)
        self.stdout.feed_eof()
        self.stderr = __import__("asyncio").StreamReader()
        self.stderr.feed_data(stderr_bytes)
        self.stderr.feed_eof()

    async def wait(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _patch_create_subprocess_exec(proc):
    import asyncio as _asyncio

    async def fake(*args, **kwargs):
        return proc

    orig = _asyncio.create_subprocess_exec
    _asyncio.create_subprocess_exec = fake
    return lambda: setattr(_asyncio, "create_subprocess_exec", orig)


def test_remote_fetch_marker_line_anchored_not_substring():
    # review MED (remote-marker-substring-not-anchored): a bare `in stderr_text` test fires
    # on agent reply text that merely MENTIONS a marker name mid-line. Reproduces the
    # review's own repro — the REAL terminal marker on its own line is MXR_JOB_FAILED, but
    # the stderr blob also contains prose naming MXR_SYNC_TIMEOUT/MXR_DONE_EMPTY mid-sentence.
    import asyncio as _asyncio

    async def _run():
        stderr_blob = (
            "JOB_ID=deadbeef-0000-4000-8000-000000000001\n"
            "the model quoted our docs, mentioning MXR_SYNC_TIMEOUT and MXR_DONE_EMPTY "
            "mid-sentence while reviewing this file\n"
            "MXR_JOB_FAILED\n"
        )
        proc = _FakeProc(returncode=1, stderr_bytes=stderr_blob.encode())
        restore = _patch_create_subprocess_exec(proc)
        try:
            return await cli._remote_fetch("mini", "oracle", "topic", 5.0)
        finally:
            restore()

    result = _asyncio.run(_run())
    assert result.error == "FAILED"
    assert result.job_id == "deadbeef-0000-4000-8000-000000000001"


def test_remote_fetch_done_empty_marker_beats_nonzero_rc():
    # The fix's own suggested check: rc!=0 AND a genuine MXR_DONE_EMPTY line must classify
    # as EMPTY (recoverable), not fall through to the opaque SSH_ERROR rc gate.
    import asyncio as _asyncio

    async def _run():
        stderr_blob = "JOB_ID=deadbeef-0000-4000-8000-000000000002\nMXR_DONE_EMPTY\n"
        proc = _FakeProc(returncode=1, stderr_bytes=stderr_blob.encode())
        restore = _patch_create_subprocess_exec(proc)
        try:
            return await cli._remote_fetch("mini", "librarian", "topic", 5.0)
        finally:
            restore()

    result = _asyncio.run(_run())
    assert result.error == "EMPTY"
    assert result.job_id == "deadbeef-0000-4000-8000-000000000002"


def test_remote_fetch_unrecognized_failure_falls_to_ssh_error_with_job_id():
    # No recognized marker: still falls back to SSH_ERROR, and still PRESERVES job_id
    # (review MED: _print_discuss_result now surfaces this handle instead of dropping it).
    import asyncio as _asyncio

    async def _run():
        stderr_blob = "JOB_ID=deadbeef-0000-4000-8000-000000000003\nsome other crash\n"
        proc = _FakeProc(returncode=1, stderr_bytes=stderr_blob.encode())
        restore = _patch_create_subprocess_exec(proc)
        try:
            return await cli._remote_fetch("mini", "kilabz", "topic", 5.0)
        finally:
            restore()

    result = _asyncio.run(_run())
    assert result.error == "SSH_ERROR"
    assert result.job_id == "deadbeef-0000-4000-8000-000000000003"


def test_drain_reports_truncation_at_cap():
    # review P2: _drain must FLAG when a reply exceeds the byte cap so a clipped body is
    # never presented as complete. Fails without the fix: the old _drain returned a bare
    # str, so the (text, truncated) unpack below raises.
    import asyncio

    async def _run():
        exact = asyncio.StreamReader()
        exact.feed_data(b"x" * 100)
        exact.feed_eof()
        text, trunc = await cli._drain(exact, 100)
        assert text == "x" * 100 and trunc is False      # exactly at cap = complete

        over = asyncio.StreamReader()
        over.feed_data(b"y" * 250)
        over.feed_eof()
        text2, trunc2 = await cli._drain(over, 100)
        assert len(text2) == 100 and trunc2 is True       # beyond cap = flagged + clipped

    asyncio.run(_run())


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
