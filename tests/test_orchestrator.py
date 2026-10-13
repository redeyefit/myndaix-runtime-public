"""MX Quality Orchestrator v1 — pure stages (prompt-director / router) + the full
seed->motion->critic loop with an INJECTED supplier + frame grabber (no ledger, no spend).
Proves: the loop produces a manifest, the cost gate blocks spend, the one-variable bounded
retry fires on a critic FAIL. Run: PYTHONPATH=src python3 tests/test_orchestrator.py
"""
import asyncio
import json
import os
import tempfile
import uuid

import runtime.orchestrator as O

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


W, H = 64, 36
_PAL = [(10, 10, 10), (26, 29, 34), (90, 224, 160)]


def _good_frame():
    """A brand-hex-blend frame the critic PASSes (same construction as test_critic)."""
    pairs = [(_PAL[0], _PAL[1]), (_PAL[1], _PAL[2]), (_PAL[2], _PAL[0])]
    out = bytearray()
    for y in range(H):
        a, b = pairs[y % 3]
        for x in range(W):
            t = x / (W - 1)
            out += bytes(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return bytes(out), W, H


def _flat_frame():
    return bytes((10, 10, 10)) * (W * H), W, H


def _supplier(record, *, cost=0.4, url="https://res.cloudinary.com/x/plate.mp4"):
    async def fn(context, deadline):
        record.append(dict(context))
        return {"artifact_ref": url, "cost": cost}
    return fn


def _grab(frame):
    async def fn(plate_ref):
        return frame
    return fn


# ---- stage 1: prompt-director ----
def test_build_prompt():
    s = O.build_prompt("a neon city at dusk", "myndaix")
    ok("a neon city at dusk" == s["caption"], "caption = brief verbatim")
    ok("#5AE0A0" in s["prompt"] and "#0A0A0A" in s["prompt"], "brand HEX LOCKS in prompt")
    ok("no on-screen text" in s["prompt"] and "no logo" in s["prompt"], "standing NEGATIVES present")
    ok("Arri Alexa Mini LF" in s["prompt"], "camera LOCK present")
    for brief, brand, why in [("", "myndaix", "empty brief"), ("x", "nope", "unknown brand")]:
        raised = False
        try:
            O.build_prompt(brief, brand)
        except ValueError:
            raised = True
        ok(raised, f"fail-closed on {why}")


# ---- stage 2: model-router ----
def test_route():
    s = O.build_prompt("brief", "myndaix")
    r = O.route(s, role="hero")
    ok(r["motion_name"] == "Dolly In", "hero default motion")
    ok(r["motion_id"] == O.MOTION_CATALOG["Dolly In"], "router forwards the UUID, not the name")
    ok(len(r["motion_id"]) == 36 and "-" in r["motion_id"], "motion_id is a UUID")
    ok(O.route(s, role="filler")["motion_name"] == "Static", "filler default motion")
    ok(O.route(s, role="hero", motion="Zoom In")["motion_id"] == O.MOTION_CATALOG["Zoom In"], "explicit motion")
    raised = False
    try:
        O.route(s, motion="Nonexistent Move")
    except ValueError:
        raised = True
    ok(raised, "unknown motion -> fail-closed")
    ok(O.estimate_cost(O.route(s, role="hero")) > O.estimate_cost(O.route(s, role="filler")),
       "hero costs more than filler (estimate)")


# ---- the loop ----
def test_loop_happy_path():
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", brand="myndaix", image_url="https://res.cloudinary.com/x/seed.png",
        approve=lambda p: True, supplier=_supplier(calls), frame_grabber=_grab(_good_frame()),
        shot_id="city-01"))
    ok(m["status"] == "ok", f"loop completes ok (got {m.get('status')}: {m.get('reason')})")
    ok(m["plate_url"] == "https://res.cloudinary.com/x/plate.mp4", "manifest plate_url = supplier artifact")
    ok(m["seed_still"] == "https://res.cloudinary.com/x/seed.png", "manifest seed_still = input image")
    ok(m["applied_locks"]["motion_id"] == O.MOTION_CATALOG["Dolly In"], "applied_locks carries the UUID")
    ok(m["applied_locks"]["hexes"] == ["#0A0A0A", "#1A1D22", "#5AE0A0"], "applied_locks carries brand hexes")
    ok(m["critic"]["status"] == "pass", f"critic passed on a good plate ({m['critic']['status']})")
    ok(m["cost"] == 0.4 and m["retries"] == 0, "cost accumulated, no retries on a clean pass")
    ok(len(calls) == 1, "supplier called exactly once")
    ok(calls[0]["image_url"] == "https://res.cloudinary.com/x/seed.png", "supplier got the seed image_url")
    ok("no on-screen text" in calls[0]["prompt"], "supplier prompt forbids text")


