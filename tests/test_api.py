"""HTTP Command-API tests - the runtime as a service, WITH API-key auth. httpx
ASGITransport drives the REAL FastAPI app (routing + Pydantic validation + the
auth dependency) in-process against real Postgres; a worker pool processes the
queue as the separate concern it is.

Setup:  brew services start postgresql@16 && createdb runtime_test
Run:    LEDGER_TEST_DSN=postgresql://localhost/runtime_test \\
            PYTHONPATH=src python3 tests/test_api.py
"""
import asyncio
import inspect
import os
import uuid

import httpx

from runtime.api import Principal, create_app
from runtime.contracts import Authority, Reach
from runtime.ledger.postgres_store import PostgresLedger
from runtime.pool import WorkerPool
from runtime.registry import REGISTRY, AgentSpec

DSN = os.environ.get("LEDGER_TEST_DSN", "postgresql://localhost/runtime_test")

ALICE, BOB, ADMIN, HUMAN = "key-alice", "key-bob", "key-admin", "key-human"
KEYS = {
    ALICE: Principal(id="alice", role="client"),
    BOB: Principal(id="bob", role="client"),
    ADMIN: Principal(id="root", role="admin"),
    HUMAN: Principal(id="human", role="client"),   # an id that collides with the provenance sentinel
}


def _register():
    REGISTRY["api-echo"] = AgentSpec(
        agent_id="api-echo", reach=Reach.CLI, authority=Authority.RESPONDER,
        model="none", role="echo",
        adapter={"kind": "cli", "prompt_channel": "arg", "argv": ["printf", "echo:%s"]})


async def _fresh_app():
    _register()
    led = await PostgresLedger.connect(DSN)
    async with led._pool.acquire() as con:
        await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await led.init_schema()
    return create_app(led, api_keys=KEYS), led


def _client(app, key=ALICE):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://runtime", headers=headers)


async def test_health():
    app, led = await _fresh_app()
    c = _client(app)
    try:
        r = await c.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"
    finally:
        await c.aclose()
        await led.close()


async def test_requires_a_valid_key():
    app, led = await _fresh_app()
    try:
        async with _client(app, key=None) as anon:                # no key -> 401, fail-closed
            assert (await anon.get("/health")).status_code == 401
            assert (await anon.post("/jobs",
                    json={"to_agent": "api-echo", "prompt": "x"})).status_code == 401
        async with _client(app, key="not-a-real-key") as bad:     # unknown key -> 401
            assert (await bad.get("/health")).status_code == 401
    finally:
        await led.close()


async def test_submit_then_status():
    app, led = await _fresh_app()
    c = _client(app)
    try:
        r = await c.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi"})
        assert r.status_code == 201, r.text
        jid = r.json()["job_id"]
        assert (await c.get(f"/jobs/{jid}")).json()["status"] == "queued"
        await WorkerPool(led, size=2).run_until_idle()
        assert (await c.get(f"/jobs/{jid}")).json()["status"] == "done"
    finally:
        await c.aclose()
        await led.close()


async def test_inbound_produces_a_reply():
    app, led = await _fresh_app()
    c = _client(app)
    try:
        r = await c.post("/inbound", json={"sender_id": "alice", "dedupe_key": str(uuid.uuid4()),
                                           "body": "ping", "to_agent": "api-echo"})
        assert r.status_code == 201, r.text
        jid = r.json()["job_id"]
        await WorkerPool(led, size=2).run_until_idle()
        job = (await c.get(f"/jobs/{jid}")).json()
        assert job["status"] == "done"
        assert any("echo:ping" in o["body"] for o in (job.get("outbound") or []))
    finally:
        await c.aclose()
        await led.close()


async def test_client_cannot_read_another_principals_job():
    app, led = await _fresh_app()
    alice, bob, admin = _client(app, ALICE), _client(app, BOB), _client(app, ADMIN)
    try:
        jid = (await alice.post("/jobs",
               json={"to_agent": "api-echo", "prompt": "secret"})).json()["job_id"]
        assert (await alice.get(f"/jobs/{jid}")).status_code == 200   # owner reads
        assert (await bob.get(f"/jobs/{jid}")).status_code == 404     # 404 (not 403) - no existence leak
        assert (await admin.get(f"/jobs/{jid}")).status_code == 200   # admin reads any
    finally:
        for c in (alice, bob, admin):
            await c.aclose()
        await led.close()


