"""Pure, dependency-free measured critic for the MX Quality Orchestrator (stage 4).

"Measure, don't eyeball" (mirrors mx-engine verify_render.check_no_clip's (ok, message)
discipline). Operates on a RAW RGB frame (bytes, w, h) — the orchestrator extracts one
downscaled frame from the supplier plate via ffmpeg (`-pix_fmt rgb24 -f rawvideo`), so this
core needs NO numpy/Pillow/ffmpeg and is unit-testable on synthetic byte frames.

v1 gate (background-plate-only): HARD-FAIL only on a missing / trivial (flat, near-empty)
plate — the thing that means generation actually failed. Palette-drift and text-presence are
ADVISORY WARNs, not fails: per the build corollary, v1 prompts the supplier to emit no text and
generates background only, so a cheap text-region FLAG (not a hard gate) is the right scope; a
calibrated hard text/face gate is v2. NO LLM in the decision.

DESIGN: docs/MX_QUALITY_ORCHESTRATOR.md §6 + corollary 8.
"""
from __future__ import annotations

__all__ = ["parse_hex", "frame_stats", "critic_generic", "DEFAULTS",
           "cosine_similarity", "critic_persona", "embed_face", "PERSONA_DEFAULTS"]

DEFAULTS = {
    "MIN_DISTINCT": 12,      # quantized (4-bit/chan) distinct colors below this = trivial/flat plate
    "MIN_LUMA_VAR": 80.0,    # luma variance below this = a near-flat frame (failed render)
    "PALETTE_TOL": 70,       # RGB euclidean distance counting a pixel as "on brand palette"
    "MIN_PALETTE_FRAC": 0.10,    # below this fraction on-palette -> WARN palette drift (advisory)
    "TEXT_EDGE_THRESH": 48,  # per-pixel horizontal luma delta counting as a hard edge
    "MAX_TEXT_FRAC": 0.14,   # high-edge fraction above this -> WARN possible text/UI (advisory)
}


def parse_hex(h: str) -> tuple[int, int, int]:
    """'#5AE0A0' or '5ae0a0' -> (r,g,b). Raises ValueError on a malformed hex."""
    s = h.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"bad hex {h!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _luma(r: int, g: int, b: int) -> int:
    return (r * 299 + g * 587 + b * 114) // 1000