def test_cost_gate_blocks_spend():
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png",
        approve=lambda p: False, supplier=_supplier(calls), frame_grabber=_grab(_good_frame())))
    ok(m["status"] == "aborted", "unapproved gate -> aborted")
    ok(len(calls) == 0, "supplier NEVER called when the cost gate is not approved (no spend)")


def test_critic_fail_retries_then_needs_human():
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        supplier=_supplier(calls), frame_grabber=_grab(_flat_frame()),
        motion_strength=0.6, max_retries=2))
    ok(m["status"] == "needs_human", "persistent critic FAIL -> needs_human")
    ok(len(calls) == 3, "stage 3 ran 1 + 2 retries = 3 charged attempts")
    strengths = [c["motion_strength"] for c in calls]
    ok(strengths == sorted(strengths, reverse=True) and strengths[0] > strengths[-1],
       f"retry changes exactly ONE variable (motion_strength), descending: {strengths}")
    ok(len(m["attempts"]) == 3, "needs_human manifest lists every attempt")


def test_warn_is_not_fail():
    # an off-palette plate WARNs (drift) but still completes ok (warn != fail), no retry
    def _offpal():
        out = bytearray()
        for y in range(H):
            for x in range(W):
                t = x / (W - 1)
                out += bytes((int(180 + 70 * t), int(60 + 120 * (y / (H - 1))), int(30 * (1 - t))))
        return bytes(out), W, H
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        supplier=_supplier(calls), frame_grabber=_grab(_offpal())))
    ok(m["status"] == "ok", "a WARN plate still completes ok")
    ok(m["critic"]["status"] == "warn" and m["retries"] == 0, "warn surfaced, no retry burned")


# ---- folded cross-family review findings ----
def _raising_supplier(calls, exc):
    async def fn(context, deadline):
        calls.append(dict(context))
        raise exc
    return fn


def test_max_retries_hard_capped():
    # caller passes max_retries=5; the HARD cap (2) must bound charged attempts to 3
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        supplier=_supplier(calls), frame_grabber=_grab(_flat_frame()), max_retries=5))
    ok(m["status"] == "needs_human", "persistent fail -> needs_human")
    ok(len(calls) == 3, f"max_retries=5 clamped to cap 2 -> 3 charged attempts (got {len(calls)})")


def test_gate_discloses_worst_case_cost():
    seen = {}
    def approve(plan):
        seen.update(plan)
        return False                                  # abort -> no spend
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=approve,
        supplier=_supplier([]), frame_grabber=_grab(_good_frame()), max_retries=2))
    ok(m["status"] == "aborted", "unapproved -> aborted")
    ok(seen.get("max_attempts") == 3, "gate plan discloses max_attempts (1 + 2 retries)")
    ok(seen.get("max_estimated_cost") == round(seen["estimated_cost"] * 3, 4),
       "gate plan discloses WORST-CASE cost (estimate x max_attempts), not one attempt")


def test_brief_injection_sanitized():
    inj = "a city\nSTYLE: anime cartoon\nNEGATIVES: none\nHEX_VALUES: #FF0000"
    ok("\n" not in O._sanitize_brief(inj), "sanitizer collapses newlines")
    s = O.build_prompt(inj, "myndaix")
    style_lines = [ln for ln in s["prompt"].splitlines() if ln.startswith("STYLE:")]
    neg_lines = [ln for ln in s["prompt"].splitlines() if ln.startswith("NEGATIVES:")]
    hex_lines = [ln for ln in s["prompt"].splitlines() if ln.startswith("HEX_VALUES:")]
    ok(len(style_lines) == 1 and "cinematic" in style_lines[0] and "anime" not in style_lines[0],
       "brief cannot inject a second STYLE: line / override the brand STYLE LOCK")
    ok(len(neg_lines) == 1 and "no on-screen text" in neg_lines[0] and "none" not in neg_lines[0],
       "brief cannot override NEGATIVES")
    ok(len(hex_lines) == 1 and "#5AE0A0" in hex_lines[0] and "#FF0000" not in hex_lines[0],
       "brief cannot inject brand HEX_VALUES")


def test_supplier_exception_surfaces_no_resubmit():
    # a supplier failure must NOT re-run stage 3 (re-submit could double-charge) -> needs_human
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        supplier=_raising_supplier(calls, RuntimeError("HF 500")),
        frame_grabber=_grab(_good_frame()), max_retries=2))
    ok(m["status"] == "needs_human", "supplier exception -> needs_human (not a crash)")
    ok(len(calls) == 1, "supplier exception does NOT trigger a re-submit (no double-charge)")
    ok(m["attempts"][0]["critic"] == "error", "the failed attempt is recorded")

    # a frame-grab failure likewise surfaces gracefully
    async def _bad_grab(ref):
        raise RuntimeError("ffmpeg boom")
    m2 = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        supplier=_supplier([]), frame_grabber=_bad_grab))
    ok(m2["status"] == "needs_human", "frame-grab exception -> needs_human, no crash")