async def test_jobs_accepts_and_persists_context():
    """POST /jobs with a context dict -> it survives to the leased Job (so an API
    principal can drive a media agent: {"context": {"image_url": ...}})."""
    app, led = await _fresh_app()
    c = _client(app)
    try:
        ctx = {"image_url": "http://example.com/cat.png", "application": "/x/y"}
        r = await c.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi", "context": ctx})
        assert r.status_code == 201, r.text
        att = await led.lease_job("w1", [])
        job = await led.get_attempt_job(att)
        assert job is not None and job.context == ctx
    finally:
        await c.aclose()
        await led.close()


async def test_jobs_accepts_and_persists_repo_scope():
    """POST /jobs with repo_id/base_ref -> they survive to the leased Job (so the cap
    in PR-2 can bucket by repo, and a fix chain can anchor to the reviewed SHA)."""
    app, led = await _fresh_app()
    c = _client(app)
    try:
        r = await c.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi",
                                        "repo_id": "fieldvision", "base_ref": "deadbeef"})
        assert r.status_code == 201, r.text
        att = await led.lease_job("w1", [])
        job = await led.get_attempt_job(att)
        assert job is not None
        assert job.repo_id == "fieldvision" and job.base_ref == "deadbeef"
    finally:
        await c.aclose()
        await led.close()


async def test_jobs_omitted_repo_scope_is_null():
    """Omitted repo_id/base_ref -> NULL on the Job (cap-exempt, never a shared bucket)."""
    app, led = await _fresh_app()
    c = _client(app)
    try:
        r = await c.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi"})
        assert r.status_code == 201, r.text
        att = await led.lease_job("w1", [])
        job = await led.get_attempt_job(att)
        assert job is not None and job.repo_id is None and job.base_ref is None
    finally:
        await c.aclose()
        await led.close()


async def test_repo_scope_validation():
    app, led = await _fresh_app()
    c = _client(app)
    try:
        # NUL in repo_id/base_ref -> 422, not a Postgres 500 (like context/text fields)
        assert (await c.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi",
                "repo_id": "bad\x00nul"})).status_code == 422
        # over-bound repo_id -> 422 (DoS guard)
        assert (await c.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi",
                "base_ref": "x" * 300})).status_code == 422
    finally:
        await c.aclose()
        await led.close()


async def test_context_validation():
    app, led = await _fresh_app()
    c = _client(app)
    try:
        # oversized context -> 422 (DoS guard), like the body bound
        big = {"k": "v" * 30_000}
        assert (await c.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi",
                "context": big})).status_code == 422
        # NUL inside context -> 422, not a Postgres 500
        assert (await c.post("/jobs", json={"to_agent": "api-echo", "prompt": "hi",
                "context": {"k": "bad\x00nul"}})).status_code == 422
        # omitted context still works (defaults to {})
        assert (await c.post("/jobs",
                json={"to_agent": "api-echo", "prompt": "hi"})).status_code == 201
    finally:
        await c.aclose()
        await led.close()


async def test_context_rejects_non_finite_numbers():
    """NaN/Infinity are not valid JSON and Postgres jsonb rejects them - they must be a
    clean 422 at validation, never a 500 at the INSERT. Sent as a RAW body because
    httpx's json= would itself re-encode (and stdlib json.loads on the server accepts
    the NaN/Infinity literals, so they reach our validator)."""
    app, led = await _fresh_app()
    c = _client(app)
    try:
        for tok in ("NaN", "Infinity", "-Infinity"):
            body = '{"to_agent": "api-echo", "prompt": "hi", "context": {"k": %s}}' % tok
            r = await c.post("/jobs", content=body,
                             headers={"Content-Type": "application/json"})
            assert r.status_code == 422, (tok, r.status_code, r.text)
    finally:
        await c.aclose()
        await led.close()


