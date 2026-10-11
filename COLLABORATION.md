# Bounded local collaboration

Existing research tasks and lifecycle management remain in place. Collaboration adds a specification, append-only evidence tables in the existing queue database, a sequential runner, deterministic validation and a terminal comparison. There are no additional dependencies.

```sh
cd ~/Projects/command-center/basic-qwen-bonsai
corepack pnpm research:console
# Menu 7: schedule a JSON specification. Menu 8: compare its run ID.
# Or schedule directly; this prints a persistent run ID:
corepack pnpm research:collaborate examples/collaboration/marketing.json
corepack pnpm research:collaborate examples/collaboration/explanation.json
corepack pnpm research:collaborate examples/collaboration/structured.json
# Optional scheduled time, with timezone:
corepack pnpm research:collaborate examples/collaboration/marketing.json --at '2026-10-12T10:00:00-05:00'
# Independent worker; the console can exit:
corepack pnpm research:worker
# Or drain jobs due when this invocation starts:
corepack pnpm research:worker --once
corepack pnpm research:collaborations
corepack pnpm research:compare RUN_ID
corepack pnpm research:cancel-collaboration RUN_ID
corepack pnpm research:status
corepack pnpm research:models
corepack pnpm research:check
corepack pnpm research:test
```

An already-running worker imports code at startup. Restart it through its owning supervisor before relying on newly installed collaboration commands; a second worker on the same queue safely refuses to start. The three recorded hardware experiments used `.research/collaboration-verification.sqlite3` to avoid interfering with a concurrently started supervisor. Open those results with `python3 tools/research/cli.py --db .research/collaboration-verification.sqlite3 console`.

The Mac must be awake and the worker running. A pending cancellation prevents dispatch. A running cancellation stops additional calls and lets the bounded current call finish cleanup; SIGINT/SIGTERM use the same boundary. A cleanup failure stops the worker and blocks conflicting execution. Existing operator reconciliation procedures still apply (`research:reconcile`). No unrelated server is killed.

## Sequence and limits

Qwen generates an initial candidate, which is the Qwen-only baseline. Bonsai reviews it. Each revision round has Qwen evaluate the feedback, Qwen revise, Bonsai review, and independent deterministic validation. Models execute sequentially through the existing manager, including its lease, identity/readiness checks and verified unloading after every call. Consecutive Qwen calls also unload between steps.

Defaults are two rounds, twenty inference requests (including readiness), 900 seconds, 1,024 output tokens per call, 12,000 response characters per call, 80,000 response characters total and zero schema retries. Each role call reserves two inference requests: the existing manager's tiny readiness inference and the substantive workflow request. Load/unload HTTP requests and bounded state polling are not text generation. Limits are editable in the example specifications. There are hard admission caps. Retries, if enabled, are a global budget of at most two schema repairs; inference failures and unknown outcomes are never automatically retried. Oversized received responses are preserved and rejected. Prompts are capped at 8,192 characters; the installed model's context limit still applies.

The deadline limits new dispatch and allocates load/inference budgets with a cleanup reserve. Cleanup takes priority over that deadline and has the existing bounded cleanup timeouts. HTTP readiness checks and diagnostics also have their own bounds; the runtime limit is not a real-time hard kill of the entire process.

Stop reasons include validated improvement, no measurable improvement, repeated disagreement, protected requirement violations, exhausted rounds/calls/runtime/output, cancellation, interruption, missing models and failed cleanup. The original, Qwen-only output and rejected revisions remain accessible. An interrupted run is marked interrupted on restart, preserving evidence, and is never automatically replayed. Schedule a new run explicitly to try again.

## Specifications and validation

Required fields are `name`, `instruction`, `source` and `original`. `format` is `text` or `json`. Inputs and exact configured model snapshots are fixed at enqueue time. `constraints` supports word/character bounds, literal required content, exact protected claims, forbidden literals, allowed numeric tokens, required/allowed Markdown headings and a restricted JSON schema. Explicit empty numeric/heading whitelists permit none. JSON schema supports types, required properties, additional-property prohibition, constants, enums, numeric bounds, string length, array length and item schemas; unsupported keywords are rejected at admission. A schema constant change is treated as a protected requirement violation.

For JSON, `metrics.field` selects a string field for word/character limits and editorial metrics; protected/required/forbidden content and numeric checks still inspect the whole object. `metrics` can declare phrases to avoid, a maximum sentence word count and a word-count ceiling (`target_words`). Improvement means at least one declared violation/goal count decreased and none increased. Raw word/character counts are diagnostics, not quality scores. Revisions are compared to the retained Qwen-only/best candidate; model votes do not define improvement.

These checks can catch explicit unauthorized fields, numbers, headings and forbidden additions. They cannot prove general semantic equivalence, factual correctness or absence of all unsupported prose. Exact protected wording is only a literal check and does not prevent every contradictory surrounding statement. `requires_semantic_verification` defaults to true: the final decision abstains because there is no independent semantic verifier. Setting it false permits acceptance only within the declared deterministic constraints. Neither model is a factual authority. No word-overlap metric or reviewer agreement establishes correctness.

## Evidence and comparison

The SQLite tables `collaboration_runs`, `collaboration_events` and `collaboration_revisions` preserve inputs, source, prompts, configured identities, request parameters, raw transport responses, parsed artifacts, critiques, feedback decisions, validation, load/inference/unload timings, lifecycle/resource observations and final outcomes. SQLite triggers reject updates/deletions of revision/event history and changes to the original specification/model snapshots. The final outcome points to an existing revision rather than replacing the original. Raw responses are committed before cleanup; a crash during unload does not discard them. Model response parsing accepts only one JSON object, optionally a complete leading think block and a whole JSON code fence; malformed/truncated output remains evidence of failure.

`research:compare` shows Original, Qwen-only, Round 1, Round 2 and Final accepted side by side, followed by full artifacts, diffs, provenance, comments, validations and decisions. An abstention has no final accepted artifact; its best candidate remains in the history. More than two configured rounds remain in the detailed history below the comparison.

The installed Prism launch script explicitly uses `enable_thinking=false`. Collaboration supplies that template option and temperature zero on Bonsai requests; this is necessary because `--reasoning-format none` leaves thought text in content and the existing budget flag alone did not suppress it. Ordinary research requests keep their existing settings. The existing lifecycle manager is unchanged by collaboration.

Research databases, model weights, caches and credentials are ignored by Git. The synthetic examples contain no private information. [Actual experiments and measurements](COLLABORATION-EXPERIMENTS.md) distinguish hardware evidence from controlled failure tests.
