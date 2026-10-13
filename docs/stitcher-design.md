# DESIGN: video stitcher (`stitch` adapter) — long video from a shot-list

**Status:** BUILT on `feature/stitcher` (v1). Jefe-approved shape; awaiting KilaBz+Oracle review → merge.
**Lives in:** `src/runtime/runner_stitch.py`, `src/runtime/ffmpeg_util.py`, `registry.py`, `runner.py`, `cli.py`.
**Prior art:** `~/research/2026-06-25-{stitcher-prior-art,brand-video-pipeline}-brief.md`.

## 1. What it does & why
DoP clips are fixed ~5s and every AI video model caps short, so long-form must be **stitched**. The `stitcher` agent takes a **shot-list**, generates each shot via DoP (camera-preset aware), **chains the previous shot's last frame as the next shot's start image** for continuity, `ffmpeg`-concatenates, applies a **deterministic brand layer**, and returns one long mp4.

This is **stage 4** of the larger pipeline (plan → keyframe → clip → stitch). v1 = the stitcher with a **hand-written** shot-list; the LLM shot-planner (stage 1) and format fan-out are deferred (YAGNI per prior-art).

## 2. The HYBRID rule (governing principle)
**AI for *approximate*** (the moving b-roll) · **deterministic for *exact*** (logo, end-card — `ffmpeg` overlay, never AI-rendered; models can't do pixel-exact logos/text). The real logo enters via the composited seed and/or the appended end card.

## 3. Architecture
- `_hf_generate()` (refactored out of `invoke_higgsfield`) is the ONE shared submit→poll primitive, holding the §5-A fail-closed charge-correctness contract. Both the single-clip agent and the stitcher call it. **motion_id / motion_strength / end_image_url** now flow through it (DoP camera presets + start/end anchoring).
- `invoke_stitch` is ONE self-contained job (not N ledger sub-jobs) → all spend under one cost ceiling. **authority=WORKSPACE_ACTOR ⇒ never auto-retried** (a retry can't silently re-charge). *Cross-run resume is **deferred to v2*** — the worker's per-attempt worktree is ephemeral, so in-workspace state can't survive a re-run; combined with never-auto-retried, an in-run manifest is dead weight (removed after review).
- Inputs ride in `Job.context` (`shotlist`, `image_url`, `chain`, `end_card_url/path`, `application`); output URL → `Result.artifact_ref`. **No spine contract change.**

## 4. Data flow
```
mxr stitcher "<brand>" --shotlist shots.json [--end-card logo.png]
 └ invoke_stitch (workspace-actor):
     for each shot i:
        start = shot.image_url  OR  prev last-frame (chain)  OR  job base image_url
        clip  = _hf_generate(prompt, start, motion_id, motion_strength, end_image_url)
        SSRF-guard artifact_ref → download clip → seg_i.mp4 ; (chain) last_frame_png → upload → next start
     ffmpeg concat(seg_*) [+ deterministic end-card clip] → final.mp4 → upload → artifact_ref
     (loop is deadline-bounded; all I/O timeouts clamped to remaining budget)
```

## 5. ffmpeg layer (`ffmpeg_util.py`)
System ffmpeg only (no python-ffmpeg dep). args-as-list, never `shell=True`, sync (called via `asyncio.to_thread`). `concat` ffprobe-gates: **demuxer `-c copy` when clips uniform** (instant, lossless), else **scale+pad+setsar+fps+format → concat filter, `libx264 -crf 18`**. `last_frame_png` full-decode (not `-sseof`). `image_to_clip` (end cards). `overlay_image` (watermark, available for v1.1).

## 6. Edge cases & failure modes
- **Partial failure** (shot _k_ fails): concat shots 0..k-1, return TERMINAL + the partial artifact + "PARTIAL k/N". No spend wasted; no auto-retry → no re-charge.
- **Cost ceiling:** `max_segments` (adapter, default 12) rejected **before** any spend (runaway N = financial DoS).
- **Chaining best-effort:** a last-frame extract/upload hiccup falls back to the next shot's own/base seed; only fails if that shot has no fallback.
- **ffmpeg missing / fails:** clean TERMINAL, never a crash. **End card optional:** any failure there is swallowed (polish, not load-bearing).
- Honors `job.timeout_s` across the WHOLE loop (each segment bounded by the overall deadline).

## 7. Security
- `HF_KEY` from env only. **SSRF** guard (in `_hf_generate`) on every URL handed to Higgsfield (image_url + end frames), incl. IPv4-mapped/private/link-local. ffmpeg runs on files we control with fixed argv. Workspace is scratch. Status/cancel URLs origin-pinned before the key is attached.

## 8. Decisions (LOCKED)
Shot-list = single prompt OR per-shot list · chaining ON by default (`chain=false` for montage) · DoP-only v1 (model-agnostic via `application`; premium = later row) · one self-contained job (no auto-retry). End card is **URL-only** (`end_card_url`, SSRF-guarded); local `end_card_path` rejected (path-traversal). **Resume deferred to v2** (was removed after review — ephemeral worktree made it a false promise).

## 9. Tests (`tests/test_stitch.py`, all green)
Mocked `_hf_generate`+ffmpeg: happy-path chaining+upload, partial-failure-returns-partial, max_segments-pre-spend reject, missing shotlist/key, explicit-image-wins/skip-chaining, end-card SSRF rejected, motion_id-lands-in-body. Real-ffmpeg fixtures: concat + last_frame + image_to_clip. Full non-DB suite: 50 passing.

## 10. Deferred (NOT in v1, per YAGNI)
LLM shot-planner (stage 1) · format/hook fan-out · Remotion animated overlays · premium model backends (fal/Seedance) · per-segment sub-job re-roll · global color-match pass.
