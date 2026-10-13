"""MX Quality Orchestrator v1 — a STANDALONE driver (not a registry agent).

Turns a one-line brand brief into a generated motion plate by replicating Higgsfield's
quality pattern on our own stack: (1) prompt-director expands the brief into a rule-bound
labeled-block prompt with verbatim LOCKS + NEGATIVES, (2) model-router picks a named camera
motion + cost estimate, (HUMAN COST GATE), (3) the supplier generates motion+background, (4)
a measured critic gates the plate. Output = a manifest mx-engine ingests (it composites; the
orchestrator never renders a brand pixel).

Per the build corollaries (docs/MX_QUALITY_ORCHESTRATOR.md), v1:
  * is a standalone module run as `PYTHONPATH=src python3 -m runtime.orchestrator "<brief>"`
    (mirrors cli.py / controller.py) — it holds its OWN PostgresLedger; it is NOT a worker-
    invoked agent (those have no ledger handle and can't submit child jobs);
  * collapses stages 1/2/4 into IN-PROCESS functions; ONLY stage 3 (the supplier — it spends
    money) is a ledger job, to the existing `higgsfield` agent;
  * enforces a HUMAN COST GATE before any spend (the ledger enforces no $ ceiling);
  * sets the stage-3 poll deadline >= the supplier Profile.timeout_s (>=600s) so it never
    false-times-out a render that keeps running AND charging (the cli.py 180s default would).

motion_id is the live `/v1/motions` UUID (verified 2026-06-28: the API field takes the UUID;
"Dolly In" etc. are human labels). image_url is rendered+uploaded UPSTREAM by mx-engine and
handed in (a public Cloudinary secure_url that passes the runner's SSRF guard).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

from runtime import critic
from runtime.registry import get as get_spec

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_WS = re.compile(r"\s+")
BRIEF_MAX = 500
MAX_RETRIES_CAP = 2          # HARD ceiling on bounded charged retries (design §6)
FRAME_GRAB_TIMEOUT_S = 45.0  # wall clock for the ffmpeg frame grab (anti-hang on a dead URL)
QUEUE_GRACE_S = 180.0        # how long a stage-3 job may sit QUEUED before the render window starts


def _sanitize_brief(brief: str) -> str:
    """Collapse control chars / newlines and cap length on the free-text brief BEFORE it is
    interpolated into the labeled-block prompt. A newline would otherwise let the brief forge a
    new labeled section (e.g. 'STYLE: anime' / 'NEGATIVES: none'), overriding the brand LOCKS
    (cross-family review). Brand LOCKS/NEGATIVES stay in non-user-controlled lines."""
    return _WS.sub(" ", _CTRL.sub(" ", brief or "")).strip()[:BRIEF_MAX].strip()

# ---- pinned motion catalog (live /v1/motions UUIDs, verified 2026-06-28) ------------------
# The API forwards `motion_id` verbatim (runner.py:330) and it must be the UUID, not the name.
MOTION_CATALOG = {
    "Dolly In":      "81ca2cd2-05db-4222-9ba0-a32e5185adfb",
    "Static":        "fa3ddb7c-53ee-4383-aa17-97ae65f180e5",
    "Push To Glass": "30a02896-cdda-469d-9ed9-52cbba1c04a8",
    "Crane Up":      "68af9add-43ea-4261-a706-16b640fdcff9",
    "Zoom In":       "fbcbec5b-30f8-4b17-ba6e-8e8d5b265562",
    "360 Orbit":     "ea035f68-b350-40f1-b7f4-7dff999fdd67",
}
HERO_MOTION = "Dolly In"      # dramatic move for a hero shot
FILLER_MOTION = "Static"     # gentle/neutral for filler

# ---- brand LOCKS (v1 hard-coded; mx-engine owns brands/<slug>.json `cinema` block in v2) --
# Fail-closed: an unknown brand raises — never let the LLM/router invent brand color (the LOCK
# is the cross-shot consistency mechanism). myndaix defaults per the design §4 worked example.
BRAND_DEFAULTS = {
    "myndaix": {
        "hexes": ["#0A0A0A", "#1A1D22", "#5AE0A0"],
        "style": "cinematic, premium, minimal, high-contrast dark tech aesthetic",
        "lighting": "low-key dramatic lighting, soft volumetric haze, subtle rim light",
        "camera_lens": "Arri Alexa Mini LF, 35mm anamorphic",
        "film_stock": "clean digital capture, fine grain, shallow depth of field",
        "banned_tropes": ["no AI-hype clichés", "no glowing-brain imagery",
                          "no generic stock-tech blue"],
    },
}
# standing NEGATIVE block — the supplier renders motion+background ONLY; WE render brand text.
STANDING_NEGATIVES = ["no on-screen text", "no watermark", "no logo", "no warped faces",
                      "no extra fingers", "no text artifacts", "no people"]
# persona/Soul-ID mode: the seed IS a person we WANT to keep — so DROP "no people" and emphasize
# identity stability instead (animating a face's #1 failure is warping/drift, caught by the Soul-ID gate).
PERSONA_NEGATIVES = ["no warped faces", "no morphing", "no identity change", "no distorted features",
                     "no extra fingers", "no extra limbs", "no on-screen text", "no watermark", "no logo"]

# rough display-only cost estimate (credits) — UNVERIFIED; measure live before any cost LOGIC.
COST_EST = {"hero": 40, "filler": 6}

POLL_INTERVAL_S = 3.0
NOMINAL_DURATION_S = 3.0     # DoP/lite nominal; not measured in v1


# ---- brand LOCK resolution: a brand file (mx-engine owns it) > the built-in fallback -----------
_BRAND_REQUIRED = ("hexes", "style", "lighting", "camera_lens", "film_stock")


def load_brand(slug: str, brands_dir: Optional[str] = None) -> dict:
    """Resolve a brand's LOCKS. If `brands_dir` (or $MX_BRANDS_DIR) is set, read
    <brands_dir>/<slug>.json and require a `cinema` block (mx-engine owns the brand schema, design
    Open Q1) — fail-closed so the LLM can never invent brand color. Hexes come from cinema.hexes or
    are derived from palette.{bg,bg_card,accent}. With NO brands_dir, fall back to the built-in
    BRAND_DEFAULTS so the orchestrator still runs standalone (CI / no mx-engine checkout)."""
    slug = (slug or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", slug):     # path-safety on the filename
        raise ValueError(f"unsafe brand slug {slug!r}")
    brands_dir = brands_dir or os.environ.get("MX_BRANDS_DIR")
    if not brands_dir:
        b = BRAND_DEFAULTS.get(slug)
        if b is None:
            raise ValueError(f"unknown brand {slug!r} and no --brands-dir/$MX_BRANDS_DIR — fail-closed")
        return b
    path = Path(brands_dir) / f"{slug}.json"
    if not path.is_file():
        raise ValueError(f"brand file not found: {path} — fail-closed")
    raw = json.loads(path.read_text())
    cinema = raw.get("cinema")
    if not isinstance(cinema, dict) or not cinema:
        raise ValueError(f"brand {slug!r} has no `cinema` block in {path} — fail-closed "
                         f"(add cinema:{{style,lighting,camera_lens,film_stock,banned_tropes}})")
    palette = raw.get("palette") or {}
    hexes = cinema.get("hexes") or [palette.get(k) for k in ("bg", "bg_card", "accent")]
    out = {"hexes": [h for h in hexes if h],
           "style": cinema.get("style"), "lighting": cinema.get("lighting"),
           "camera_lens": cinema.get("camera_lens"), "film_stock": cinema.get("film_stock"),
           "banned_tropes": list(cinema.get("banned_tropes") or [])}
    missing = [k for k in _BRAND_REQUIRED if not out.get(k)]
    if missing:
        raise ValueError(f"brand {slug!r} cinema block missing {missing} in {path} — fail-closed")
    return out


# ============================ stage 1: prompt-director (in-process) =========================
def build_prompt(brief: str, brand: str, *, brands_dir: Optional[str] = None,
                 render_type: str = "generic") -> dict:
    """Expand the brief into the labeled-block prompt. The ONLY free-text slot is Caption (the
    brief); every brand slot is filled VERBATIM from the resolved brand LOCKS (file or fallback).
    render_type 'persona' swaps the NEGATIVES + COMPOSITION for animating a SUBJECT (keep identity,
    not 'no people'). Fail-closed on an unknown brand or a missing LOCK slot (design §4)."""
    brief = _sanitize_brief(brief)
    if not brief:
        raise ValueError("empty brief")
    if render_type not in ("generic", "persona"):
        raise ValueError(f"bad render_type {render_type!r}")
    b = load_brand(brand, brands_dir)
    missing = [k for k in _BRAND_REQUIRED if not b.get(k)]
    if missing:
        raise ValueError(f"brand {brand!r} missing LOCK slots {missing} — fail-closed")
    persona = render_type == "persona"
    negatives = (PERSONA_NEGATIVES if persona else STANDING_NEGATIVES) + list(b["banned_tropes"])
    composition = ("subtle, natural camera + subject motion; keep the subject's identity, face, and "
                   "proportions STABLE and unchanged" if persona
                   else "cinematic hero composition, rule-of-thirds, deliberate negative space")
    hexes = list(b["hexes"])
    prompt = "\n".join([
        f"Caption: {brief}",
        f"STYLE: {b['style']}",
        f"COMPOSITION: {composition}",
        f"SCENE: {brief}",
        f"CINEMATOGRAPHY_AND_LIGHTING: {b['lighting']}",
        f"CAMERA_AND_LENS: {b['camera_lens']}",
        f"PHYSICAL_ATTRIBUTES: {b['film_stock']}",
        f"HEX_VALUES: {', '.join(hexes)}",
        f"NEGATIVES: {', '.join(negatives)}",
    ])
    return {
        "prompt": prompt,
        "caption": brief,
        "locks": {"hexes": hexes, "style": b["style"], "camera_lens": b["camera_lens"]},
        "negatives": negatives,
        "render_type": render_type,
        "total_shots": 1,
    }


# ============================ stage 2: model-router (in-process) ============================
def route(shot: dict, *, role: str = "hero", motion: Optional[str] = None,
          motion_strength: Optional[float] = None) -> dict:
    """Trivial v1 router: one shot -> the higgsfield (dop/lite image->video) supplier; pick a
    named motion from the pinned catalog (-> its UUID); attach a display-only cost estimate."""
    if role not in ("hero", "filler"):
        raise ValueError(f"bad role {role!r}")
    name = motion or (HERO_MOTION if role == "hero" else FILLER_MOTION)
    if name not in MOTION_CATALOG:
        raise ValueError(f"unknown motion {name!r}; choices: {sorted(MOTION_CATALOG)}")
    ms = 0.5 if motion_strength is None else float(motion_strength)
    return {
        "to_agent": "higgsfield",
        "role": role,
        "motion_name": name,
        "motion_id": MOTION_CATALOG[name],
        "motion_strength": ms,
        "cost_est": COST_EST.get(role, COST_EST["filler"]),
        "prompt": shot["prompt"],
        "locks": shot["locks"],
    }


def estimate_cost(routed: dict) -> float:
    return float(routed["cost_est"])


# ================================ the driver ===============================================
SupplierFn = Callable[[dict, float], Awaitable[dict]]      # (context, deadline) -> {artifact_ref, cost}
FrameGrabber = Callable[[str], Awaitable[tuple]]           # (plate_ref) -> (rgb_bytes, w, h)
ApproveFn = Callable[[dict], bool]                         # (plan) -> approved?


class OrchestratorDriver:
    """Runs stages 1 -> 2 -> [human cost gate] -> 3 (ledger) -> 4 (critic), threading each
    stage's output into the next. Ledger is connected LAZILY (only the real supplier path needs
    it), so the pure loop is testable with an injected supplier + frame grabber and no DB."""

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn
        self._led = None

    async def _ledger(self):
        if self._led is None:
            from runtime.ledger.postgres_store import PostgresLedger
            dsn = self.dsn or "postgresql://localhost/runtime"
            self._led = await PostgresLedger.connect(dsn)
        return self._led

    async def close(self):
        if self._led is not None:
            try:
                await self._led.close()
            finally:
                self._led = None

    # ---- stage 3 (the only ledger job): submit to higgsfield, poll >=600s, read artifact ----
    async def _supplier_ledger(self, context: dict, deadline_s: float) -> dict:
        led = await self._ledger()
        jid = await led.submit_job(to_agent="higgsfield", prompt=context["prompt"],
                                   context=context, created_by="orchestrator",
                                   repo_id=context.get("repo_id"))
        loop = asyncio.get_event_loop()
        # Two windows (cross-family MAJOR): the supplier's render timeout starts when the worker
        # LEASES the job, not at submit. So allow QUEUE_GRACE while QUEUED, then a FULL deadline_s
        # render window from the first leased/running observation — queue delay can't false-time-out
        # a still-charging render. A timeout surfaces the job id (it may have charged); never abandon.
        queue_cap = loop.time() + QUEUE_GRACE_S
        render_cap = None
        st = {}
        while True:
            now = loop.time()
            st = await led.get_status(jid)
            stt = st.get("status") if st else None
            if stt in ("done", "failed", "dead"):
                break
            if stt in ("leased", "running") and render_cap is None:
                render_cap = now + deadline_s
            cap = render_cap if render_cap is not None else queue_cap
            if now >= cap:
                where = "rendering" if render_cap is not None else "queued"
                # CANCEL before returning: a still-QUEUED job left alive would later lease + CHARGE
                # after the caller already saw needs_human (cross-family CRITICAL). cancel() flips a
                # queued/leased/running job -> dead; a leased one is best-effort (it may already have
                # charged) but a queued one is reliably prevented.
                try:
                    await led.cancel(jid)
                except Exception:                          # noqa: BLE001 — cancel is best-effort
                    pass
                raise TimeoutError(f"stage-3 job {jid} stuck {where} past its deadline — cancelled; "
                                   f"recover via `mxr get {jid}` (a leased job may have charged)")
            await asyncio.sleep(POLL_INTERVAL_S)
        if st.get("status") != "done" or not st.get("artifact_ref"):
            err = next((a.get("text") for a in (st.get("attempts") or [])
                        if a.get("status") == "failed" and a.get("text")), st.get("status"))
            raise RuntimeError(f"stage-3 supplier {st.get('status')}: {err}")
        # get_status does NOT expose Result.cost -> v1 manifest uses the gate estimate (Open Q5).
        return {"artifact_ref": st["artifact_ref"], "cost": float(context.get("cost_est", 0.0)),
                "job_id": str(jid)}

    async def _grab_frame(self, plate_ref: str, w: int = 64, h: int = 36) -> tuple:
        """Extract ONE downscaled rgb24 frame from the plate via ffmpeg, for the critic.
        HTTPS-only + SSRF-guarded (a poisoned artifact_ref must not make ffmpeg fetch a
        loopback/file target — the runner does NOT scheme-check the result url) + wall-clock
        bounded (a dead-but-open URL must not hang the driver AFTER a paid render). v1 plates are
        public Higgsfield/Cloudinary https urls; tests inject their own grabber."""
        if not plate_ref.startswith("https://"):
            raise ValueError(f"refusing to grab a non-https plate_ref: {plate_ref!r}")
        from runtime.runner import _reject_unsafe_url
        reason = await _reject_unsafe_url(plate_ref)
        if reason:
            raise ValueError(f"plate_ref rejected (SSRF): {reason}")
        # -protocol_whitelist blocks ffmpeg protocol-switching tricks (file/pipe/concat/gopher).
        # RESIDUAL (accepted, codebase-wide): _reject_unsafe_url resolves the host once but ffmpeg
        # re-fetches, so an https REDIRECT or DNS-rebind to an internal host is not fully closed —
        # the SAME documented limitation _reject_unsafe_url carries for every image_url in the
        # runner. v2 hardening = a shared fetch-to-temp helper (redirects off, resolved-IP pinned,
        # byte cap) used everywhere, not a one-off here.
        cmd = ["ffmpeg", "-v", "error", "-protocol_whitelist", "https,tls,tcp,crypto",
               "-rw_timeout", "20000000", "-i", plate_ref,
               "-frames:v", "1", "-vf", f"scale={w}:{h}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=FRAME_GRAB_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
            raise RuntimeError(f"ffmpeg frame-grab timed out after {FRAME_GRAB_TIMEOUT_S:.0f}s")
        if proc.returncode != 0 or len(out) < w * h * 3:
            raise RuntimeError(f"ffmpeg frame-grab failed ({proc.returncode}): {err.decode()[:200]}")
        return out[:w * h * 3], w, h

    # ---- persona / Soul-ID gate (stage 4 for render_type="persona") ----
    async def _grab_frame_png(self, plate_ref: str, out_path: str, w: int = 512) -> None:
        """Extract one mid-clip frame of the plate to a PNG FILE for embed_face (bigger than the
        critic's 64x36 so the face is embeddable). Same https-only + SSRF + timeout guards."""
        if not plate_ref.startswith("https://"):
            raise ValueError(f"refusing to grab a non-https plate_ref: {plate_ref!r}")
        from runtime.runner import _reject_unsafe_url
        reason = await _reject_unsafe_url(plate_ref)
        if reason:
            raise ValueError(f"plate_ref rejected (SSRF): {reason}")
        cmd = ["ffmpeg", "-y", "-v", "error", "-protocol_whitelist", "https,tls,tcp,crypto",
               "-rw_timeout", "20000000", "-ss", "2", "-i", plate_ref, "-frames:v", "1",
               "-vf", f"scale={w}:-1", out_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=FRAME_GRAB_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
            raise RuntimeError(f"ffmpeg persona frame-grab timed out after {FRAME_GRAB_TIMEOUT_S:.0f}s")
        if proc.returncode != 0 or not Path(out_path).is_file():
            raise RuntimeError(f"ffmpeg persona frame-grab failed ({proc.returncode}): {err.decode()[:200]}")

    async def _embed_ref(self, ref_image: Optional[str], image_url: str) -> list:
        """Reference identity embedding (runs ONCE, pre-loop): from a local ref_image if given, else
        download the public https seed (SSRF-guarded) and embed it. Raises if no face / no InsightFace."""
        if ref_image:
            emb, _ = critic.embed_face(ref_image)
            if emb is None:
                raise RuntimeError(f"no face found in ref image {ref_image!r}")
            return emb
        if not image_url.startswith("https://"):
            raise ValueError("persona ref: image_url must be https (or pass ref_image)")
        from runtime.runner import _reject_unsafe_url
        reason = await _reject_unsafe_url(image_url)
        if reason:
            raise ValueError(f"persona ref image_url rejected (SSRF): {reason}")
        import tempfile

        import httpx
        # follow_redirects=False: _reject_unsafe_url only classifies the ORIGINAL host (it does not
        # pin the resolved IP or re-check redirect targets), so following a 30x would let the untrusted
        # host bounce us to 169.254.169.254 / localhost / RFC1918 — SSRF. Refuse redirects, matching the
        # runner's Higgsfield fetch hardening. A legit redirecting URL must be passed pre-resolved or via
        # --ref-image. mkstemp (not mktemp): no predictable-name TOCTOU; always removed in finally.
        fd, tmp = tempfile.mkstemp(suffix=".png")
        try:
            r = httpx.get(image_url, headers={"User-Agent": "Mozilla/5.0"},
                          follow_redirects=False, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"persona ref download {r.status_code} "
                                   f"(redirects refused as an SSRF guard — pass a direct https URL or --ref-image)")
            with os.fdopen(fd, "wb") as fh:      # write via the mkstemp fd — NOT close-then-reopen-by-name
                fd = None                        # fdopen OWNS it now; the `with` closes it even if write()
                fh.write(r.content)              # raises — so finally must NOT also close it (double-close/OSError)
            emb, _ = critic.embed_face(tmp)
        finally:
            if fd is not None:
                os.close(fd)                     # httpx raised / non-200 BEFORE fdopen -> close the raw fd
            try:
                os.remove(tmp)
            except OSError:
                pass
        if emb is None:
            raise RuntimeError("no face found in the seed image (persona ref)")
        return emb

    async def _judge_persona(self, plate_ref: str, ref_emb: list) -> dict:
        """Stage-4 Soul-ID gate: extract a plate frame, embed the largest face, gate identity vs the
        reference. A FAIL carries a one-variable retry hint (LOWER motion_strength reduces warping)."""
        import tempfile
        fd, png = tempfile.mkstemp(suffix=".png")   # mkstemp (not mktemp): no TOCTOU; removed in finally
        os.close(fd)
        try:
            await self._grab_frame_png(plate_ref, png)
            frame_emb, face_frac = critic.embed_face(png)
        finally:
            try:
                os.remove(png)
            except OSError:
                pass
        v = critic.critic_persona(frame_emb, ref_emb, face_frac)
        v["retry_hint"] = {"motion_strength_delta": -0.1} if v["status"] == "fail" else None
        return v

    async def run(self, brief: str, *, brand: str = "myndaix", image_url: str,
                  approve: ApproveFn, shot_id: str = "shot-01", role: str = "hero",
                  render_type: str = "generic", ref_image: Optional[str] = None,
                  motion: Optional[str] = None, motion_strength: Optional[float] = None,
                  end_image_url: Optional[str] = None, repo_id: Optional[str] = None,
                  max_retries: int = 2, brands_dir: Optional[str] = None,
                  supplier: Optional[SupplierFn] = None,
                  frame_grabber: Optional[FrameGrabber] = None, persona_judge=None) -> dict:
        supplier = supplier or self._supplier_ledger
        frame_grabber = frame_grabber or self._grab_frame
        max_retries = max(0, min(MAX_RETRIES_CAP, int(max_retries)))   # HARD cap on charged retries

        # stage 1 + 2 (free, in-process)
        shot = build_prompt(brief, brand, brands_dir=brands_dir, render_type=render_type)
        hexes = shot["locks"]["hexes"]               # resolved brand hexes (brand file or fallback)
        routed = route(shot, role=role, motion=motion, motion_strength=motion_strength)
        cost_est = estimate_cost(routed)

        # persona Soul-ID gate needs a reference embedding — resolve it BEFORE the cost gate so a
        # missing InsightFace / faceless reference fails for FREE (pre-spend), never after a charge.
        ref_emb = None
        if render_type == "persona" and persona_judge is None:
            try:
                ref_emb = await self._embed_ref(ref_image, image_url)
            except Exception as e:                       # noqa: BLE001
                return {"status": "needs_human", "reason": f"persona reference unavailable: {e}",
                        "plan": {"brief": brief, "brand": brand, "render_type": render_type}}

        # ===== HUMAN COST GATE (before ANY spend) =====
        # disclose the WORST-CASE spend: the loop may run 1 + max_retries charged attempts, so the
        # gate must approve the max, not a single attempt (cross-family CRITICAL).
        max_attempts = max_retries + 1
        plan = {"brief": brief, "brand": brand, "shot_id": shot_id, "image_url": image_url,
                "motion": routed["motion_name"], "motion_id": routed["motion_id"],
                "motion_strength": routed["motion_strength"], "estimated_cost": cost_est,
                "max_attempts": max_attempts,
                "max_estimated_cost": round(cost_est * max_attempts, 4)}
        if not approve(plan):
            return {"status": "aborted", "reason": "cost gate not approved", "plan": plan}

        # ===== stage 3 (supplier, spends) + stage 4 (critic), bounded one-variable retry =====
        spec = get_spec("higgsfield")
        prof_to = getattr(getattr(spec, "profile", None), "timeout_s", 600) if spec else 600
        deadline = max(600.0, float(prof_to or 600))
        ms = routed["motion_strength"]
        attempts = []
        result = None
        for attempt in range(max_attempts):
            ctx = {"image_url": image_url, "prompt": routed["prompt"],
                   "motion_id": routed["motion_id"], "motion_strength": ms,
                   "cost_est": cost_est, "repo_id": repo_id}
            if end_image_url:
                ctx["end_image_url"] = end_image_url
            # A supplier/frame-grab failure is NOT a critic FAIL: do NOT re-run stage 3 (a re-submit
            # of a non-idempotent paid supplier could double-charge). Record + surface to a human,
            # never crash the driver (cross-family + workflow MAJOR). Split so that if the supplier
            # SUCCEEDS (charged) but the downstream frame-grab/critic fails, the human still keeps
            # the paid artifact_ref + cost (cross-family MAJOR — don't discard a paid plate).
            try:
                sup = await supplier(ctx, deadline)
            except Exception as e:                       # noqa: BLE001 — supplier failed (no plate)
                attempts.append({"motion_strength": ms, "cost": 0.0, "critic": "error",
                                 "reasons": [f"supplier: {type(e).__name__}: {e}"]})
                return {"status": "needs_human", "reason": f"stage-3 supplier error: {e}",
                        "attempts": attempts, "plan": plan}
            try:
                if render_type == "persona":
                    verdict = (await persona_judge(sup["artifact_ref"]) if persona_judge
                               else await self._judge_persona(sup["artifact_ref"], ref_emb))
                else:
                    rgb, w, h = await frame_grabber(sup["artifact_ref"])
                    verdict = critic.critic_generic(rgb, w, h, hexes=hexes)
            except Exception as e:                       # noqa: BLE001 — paid plate, QC step failed
                attempts.append({"motion_strength": ms, "cost": sup.get("cost") or cost_est,
                                 "artifact_ref": sup.get("artifact_ref"), "job_id": sup.get("job_id"),
                                 "critic": "error", "reasons": [f"stage-4 QC: {type(e).__name__}: {e}"]})
                return {"status": "needs_human", "plate_url": sup.get("artifact_ref"),
                        "cost": round(sup.get("cost") or cost_est, 4),
                        "reason": f"stage-4 QC failed (the plate WAS rendered + charged): {e}",
                        "attempts": attempts, "plan": plan}
            attempts.append({"motion_strength": ms, "cost": sup.get("cost", 0.0),
                             "critic": verdict["status"], "reasons": verdict["reasons"]})
            if verdict["status"] != "fail":
                result = (sup, verdict)
                break
            hint = verdict.get("retry_hint") or {}
            ms = round(max(0.0, ms + hint.get("motion_strength_delta", -0.15)), 3)   # ONE variable
        if result is None:
            return {"status": "needs_human", "reason": "critic FAIL after retries",
                     "attempts": attempts, "plan": plan}

        sup, verdict = result
        total_cost = round(sum(a.get("cost", 0.0) for a in attempts), 4)
        return {
            "status": "ok",
            "plate_url": sup["artifact_ref"],
            "shot_id": shot_id,
            "duration": NOMINAL_DURATION_S,
            "render_type": render_type,
            "applied_locks": {"hexes": hexes, "camera_preset": routed["motion_name"],
                              "motion_id": routed["motion_id"],
                              "motion_strength": ms, "brand": brand},
            "seed_still": image_url,
            "cost": total_cost,
            "critic": {"status": verdict["status"], "metric": verdict["metric"],
                       "reasons": verdict["reasons"]},
            "prompt": routed["prompt"],
            "retries": len(attempts) - 1,
        }


# ================================ CLI entrypoint ===========================================
def _render_gate(plan: dict) -> str:
    """The human-facing cost-gate text. MUST surface the WORST-CASE spend (max_attempts +
    max_estimated_cost), NOT a single attempt, so the operator approves what can actually be
    charged across the bounded retries (cross-family review CRITICAL)."""
    keys = ("brief", "brand", "shot_id", "motion", "motion_strength",
            "estimated_cost", "max_attempts", "max_estimated_cost", "image_url")
    lines = ["=== MX QUALITY ORCHESTRATOR — COST GATE (no spend until you approve) ==="]
    lines += [f"  {k:18}: {plan.get(k)}" for k in keys]
    lines.append(f"  >> approving authorizes UP TO {plan.get('max_attempts')} charged attempt(s) "
                 f"= up to {plan.get('max_estimated_cost')} credits (worst case incl. retries)")
    return "\n".join(lines)


def _interactive_approve(plan: dict) -> bool:
    print("\n" + _render_gate(plan), file=sys.stderr)
    try:
        ans = input("approve spend? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


async def _amain(args) -> int:
    drv = OrchestratorDriver(dsn=args.dsn)
    approve = (lambda _p: True) if args.yes else _interactive_approve
    try:
        manifest = await drv.run(
            args.brief, brand=args.brand, image_url=args.image_url, approve=approve,
            shot_id=args.shot_id, role=args.role, motion=args.motion,
            motion_strength=args.motion_strength, end_image_url=args.end_image_url,
            repo_id=args.repo, brands_dir=args.brands_dir,
            render_type=args.render_type, ref_image=args.ref_image)
    finally:
        await drv.close()
    print(json.dumps(manifest, indent=2))
    return 0 if manifest.get("status") == "ok" else 1


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m runtime.orchestrator",
                                description="MX Quality Orchestrator v1 — brief -> motion plate")
    p.add_argument("brief", help="one-line brand brief")
    p.add_argument("--brand", default="myndaix")
    p.add_argument("--image-url", dest="image_url", required=True,
                   help="public seed still URL (rendered+uploaded by mx-engine; image->video)")
    p.add_argument("--shot-id", dest="shot_id", default="shot-01")
    p.add_argument("--role", default="hero", choices=["hero", "filler"])
    p.add_argument("--render-type", dest="render_type", default="generic",
                   choices=["generic", "persona"],
                   help="persona = animate a SUBJECT; stage-4 runs the Soul-ID face gate vs --ref-image/seed")
    p.add_argument("--ref-image", dest="ref_image", default=None,
                   help="persona: local reference still for the identity gate (else the seed image_url)")
    p.add_argument("--motion", default=None, help=f"motion name (default by role); one of {sorted(MOTION_CATALOG)}")
    p.add_argument("--motion-strength", dest="motion_strength", type=float, default=None)
    p.add_argument("--end-image-url", dest="end_image_url", default=None)
    p.add_argument("--repo", default=None, help="repo_id scope for the stage-3 job")
    p.add_argument("--brands-dir", dest="brands_dir", default=None,
                   help="dir of brand <slug>.json files (mx-engine/brands); requires a `cinema` "
                        "block, fail-closed. Omit to use the built-in fallback. ($MX_BRANDS_DIR)")
    p.add_argument("--dsn", default=None, help="MYNDAIX_DSN override")
    p.add_argument("--yes", action="store_true", help="auto-approve the cost gate (non-interactive)")
    args = p.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
