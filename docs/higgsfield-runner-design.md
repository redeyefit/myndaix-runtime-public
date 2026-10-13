# DESIGN: Higgsfield video-generation runner (`api`/`higgsfield` adapter)

**Status:** Oracle design review PASSED (§7b). All decisions locked (§7). **READY to build via `/feature`** — run it from THIS repo: `cd ~/code/active/myndaix-runtime && claude` → `/feature`. (Do NOT dispatch this to a headless orchestrator; run /feature interactively here under Jefe's gates.)
**Author:** Mack (research session, 2026-06-23). Grounded in live-verified API + runtime code.
**Target repo:** myndaix-runtime · **Lives in:** `src/runtime/runner.py`, `src/runtime/registry.py`

---

## 1. What it does and why

Add **Higgsfield** as a roster agent so the runtime can generate video (and stills) as a
durable job: `mxr higgsfield "<motion prompt>"` (+ an input image) → leased → submitted to
Higgsfield → polled → the **mp4 URL** comes back as the job's reply/artifact.

**Why a new runner and not the existing `invoke_api`:** `runner.py:79 invoke_api` is hardwired
to OpenAI-style chat — `Authorization: Bearer`, body `{model, messages:[…]}`, synchronous
`choices[0].message.content`. Higgsfield is none of those (LIVE-VERIFIED against the account):
- Auth: `Authorization: Key <KEY_ID:KEY_SECRET>` (one colon-joined string in `$HF_KEY`).
- Submit: `POST https://platform.higgsfield.ai/<application>` body `{prompt, image_url}`.
- **Async queue:** 200 → `{request_id, status_url}`; poll `GET /requests/{id}/status` →
  `queued → in_progress → completed|failed|nsfw`; completed adds `video:{url}` (or `images:[{url}]`).
- Confirmed image-to-video path: `/higgsfield-ai/dop/lite` (~$0.13/5s). Real 18.9 MB mp4 produced.

So: **one new runner function + one roster row + a 2-line dispatch branch.** The roster row is
DATA (registry.py principle: "adding an agent is a new AgentSpec row, never a spine edit"); the
new async-media *protocol* is a one-time runner addition — exactly as `invoke_api` was added for
Perplexity.

## 2. Data flow

```
mxr higgsfield "<prompt>" --image <url>
   └─ cli.submit → ledger.submit_job(to_agent="higgsfield", prompt, context={image_url, application?})
        └─ worker leases job → runner.invoke("higgsfield", job)
             └─ reach=API, adapter.kind="higgsfield" → invoke_higgsfield(spec, job)
                  1. key = os.environ["HF_KEY"]            # secret from env, never the roster
                  2. app = job.context.get("application") or adapter["application"]
                  3. POST base+app  {prompt: job.prompt, image_url: job.context["image_url"], ...}
                  4. poll status_url every 2s until terminal OR job.timeout_s
                  5. completed → Result(OK, artifact_ref=<video.url|images[0].url>, cost=…)
        └─ transactional outbox → reply delivered to cli:operator
```

Inputs ride in `Job.context` (`contracts.py:115`, free-form dict) → **no contract change**.
Output mp4 URL → `Result.artifact_ref` (`contracts.py:130`, already meant for artifacts).

## 3. Integration points (exact)

| Change | File | Kind |
|---|---|---|
| `invoke_higgsfield(spec, job)` — submit+poll, error mapping | `src/runtime/runner.py` (new fn ~after `invoke_api`) | code |
| Dispatch API agents on `adapter["kind"]` (`"higgsfield"`→new, else→`invoke_api`) | `src/runtime/runner.py:123 invoke()` | code (2 lines) |
| `AgentSpec(agent_id="higgsfield", reach=API, authority=RESPONDER, …)` | `src/runtime/registry.py:40 V1_ROSTER` | DATA |
| `--image`/`--application` flags → populate `context` | `src/runtime/cli.py:25 submit` (see §6 open Q) | code |
| Unit tests (mocked httpx transport, like `invoke_api`'s) | `tests/` | test |

Proposed roster row:
```python
AgentSpec(agent_id="higgsfield", reach=Reach.API, authority=Authority.RESPONDER,
          model="dop-lite", role="image/text→video generation",
          profile=Profile(timeout_s=600, cost_budget=2.0),
          adapter={"kind": "higgsfield",
                   "base": "https://platform.higgsfield.ai",
                   "secret_ref": "HF_KEY",
                   "application": "/higgsfield-ai/dop/lite"})
```

## 4. Edge cases & failure modes (error-class mapping)

| Condition | ResultStatus / ErrorClass | Notes |
|---|---|---|
| `$HF_KEY` missing | ERROR / TERMINAL | mirror `invoke_api:96` |
| 401/403 (bad key) | ERROR / TERMINAL | no retry |
| 404 "Model not found" | ERROR / TERMINAL | bad `application` |
| 422/400 (bad body) | ERROR / TERMINAL | validation |
| 5xx / network on **submit** | ERROR / **RETRYABLE** | not yet charged → safe to retry |
| terminal status `failed`/`nsfw` | ERROR / TERMINAL | Higgsfield **refunds** these |
| exceeds `job.timeout_s` while polling | TIMEOUT / **see §5** | the double-charge trap |
| completed but no url field | ERROR / TERMINAL | unexpected shape |

## 5. ⚠️ The one load-bearing decision: idempotency / no double-charge

RESPONDER results that are RETRYABLE get **auto-retried** by the worker. If `invoke_higgsfield`
has *already submitted* (job enqueued = **charged**) and then a poll blip returns RETRYABLE, the
retry **re-submits and charges again**. Options:

- **(A) v1 simple — fail-closed after submit:** once we hold a `request_id`, any later failure
  (poll error, timeout) returns **TERMINAL** (no retry; never double-charges). Cost: a transient
  network blip during a long render wastes that one generation. *Recommended for v1.*
- **(B) idempotent resume:** persist `request_id` into the ledger/context on submit; a retry that
  finds an existing `request_id` **resumes polling** instead of re-submitting. Correct but needs a
  ledger touch — defer to v2.

Pre-submit failures stay RETRYABLE in both. **Decision needed from Jefe: A or B for v1.**

## 6. Security surface

- **Secret:** `HF_KEY` read from `os.environ[secret_ref]` only — never the roster/adapter
  (matches `invoke_api` + your security.md). Lives in `~/.myndaix/.secrets` (chmod 600),
  loaded via `~/.myndaix/load-secrets.sh`. Never logged (mask in any error text).
- **Untrusted input:** `prompt`/`image_url` come from the dispatcher. `image_url` is sent to a
  third party — validate it's an `http(s)` URL; don't allow `file://`/SSRF-y schemes.
- **Cost as an attack/footgun surface:** `cost_budget` in Profile caps spend; premium model paths
  cost 3–5× DoP — keep the roster pinned to `dop/lite` until a path is explicitly promoted.
- **No FS side effects** (RESPONDER, no worktree) → smaller blast radius than workspace-actors.

## 7. Decisions (LOCKED by Jefe 2026-06-23)
1. **§5 idempotency → A (fail-closed after submit).** Once a `request_id` exists, any later
   failure is TERMINAL — never retried, cannot double-charge. Idempotent-resume (B) is deferred to v2.
2. **CLI input → add `mxr … --image <url>` (and `--application`).** Flags populate `Job.context`;
   `cli.py:submit` must thread `context` through `ledger.submit_job`.
3. **Models → DoP only for v1** (`/higgsfield-ai/dop/lite`). Premium (Kling 2.1 etc.) is a later
   one-row roster addition behind the same runner; not in v1 scope.

## 7b. Gate 1 — Oracle design review (PASSED) + resolutions
Oracle reviewed this design: **"sound for v1, proceed after P0/P1s."** Resolutions (decided by Jefe, with live-API facts from Mack) — **fold these into the build:**
- **P0 — brittle polling:** retry transient errors (5xx/429/network) *during polling* up to **3× with 2s backoff**, then TERMINAL. Internal poll-retries are NOT a re-submit → consistent with fail-closed (§5).
- **P1 — cancellation:** the cancel endpoint **exists** (every submit returns `cancel_url` = `POST /requests/{id}/cancel`). Wire **best-effort cancel-on-timeout**. CAVEAT (live-verified): it only cancels while **queued**, not mid-render — document the limitation in code.
- **P1 — artifact URL TTL:** **verified a non-issue** — Higgsfield CDN URLs are plain public objects (200 after hours, no `X-Amz-`/`Signature`/`Expires` params). Pass the URL straight to `artifact_ref`; no download-and-store in v1.
- **P2 — SSRF:** reject private/link-local `image_url` ranges (169.254.0.0/16, 10/8, 172.16/12, 192.168/16, 127/8).
- **P2 — unknown terminal states:** unrecognized terminal status → TERMINAL with the status logged.

## 8. Test plan (test.sh / pytest)
- **Unit (no network):** mock httpx transport (as `invoke_api` tests do) — submit→poll→completed→`artifact_ref`; 401→TERMINAL; 5xx-on-submit→RETRYABLE; failed-status→TERMINAL; post-submit failure→TERMINAL (per §5A); timeout honored.
- **Live smoke (1 credit, gated):** `mxr higgsfield "gentle push-in" --image <url>` against the real key → expect a `cloud-cdn.higgsfield.ai/*.mp4`. Reuse `~/research/higgsfield_smoke_test.py` logic.
- **Security:** `image_url=file:///etc/passwd` → rejected; missing `HF_KEY` → clean TERMINAL, not a crash.

## 9. Reference
- Live-verified API facts + endpoint catalog: `~/research/2026-06-23-higgsfield-cloud-api-image-to-video.md`
- Working reference impl: `~/research/higgsfield_smoke_test.py` (venv `~/research/.venv-higgsfield/`)