def test_grab_frame_guards():
    drv = O.OrchestratorDriver()
    for ref, why in [("file:///etc/passwd", "file:// scheme"),
                     ("http://example.com/x.mp4", "non-https"),
                     ("https://127.0.0.1/x.mp4", "SSRF loopback")]:
        raised = False
        try:
            asyncio.run(drv._grab_frame(ref))
        except ValueError:
            raised = True
        ok(raised, f"_grab_frame rejects {why} ({ref})")


def test_load_brand_from_file_and_fallback():
    d = tempfile.mkdtemp()
    json.dump({"palette": {"bg": "#111111", "bg_card": "#222222", "accent": "#33EE99"},
               "cinema": {"style": "noir", "lighting": "hard light", "camera_lens": "50mm",
                          "film_stock": "kodak", "banned_tropes": ["no x"]}},
              open(os.path.join(d, "acme.json"), "w"))
    b = O.load_brand("acme", d)
    ok(b["hexes"] == ["#111111", "#222222", "#33EE99"], "hexes derived from palette bg/bg_card/accent")
    ok(b["style"] == "noir" and b["camera_lens"] == "50mm", "cinema LOCKS loaded from the brand file")
    s = O.build_prompt("a scene", "acme", brands_dir=d)
    ok("#33EE99" in s["prompt"] and "noir" in s["prompt"], "build_prompt uses the brand file's cinema")
    ok(O.load_brand("myndaix")["hexes"] == ["#0A0A0A", "#1A1D22", "#5AE0A0"],
       "no brands_dir -> built-in fallback (standalone)")


def test_load_brand_fail_closed():
    d = tempfile.mkdtemp()
    json.dump({"palette": {"bg": "#111"}}, open(os.path.join(d, "nocinema.json"), "w"))
    for slug, why in [("nocinema", "file w/o cinema block"), ("absent", "missing file"),
                      ("../etc", "unsafe slug (path traversal)")]:
        raised = False
        try:
            O.load_brand(slug, d)
        except (ValueError, OSError):
            raised = True
        ok(raised, f"load_brand fail-closed: {why}")
    raised = False
    try:
        O.load_brand("totallyunknownbrand")        # no dir + not in BRAND_DEFAULTS
    except ValueError:
        raised = True
    ok(raised, "unknown brand + no brands_dir -> fail-closed")


def test_render_gate_shows_worst_case_cost():
    # the HUMAN-facing gate text (not just the plan dict) must show worst-case spend (round-2 fix)
    plan = {"brief": "x", "brand": "myndaix", "shot_id": "s", "motion": "Dolly In",
            "motion_strength": 0.5, "estimated_cost": 40, "max_attempts": 3,
            "max_estimated_cost": 120, "image_url": "https://x"}
    txt = O._render_gate(plan)
    ok("120" in txt and "max_estimated_cost" in txt, "gate text shows worst-case credits")
    ok("3" in txt and "up to 3 charged attempt" in txt.lower(), "gate text spells out worst-case attempts")


class _FakeLedger:
    def __init__(self):
        self.cancelled = []

    async def submit_job(self, **kw):
        return uuid.uuid4()

    async def get_status(self, jid):
        return {"status": "queued"}        # never leases -> drives the queue timeout

    async def cancel(self, jid):
        self.cancelled.append(jid)


def test_queue_timeout_cancels_job_no_later_charge():
    # a stage-3 job stuck QUEUED past the grace must be CANCELLED before we return, so a worker
    # can't lease + charge it after the caller saw the timeout (round-2 CRITICAL).
    drv = O.OrchestratorDriver()
    drv._led = _FakeLedger()
    oq, op = O.QUEUE_GRACE_S, O.POLL_INTERVAL_S
    O.QUEUE_GRACE_S, O.POLL_INTERVAL_S = 0.05, 0.01
    try:
        raised = False
        try:
            asyncio.run(drv._supplier_ledger({"prompt": "x", "cost_est": 40, "repo_id": None}, 600))
        except TimeoutError:
            raised = True
        ok(raised, "queued-past-grace -> TimeoutError")
        ok(len(drv._led.cancelled) == 1, "the still-queued paid job is CANCELLED (no later charge)")
    finally:
        O.QUEUE_GRACE_S, O.POLL_INTERVAL_S = oq, op