def frame_stats(rgb: bytes, w: int, h: int, hexes: list[str]) -> dict:
    """Measure ONE raw rgb24 frame. Pure arithmetic over the byte array (no deps).
    Returns distinct_colors, luma_var, palette_frac (frac of px near a brand hex), text_frac
    (frac of horizontally high-contrast px ≈ text/UI heuristic), and pixel count."""
    n = w * h
    if n <= 0 or len(rgb) < n * 3:
        return {"pixels": 0, "distinct_colors": 0, "luma_var": 0.0,
                "palette_frac": 0.0, "text_frac": 0.0}
    pal = [parse_hex(x) for x in (hexes or [])]
    tol2 = DEFAULTS["PALETTE_TOL"] ** 2
    edge_thr = DEFAULTS["TEXT_EDGE_THRESH"]

    seen: set[int] = set()
    luma_sum = 0
    luma_sq = 0
    on_pal = 0
    high_edge = 0
    lumas = [0] * n
    for i in range(n):
        o = i * 3
        r, g, b = rgb[o], rgb[o + 1], rgb[o + 2]
        seen.add((r >> 4 << 8) | (g >> 4 << 4) | (b >> 4))   # 4-bit/chan quantize
        lu = _luma(r, g, b)
        lumas[i] = lu
        luma_sum += lu
        luma_sq += lu * lu
        for (pr, pg, pb) in pal:
            if (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2 <= tol2:
                on_pal += 1
                break
    # horizontal high-contrast edges (row-wise), a cheap text/UI proxy
    edges = 0
    for y in range(h):
        base = y * w
        prev = lumas[base]
        for x in range(1, w):
            cur = lumas[base + x]
            if abs(cur - prev) > edge_thr:
                edges += 1
            prev = cur
    high_edge = edges
    mean = luma_sum / n
    var = max(0.0, luma_sq / n - mean * mean)
    edge_denom = max(1, h * (w - 1))
    return {
        "pixels": n,
        "distinct_colors": len(seen),
        "luma_var": round(var, 2),
        "palette_frac": round(on_pal / n, 4),
        "text_frac": round(high_edge / edge_denom, 4),
    }


def critic_generic(rgb: bytes, w: int, h: int, *, hexes: list[str],
                   cfg: dict | None = None) -> dict:
    """Gate a generic background plate frame. HARD-FAIL only on missing/trivial (a failed
    render); palette-drift + text-presence are advisory WARNs. Returns:
      {status: 'pass'|'warn'|'fail', metric: {...}, reasons: [...], retry_hint: {...}|None}
    retry_hint (on FAIL) adjusts exactly ONE variable (motion_strength) per the design's
    bounded-one-variable retry rule."""
    c = {**DEFAULTS, **(cfg or {})}
    st = frame_stats(rgb, w, h, hexes)
    reasons: list[str] = []

    if st["pixels"] == 0:
        return {"status": "fail", "metric": st, "reasons": ["empty/unreadable frame"],
                "retry_hint": {"motion_strength_delta": -0.15}}
    trivial = (st["distinct_colors"] < c["MIN_DISTINCT"] or st["luma_var"] < c["MIN_LUMA_VAR"])
    if trivial:
        reasons.append(f"trivial plate (distinct={st['distinct_colors']}<{c['MIN_DISTINCT']} "
                       f"or luma_var={st['luma_var']}<{c['MIN_LUMA_VAR']}) — likely failed render")
        return {"status": "fail", "metric": st, "reasons": reasons,
                "retry_hint": {"motion_strength_delta": -0.15}}

    if st["palette_frac"] < c["MIN_PALETTE_FRAC"]:
        reasons.append(f"palette drift (on-brand frac {st['palette_frac']} < {c['MIN_PALETTE_FRAC']})")
    if st["text_frac"] > c["MAX_TEXT_FRAC"]:
        reasons.append(f"possible text/UI on plate (edge frac {st['text_frac']} > {c['MAX_TEXT_FRAC']}; "
                       "advisory heuristic — supplier must render NO text)")
    status = "warn" if reasons else "pass"
    return {"status": status, "metric": st, "reasons": reasons, "retry_hint": None}


# ============================ persona / Soul-ID gate (v2 trigger) ===========================
# A face-embedding identity gate for persona renders (e.g. "Agent Steve"): does the generated
# plate actually show the brand persona, not a drifted face? Thresholds are cosine SIMILARITY on
# 512-D ArcFace (buffalo_l) embeddings, per the Recon brief (more lenient than the design body's
# distance guess; cosine distance = 1 - similarity). Calibrate on a labeled set before relying on it.
# The GATE LOGIC below is pure + tested; the actual embedding (embed_face) is behind an OPTIONAL
# InsightFace import — install `insightface onnxruntime` + seed reference stills to use it live.
PERSONA_DEFAULTS = {
    "SIM_PASS": 0.45,        # cosine similarity >= this -> same person (Recon: 0.35-0.45 "likely same")
    "SIM_WARN": 0.35,        # [SIM_WARN, SIM_PASS) -> WARN (borderline); below -> FAIL (drifted)
    "MIN_FACE_FRAC": 0.04,   # face_area/frame_area below this -> too small to trust identity (ABORT)
}


def cosine_similarity(a, b) -> float:
    """Cosine similarity of two equal-length embedding vectors (pure; no numpy)."""
    if not a or not b or len(a) != len(b):
        raise ValueError("embeddings must be non-empty + equal length")
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def critic_persona(frame_embedding, ref_embedding, face_area_frac: float,
                   *, cfg: dict | None = None) -> dict:
    """Gate ONE persona plate frame against a reference embedding (mean of N canonical stills).
    Pure: the caller supplies the embeddings (via embed_face). FAIL on no-face or too-small-face
    (can't trust identity) or a similarity below the warn floor; WARN in the borderline band; PASS
    above. Returns {status, metric, reasons}."""
    c = {**PERSONA_DEFAULTS, **(cfg or {})}
    if frame_embedding is None:
        return {"status": "fail", "metric": {"face": False}, "reasons": ["no face detected on the plate"]}
    if face_area_frac < c["MIN_FACE_FRAC"]:
        return {"status": "fail", "metric": {"face_area_frac": round(face_area_frac, 4)},
                "reasons": [f"face too small ({face_area_frac:.3f} < {c['MIN_FACE_FRAC']}) — "
                            "can't trust identity"]}
    sim = cosine_similarity(frame_embedding, ref_embedding)
    m = {"similarity": round(sim, 4), "face_area_frac": round(face_area_frac, 4)}
    if sim >= c["SIM_PASS"]:
        return {"status": "pass", "metric": m, "reasons": []}
    if sim >= c["SIM_WARN"]:
        return {"status": "warn", "metric": m,
                "reasons": [f"persona similarity {sim:.3f} in warn band "
                            f"[{c['SIM_WARN']}, {c['SIM_PASS']})"]}
    return {"status": "fail", "metric": m,
            "reasons": [f"persona mismatch: similarity {sim:.3f} < {c['SIM_WARN']}"]}


def embed_face(image_path: str):
    """Largest-face 512-D ArcFace embedding + its area fraction, via InsightFace (buffalo_l).
    Returns (embedding: list[float], face_area_frac: float), or (None, 0.0) if no face is found.
    Raises RuntimeError if InsightFace/onnxruntime aren't installed (v2 dependency:
    `pip install insightface onnxruntime`). NOT unit-tested here (heavy model + weights download);
    the GATE LOGIC is covered via critic_persona with injected embeddings."""
    try:
        import cv2
        import numpy as np
        from insightface.app import FaceAnalysis
    except ImportError as e:
        raise RuntimeError("persona gate needs InsightFace + onnxruntime + opencv "
                           "(pip install insightface onnxruntime opencv-python-headless)") from e
    img = cv2.imread(image_path)
    if img is None:
        raise RuntimeError(f"could not read image: {image_path}")
    app = FaceAnalysis(name="buffalo_l")
    app.prepare(ctx_id=-1)                      # CPU
    faces = app.get(img)
    if not faces:
        return None, 0.0
    fh, fw = img.shape[:2]
    frame_area = float(fh * fw) or 1.0
    largest = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    x0, y0, x1, y1 = largest.bbox
    area_frac = max(0.0, (x1 - x0) * (y1 - y0)) / frame_area
    return [float(v) for v in largest.embedding], float(area_frac)
