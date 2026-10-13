# docs/reviews — committed review transcripts

Raw reviewer output from the adversarial review rounds behind two of the larger slices — the
docs-only automerge gate and the controller loop — plus one targeted fix. Most are OpenAI Codex
and Gemini reviews (different model families on purpose; they catch different things); two are
Claude adversarial-workflow fleet runs. Committed deliberately as the audit trail behind those
design docs: each records what a reviewer claimed, what was verified or refuted, and which
findings were folded into the design or the code. Names roughly follow
`<feature>-<stage>-<reviewer>`.

These are point-in-time records — machine paths, branch names, and verdicts reflect the state at
review time and are never edited after the fact. Other slices record their review rounds and
folded findings inside their design docs in `docs/`.
