"""Stitcher (workspace-actor): build a long video from a shot-list.

Per shot: generate a clip via DoP (runner._hf_generate), chaining the previous shot's
last frame as the next shot's start image for continuity; download each clip; then
ffmpeg-concat and apply a DETERMINISTIC brand layer (the HYBRID rule — AI for the moving
b-roll, exact tools for the logo/end-card, because models can't render a logo pixel-exact).

One self-contained job (NOT N ledger sub-jobs) so the whole spend sits under one cost
ceiling. authority=WORKSPACE_ACTOR -> the worker NEVER auto-retries it, so a failed run is
never silently re-charged. Partial failure returns the clips that DID succeed, concatenated
— spend on good shots is never thrown away.

NOTE (v1): there is no cross-run RESUME. The worker creates and destroys a fresh worktree
per attempt, so any in-workspace state (clips, a manifest) does not survive between runs;
combined with never-auto-retried, an in-run manifest would be dead weight. Idempotent resume
(persisted clips keyed by a stable run id, skip-already-charged) is deferred to v2.

Shot-list (job.context["shotlist"]) = ordered list of dicts:
  {prompt, motion_id?, motion_strength?, image_url?, end_image_url?, application?}
Job-level context: image_url (base/first seed), chain (bool, default True), end_card_url
(branded end-card image URL to append — SSRF-guarded), application (default model path).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from typing import Optional

from runtime.contracts import ErrorClass, Job, Result, ResultStatus
from runtime.registry import AgentSpec
from runtime import ffmpeg_util as fu
from runtime.runner import (
    _hf_generate, _hf_float, _hf_int, _ms, _reject_unsafe_url,
    _HF_POLL_INTERVAL_S, _HF_POLL_RETRY_BACKOFF_S, _HF_POLL_RETRY_MAX,
)

_STITCH_MAX_SEGMENTS = 12      # cost guardrail: N shots x ~$0.13 — a runaway N is a $ DoS
_HTTP_TIMEOUT_CAP_S = 120      # ceiling on a clip download / chained-frame upload (small, ~MBs)
_MAX_CLIP_BYTES = 256 * 1024 * 1024     # DoP clips ~20MB; generous ceiling (OOM/disk DoS guard)
_MAX_ENDCARD_BYTES = 32 * 1024 * 1024   # an end-card is an image

# KNOWN v1 LIMITATION (review): the SSRF guard resolves DNS once, then httpx resolves again
# at connect time -> a DNS-rebinding/TOCTOU attacker could pass the check then connect to an
# internal IP. Full protection needs resolve-then-pin-the-IP (Host header) or an egress proxy
# (e.g. Smokescreen); deferred to v2. v1 mitigations in place: scheme allowlist + private-range
# rejection (runner._reject_unsafe_url), redirects disabled (follow_redirects=False below), and
# a streamed byte cap. Fetched URLs here are operator-provided (end_card_url) or Higgsfield's own
# CDN (clips), limiting real-world exposure for internal tooling.


async def _download_capped(client, url: str, dest_path: str, max_bytes: int,
                           deadline: float) -> str:
    """Stream a GET to dest_path, aborting if it exceeds max_bytes (Content-Length OR actual
    bytes) — an unbounded .content read could OOM the worker / fill the workspace. Caller
    SSRF-guards `url` first; this client has redirects disabled."""
    written = 0
    async with client.stream("GET", url, timeout=_remaining(deadline)) as r:
        r.raise_for_status()
        cl = r.headers.get("content-length")
        if cl is not None and cl.isdigit() and int(cl) > max_bytes:
            raise ValueError(f"download exceeds cap: content-length {cl} > {max_bytes}")
        with open(dest_path, "wb") as f:
            async for chunk in r.aiter_bytes():
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"download exceeded cap {max_bytes} bytes")
                f.write(chunk)
    return dest_path


def _remaining(deadline: float, cap: float = _HTTP_TIMEOUT_CAP_S) -> float:
    """Budget-bounded I/O timeout: never exceed the overall job deadline, capped (per-call) so
    one slow transfer can't eat the whole budget, floored above 0 (httpx reads 0 as instant-fail).
    `cap` lets the big final-video upload use a larger ceiling than small per-clip transfers."""
    return max(0.001, min(cap, deadline - time.monotonic()))


def _err(text: str, *, started: float, cost: Optional[float] = None,
         status: ResultStatus = ResultStatus.ERROR,
         ec: ErrorClass = ErrorClass.TERMINAL, artifact: Optional[str] = None) -> Result:
    return Result(status=status, error_class=ec, text=text, artifact_ref=artifact,
                  cost=cost or None, ms=_ms(started))


async def _hf_upload(client, base: str, key: str, data: bytes, content_type: str,
                     deadline: float, cap: float = _HTTP_TIMEOUT_CAP_S) -> str:
    """Replicate Higgsfield's two-step upload with raw httpx (no SDK dep):
    POST /files/generate-upload-url {content_type} -> {public_url, upload_url};
    PUT the bytes to upload_url (presigned, no auth header); return public_url.
    Both calls are bounded by the remaining job budget (per-call `cap` — larger for the
    big final-video upload than for small chained-frame uploads)."""
    r = await client.post(base.rstrip("/") + "/files/generate-upload-url",
                          headers={"Authorization": f"Key {key}",
                                   "Content-Type": "application/json"},
                          json={"content_type": content_type}, timeout=_remaining(deadline, cap))
    r.raise_for_status()
    j = r.json()
    public_url, upload_url = j["public_url"], j["upload_url"]
    # SSRF guard: upload_url is server-returned and we PUT to it directly — a malicious/
    # compromised response could point it at an internal/link-local host. Guard it like
    # every other fetched URL (presigned uploads go to public CDN/S3, so a public host is fine).
    if await _reject_unsafe_url(upload_url):
        raise ValueError(f"upload_url rejected (SSRF): {upload_url}")
    pr = await client.put(upload_url, content=data,
                          headers={"Content-Type": content_type}, timeout=_remaining(deadline, cap))
    pr.raise_for_status()
    return public_url


async def invoke_stitch(spec: AgentSpec, job: Job, *, transport=None) -> Result:
    try:
        import httpx
    except ImportError:
        return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                      text="stitcher needs httpx (pip install httpx)")
    started = time.monotonic()
    a = spec.adapter
    base = a.get("base")
    if not base:
        return _err("stitcher adapter missing 'base'", started=started)
    secret_ref = a.get("secret_ref")
    key = os.environ.get(secret_ref) if secret_ref else None
    if not key:
        return _err(f"missing API key in env: {secret_ref or '<secret_ref unset>'}", started=started)
    default_app = job.context.get("application") or a.get("application")

    shots = job.context.get("shotlist")
    if not isinstance(shots, list) or not shots:
        return _err("stitcher job missing non-empty 'shotlist' in context", started=started)
    if not all(isinstance(s, dict) for s in shots):
        return _err("stitcher shotlist entries must be objects", started=started)
    max_seg = _hf_int(a.get("max_segments"), _STITCH_MAX_SEGMENTS)
    if len(shots) > max_seg:
        # fail-closed BEFORE spending: a runaway shot count is a financial DoS.
        return _err(f"shotlist has {len(shots)} shots > max_segments {max_seg}", started=started)

    chain = job.context.get("chain", True)
    workdir = job.worktree_path or tempfile.mkdtemp(prefix="mdx-stitch-")
    os.makedirs(workdir, exist_ok=True)

    # Use the agent's PROFILE timeout, not job.timeout_s: the spine's lease_job builds the
    # Job without applying Profile.timeout_s, so job.timeout_s is the dead 300s default — far
    # too short for 5 sequential renders. (Spine bug filed separately; this reads the source of
    # truth directly.) spec.profile.timeout_s = 2400 for the stitcher.
    deadline = started + spec.profile.timeout_s
    poll_interval = _hf_float(a.get("poll_interval_s"), _HF_POLL_INTERVAL_S)
    retry_backoff = _hf_float(a.get("poll_retry_backoff_s"), _HF_POLL_RETRY_BACKOFF_S)
    retry_max = _hf_int(a.get("poll_retry_max"), _HF_POLL_RETRY_MAX)

    clips: list[str] = []
    total_cost = 0.0
    failed_at: Optional[int] = None
    fail_reason: Optional[str] = None
    prev_last_url: Optional[str] = None

    # follow_redirects=False: a safe URL must not 30x-redirect past the SSRF guard to an
    # internal host (httpx already defaults to this; set explicitly to lock the intent).
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        for i, shot in enumerate(shots):
            if time.monotonic() >= deadline:
                failed_at, fail_reason = i, f"job deadline ({job.timeout_s}s) exceeded before shot {i}"
                break
            seg_path = os.path.join(workdir, f"seg_{i:03d}.mp4")
            start_img = (shot.get("image_url")
                         or (prev_last_url if (chain and i > 0) else None)
                         or job.context.get("image_url"))
            if not start_img:
                failed_at, fail_reason = i, "no image_url (and no chained/base seed)"
                break
            app = shot.get("application") or default_app
            if not app:
                failed_at, fail_reason = i, "no 'application' (model path) for shot"
                break

            res = await _hf_generate(
                client, base=base, application=app, key=key,
                prompt=shot.get("prompt") or job.prompt, image_url=start_img,
                started=time.monotonic(), deadline=deadline,
                motion_id=shot.get("motion_id"), motion_strength=shot.get("motion_strength"),
                end_image_url=shot.get("end_image_url"),
                poll_interval=poll_interval, retry_backoff=retry_backoff, retry_max=retry_max)
            if res.status is not ResultStatus.OK or not res.artifact_ref:
                failed_at, fail_reason = i, res.text
                break
            total_cost += res.cost or 0.0

            # the artifact url is Higgsfield's, but guard it anyway — it's the one URL we
            # FETCH, and 'SSRF-check every fetched URL' is the codebase invariant.
            if await _reject_unsafe_url(res.artifact_ref):
                failed_at, fail_reason = i, f"artifact_ref rejected (SSRF): {res.artifact_ref}"
                break
            try:
                await _download_capped(client, res.artifact_ref, seg_path,
                                       _MAX_CLIP_BYTES, deadline)
            except Exception as e:   # noqa: BLE001 - any download failure -> partial, not a crash
                failed_at, fail_reason = i, f"clip download failed: {e}"
                break
            clips.append(seg_path)

            # CHAIN: extract this clip's last frame, upload it, feed it as the next start.
            # Skip if the next shot brings its own image_url, or this is the last shot.
            last_url = None
            if chain and i < len(shots) - 1 and not shots[i + 1].get("image_url"):
                try:
                    frame_png = os.path.join(workdir, f"frame_{i:03d}.png")
                    await asyncio.to_thread(fu.last_frame_png, seg_path, frame_png)
                    with open(frame_png, "rb") as f:
                        last_url = await _hf_upload(client, base, key, f.read(),
                                                    "image/png", deadline)
                except Exception:   # noqa: BLE001 - chaining is best-effort; next shot falls back
                    last_url = None
            # strict assign (NOT `last_url or prev_last_url`): if this shot didn't produce a
            # chain frame (skipped or extraction failed), clear it so the next no-image shot
            # falls back to the base seed — never silently chains from an OLDER shot's frame.
            prev_last_url = last_url

        # -- assemble whatever succeeded --
        if not clips:
            return _err(f"stitch produced no clips (failed at shot {failed_at}: {fail_reason})",
                        started=started, cost=total_cost)
        # Write the final video to a STABLE local dir. NOT uploaded: Higgsfield's upload
        # endpoint is image/audio-only (rejects video/mp4 with 422), and this is a local-first
        # runtime — the deliverable is a local file the operator opens. The worker's worktree is
        # ephemeral (cleaned per-attempt), so the final mp4 must live OUTSIDE it.
        out_dir = os.path.expanduser(os.environ.get("MDX_STITCH_OUT", "~/.myndaix/stitches"))
        final_path = os.path.join(out_dir, f"stitch_{job.id}.mp4")
        try:
            os.makedirs(out_dir, exist_ok=True)
            seq = list(clips)
            # HYBRID brand layer: append a deterministic end card (exact logo) if given.
            end_card = await _resolve_end_card(client, job, workdir, clips[0], deadline)
            if end_card:
                seq.append(end_card)
            await asyncio.to_thread(fu.concat, seq, final_path)
        except fu.FfmpegError as e:
            return _err(f"stitch assembly (ffmpeg) failed: {e}", started=started, cost=total_cost)
        except Exception as e:   # noqa: BLE001
            return _err(f"stitch assembly failed: {type(e).__name__}: {e}",
                        started=started, cost=total_cost)

    if failed_at is not None:
        # partial success: hand back the concatenated good shots (local path) + the reason.
        return _err(f"stitch PARTIAL {len(clips)}/{len(shots)} shots "
                    f"(failed at {failed_at}: {fail_reason})",
                    started=started, cost=total_cost, artifact=final_path)
    return Result(status=ResultStatus.OK, text=final_path, artifact_ref=final_path,
                  cost=total_cost or None, ms=_ms(started))


async def _resolve_end_card(client, job: Job, workdir: str, ref_clip: str,
                            deadline: float) -> Optional[str]:
    """If the job supplies a branded end-card image URL (end_card_url), download it
    (SSRF-guarded) and render it into a static clip sized to match the first clip so it
    concats uniformly. URL-only by design: a local end_card_path from untrusted job
    context is a path-traversal/arbitrary-read risk, so it is NOT honored. Returns the
    clip path, or None if no end card / on any failure (end card is optional polish)."""
    url = job.context.get("end_card_url")
    if not url:
        return None
    try:
        # WE fetch this URL directly -> SSRF guard (reject internal/loopback/link-local).
        if await _reject_unsafe_url(url):
            return None
        img = os.path.join(workdir, "endcard_src")
        await _download_capped(client, url, img, _MAX_ENDCARD_BYTES, deadline)
        s = await asyncio.to_thread(fu.probe, ref_clip)
        size = (int(s["width"]), int(s["height"]))
        dur = _hf_float(job.context.get("end_card_seconds"), 2.0)
        out = os.path.join(workdir, "endcard.mp4")
        await asyncio.to_thread(lambda: fu.image_to_clip(img, out, duration=dur, size=size))
        return out
    except Exception:   # noqa: BLE001 - end card is optional; never break the render
        return None
