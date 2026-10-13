"""ffmpeg/ffprobe helpers for the stitcher: concat, last-frame extract, image->clip,
logo overlay. System ffmpeg ONLY (no python-ffmpeg deps — that lib is dead); every
call is args-as-list through subprocess, NEVER shell=True, on files WE control.

These are SYNC (subprocess.run). The async runner calls them via asyncio.to_thread so
they don't block the event loop. Any failure raises FfmpegError with the captured
stderr tail; the caller (invoke_stitch) maps that to a clean TERMINAL Result.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional, Sequence


class FfmpegError(RuntimeError):
    """ffmpeg/ffprobe missing, timed out, or exited non-zero (stderr tail attached)."""


def _bin(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise FfmpegError(f"{name} not found on PATH (install: brew install ffmpeg)")
    return p


def _run(argv: Sequence[str], timeout: float = 600) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(list(argv), capture_output=True, timeout=timeout)
    except FileNotFoundError as e:           # binary vanished between which() and run()
        raise FfmpegError(str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise FfmpegError(f"{argv[0]} timed out after {timeout}s") from e
    if r.returncode != 0:
        tail = (r.stderr or b"").decode(errors="replace")[-600:]
        raise FfmpegError(f"{os.path.basename(str(argv[0]))} exit {r.returncode}: {tail}")
    return r


def probe(path: str) -> dict:
    """Return the first video stream's key properties (raises if no video stream)."""
    r = _run([_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
              "-show_entries",
              "stream=codec_name,width,height,pix_fmt,sample_aspect_ratio,r_frame_rate,time_base",
              "-of", "json", path], timeout=30)
    try:
        streams = (json.loads(r.stdout or b"{}").get("streams")) or []
    except ValueError as e:
        raise FfmpegError(f"ffprobe gave non-JSON for {path}: {e}") from e
    if not streams:
        raise FfmpegError(f"no video stream in {path}")
    return streams[0]


def clips_uniform(paths: Sequence[str]) -> bool:
    """True if every clip shares codec/dimensions/pixfmt/SAR/timebase — i.e. the
    concat demuxer can stream-copy them losslessly with no re-encode."""
    if len(paths) < 2:
        return True
    # include r_frame_rate: two clips can share time_base yet differ in real frame cadence
    # (VFR / different fps), which the stream-copy concat would join into a glitchy result.
    keys = ("codec_name", "width", "height", "pix_fmt", "sample_aspect_ratio",
            "time_base", "r_frame_rate")
    sig0 = None
    for p in paths:
        s = probe(p)
        sig = tuple(s.get(k) for k in keys)
        if sig0 is None:
            sig0 = sig
        elif sig != sig0:
            return False
    return True


def concat(paths: Sequence[str], out_path: str, *, fps: int = 30, crf: int = 18) -> str:
    """Concatenate clips into out_path. Demuxer stream-copy when uniform (instant,
    lossless); otherwise normalize (scale+pad to the FIRST clip's dimensions, unify
    sar/fps/pixfmt) and re-encode with libx264. Video-only (-an): i2v clips are silent;
    audio is scored in post."""
    paths = [p for p in paths if p]
    if not paths:
        raise FfmpegError("concat: no clips")

    if clips_uniform(paths):
        listfile = out_path + ".concat.txt"
        with open(listfile, "w") as f:
            for p in paths:
                # single-quote-escape per ffmpeg concat-demuxer rules
                safe = os.path.abspath(p).replace("'", r"'\''")
                f.write(f"file '{safe}'\n")
        try:
            _run([_bin("ffmpeg"), "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                  "-c", "copy", "-an", out_path])
        finally:
            try:
                os.remove(listfile)
            except OSError:
                pass
        return out_path

    # re-encode branch: target = first clip's dimensions
    s0 = probe(paths[0])
    w, h = int(s0["width"]), int(s0["height"])
    argv: list[str] = [_bin("ffmpeg"), "-y"]
    for p in paths:
        argv += ["-i", p]
    parts, labels = [], ""
    for i in range(len(paths)):
        parts.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
        labels += f"[v{i}]"
    filt = ";".join(parts) + f";{labels}concat=n={len(paths)}:v=1:a=0[outv]"
    argv += ["-filter_complex", filt, "-map", "[outv]",
             "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", "-an", out_path]
    _run(argv)
    return out_path


def last_frame_png(video_path: str, out_png: str) -> str:
    """Extract the final frame as a PNG by full decode (-update 1 overwrites until the
    last frame remains). NOT -sseof, which snaps to the nearest keyframe. Used as the
    next segment's conditioning image for last-frame chaining."""
    _run([_bin("ffmpeg"), "-y", "-i", video_path, "-an", "-update", "1",
          "-q:v", "2", out_png])
    if not os.path.isfile(out_png) or os.path.getsize(out_png) == 0:
        raise FfmpegError(f"last_frame_png produced no output for {video_path}")
    return out_png


def image_to_clip(image_path: str, out_path: str, *, duration: float = 2.0,
                  fps: int = 30, size: Optional[tuple[int, int]] = None) -> str:
    """Render a still image into a static video clip (e.g. a branded end card).
    Optionally scale+pad to `size` so it concats uniformly with the generated clips."""
    vf = []
    if size:
        w, h = size
        vf.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
        vf.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    vf.append("setsar=1")
    vf.append("format=yuv420p")
    _run([_bin("ffmpeg"), "-y", "-loop", "1", "-i", image_path, "-t", str(duration),
          "-r", str(fps), "-vf", ",".join(vf), "-c:v", "libx264", "-crf", "18",
          "-an", out_path])
    return out_path


def overlay_image(video_path: str, image_path: str, out_path: str, *,
                  x: str = "(W-w)/2", y: str = "(H-h)/2", crf: int = 18) -> str:
    """Burn a (transparent-PNG) logo/watermark over a video deterministically — the
    HYBRID rule's exact layer (the real mark, pixel-perfect, never AI-rendered).
    Default position is centered; pass ffmpeg expressions for corners."""
    _run([_bin("ffmpeg"), "-y", "-i", video_path, "-i", image_path,
          "-filter_complex", f"[0:v][1:v]overlay={x}:{y}:format=auto[outv]",
          "-map", "[outv]", "-c:v", "libx264", "-crf", str(crf),
          "-pix_fmt", "yuv420p", "-an", out_path])
    return out_path
