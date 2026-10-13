"""Stitcher tests. The per-segment generation (_hf_generate) and ffmpeg calls are
mocked so these run fast and offline; a couple of tests exercise REAL ffmpeg against
tiny generated fixtures to prove the concat/last-frame helpers. Sync test functions
drive the async runner via asyncio.run (matches test_runner.py; no asyncio plugin needed).
"""
import asyncio
import os
import shutil
import subprocess
import uuid

import httpx
import pytest

from runtime.contracts import Authority, ErrorClass, Job, Reach, Result, ResultStatus
from runtime.registry import AgentSpec
from runtime import ffmpeg_util, runner_stitch


# -- fixtures / helpers ----------------------------------------------------
def _spec(max_segments=12):
    return AgentSpec(agent_id="stitcher", reach=Reach.API, authority=Authority.WORKSPACE_ACTOR,
                     model="dop-lite", role="stitch",
                     adapter={"kind": "stitch", "base": "https://platform.higgsfield.ai",
                              "secret_ref": "HF_KEY", "application": "/higgsfield-ai/dop/lite",
                              "max_segments": max_segments})


def _job(shots, workdir, **ctx):
    c = {"shotlist": shots}
    c.update(ctx)
    return Job(id=uuid.uuid4(), to_agent="stitcher", prompt="brand reveal",
               context=c, worktree_path=str(workdir), timeout_s=300)


def _mock_transport():
    """Handles the download (GET), upload-url POST, and presigned PUT. Each upload-url
    request returns a DISTINCT public_url so chaining + final can be asserted."""
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u.endswith("/files/generate-upload-url"):
            state["n"] += 1
            n = state["n"]
            # upload_url must resolve to a public host (now SSRF-guarded before PUT)
            return httpx.Response(200, json={"public_url": f"https://cdn/up{n}.png",
                                             "upload_url": f"https://example.com/put{n}"})
        if req.method == "PUT":
            return httpx.Response(200)
        if req.method == "GET":
            return httpx.Response(200, content=b"FAKE_MP4_BYTES")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _patch_ffmpeg(monkeypatch):
    """Replace the ffmpeg shellouts with file-creating stubs (no real ffmpeg)."""
    def fake_last_frame(video, out):
        with open(out, "wb") as f:
            f.write(b"PNG")
        return out

    def fake_concat(paths, out, **kw):
        with open(out, "wb") as f:
            f.write(b"MP4")
        return out

    def fake_probe(p):
        return {"width": "1080", "height": "1920", "codec_name": "h264",
                "pix_fmt": "yuv420p", "sample_aspect_ratio": "1:1", "time_base": "1/30"}

    def fake_img2clip(img, out, **kw):
        with open(out, "wb") as f:
            f.write(b"MP4")
        return out

    monkeypatch.setattr(ffmpeg_util, "last_frame_png", fake_last_frame)
    monkeypatch.setattr(ffmpeg_util, "concat", fake_concat)
    monkeypatch.setattr(ffmpeg_util, "probe", fake_probe)
    monkeypatch.setattr(ffmpeg_util, "image_to_clip", fake_img2clip)


def _ok_gen(calls, fail_at=None):
    """A fake _hf_generate that records each call and returns OK (or a failure at fail_at)."""
    async def gen(client, *, base, application, key, prompt, image_url, started, deadline, **kw):
        calls.append({"image_url": image_url, "motion_id": kw.get("motion_id"),
                      "application": application, "prompt": prompt})
        if fail_at is not None and len(calls) - 1 == fail_at:
            return Result(status=ResultStatus.ERROR, error_class=ErrorClass.TERMINAL,
                          text="gen boom")
        # use a resolvable host: the real invoke_stitch SSRF-guards artifact_ref before download
        return Result(status=ResultStatus.OK, text=f"https://example.com/clip{len(calls)}.mp4",
                      artifact_ref=f"https://example.com/clip{len(calls)}.mp4", cost=0.13)
    return gen


# -- tests -----------------------------------------------------------------
def test_stitch_happy_path_chains_and_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_KEY", "kid:secret")
    monkeypatch.setenv("MDX_STITCH_OUT", str(tmp_path / "out"))   # final mp4 -> temp, not ~/.myndaix
    _patch_ffmpeg(monkeypatch)
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls))
    job = _job([
        {"prompt": "open", "image_url": "https://seed/0.png", "motion_id": "M0"},
        {"prompt": "mid", "motion_id": "M1"},
        {"prompt": "close", "motion_id": "M2"},
    ], tmp_path)
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), job, transport=_mock_transport()))
    assert r.status is ResultStatus.OK, r.text
    assert len(calls) == 3
    # shot 0 uses its explicit seed; shots 1 & 2 chain off the uploaded last frame
    assert calls[0]["image_url"] == "https://seed/0.png"
    assert calls[1]["image_url"] == "https://cdn/up1.png"   # chained (frame upload)
    assert calls[2]["image_url"] == "https://cdn/up2.png"   # chained
    assert calls[0]["motion_id"] == "M0"
    # final = a LOCAL mp4 path (no video upload — Higgsfield upload is image/audio-only)
    assert r.artifact_ref.endswith(f"stitch_{job.id}.mp4")
    assert os.path.isfile(r.artifact_ref)
    assert r.cost == pytest.approx(0.39)


