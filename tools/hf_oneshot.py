"""hf_oneshot — one-shot Higgsfield video generation, no worker-pool/postgres needed.

Calls the LIVE-verified `runner.invoke_higgsfield` directly (same path the shipped
runner uses), polls the Higgsfield queue, and downloads the resulting mp4 locally.
This is the direct API path — it does NOT go through the durable runtime/ledger
(that's `mxr`, which needs `runtime.serve` + $MYNDAIX_DSN running).

Driven by the `hf` wrapper on PATH; not meant to be called bare (needs PYTHONPATH=src
+ HF_KEY in env). Usage via the wrapper:

    hf "<prompt>" --image <url>                     # default agent: higgsfield (dop/lite)
    hf "<prompt>" --image <url> --agent higgsfield-kling   # 1080p, quality winner
    hf "<prompt>" --image <url> --out ~/clips/x.mp4
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
import urllib.request

from runtime import runner
from runtime.contracts import Job, ResultStatus
from runtime.registry import get as get_spec


def _resolve_spec(agent: str):
    """Roster row by id, or an ad-hoc raw application path via `app:/path`."""
    if agent.startswith("app:"):
        from runtime.contracts import Authority, Reach
        from runtime.registry import AgentSpec
        return AgentSpec(
            agent_id="higgsfield-adhoc", reach=Reach.API,
            authority=Authority.RESPONDER, model="adhoc", role="adhoc",
            adapter={"kind": "higgsfield", "base": "https://platform.higgsfield.ai",
                     "secret_ref": "HF_KEY", "application": agent[len("app:"):]})
    return get_spec(agent)


async def run(args: argparse.Namespace) -> int:
    if not os.environ.get("HF_KEY"):
        print("FAIL: HF_KEY not in env (the `hf` wrapper sources it — run via `hf`, "
              "or `source ~/.myndaix/load-secrets.sh`)", file=sys.stderr)
        return 2

    spec = _resolve_spec(args.agent)
    if spec is None:
        print(f"FAIL: '{args.agent}' not in roster. Known media rows: higgsfield, "
              f"higgsfield-kling, higgsfield-minimax (or app:/raw/path).", file=sys.stderr)
        return 2

    context: dict = {}
    if args.image:
        context["image_url"] = args.image

    job = Job(id=uuid.uuid4(), to_agent=args.agent, prompt=args.prompt,
              context=context, timeout_s=args.timeout)

    print(f"[*] agent : {args.agent}   base={spec.adapter['base']} "
          f"app={spec.adapter['application']}", file=sys.stderr)
    if args.image:
        print(f"[*] image : {args.image}", file=sys.stderr)
    print(f"[*] prompt: {args.prompt[:120]}{'...' if len(args.prompt) > 120 else ''}",
          file=sys.stderr)
    print(f"[*] submitting to live Higgsfield queue + polling (up to {args.timeout:.0f}s)...",
          file=sys.stderr, flush=True)

    t0 = time.monotonic()
    result = await runner.invoke_higgsfield(spec, job)
    dt = time.monotonic() - t0

    print(f"[=] status={result.status.value} "
          f"err={result.error_class.value if result.error_class else '-'} "
          f"cost={result.cost} elapsed={dt:.1f}s", file=sys.stderr)

    if result.status is not ResultStatus.OK or not result.artifact_ref:
        print(f"FAIL: no artifact. {result.text[:300]}", file=sys.stderr)
        return 1

    out_path = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    print(f"[*] downloading -> {out_path}", file=sys.stderr, flush=True)
    # the CDN 403s urllib's default UA; send a browser-like one.
    req = urllib.request.Request(
        result.artifact_ref,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req) as resp, open(out_path, "wb") as f:
        f.write(resp.read())
    size = os.path.getsize(out_path)

    if size <= 100_000:
        print(f"FAIL: file too small ({size} bytes) — {result.artifact_ref}", file=sys.stderr)
        return 1
    print(f"[=] saved {size/1_000_000:.1f} MB", file=sys.stderr)
    # stdout = just the path, so `hf ... ` is scriptable (e.g. `open "$(hf ...)"`)
    print(out_path)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="hf", description="one-shot Higgsfield image/text -> video")
    p.add_argument("prompt", help="the motion/scene prompt")
    p.add_argument("--image", metavar="URL",
                   help="seed image url (image->video). Omit for text->video.")
    p.add_argument("--agent", default="higgsfield",
                   help="roster row (default: higgsfield=dop/lite 720p; "
                        "higgsfield-kling=1080p; higgsfield-minimax) or app:/raw/path")
    p.add_argument("--out", default="./hf_out.mp4",
                   help="output mp4 path (default: ./hf_out.mp4)")
    p.add_argument("--timeout", type=float, default=480.0,
                   help="max seconds to poll (default 480; minimax needs ~900)")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
