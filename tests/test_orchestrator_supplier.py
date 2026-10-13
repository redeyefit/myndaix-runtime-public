"""No-spend supplier CI — exercises the REAL invoke_higgsfield against an httpx.MockTransport
(the transport= kwarg), proving the stage-3 request-build + poll-parse + charge-safety with no
network and no credits spent (mirrors tests/test_stitch.py's mock-transport pattern).

SSRF (_reject_unsafe_url) is stubbed to a no-op here ONLY so the test is offline/deterministic
(it does real DNS otherwise); the SSRF guard has its own coverage in the runner tests.

Run: PYTHONPATH=src python3 tests/test_orchestrator_supplier.py
"""
import asyncio
import os
import uuid

import httpx

from runtime import runner
from runtime.contracts import Job, ResultStatus, ErrorClass
from runtime.registry import get as get_spec

PASS = [0]
FAIL = [0]
DOLLY_IN = "81ca2cd2-05db-4222-9ba0-a32e5185adfb"


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


def _transport(*, submit_code=200, poll_status="completed",
               video_url="https://res.cloudinary.com/x/plate.mp4", cost=0.39):
    calls = {"post": 0, "get": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            calls["post"] += 1
            if submit_code >= 300:
                return httpx.Response(submit_code, text="upstream boom")
            return httpx.Response(submit_code, json={
                "request_id": "req-1",
                "status_url": "https://platform.higgsfield.ai/requests/req-1/status",
                "cancel_url": "https://platform.higgsfield.ai/requests/req-1/cancel"})
        if req.method == "GET":
            calls["get"] += 1
            body = {"status": poll_status}
            if poll_status == "completed":
                body["video"] = {"url": video_url}
                body["cost"] = cost
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def _job(ctx_over=None):
    ctx = {"image_url": "https://res.cloudinary.com/x/seed.png",
           "motion_id": DOLLY_IN, "motion_strength": 0.5}
    if ctx_over is not None:
        ctx = ctx_over
    return Job(id=uuid.uuid4(), to_agent="higgsfield", prompt="cinematic plate",
               context=ctx, timeout_s=300)


async def _run(transport, job):
    return await runner.invoke_higgsfield(get_spec("higgsfield"), job, transport=transport)


def main():
    os.environ["HF_KEY"] = "kid:secret"          # mocked transport -> never used against a real API
    orig = runner._reject_unsafe_url

    async def _noop(_u):
        return None
    runner._reject_unsafe_url = _noop
    try:
        # --- happy path: real supplier code, mocked HTTP, NO spend ---
        t, calls = _transport()
        res = asyncio.run(_run(t, _job()))
        ok(res.status is ResultStatus.OK, f"mock supplier -> OK ({res.status})")
        ok(res.artifact_ref == "https://res.cloudinary.com/x/plate.mp4", "artifact_ref = video url")
        ok(res.text == res.artifact_ref, "text mirrors the url")
        ok(res.cost == 0.39, f"cost parsed from poll payload ({res.cost})")
        ok(calls["post"] == 1 and calls["get"] >= 1, "submitted once + polled")
        print("PASS test_mock_supplier_happy_path")

        # --- charge-safety: a non-2xx submit is TERMINAL (never re-POSTs / double-charges) ---
        t, calls = _transport(submit_code=500)
        res = asyncio.run(_run(t, _job()))
        ok(res.status is ResultStatus.ERROR and res.error_class is ErrorClass.TERMINAL,
           f"submit 5xx -> TERMINAL fail-closed ({res.status}/{res.error_class})")
        ok(calls["post"] == 1, "exactly one submit attempt (no retry-to-double-charge)")
        print("PASS test_mock_supplier_submit_5xx_terminal")

        # --- missing image_url is TERMINAL BEFORE any HTTP (no submit, no spend) ---
        t, calls = _transport()
        res = asyncio.run(_run(t, _job({"motion_id": DOLLY_IN})))   # no image_url
        ok(res.status is ResultStatus.ERROR and res.error_class is ErrorClass.TERMINAL,
           "missing image_url -> TERMINAL")
        ok(calls["post"] == 0, "no submit attempted when image_url is missing (pre-spend guard)")
        print("PASS test_mock_supplier_missing_image_url")

        # --- a 'failed' poll status is TERMINAL (post-charge, no retry) ---
        t, calls = _transport(poll_status="failed")
        res = asyncio.run(_run(t, _job()))
        ok(res.status is ResultStatus.ERROR and res.error_class is ErrorClass.TERMINAL,
           "poll 'failed' -> TERMINAL")
        print("PASS test_mock_supplier_poll_failed_terminal")
    finally:
        runner._reject_unsafe_url = orig

    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