def test_stitch_partial_failure_returns_what_succeeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_KEY", "kid:secret")
    monkeypatch.setenv("MDX_STITCH_OUT", str(tmp_path / "out"))
    _patch_ffmpeg(monkeypatch)
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls, fail_at=1))  # 2nd shot fails
    shots = [{"prompt": "a", "image_url": "https://seed/0.png"},
             {"prompt": "b"}, {"prompt": "c"}]
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), _job(shots, tmp_path),
                                                transport=_mock_transport()))
    assert r.status is ResultStatus.ERROR
    assert "PARTIAL 1/3" in r.text
    assert r.artifact_ref is not None      # the 1 good shot was still concatenated + returned
    assert len(calls) == 2                 # stopped after the failure


def test_stitch_rejects_over_max_segments_before_spending(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_KEY", "kid:secret")
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls))
    shots = [{"prompt": str(i)} for i in range(5)]
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(max_segments=3), _job(shots, tmp_path)))
    assert r.status is ResultStatus.ERROR
    assert "max_segments" in r.text
    assert calls == []                     # fail-closed: nothing generated, nothing charged


def test_stitch_missing_shotlist_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_KEY", "kid:secret")
    job = Job(id=uuid.uuid4(), to_agent="stitcher", prompt="x", context={},
              worktree_path=str(tmp_path), timeout_s=300)
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), job))
    assert r.status is ResultStatus.ERROR
    assert "shotlist" in r.text


def test_stitch_missing_key_is_terminal(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_KEY", raising=False)
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), _job([{"prompt": "a"}], tmp_path)))
    assert r.status is ResultStatus.ERROR
    assert "API key" in r.text


def test_stitch_explicit_image_wins_and_skips_chaining(tmp_path, monkeypatch):
    """An explicit per-shot image_url overrides the chain; and when the NEXT shot brings
    its own image, the current shot does NOT do a last-frame extract+upload (chain skipped)."""
    monkeypatch.setenv("HF_KEY", "kid:secret")
    monkeypatch.setenv("MDX_STITCH_OUT", str(tmp_path / "out"))
    lf_calls = []

    def fake_last_frame(video, out):
        lf_calls.append(video); open(out, "wb").write(b"PNG"); return out

    def fake_concat(paths, out, **kw):
        open(out, "wb").write(b"MP4"); return out

    monkeypatch.setattr(ffmpeg_util, "last_frame_png", fake_last_frame)
    monkeypatch.setattr(ffmpeg_util, "concat", fake_concat)
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls))
    shots = [{"prompt": "a", "image_url": "https://example.com/A.png"},
             {"prompt": "b", "image_url": "https://example.com/B.png"},  # mid-list explicit
             {"prompt": "c"}]
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), _job(shots, tmp_path),
                                                transport=_mock_transport()))
    assert r.status is ResultStatus.OK, r.text
    assert calls[1]["image_url"] == "https://example.com/B.png"   # explicit beats chain
    # shot 0 must NOT extract a last frame (shot 1 has its own image); shot 1 -> shot 2 DOES.
    assert len(lf_calls) == 1                                     # only one chain hop (1->2)