async def test_context_deep_nesting_is_clean_not_recursionerror():
    """A deeply-nested context must not surface as an uncaught RecursionError (-> 500)
    from our validator; a clean 422 (ValidationError) is the contract."""
    from runtime.api import SubmitIn
    deep: dict = {}
    cur = deep
    for _ in range(3000):                 # well past the default recursion limit
        cur["k"] = {}
        cur = cur["k"]
    try:
        SubmitIn(to_agent="api-echo", prompt="hi", context=deep)
        # if it validates without error that's fine too (no 500); the point is NO raise
    except RecursionError:
        raise AssertionError("deep context surfaced as RecursionError (would be a 500)")
    except Exception:
        pass  # pydantic ValidationError (-> 422) is the expected/acceptable outcome


async def test_unknown_job_404():
    app, led = await _fresh_app()
    c = _client(app)
    try:
        assert (await c.get(f"/jobs/{uuid.uuid4()}")).status_code == 404
    finally:
        await c.aclose()
        await led.close()


async def test_malformed_inbound_422():
    app, led = await _fresh_app()
    c = _client(app)
    try:
        assert (await c.post("/inbound", json={"body": "x"})).status_code == 422
    finally:
        await c.aclose()
        await led.close()


async def test_input_validation():
    app, led = await _fresh_app()
    c = _client(app)
    try:
        # empty dedupe_key -> 422 (would otherwise collapse distinct messages)
        assert (await c.post("/inbound", json={"sender_id": "a", "dedupe_key": "",
                "body": "x", "to_agent": "api-echo"})).status_code == 422
        # oversized body -> 422 (DoS guard)
        assert (await c.post("/inbound", json={"sender_id": "a", "dedupe_key": "k",
                "body": "x" * 200_000, "to_agent": "api-echo"})).status_code == 422
        # NUL byte -> a clean 422, not a Postgres 500
        assert (await c.post("/jobs",
                json={"to_agent": "api-echo", "prompt": "bad\x00nul"})).status_code == 422
    finally:
        await c.aclose()
        await led.close()


async def test_namespaced_owner_blocks_provenance_collision():
    """A client whose id is literally 'human' must NOT read terminal/CLI jobs (whose
    created_by defaults to the 'human' provenance sentinel) - the api:<id> namespace
    is what prevents that collision (the P1 the review caught)."""
    app, led = await _fresh_app()
    human, admin = _client(app, HUMAN), _client(app, ADMIN)
    try:
        jid = str(await led.submit_job(to_agent="api-echo", prompt="internal"))  # created_by='human'
        assert (await human.get(f"/jobs/{jid}")).status_code == 404
        assert (await admin.get(f"/jobs/{jid}")).status_code == 200
    finally:
        await human.aclose()
        await admin.aclose()
        await led.close()


async def test_load_api_keys_is_strict():
    from runtime.api import load_api_keys
    ok = load_api_keys("t1:alice:client,t2:root:admin")
    assert ok["t1"].id == "alice" and ok["t2"].role == "admin"
    for bad in ["secret",                       # token-only -> no token:id:role
                "tok:p:Admin",                  # bad role casing
                "dup:a:client,dup:b:admin",     # duplicate token (silent last-wins)
                ":alice:admin",                 # empty token (phantom key)
                "tok::admin",                   # empty id
                "tok:p:",                        # empty role
                "a:alice:client,b:alice:client"]:  # duplicate principal id (shared ownership)
        raised = False
        try:
            load_api_keys(bad)
        except ValueError:
            raised = True
        assert raised, f"load_api_keys must reject {bad!r}"


async def test_api_namespace_reserved_from_agents():
    """An agent can never be named 'api:*' - that prefix is reserved for API job
    ownership, so an agent (or a future sub-job) can't forge a created_by an API
    principal could read."""
    raised = False
    try:
        AgentSpec(agent_id="api:alice", reach=Reach.CLI, authority=Authority.RESPONDER,
                  model="none", role="x", adapter={"kind": "cli", "argv": ["true"]})
    except Exception:
        raised = True
    assert raised, "an agent_id starting with 'api:' must be rejected"


async def test_docs_routes_disabled():
    app, led = await _fresh_app()
    try:
        async with _client(app, key=None) as anon:
            assert (await anon.get("/openapi.json")).status_code == 404
            assert (await anon.get("/docs")).status_code == 404
    finally:
        await led.close()


async def _main():
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and inspect.iscoroutinefunction(_fn):
            await _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")


if __name__ == "__main__":
    asyncio.run(_main())