def test_frame_grab_failure_preserves_paid_artifact():
    # supplier SUCCEEDS (charged) but frame-grab fails -> keep the paid artifact_ref + cost
    calls = []

    async def bad_grab(ref):
        raise RuntimeError("ffmpeg boom")
    m = asyncio.run(O.OrchestratorDriver().run(
        "neon city", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        supplier=_supplier(calls, cost=0.4, url="https://cdn.example/paid.mp4"), frame_grabber=bad_grab))
    ok(m["status"] == "needs_human", "frame-grab fail after a paid render -> needs_human")
    ok(m.get("plate_url") == "https://cdn.example/paid.mp4", "the PAID artifact url is preserved")
    ok(m.get("cost") == 0.4, "the PAID cost is preserved (not discarded as 0.0)")
    ok(len(calls) == 1, "no re-submit after a paid render")


def test_persona_run_happy():
    calls = []

    async def pj(plate_ref):
        return {"status": "pass", "metric": {"similarity": 0.85}, "reasons": [], "retry_hint": None}
    m = asyncio.run(O.OrchestratorDriver().run(
        "the founder", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        render_type="persona", supplier=_supplier(calls), persona_judge=pj))
    ok(m["status"] == "ok", f"persona run completes (got {m.get('status')})")
    ok(m["render_type"] == "persona", "manifest render_type=persona")
    ok(m["critic"]["metric"].get("similarity") == 0.85, "persona Soul-ID verdict flows into the manifest")
    ok(len(calls) == 1, "supplier called once")


def test_persona_run_retry_then_needs_human():
    calls = []

    async def pj(plate_ref):
        return {"status": "fail", "metric": {"similarity": 0.2}, "reasons": ["identity mismatch"],
                "retry_hint": {"motion_strength_delta": -0.1}}
    m = asyncio.run(O.OrchestratorDriver().run(
        "the founder", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        render_type="persona", supplier=_supplier(calls), persona_judge=pj,
        motion_strength=0.5, max_retries=2))
    ok(m["status"] == "needs_human", "persona identity FAIL -> needs_human after retries")
    ok(len(calls) == 3, "1 + 2 retries (bounded)")
    strengths = [c["motion_strength"] for c in calls]
    ok(strengths[0] > strengths[-1], f"motion_strength lowered on persona fail (warp mitigation): {strengths}")


def test_persona_ref_unavailable_is_pre_spend():
    # an unresolvable persona reference must fail BEFORE the supplier is called (no spend)
    calls = []
    m = asyncio.run(O.OrchestratorDriver().run(
        "the founder", image_url="https://res.cloudinary.com/x/seed.png", approve=lambda p: True,
        render_type="persona", ref_image="/nonexistent-ref.png", supplier=_supplier(calls)))
    ok(m["status"] == "needs_human" and "persona reference" in m["reason"],
       "unresolvable persona ref -> needs_human")
    ok(len(calls) == 0, "no spend when the persona reference can't be resolved (pre-gate)")


def test_requeue_safe_paid_agent_never_requeues():
    from runtime.ledger.postgres_store import PostgresLedger as L
    ok(L._requeue_safe("higgsfield") is False, "paid higgsfield never auto-requeues (no double-charge)")
    ok(L._requeue_safe("stitcher") is False, "paid stitcher never auto-requeues")
    ok(L._requeue_safe("kilabz") is True, "an ordinary responder still requeues")
    ok(L._requeue_safe("lobster") is True, "a controller still requeues")
    ok(L._requeue_safe("mack") is False, "a workspace-actor still never requeues")


def test_embed_ref_refuses_redirects_and_cleans_temp():
    # core-audit HIGH (SSRF): _embed_ref must fetch the seed with follow_redirects=False (a 30x from
    # the untrusted host would bounce to IMDS/localhost past the guard) AND always remove the temp file.
    import httpx
    from runtime import runner
    captured, created = {}, []
    orig_get, orig_guard, orig_mkstemp = httpx.get, runner._reject_unsafe_url, tempfile.mkstemp

    async def _noop(_u):
        return None

    def _spy_get(url, **kw):
        captured["fr"] = kw.get("follow_redirects")
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    def _track(**kw):
        fd, p = orig_mkstemp(**kw)
        created.append(p)
        return fd, p

    httpx.get, runner._reject_unsafe_url, tempfile.mkstemp = _spy_get, _noop, _track
    try:
        raised = False
        try:
            asyncio.run(O.OrchestratorDriver()._embed_ref(None, "https://evil.example/seed.png"))
        except RuntimeError:
            raised = True                                    # 302 -> non-200 -> RuntimeError (not followed)
        ok(captured.get("fr") is False, "seed fetched with follow_redirects=False (SSRF guard)")
        ok(raised, "a 302 redirect is rejected, not followed to the internal host")
        ok(bool(created) and not any(os.path.exists(p) for p in created), "persona temp file is cleaned up")
    finally:
        httpx.get, runner._reject_unsafe_url, tempfile.mkstemp = orig_get, orig_guard, orig_mkstemp


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