def test_stitch_end_card_ssrf_rejected(tmp_path, monkeypatch):
    """An internal/loopback end_card_url is rejected by the SSRF guard BEFORE any fetch,
    and is silently skipped (the render still succeeds without the end card)."""
    monkeypatch.setenv("HF_KEY", "kid:secret")
    monkeypatch.setenv("MDX_STITCH_OUT", str(tmp_path / "out"))
    calls = []
    monkeypatch.setattr(runner_stitch, "_hf_generate", _ok_gen(calls))
    # patch ffmpeg but record image_to_clip calls (proves the end card path ran or not)
    i2c_calls = []

    def fake_last_frame(video, out):
        open(out, "wb").write(b"PNG"); return out

    def fake_concat(paths, out, **kw):
        open(out, "wb").write(b"MP4"); return out

    def fake_probe(p):
        return {"width": "1080", "height": "1920", "codec_name": "h264",
                "pix_fmt": "yuv420p", "sample_aspect_ratio": "1:1", "time_base": "1/30"}

    def fake_img2clip(img, out, **kw):
        i2c_calls.append(img); open(out, "wb").write(b"MP4"); return out

    monkeypatch.setattr(ffmpeg_util, "last_frame_png", fake_last_frame)
    monkeypatch.setattr(ffmpeg_util, "concat", fake_concat)
    monkeypatch.setattr(ffmpeg_util, "probe", fake_probe)
    monkeypatch.setattr(ffmpeg_util, "image_to_clip", fake_img2clip)

    shots = [{"prompt": "a", "image_url": "https://example.com/0.png"}]
    job = _job(shots, tmp_path, end_card_url="http://169.254.169.254/evil.png")  # AWS metadata IP
    r = asyncio.run(runner_stitch.invoke_stitch(_spec(), job, transport=_mock_transport()))
    assert r.status is ResultStatus.OK, r.text          # render still succeeds
    assert i2c_calls == []                              # end card was NOT built (SSRF-rejected, never fetched)

    # positive control: a SAFE end_card_url DOES get fetched + built (proves the guard isn't
    # silently skipping every end card — the await-trap false-pass the review warned about).
    i2c_calls.clear()
    job2 = _job(shots, tmp_path, end_card_url="https://example.com/card.png")
    r2 = asyncio.run(runner_stitch.invoke_stitch(_spec(), job2, transport=_mock_transport()))
    assert r2.status is ResultStatus.OK, r2.text
    assert len(i2c_calls) == 1                          # safe end card WAS built


def test_hf_upload_rejects_internal_upload_url():
    """_hf_upload must SSRF-guard the server-returned upload_url before the PUT — an
    internal/link-local upload_url is rejected and NO PUT is made (Oracle P1)."""
    import time
    put_calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/files/generate-upload-url"):
            return httpx.Response(200, json={"public_url": "https://cdn.example/ok.png",
                                             "upload_url": "http://169.254.169.254/put"})
        if req.method == "PUT":
            put_calls["n"] += 1
            return httpx.Response(200)
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await runner_stitch._hf_upload(c, "https://platform.higgsfield.ai", "k:s",
                                           b"data", "image/png", time.monotonic() + 60)

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert put_calls["n"] == 0   # rejected BEFORE any PUT left the process


# -- motion_id wiring (invoke_higgsfield through the shared helper) ---------
def test_motion_id_lands_in_submit_body(monkeypatch):
    from runtime import runner
    monkeypatch.setenv("HF_KEY", "kid:secret")
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/dop/lite"):
            import json as _j
            captured.update(_j.loads(req.content))
            return httpx.Response(200, json={"request_id": "r1",
                                             "status_url": "https://platform.higgsfield.ai/requests/r1/status"})
        return httpx.Response(200, json={"status": "completed",
                                         "video": {"url": "https://cdn/out.mp4"}})

    spec = AgentSpec(agent_id="higgsfield", reach=Reach.API, authority=Authority.RESPONDER,
                     model="dop-lite", role="v",
                     adapter={"kind": "higgsfield", "base": "https://platform.higgsfield.ai",
                              "secret_ref": "HF_KEY", "application": "/higgsfield-ai/dop/lite"})
    job = Job(id=uuid.uuid4(), to_agent="higgsfield", prompt="reveal",
              context={"image_url": "https://example.com/x.png", "motion_id": "AGENT-REVEAL-UUID",
                       "motion_strength": 0.4}, timeout_s=60)
    r = asyncio.run(runner.invoke_higgsfield(spec, job, transport=httpx.MockTransport(handler)))
    assert r.status is ResultStatus.OK, r.text
    assert captured.get("motion_id") == "AGENT-REVEAL-UUID"
    assert captured.get("motion_strength") == 0.4


# -- real ffmpeg fixtures (skip if ffmpeg absent) --------------------------
_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_clip(path, color):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:s=320x240:d=0.5:r=30",
                    "-pix_fmt", "yuv420p", "-an", path], capture_output=True, check=True)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_ffmpeg_concat_and_last_frame_real(tmp_path):
    a, b = str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")
    _make_clip(a, "red")
    _make_clip(b, "blue")
    assert ffmpeg_util.clips_uniform([a, b]) is True
    out = ffmpeg_util.concat([a, b], str(tmp_path / "cat.mp4"))
    assert os.path.getsize(out) > 0
    png = ffmpeg_util.last_frame_png(a, str(tmp_path / "last.png"))
    assert os.path.getsize(png) > 0


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_ffmpeg_image_to_clip_real(tmp_path):
    card = str(tmp_path / "card.png")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1",
                    "-frames:v", "1", card], capture_output=True, check=True)
    clip = ffmpeg_util.image_to_clip(card, str(tmp_path / "card.mp4"), duration=1.0, size=(320, 240))
    assert os.path.getsize(clip) > 0
