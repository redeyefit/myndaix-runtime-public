# MX Quality Orchestrator — Handoff Design (myndaix-runtime)

**For the builder.** This doc = the **full design** (below) + **post-review BUILD COROLLARIES** (immediately
following) that **OVERRIDE the body where they conflict** + **verified externals** (Recon). Produced
2026-06-28 by a research→design→adversarial-review workflow. Build **v1** per the corollaries + the §9
checklist. Boundary: this lives in **myndaix-runtime**; mx-engine *consumes* the output (never the reverse).

---

## ⚠️ BUILD COROLLARIES — read first; these OVERRIDE the design body

The adversarial review returned **REVISE**. Apply these before/while building:

1. **Orchestrator = a STANDALONE Python driver, NOT a registry agent.** A worker-invoked agent has no
   ledger handle and *cannot* submit child jobs — `contracts.py:32`'s "CONTROLLER may emit dispatches" is an
   UNIMPLEMENTED label (only WORKSPACE_ACTOR's worktree is special-cased anywhere). Build it like
   `cli.py`/`controller.py`: a module `runtime/orchestrator.py` that holds its own `PostgresLedger` and runs
   the submit→poll→read loop. Invoke as **`PYTHONPATH=src python3 -m runtime.orchestrator "<brief>"`**
   (mirrors `python3 -m runtime.controller tick`). **REJECT** the "new `adapter.kind==orchestrator` handler
   in runner.py" option — an invoke handler has no ledger handle. *(Resolves Open Q2.)*

2. **The live ledger writer is `runtime.ledger.postgres_store.PostgresLedger`, NOT `command_api.py`.**
   `command_api.py` is a `typing.Protocol` with `...` bodies (its header says the impl "is the next build
   phase"). Call `PostgresLedger.connect(DSN)` → `.submit_job(...)` (`postgres_store.py:185`) /
   `.get_status(job_id)` (`postgres_store.py:829`), exactly as `cli.py:22,48,55,66`. Mentally repoint every
   "CommandAPI / command_api.py" citation in the body to `PostgresLedger`.

3. **No $ ceiling is enforced by the ledger.** `submit_job` admission checks ONLY `max_depth`/`max_children`
   (`postgres_store.py:220`); `cost_budget` is decorative (a Profile field + comments, never gated — the
   `cost_budget=2.0` on the higgsfield row is inert). The ONLY real spend guards are **(a) the human cost
   gate you BUILD (§7)** and **(b) stitcher's `max_segments=12` fail-closed** (`runner_stitch.py:135`). Do
   not assume the ledger stops overspend.

4. **★ CRITICAL poll-timeout/charge trap.** The CLI sync-wait default is **180s** (`MXR_TIMEOUT_S`,
   `cli.py:36-42`) but the higgsfield supplier **Profile timeout is 600s**. If the driver clones the
   `cli.py` poll loop verbatim, it will **abandon a real DoP render at 180s while the ledger job keeps
   running AND CHARGING** — a false timeout on a job that spent credits. Set the stage-3 poll deadline
   **≥ the supplier Profile timeout (≥600s; stitcher 2400s)**. (Also: the supplier's effective timeout is the
   registry row's `Profile.timeout_s`, not `job.timeout_s` — to change it, edit the registry row.)

5. **v1 minimal shape: collapse stages 1/2/4 into in-process functions in the driver; only stage 3 is a
   ledger job.** prompt-director (one LLM call), model-router (a trivial dict), critic (a numpy/Pillow
   check) are side-effect-free prompt→data steps — they do NOT need to be separate registry agents + polled
   jobs for v1. **Stage 3 (the supplier — it spends money) is the only thing that MUST be a ledger job** (to
   `higgsfield`, for the no-double-charge guarantee). This is the anti-over-engineering default; promote the
   three to ledger rows later if you want per-stage observability.

6. **`igmedia.upload_public(path, kind)` returns a 2-TUPLE `(secure_url, public_id)`** (`igmedia.py:28-35`),
   not a bare URL — unpack it. Per the boundary, **mx-engine** renders the seed, calls `upload_public`, and
   hands the orchestrator the unpacked **`secure_url`** as `image_url` (it passes `_reject_unsafe_url` —
   public Cloudinary https). *(Resolves Open Q4.)*

7. **The mx-engine ingest seam is a genuine NEW ffmpeg code path, not just a kwarg.** `reelgen`'s
   `bg_concat` (`reelgen.py:153`) is a karaoke caption-bg concat-list, NOT raw plate frames. Feeding a
   supplier plate = a new bg-video input branch in `beat_clip`'s ffmpeg graph (`reelgen.py:168`). It's real
   BUILD (v2-adjacent); v1 uses the manual hand-paste seam (§8).

8. **Add a measured "no brand pixels / no text" back-check to the critic — or explicitly defer + justify.**
   `banned_tropes` is currently enforced only by *prompting* the supplier (§4); "measure, don't eyeball"
   wants a back-check. v1 critic should at least flag text-region presence on the plate (cheap edge/contour
   heuristic), OR state plainly that it's deferred and why a background-plate-only v1 makes that acceptable.

### Verified externals (Recon, sourced — fold into §5.4 / §6)
- **Text→video path = the Higgsfield MCP connector `https://mcp.higgsfield.ai/mcp`** — no API key (auth via
  Higgsfield account login), exposes **30+ models incl. Seedance 2.0 + Kling** (generate images/videos, train
  characters, history). This is the **v2 mechanism to reach the full fleet** — there is **no documented raw
  REST API**. Verify the exact tool schema by connecting + enumerating tools (vendor-dominated evidence).
  Rough cost: ~150 free credits/mo, ~30–60 credits/call (per-model UNVERIFIED — measure before cost logic).
- **Persona face gate = InsightFace + ArcFace (`buffalo_l`, 512-D embeddings).** Start thresholds: cosine
  **similarity ≥ 0.35–0.45 = "likely same," ≥ 0.50 stricter** (≈ cosine distance ≤ 0.55–0.65); tune for your
  FAR/FRR. *(More lenient than the body §6's 0.30/0.40 distance guess — prefer these + calibrate on a labeled set.)*

### Other verified caveats (from the review's double-check)
- **`motion_id` name-vs-UUID is unresolved** — `cli.py:186` calls it a UUID from `GET /v1/motions`; the report
  lists names ("Dolly In"). The runner forwards it verbatim (`runner.py:330`). **Pull the live `/v1/motions`
  list and pin the router's enum before shipping** — don't hard-code "Dolly In" until confirmed.
- **Single-clip stage-3 is double-charge-safe by construction** (post-submit errors are TERMINAL,
  `runner.py:338-361`), not by authority. Multi-shot → use `stitcher` (WORKSPACE_ACTOR, never auto-retried).
- **No-spend CI is real**: `httpx.MockTransport` via the `transport=` kwarg (`tests/test_stitch.py:36-55`);
  live one-shot via `tools/hf_oneshot.py`. Both check out.

---

## FULL DESIGN (body — read the corollaries above first; they override conflicts)

# MX Quality Orchestrator — Design Doc (handoff-ready)

**Status:** ready to build (v1) · **Lives in:** `~/code/active/myndaix-runtime` · **Date:** 2026-06-28
**Author intent:** a builder (another agent/session) implements v1 from this doc alone. Every interface claim below is cited to real code (`file:line`). When a thing must be **BUILT**, it says BUILD. When it already **EXISTS**, it says EXISTS.

---

## 1. Goal & non-goals

**Goal.** Turn a one-line brand brief into a high-end **generated motion asset** (a cinematic video hook / b-roll plate — and later persona stills) by replicating *Higgsfield's quality pattern* on our own stack: an LLM "enhancer" expands the brief into a **structured, rule-bound prompt with verbatim LOCKS** (palette hexes / subject / material / style / atmosphere) + `anti_defaults`/`banned_tropes`, a router picks the supplier per shot, a supplier generates **MOTION + background only**, and a **measured critic** gates the output before it is accepted. A human approves spend before any supplier is called.

The output is an **asset URL/path + metadata**. That is the whole job.

**Non-goals (explicit).** The orchestrator does **NOT**:
- do brand-lock compositing (MX vector overlay, gold wordmark, karaoke captions, end-card) — that is mx-engine `reelgen.py` (cited §8).
- render the deterministic seed frame — that is mx-engine (Chrome path / `mx.js` vector). The supplier `image_url` is rendered *upstream* by mx-engine (`HIGGSFIELD_PIPELINE_REPORT.md:231`).
- publish — that is mx-engine `mxq` → `igpublish.py`.
- let any supplier generate brand text, logos, or fake software UI. For product demos use real screen-capture; the supplier only does motion/background (`HIGGSFIELD_PIPELINE_REPORT.md:163, 358`).

**v1 boundary on capability:** image→video only (DoP/lite). Text→video is v2 (§5, §9).

---

## 2. Where it lives & why

**It lives in `myndaix-runtime`.** That repo IS the generation-orchestration layer: a registry of agents (`registry.py:42-121`), an immutable Job/Result contract (`contracts.py:108-133`), a durable Postgres ledger written only through the Command API (`command_api.py:16-90`), a runner that dispatches to suppliers (`runner.py:434-448`), and a CLI to submit jobs (`cli.py:155-200`). The supplier call (`invoke_higgsfield`, `runner.py:240-296`) and the multi-shot idiom (`invoke_stitch`, `runner_stitch.py:112-249`) already exist here.

**The boundary (decided, immutable):**

| Concern | Owner | Why |
|---|---|---|
| brief → structured prompt, routing, supplier call, measured QC gate | **myndaix-runtime** (this orchestrator) | it has the agent pool + supplier runner + ledger |
| deterministic seed frame, brand-lock composite, captions, publish | **mx-engine** | it's the deterministic factory (`reelgen.py`, `mxq.py`, `igmedia.py`) |

**How mx-engine consumes the output.** The orchestrator hands back a JSON manifest `{plate_url, shot_id, duration, applied_locks, cost}`. mx-engine reads it, fetches `plate_url`, and feeds it into `reelgen.py` as the moving background of a `media` beat (ingest point cited §8). mx-engine never generates; the orchestrator never composites.

---

## 3. Architecture — the 4 v1 stages on the runtime's real primitives

### 3.1 The flow

```
operator: mxr orchestrator "<brief>" --repo mx-engine  (--brand myndaix)
   │
   ▼
[orchestrator]  authority=CONTROLLER  (BUILD: new AgentSpec row + invoke handler)
   │
   ├─(1)──> submit_job(to_agent="prompt-director", prompt=brief, context={brand:"myndaix"})
   │           └─ RESPONDER, returns structured shotlist JSON (LOCKS filled from brand config)
   │
   ├─(2)──> submit_job(to_agent="model-router", context={shotlist:<from-1>})
   │           └─ RESPONDER, returns routed shotlist (motion_id + application + cost est per shot)
   │
   ├─ === HUMAN COST GATE ===  (orchestrator pauses; shows total cost est; waits for approval)
   │
   ├─(3)──> submit_job(to_agent="higgsfield" | "stitcher",
   │                   context={image_url:<seed-from-mx-engine>, shotlist:<from-2>, ...})
   │           └─ RESPONDER (higgsfield) / WORKSPACE_ACTOR (stitcher); calls the supplier; charges credits
   │
   └─(4)──> submit_job(to_agent="critic", context={artifact_ref:<from-3>, render_type:"generic"|"persona"})
               └─ RESPONDER, returns pass/fail (+ one-variable retry hint); aborts on FAIL
   │
   ▼
return manifest {plate_url, duration, applied_locks, cost} to caller → mx-engine ingests
```

### 3.2 Mapping to real primitives (cite each)

- **An agent = an `AgentSpec` row** (`registry.py:17-28`): `agent_id, reach, authority, model, role, profile, adapter`. Adding an agent is a new row, never a spine edit (`registry.py:1-7`). The four stage agents are four new rows (BUILD) plus the existing `higgsfield`/`stitcher` rows for stage 3 (EXISTS, `registry.py:99-120`).

- **A stage = one Job** (`contracts.py:108-121`): `id, to_agent, prompt, context (free-form dict), repo_id, base_ref, timeout_s, attempt_no`. **`context` is THE place for stage I/O** — `invoke_higgsfield` already reads `image_url/motion_id/motion_strength/end_image_url/application` from `job.context` (`runner.py:263-292`); the CLI builds it from flags in `_build_context` (`cli.py:94-118`).

- **A stage result = a `Result`** (`contracts.py:125-132`): `status (OK/ERROR/TIMEOUT/KILLED/NEEDS_HUMAN), text, error_class (RETRYABLE/TERMINAL/NEEDS_HUMAN), artifact_ref, cost, ms`.

- **Authority drives retry-safety** (`contracts.py:27-33`):
  - Stages 1, 2, 4 = **RESPONDER** (prompt→text, no side-effects, auto-retry safe).
  - Stage 3 = **RESPONDER** (`higgsfield`, `registry.py:99`) for a single clip, or **WORKSPACE_ACTOR** (`stitcher`, `registry.py:110`) for multi-shot. WORKSPACE_ACTOR is **never auto-retried** by the worker (`worker.py:93` only gives it a worktree; `command_api.py:52-54` "workspace-actors never auto-retry"). This is the financial safety: a charged supplier call is never silently re-charged (`runner.py:246-252`).
  - The orchestrator itself = **CONTROLLER** ("may emit new dispatches via Command API only", `contracts.py:32`). BUILD.

- **Job submission = `CommandAPI.submit_job(...)`** (`command_api.py:22-36`): `to_agent, prompt, context, parent_id, inbound_event_id, created_by, repo_id, base_ref, priority` → returns the job UUID. It runs admission checks (max_depth / max_children / cost_budget) then queues. `cli.py:55-57` wraps it.

- **Polling between stages = `CommandAPI.get_status(job_id)`** (`command_api.py:89`). The CLI already does the exact loop the orchestrator needs: submit, then poll `get_status` until `status in ("done","failed","dead")`, then read `outbound[].body` (the reply) and `artifact_ref` (`cli.py:63-89`; `mxr get` returns `artifact_ref` as JSON, `cli.py:137-149`). **Reuse this pattern.**

### 3.3 How the staged + human-gated flow is represented durably

**Decision for v1 (anti-over-engineering): the orchestrator is a single CONTROLLER agent that runs the 4 stages as an in-process async loop, submitting each stage as its own ledger Job and polling for completion before the next** (the same submit→poll→read loop as `cli.py:63-89`). Each stage Job is durable in Postgres (status, attempts, artifact_ref, cost, outbound). The orchestrator carries `pipeline_state` in its own `job.context` (`current_stage`, `total_stages`, `stage_results[]`, `total_cost`) so a status read shows where the run is.

**The ledger has no native pause/approval state** — `job.status` is only `queued|leased|running|done|failed|dead` (`contracts.py:44-51`; confirmed in `schema.sql`, no `pending_approval`). For v1 the human gate is **out-of-band and synchronous**: the orchestrator computes stage-2's cost estimate, surfaces it to the operator (CLI prompt / printed line), and **does not submit stage 3 until approved**. No schema change. (A durable `approval_gate` table for async dashboards is v2, §9.)

Parent/child linkage (`submit_job(parent_id=...)`, `command_api.py:25`; `schema.sql` `job.parent_id`, `root_id`, depth≤8) is **available** and used so all stage jobs share a `root_id` (cost scope). But v1 does **not** rely on child-completion fan-in (the ledger has no "wait on all children" query) — the orchestrator serializes stages itself via polling. Keep it simple.

---

## 4. The prompt-director (stage 1)

**What it does.** Expands the one-line brief into a structured, rule-bound prompt where every brand-locked slot is filled **deterministically from mx-engine `brands/<slug>.json`**, and the LLM writes **only the dynamic Caption / scene description** — never brand color, identity, camera, or negatives (`HIGGSFIELD_PIPELINE_REPORT.md:138`).

**Authority:** RESPONDER. **Reach:** the cheapest viable — v1 can be a CLI agent (`claude -p`, like `lobster`/`mack`, `registry.py:46-53`) or an API agent. BUILD as a thin agent that loads the brand config, runs the LLM for the Caption only, then assembles the labeled block by template.

**Prompt-template shape** (the Soul Cinema labeled-block schema, `HIGGSFIELD_PIPELINE_REPORT.md:127-138`; worked example at `:255-267`):

```
Caption:        <LLM-written, 1-3 sentences — the ONLY free-text slot>
STYLE:          <from brand.style_block>            # LOCK
COMPOSITION:    <from brief intent / shot role>
SCENE:          <LLM + brief; subject, setting>
CINEMATOGRAPHY_AND_LIGHTING: <from brand.lighting>  # LOCK
CAMERA_AND_LENS: <from brand.camera_lens>           # LOCK (e.g. "Arri Alexa Mini LF, 35mm anamorphic")
PHYSICAL_ATTRIBUTES: <materials, physics>           # LOCK-ish
HEX_VALUES:     <brand.palette hexes>               # LOCK (verbatim across all shots)
NEGATIVES:      <brand.banned_tropes + standing block>  # LOCK
```

**The LOCKS** (held verbatim across every shot — the consistency mechanism, `HIGGSFIELD_PIPELINE_REPORT.md:338, 356`): `HEX_VALUES`, `SUBJECT`, `MATERIAL`, `STYLE`, `ATMOSPHERE`.

**`anti_defaults` / `banned_tropes`** — the standing negative block, baked into every prompt (`HIGGSFIELD_PIPELINE_REPORT.md:163`): `no on-screen text, no watermark, no logo, no warped faces, no extra fingers, no text artifacts, no people` (we render brand text ourselves; the supplier must NOT generate text). Brand-specific bans (e.g. "no AI-hype clichés") come from the brand config.

**⚠ BRAND SCHEMA GAP (must address).** The live `brands/myndaix.json` has `palette` (hexes), `persona`, `hashtags.blocklist` — but **no** `style_block`, `lighting`, `camera_lens`, `banned_tropes`, or `anti_defaults` keys. Verified: the only color source today is `palette.{bg,bg_card,accent,mark,ink,...}` (`brands/myndaix.json`). So the builder must **add** these keys to the brand schema (in mx-engine), or hard-code MyndAIX defaults in the prompt-director for v1 (`HIGGSFIELD_PIPELINE_REPORT.md:262-265`: `Arri Alexa Mini LF, 35mm anamorphic`; hexes `#0A0A0A,#1A1D22,#5AE0A0`). **Recommendation:** add `cinema: {style, lighting, camera_lens, film_stock, banned_tropes:[...]}` to `brands/<slug>.json`; prompt-director reads it deterministically and fails-closed if missing (don't let the LLM invent brand color).

**Output** (parsed by model-router): `{shots: [{caption, look_block, camera_role:"hero"|"filler", subject}], total_shots, locks:{...}}`. For v1 a single-shot output is fine.

---

## 5. Supplier integration (stage 3)

### 5.1 The exact supplier call (EXISTS)

Single clip — `invoke_higgsfield(spec, job)` (`runner.py:240-296`). It:
- requires `spec.adapter.base` and an `application` (from `job.context["application"]` or `adapter["application"]`, mandatory, `runner.py:259-266`);
- reads the key from `os.environ[adapter["secret_ref"]]` = `HF_KEY`, fail-closed if missing (`runner.py:267-273`);
- **requires `job.context["image_url"]`** — missing → `TERMINAL` error before any request (`runner.py:275-278`);
- uses `spec.profile.timeout_s` as the deadline, **NOT `job.timeout_s`** (the spine doesn't apply Profile.timeout_s; `job.timeout_s` is a dead 300s default — `runner.py:281-283`);
- POSTs `{prompt, image_url, motion_id?, motion_strength?, end_image_url?}` to `base + application`, gets `{request_id, status_url, cancel_url}`, polls `status_url` until `completed` → `{video:{url}, cost}` (`runner.py:327-427`);
- returns `Result(status=OK, text=url, artifact_ref=url, cost=cost)` (`runner.py:417`).

`job.context` for `higgsfield`: `{image_url (MANDATORY str), application?, motion_id?, motion_strength?, end_image_url?}`.

Multi-shot — `invoke_stitch(spec, job)` (`runner_stitch.py:112-249`). Reads `job.context["shotlist"]` = ordered list of `{prompt, motion_id?, motion_strength?, image_url?, end_image_url?, application?}` (`runner_stitch.py:18-21`), loops `_hf_generate` per shot, chains each clip's last frame as the next seed via the two-step upload (`runner_stitch.py:202-217, 87-110`), concatenates with ffmpeg + optional deterministic end-card (`runner_stitch.py:233-236`), and returns a single `Result` with the **local mp4 path** as `artifact_ref` and `total_cost` (`runner_stitch.py:248`). Fail-closed cost guard: `len(shots) > max_segments` (12) aborts before any spend (`runner_stitch.py:135-137`). Partial success returns the good clips concatenated + reason (`runner_stitch.py:243-247`).

**Routing is automatic:** `runner.invoke()` sends `adapter.kind=="higgsfield"` → `invoke_higgsfield`, `=="stitch"` → `invoke_stitch` (`runner.py:441-448`). The orchestrator just submits a Job to `to_agent="higgsfield"` or `"stitcher"`.

### 5.2 The MANDATORY seed image — where it comes from

DoP/lite is **image→motion**; `image_url` is mandatory and enforced (`runner.py:275-278`; `HIGGSFIELD_PIPELINE_REPORT.md:65, 231`). The seed is rendered **by mx-engine** (Chrome path / `mx.js` vector / Soul-ID still), uploaded to a public URL (mx-engine already has `igmedia.upload_public(path, kind)` → Cloudinary `secure_url`, `igmedia.py:28-35`), and passed to the orchestrator as `context.image_url`. The supplier touches **motion + background only**. Every URL is SSRF-guarded before use (`image_url`, `end_image_url`, the fetched `artifact_ref`, `end_card_url`, presigned `upload_url`) via `_reject_unsafe_url` (`runner.py:319-325`; `runner_stitch.py:191, 264, 104`) — so the seed must resolve to a **public** host (Cloudinary is fine; localhost/private is rejected).

### 5.3 Camera motion = a NAMED preset, never free prose

`motion_id` is a closed enum of named DoP moves (`HIGGSFIELD_PIPELINE_REPORT.md:154-155`); the LLM may only **select**, never free-prompt camera. It lands in the request body as `motion_id` (+ optional `motion_strength`, a finite float or omitted) (`runner.py:330-334`). The CLI flag is `--motion-id UUID` (`cli.py:186-189`) — note the runtime treats it as the preset id Higgsfield's API accepts (`GET /v1/motions`). **The model-router selects this from the enum** (hero → a dramatic move, filler → `Static`/gentle `Dolly In`).

### 5.4 The text→video gap (v2, FLAGGED)

The live `V1_ROSTER` loads **only** `higgsfield` (dop/lite image→video) and `stitcher` (`registry.py:99-120`). Kling/Seedance text→video are **not** loaded — they appear only in wrapper help text, not the roster (`HIGGSFIELD_PIPELINE_REPORT.md:65`; verified `registry.py:42-121` contains no kling/seedance row). To add text→video you must **BUILD a new `AgentSpec` row + a new `adapter.kind` handler in `runner.py`** (e.g. `invoke_seedance` following the `_hf_generate` shape), or wire the `mcp.higgsfield.ai` connector (`HIGGSFIELD_PIPELINE_REPORT.md:66, 232`). **Both are v2.** v1 always has a deterministic seed, so DoP/lite is sufficient.

---

## 6. The measured critic (stage 4)

**Pattern.** Mirror `verify_render.py::check_no_clip`, which returns `(ok, message)` and is wired into `reelgen.py` to **abort** a clipped MX head (`verify_render.py:21-46`; `reelgen.py:293-297`). Adopt the same "measure, don't eyeball" discipline for supplier plates. RESPONDER authority. BUILD a `critic` agent.

**Two modes (driven by `context.render_type`):**

1. **Generic plate** (v1 default): assert the returned plate is present and non-trivial — fetch metadata, check `duration`/`resolution`/`aspect` match the routed expectation, and (cheap) sample a frame and confirm dominant colors sit near the brand `HEX_VALUES` within tolerance (palette-drift guard). On the **composited** output, the existing `check_no_clip` still runs in mx-engine (`reelgen.py:293`); the critic here gates the **plate** before compositing.

2. **Persona** (Agent Steve — v2 trigger): face-embedding-distance gate, `verify_persona.py` pattern (`HIGGSFIELD_PIPELINE_REPORT.md:194-205`): build a reference embedding (mean of N canonical Steve stills); per generated frame, detect the largest face; if no face or `face_area/frame_area < 0.04` → ABORT (too small to trust); compute cosine distance `d`; gate `d ≤ 0.30 PASS | 0.30 < d ≤ 0.40 WARN | d > 0.40 FAIL`. Thresholds are starting calibration — tune on a labeled set (`HIGGSFIELD_PIPELINE_REPORT.md:206`).

**Bounded retry — change exactly ONE variable** (`HIGGSFIELD_PIPELINE_REPORT.md:152, 244`). On FAIL, the critic returns a retry hint that adjusts only `motion_strength`; the orchestrator re-runs stage 3 with that single change, **max 2 retries**, then surfaces to a human (`Result.status = NEEDS_HUMAN`). **Cost caveat:** stage-3 supplier calls are charged on submit and never auto-retried (`runner.py:246-252`); a retry is a **new charged job** the orchestrator submits deliberately *after* the critic fails — so it counts against the human-approved budget. Keep retries ≤2.

---

## 7. Human approval / cost gate

Mirror Higgsfield's **per-step credit-approval gate** (`HIGGSFIELD_PIPELINE_REPORT.md:35, 108, 359`) and mx-engine's own `mxq` draft→approve→run discipline.

**v1 (minimal):** a **single synchronous gate before stage 3** (before any spend). After model-router (stage 2), the orchestrator computes a cost estimate (heuristic: hero ≈ 40 cr, filler ≈ 6 cr — numbers UNVERIFIED, `HIGGSFIELD_PIPELINE_REPORT.md:67, 92`; watch real Higgsfield costs before wiring cost logic), prints `plan + estimated cost`, and **blocks until the operator approves**. Stages 1, 2, 4 are free (LLM/QC), so gating once before generation matches Higgsfield's "credits only burn at generation" (`HIGGSFIELD_PIPELINE_REPORT.md:21`).

**Never auto-spend.** The orchestrator must never submit a stage-3 job without an explicit approval signal. Because the supplier charges on submit and is fail-closed on every later failure (`runner.py:246-252`, `352-361`), approval **must** precede invoke.

**v2:** durable per-stage `approval_gate` table + async dashboard/Slack approve+resume (so the operator isn't blocking a terminal). Flagged §9.

---

## 8. Hand-back contract to mx-engine

**The orchestrator returns a manifest** (as the job's `outbound` reply body / a JSON file), shape:

```json
{
  "plate_url": "https://res.cloudinary.com/.../plate.mp4",   // or Higgsfield CDN url (public, https, SSRF-clean)
  "shot_id": "playa-intro-01",
  "duration": 3.0,
  "render_type": "generic",
  "applied_locks": { "hexes": ["#0A0A0A","#1A1D22","#5AE0A0"],
                     "camera_preset": "Dolly In", "motion_strength": 0.4,
                     "brand": "myndaix" },
  "seed_still": "https://res.cloudinary.com/.../playa-seed.png",  // audit trail
  "cost": 0.39,
  "critic": { "status": "pass", "metric": null }
}
```

This mirrors mx-engine's existing manifest discipline (`mxq.py:asset_manifest` hashes rendered files; the orchestrator hands a parallel manifest, `mxq.py:177-190`).

**The ingest point in mx-engine** (where the asset is consumed, `reelgen.py:302-327`): the beat loop already branches on `beat.get("media")`. A `media` beat renders progressive B-roll via `render_scene()` today (`reelgen.py:312-321`). **BUILD (in mx-engine, v2-adjacent):** extend the `media` struct to carry a `supplier_plate` and add a code path that, instead of calling `render_scene()`, **fetches `supplier_plate.url`**, converts it to a frame sequence, and feeds it to the existing `beat_clip(..., bg_concat=<plate frames>)` (the same `bg_concat` arg the karaoke path uses, `reelgen.py:326`). The deterministic MX overlay, gold wordmark, karaoke pop, and ducked lo-fi bed (`MUSIC_DB=-19dB`, `VOICE_DB=-4dB`) are layered on top by `reelgen.py` exactly as today — the supplier never rendered a brand pixel.

New beat shape (mx-engine side):
```json
{ "text": "...", "media": { "scene": "playa", "supplier_plate": {
    "url": "...", "shot_id": "...", "duration": 3.0,
    "applied_locks": {...} } } }
```

**v1 manual seam (anti-over-engineering):** for the very first end-to-end proof, the operator runs the orchestrator, gets the manifest, and **hand-pastes** `supplier_plate` into a `reel.json` beat. Automating the seam (mx-engine reads the manifest directly) is a fast-follow. This keeps v1 to "prove the seed→motion→critic loop."

---

## 9. v1 build checklist (ordered, small, shippable) vs v2 (deferred)

### v1 — prove the pattern on ONE beat, end to end, with a measured gate

Ordered so each step is testable before the next:

1. **Brand schema:** add `cinema:{style, lighting, camera_lens, film_stock, banned_tropes:[...]}` to `brands/myndaix.json` (mx-engine). EXISTS: `palette`. BUILD: the `cinema` block. *(Or hard-code MyndAIX defaults in step 2 for the very first run.)*
2. **prompt-director agent** (BUILD): new `AgentSpec` (RESPONDER, CLI `claude -p`). Reads `brands/<slug>.json`, fills the labeled-block template (§4), LLM writes only `Caption`, fails-closed if a LOCK slot is missing. Returns shotlist JSON. Test with `brands/myndaix.json`.
3. **model-router agent** (BUILD): new `AgentSpec` (RESPONDER). v1 rule = trivial: one shot → route to `higgsfield` (dop/lite) with seed; pick `motion_id` from the enum (hero vs filler stub), estimate cost. Returns routed shotlist.
4. **critic agent** (BUILD): new `AgentSpec` (RESPONDER). v1 = generic-plate check (present + non-trivial + duration/aspect + palette-near-hexes). Returns `{status, metric, retry_hint?}`. Port the `check_no_clip` return-shape discipline (`verify_render.py:21-46`).
5. **orchestrator agent + handler** (BUILD): new `AgentSpec` (authority=CONTROLLER, `registry.py`), plus an `invoke_orchestrator` in `runner.py` (route a new `adapter.kind=="orchestrator"`, `runner.py:441-448`) **or** a Python driver invoked via `invoke_cli`. The driver runs the submit→poll→read loop (clone `cli.py:63-89`) for stages 1→2→[gate]→3→4, threading each stage's result into the next stage's `context`. Carries `pipeline_state` in its own context.
6. **human cost gate** (BUILD): after stage 2, print plan + estimate, block until approved, only then submit stage 3 (§7).
7. **seed-still seam** (EXISTS in mx-engine): render the beat frame via Chrome → `igmedia.upload_public(path,"image")` → public `image_url`. Pass into stage 3 context.
8. **stage 3 = existing supplier** (EXISTS): submit to `higgsfield` with `{image_url, motion_id, motion_strength}` (`runner.py:240-296`). No new supplier code.
9. **hand-back manifest** (BUILD): orchestrator emits the §8 JSON; operator hand-pastes `supplier_plate` into a `reel.json` beat (manual seam) for the first proof.
10. **end-to-end test** (BUILD): `mxr orchestrator "<brief>"` → manifest with a playable `plate_url`. Mock Higgsfield via the test transport (`tests/test_runner.py`/`test_stitch.py` mock-transport pattern) for no-spend CI; one live run with `HF_KEY` via the `hf` one-shot harness (`tools/hf_oneshot.py`) to verify real behavior. Assert: brand hexes present in prompt, no supplier text, cost accumulated, partial-failure path returns good clips.

### v2 — deferred (do NOT build in v1)

- **Text→video:** new `AgentSpec` row + `invoke_seedance`/`invoke_kling` handler, or `mcp.higgsfield.ai` connector (§5.4; `registry.py:99-120` confirms not loaded).
- **Multi-stage moodboard→storyboard→video** (the full Higgsfield staged pipeline, `HIGGSFIELD_PIPELINE_REPORT.md:354`) — v1 is single-shot seed→motion→critic.
- **Persona / Soul-ID** (Agent Steve) + `verify_persona.py` face-distance gate green across a representative set before shipping (`HIGGSFIELD_PIPELINE_REPORT.md:208`). Vector MX stays the zero-drift default.
- **Durable async approval gate** (`approval_gate` table + dashboard resume) — v1 is a synchronous CLI gate (§7).
- **Idempotent resume / recipe memory** — stitcher has no cross-run resume by design (`runner_stitch.py:13-16`); persisted clips + episodic param cache table are v2.
- **Real cost model** — current credit numbers are UNVERIFIED (`HIGGSFIELD_PIPELINE_REPORT.md:92`); measure live first.
- **Auto-ingest seam** (mx-engine reads the manifest directly instead of hand-paste).

---

## 10. Open questions for the builder

1. **Brand `cinema` block — schema or hard-code?** Add `cinema:{style,lighting,camera_lens,film_stock,banned_tropes}` to `brands/<slug>.json` now (cleaner, mx-engine owns it), or hard-code MyndAIX defaults in prompt-director for the first run? (Recommend: add the block; fail-closed if missing.)
2. **Orchestrator dispatch shape:** new `adapter.kind=="orchestrator"` handler in `runner.py`, or a Python driver wrapped by `invoke_cli`? CONTROLLER authority must be wired through `runner.invoke` either way (`runner.py:441-448`).
3. **`motion_id` source of truth:** the CLI calls it a UUID from `GET /v1/motions` (`cli.py:186-187`), but the report enumerates human-readable preset names ("Dolly In", `HIGGSFIELD_PIPELINE_REPORT.md:155`). Does the dop/lite API accept names or UUIDs? Pull the live `/v1/motions` list and pin the enum the router selects from.
4. **Seed-still URL host:** mx-engine `igmedia.upload_public` returns a Cloudinary `secure_url` (`igmedia.py:28-35`) — confirm it passes `_reject_unsafe_url` (it should; public CDN). Who triggers the upload — mx-engine before calling the orchestrator, or the orchestrator? (Recommend: mx-engine renders+uploads, hands the orchestrator the URL.)
5. **Cost-estimate heuristic:** hero≈40 / filler≈6 credits are unverified (`HIGGSFIELD_PIPELINE_REPORT.md:67,92`). Acceptable as a v1 *display-only* estimate at the gate, or measure one live DoP clip first?
6. **Critic palette tolerance:** what cosine/RGB tolerance counts as "plate matches brand hexes" before it's a FAIL vs WARN? Needs a calibration pass on a few sample plates.
7. **Stitcher vs single higgsfield for v1:** stage 3 single-shot uses `higgsfield` (RESPONDER, `profile.timeout_s=600`, `registry.py:101`). If v1 ever does 2+ shots, switch to `stitcher` (WORKSPACE_ACTOR, 2400s, needs `repo_id` for a worktree, `worker.py:93`). Which is the v1 target — strictly single-shot?
8. **Where does the orchestrator's final manifest live** — only the ledger `outbound` reply (`cli.py:75`), or also a written JSON file mx-engine watches? (v1: reply body; v2: file for auto-ingest.)

---

## 11. v1 BUILD NOTES (as-built 2026-06-28) — open-Q resolutions + cross-family review folds

**Built:** `src/runtime/orchestrator.py` (standalone driver) + `src/runtime/critic.py` (pure, dependency-free) + tests `test_orchestrator.py` / `test_critic.py` / `test_orchestrator_supplier.py`. Run: `PYTHONPATH=src python3 -m runtime.orchestrator "<brief>" --image-url <https-seed>`.

**Open questions resolved:**
1. **Brand `cinema` block** → HARD-CODED myndaix defaults in `orchestrator.BRAND_DEFAULTS` (fail-closed on unknown brand / missing LOCK slot). mx-engine owning `brands/<slug>.json` is v2.
2. **Dispatch shape** → standalone Python driver (corollary 1), NOT a runner `adapter.kind` handler.
3. **`motion_id`** → RESOLVED against the LIVE `/v1/motions`: the API field takes the **UUID** (the report's "Dolly In" etc. are display names). Router pins name→UUID (`MOTION_CATALOG`) and forwards the UUID.
4. **Seed-still host** → mx-engine renders+uploads and hands the orchestrator the public `secure_url` as `image_url` (corollary 6). The orchestrator never uploads.
5. **Cost estimate** → display-only at the gate (`COST_EST` hero 40 / filler 6, UNVERIFIED); the manifest `cost` is the estimate (`get_status` does not expose `Result.cost`). Real cost model is v2.
6. **Critic tolerance** → `critic.DEFAULTS` (PALETTE_TOL 70 / MIN_PALETTE_FRAC 0.10 / MAX_TEXT_FRAC 0.14); palette-drift + text are advisory **WARN**, only missing/trivial is a hard FAIL. Calibrate on real plates in v2.
7. **Stitcher vs higgsfield** → v1 is strictly **single-shot `higgsfield`**.
8. **Manifest location** → v1 returns the JSON to stdout (operator hand-pastes into a `reel.json` beat — the manual seam, §8).

**Cross-family code review folded (kilabz/codex + oracle/Gemini-3.1-Pro + a 7-dimension verify workflow):**
- **[CRITICAL, money]** `higgsfield` (RESPONDER) would be **auto-requeued by `reclaim_expired` on a worker crash** after the charged submit → double charge. Fixed: `non_idempotent: true` adapter flag on the paid suppliers + `_requeue_safe` returns False for them (crashed paid job → dead+surfaced, never resubmitted). The "double-charge-safe by construction" claim only covered clean TERMINAL *returns*; this closes the worker-CRASH window.
- **[CRITICAL, money]** the cost gate disclosed a 1-attempt estimate but the loop could spend `1+max_retries`, and `max_retries` was uncapped. Fixed: HARD cap `max_retries ≤ 2`; the gate plan discloses `max_attempts` + worst-case `max_estimated_cost`.
- **[MAJOR, liveness]** `_grab_frame` ffmpeg had no timeout → hang after a paid render on a dead-but-open URL. Fixed: `asyncio.wait_for` + `proc.kill()` + ffmpeg `-rw_timeout`.
- **[MAJOR, security]** `_grab_frame` had no SSRF guard + allowed `file://`. Fixed: **https-only** + `runner._reject_unsafe_url` on the plate_ref (the runner does not scheme-check the result url).
- **[MAJOR, injection]** the free-text brief was interpolated into the labeled block → newline/label injection could override LOCKS/NEGATIVES. Fixed: `_sanitize_brief` collapses control chars/newlines + caps length; LOCKS/NEGATIVES stay on non-user lines.
- **[MAJOR, correctness]** the retry loop had no exception handling → a supplier/frame error escaped + crashed the CLI. Fixed: a supplier/frame exception → `needs_human` with the recorded attempt, and **no re-run** (a re-submit could double-charge — only a critic FAIL retries).
- **[MAJOR, poll-deadline]** the poll deadline started at submit, but the supplier's 600s starts at lease → queue delay could false-time-out a charging render. Fixed: a `QUEUE_GRACE` window while queued, then a full `deadline_s` render window from the first leased/running observation.
- **[MINOR]** `get_spec("higgsfield")` None-guard; design doc restored into the repo.
- **Reviewers confirmed clean:** charge-gate-precedes-spend, critic buffer safety + math, `get_status` field reads. Refuted (not folded): "real ledger path untested" (covered by the loop + mock-transport tests), cost-readback (accepted v1 simplification), concurrent-runs (speculative).

**KNOWN RESIDUAL (round-3, accepted for v1 — bounded; flagged for a dedicated follow-up):**
`_supplier_ledger` cancels a timed-out job (`led.cancel`), but cancelling a non-idempotent paid job
that is racing a worker lease is a fundamental **TOCTOU on a network call**: a worker can lease + reach
the paid submit in the window between the DB ownership read and the HTTP POST, so a charge can begin
just after cancel. `get_attempt_job`'s read could be made a locking ownership gate (lock attempt→job
FOR UPDATE, canonical order) to NARROW the window, but it cannot fully close it (the submit happens
after any DB check) — it is the SAME best-effort limitation the codebase already accepts for
"already-leased may have charged." **Blast radius is bounded:** the `non_idempotent` flag sends a
charged-after-cancel job to `dead` (NOT requeued), so it is at most ONE extra surfaced charge
(recover via `mxr get <jid>`), never an unbounded re-charge. **Follow-up (own PR, cap_stress-verified,
deadlock-ordering-sensitive — do NOT rush into the orchestrator PR):** make `get_attempt_job` a
locking ownership gate + a `begin_attempt` CAS immediately before `_invoke`, to shrink the window to
[CAS commit → HTTP submit]. Benefits every paid agent, not just the orchestrator.
