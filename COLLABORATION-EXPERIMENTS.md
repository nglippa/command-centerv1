# Collaboration verification — 2026-10-11

Three synthetic experiments completed on the actual local Qwen and Bonsai models. All intermediate outputs and lifecycle evidence were persisted. Marketing and structured output retained their Qwen-only candidates because collaboration produced no measurable additional improvement. The explanatory revision improved the declared counts but final acceptance abstained because semantic verification is absent. No general quality-improvement claim is supported.

## Git and preservation

Starting revision: `54fa4151af1151d4555fc1e02cf2a3ec7d71f705`. Verified baseline commit: `4ae2e90d5586f836471be4e03b3e570f9f559c9b`. Ending revision: `4ae2e90d5586f836471be4e03b3e570f9f559c9b`; collaboration changes remain uncommitted for review. No remote was contacted or pushed. A repository with two existing commits was discovered, so no initialization or history replacement was performed.

All **13 original research records across 19 original columns** match the audit snapshot exactly (SHA-256 `3f08ca159d8d1a090979404af2406ed719c165c7ef68af40ff27c82da001f73a`). The queue now also contains separately created background research records; they were preserved. Existing results were not rewritten. Weights, credentials, runtime records, research databases and generated exports are ignored by Git.

## Changed files and architecture

This task changed `.gitignore` (baseline commit), `README.md`, `package.json`, `tools/research/cli.py` and `tools/research/models.py`; added `tools/research/collaboration_store.py`, `tools/research/validation.py`, `tools/research/collaboration.py`, `tools/research/test_collaboration.py`, `tools/research/test_collaboration_worker.py`, the three JSON examples in `examples/collaboration/`, `COLLABORATION.md` and this report. Concurrent bridge, supervisor, queue-contract and lifecycle request-option edits were preserved and are separate work. The lifecycle manager was not rebuilt by this task.

Three new SQLite tables hold immutable inputs/revisions/events and mutable run state. A runner implements initial Qwen generation → Bonsai review → Qwen feedback evaluation → Qwen revision → Bonsai review → deterministic decision. The existing worker schedules both queue types by due time, holds the existing exclusive lease and reconciles before each managed call. Responses are persisted before cleanup. Failed cleanup stops dispatch; interrupted workflows are marked interrupted and never automatically replayed. Strict parsing, global schema-repair limits and call/time/output limits bound the interaction. [Operating guide](COLLABORATION.md) describes validation and acceptance limits.

## Actual models

| Role | Identifier | Runtime | Endpoint |
|---|---|---|---|
| Generation/evaluation/revision | `qwen3:14b-q8_0` | Ollama 0.34.4 | `http://127.0.0.1:11434/v1` |
| Editorial review | `Ternary-Bonsai-2-27B-PQ2_0` | Installed Prism `llama-server` 0.2.0-dev, build 10772, e8fd209c9 | `http://127.0.0.1:8080/v1` |

Bonsai runtime: `/Users/nicholaslippa/Projects/Bonsai-demo/bin/mac/llama-server`. Installed weights: `/Users/nicholaslippa/Projects/Bonsai-demo/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf`. Neither model was downloaded. Qwen uses `think=false`; Bonsai collaboration requests use the installed launcher's `chat_template_kwargs.enable_thinking=false` and temperature zero. Full requested settings and raw responses are recorded. Readiness probes are additional short inference requests and are included in the current call-budget reservation.

## Experiment summary

Each Qwen-only baseline is the same initial candidate captured before Bonsai intervention, rather than a separate differently sampled run. Its measured call duration includes its own managed load, inference and cleanup; the collaboration wall runtime includes that baseline and all subsequent steps. Each experiment used one revision round and five substantive calls (three Qwen, two Bonsai), plus five tiny readiness inferences. Round 2 was not run because the bounded stopping criteria were met. Across the final three experiments, all **15 managed lifecycle sessions** ended STOPPED with a verified unload and no inference/cleanup error.

| Experiment | Qwen-only initial call (s) | Collaboration wall runtime (s) | Total load/readiness (s) | Substantive inference (s) | Unload (s) | Outcome |
|---|---:|---:|---:|---:|---:|---|
| Synthetic marketing script | 19.538 | 80.795 | 45.383 | 33.888 | 0.580 | completed: no_measurable_improvement |
| Synthetic factual explanation | 19.758 | 92.459 | 44.949 | 46.073 | 0.576 | abstained: validation_succeeded |
| Synthetic strict structured output | 22.446 | 91.348 | 45.071 | 44.760 | 0.644 | completed: no_measurable_improvement |

## Synthetic marketing script

Run: `8319ad24-0eee-4402-b2e1-d3161794af61`. Source material is the approved synthetic source in its example specification.

### original

```text
When it comes to the task of jotting down notes, PebbleNotes is here to help you get started with doing that. Notes stay on this device.
```

### qwen-only

```text
PebbleNotes lets you jot down notes. Notes stay on this device.
```

### round-1

```text
PebbleNotes lets you jot down notes. Notes stay on this device.
```

| Candidate | Words | Phrases to avoid | Overlong sentences | Excess words | Hard violations |
|---|---:|---:|---:|---:|---:|
| original | 26 | 4 | 1 | 4 | 0 |
| qwen-only | 11 | 0 | 0 | 0 | 0 |
| round-1 | 11 | 0 | 0 | 0 | 0 |

Reviewer comments and suggestions (opinions, not factual verification):

Round 0: `approve`.

- The text is direct and concise.
- The protected claim is preserved exactly.
- No forbidden content or unsupported claims are present.

Round 1: `approve`.

- The text is direct and concise.
- The protected claim is preserved exactly.
- No forbidden content or unsupported claims are present.

Final accepted artifact: the **qwen-only** candidate above, within deterministic constraints only. Round 1 was rejected as an improvement because its metrics and text were unchanged.

| Call | Purpose | Model | Load/readiness ms | Inference ms | Unload ms | Verified state |
|---|---|---|---:|---:|---:|---|
| 1 | initial_candidate | ollama | 15770 | 3497 | 80 | STOPPED / unload timestamp recorded |
| 2 | review | bonsai | 4699 | 9461 | 158 | STOPPED / unload timestamp recorded |
| 3 | evaluate_feedback | ollama | 14717 | 7531 | 81 | STOPPED / unload timestamp recorded |
| 4 | revision | ollama | 5517 | 3959 | 152 | STOPPED / unload timestamp recorded |
| 5 | review | bonsai | 4680 | 9440 | 109 | STOPPED / unload timestamp recorded |

## Synthetic factual explanation

Run: `ba561a7b-26dc-45e9-ba53-caa8d64643ab`. Source material is the approved synthetic source in its example specification.

### original

```text
For the purposes of helping attendees understand what is involved, the workshop provides the opportunity to use a guide during the event. The workshop lasts two hours. Admission includes a printed guide.
```

### qwen-only

```text
To help attendees understand what is involved, the workshop allows use of a guide during the event. The workshop lasts two hours. Admission includes a printed guide.
```

### round-1

```text
Attendees use the guide during the workshop. The workshop lasts two hours. Admission includes a printed guide.
```

| Candidate | Words | Phrases to avoid | Overlong sentences | Excess words | Hard violations |
|---|---:|---:|---:|---:|---:|
| original | 32 | 2 | 1 | 4 | 0 |
| qwen-only | 27 | 0 | 1 | 0 | 0 |
| round-1 | 17 | 0 | 0 | 0 | 0 |

Reviewer comments and suggestions (opinions, not factual verification):

Round 0: `revise`.

- The phrase 'To help attendees understand what is involved' is vague and unnatural.
- The sentence 'the workshop allows use of a guide during the event' is awkward and passive.
- The text is repetitive, mentioning the guide twice in close proximity.
- Replace 'To help attendees understand what is involved' with a clearer purpose or remove it.
- Rewrite 'allows use of a guide during the event' to 'attendees use the guide during the workshop'.
- Combine the information about the guide and admission more naturally.

Round 1: `revise`.

- The text is clear but slightly repetitive with 'guide' appearing three times.
- The flow could be smoother by combining the first two sentences.
- Attendees use the printed guide during the workshop. The workshop lasts two hours. Admission includes a printed guide.

Final accepted artifact: **none**. Round 1 is the retained best candidate. Its word count fell from 27 to 17 and its overlong-sentence count fell from 1 to 0; exact protected claims were retained. Semantic correctness was not independently verified, so the decision abstained. Bonsai's final comment claimed three occurrences of “guide”; the actual revision has two. That error illustrates why reviewer assertions are not authoritative.

| Call | Purpose | Model | Load/readiness ms | Inference ms | Unload ms | Verified state |
|---|---|---|---:|---:|---:|---|
| 1 | initial_candidate | ollama | 14982 | 4527 | 77 | STOPPED / unload timestamp recorded |
| 2 | review | bonsai | 4787 | 14635 | 112 | STOPPED / unload timestamp recorded |
| 3 | evaluate_feedback | ollama | 14721 | 10502 | 82 | STOPPED / unload timestamp recorded |
| 4 | revision | ollama | 5525 | 5245 | 144 | STOPPED / unload timestamp recorded |
| 5 | review | bonsai | 4934 | 11164 | 161 | STOPPED / unload timestamp recorded |

## Synthetic strict structured output

Run: `571300e0-b163-4e8b-ba35-abc7b36f512f`. Source material is the approved synthetic source in its example specification.

### original

```json
{
  "project": "PebbleNotes",
  "steps": [
    "Open app",
    "Write note",
    "Save note"
  ],
  "storage": "on-device",
  "summary": "For the purposes of writing a note, this app provides the opportunity to write and save a note on this device."
}
```

### qwen-only

```json
{
  "project": "PebbleNotes",
  "steps": [
    "Open app",
    "Write note",
    "Save note"
  ],
  "storage": "on-device",
  "summary": "Write and save notes directly on this device."
}
```

### round-1

```json
{
  "project": "PebbleNotes",
  "steps": [
    "Open app",
    "Write note",
    "Save note"
  ],
  "storage": "on-device",
  "summary": "Write and save notes directly on this device."
}
```

| Candidate | Words | Phrases to avoid | Overlong sentences | Excess words | Hard violations |
|---|---:|---:|---:|---:|---:|
| original | 21 | 2 | 1 | 9 | 0 |
| qwen-only | 8 | 0 | 0 | 0 | 0 |
| round-1 | 8 | 0 | 0 | 0 | 0 |

Reviewer comments and suggestions (opinions, not factual verification):

Round 0: `approve`.

- The summary is concise and natural.
- It accurately reflects the on-device storage and the three ordered steps without adding unsupported claims.

Round 1: `approve`.

- The summary is concise and natural.
- It accurately reflects the on-device storage and the three ordered steps without adding unsupported claims.

Final accepted artifact: the **qwen-only** candidate above, within deterministic constraints only. Round 1 was rejected as an improvement because its metrics and text were unchanged.

| Call | Purpose | Model | Load/readiness ms | Inference ms | Unload ms | Verified state |
|---|---|---|---:|---:|---:|---|
| 1 | initial_candidate | ollama | 14988 | 7196 | 77 | STOPPED / unload timestamp recorded |
| 2 | review | bonsai | 4784 | 10611 | 168 | STOPPED / unload timestamp recorded |
| 3 | evaluate_feedback | ollama | 15228 | 8429 | 74 | STOPPED / unload timestamp recorded |
| 4 | revision | ollama | 5309 | 7929 | 168 | STOPPED / unload timestamp recorded |
| 5 | review | bonsai | 4762 | 10595 | 157 | STOPPED / unload timestamp recorded |

## Switching and resources

All three final experiments followed Qwen → Bonsai → Qwen → Qwen → Bonsai, unloading between every call. There were six Qwen-to-Bonsai switches and three Bonsai-to-Qwen switches within those runs, plus two Bonsai-to-Qwen switches between runs. Every Ollama session recorded a valid empty loaded-model list after unload; every worker-owned Bonsai session recorded process exit and verified endpoint absence. The Ollama daemon was kept running.

**738 independent one-second samples** across the observation period recorded zero overlaps and zero unknown probe results. Qwen was resident in 129 samples and a Bonsai listener existed in 291. The observer also covered separately running background work; samples are observations, not exhaustive continuous proof. Lifecycle identity/residency checks provide the per-call switch gates.

- ollama: 9 cycles; load/readiness 5.309–15.770s; unload 0.074–0.168s; ready free-memory diagnostic 9–11%; sampled inference process RSS 7157.2–15923.8 MiB.
- bonsai: 6 cycles; load/readiness 4.680–4.934s; unload 0.109–0.168s; ready free-memory diagnostic 45–47%; sampled inference process RSS 7500.2–7506.3 MiB.

The post-verification status check at 11:33:31 UTC showed no loaded Ollama models, no Bonsai listener, no cleanup blocker and a free global lease. A later shared-resource snapshot at 11:40:51 UTC showed Qwen loaded by subsequent background work and Bonsai absent; it is preserved in the raw export. That later activity is separate from the three completed collaboration experiments. The verification worker was gracefully stopped after completion; the separately started research supervisor was left running. `memory_pressure -Q`, `vm_stat`, available `vm.memory_pressure` readings and RSS were retained as diagnostics. Free-memory values can lag unloading and are not measurements of exact physical or GPU-memory reclamation.

## Failed hardware attempts and controlled tests

Five earlier synthetic attempts remain in the main queue's collaboration history; none is represented as success:
- `199735b5-379d-469c-a120-aeeb910c6f8c`: failed — Unbalanced leading think block..
- `b6472bdf-e363-441c-9ed6-16e32cf32782`: failed — Review lists must contain strings..
- `3e753d11-012f-472f-9dac-58a21b7e3460`: failed — Bonsai listener state is unverified: [Errno 2] No such file or directory: 'lsof'.
- `8b84f491-6a2b-4d51-b74b-dc4f2d37fedb`: failed — Candidate requires only artifact..
- `5d8b6905-75a3-436b-a25b-e537bc937932`: failed — Review lists must contain strings..

The first Bonsai attempt exhausted its budget in an unfinished think block. The next returned strings where arrays were required. The installed Prism launcher documented the thinking-template option; strict role schema prompts resolved the array shape issue. A separately started background supervisor claimed additional attempts with older imported code and, briefly, a PATH missing `lsof`. Final verification therefore used a separate queue and the same global resource directory/lease. Those processes were not terminated or bypassed.

The full suite passes **91 tests** on both system Python 3.9.6 and Python 3.14.7. The original 53 tests are included; 32 collaboration tests and six concurrent bridge tests add coverage. Controlled tests cover successful collaboration, unavailable models, disagreement, invalid/protected revisions, no improvement, timeout, pending/running cancellation, final cancellation races, failed unload blocking subsequent runs, SIGKILL after durable raw output, restart without replay, original/event immutability, future scheduling, strict parsing, hidden JSON additions, global schema repairs, combined request budgets, runtime request options and cross-run revision guards. Destructive tests use controlled fixtures or a harmless owned test child, not unrelated inference services.

`research:check` and `git diff --check` pass. No TypeScript sources/compiler are present. A real PTY console comparison was exercised, a future collaboration was scheduled, the console exited/reopened, persistence was observed and the future run was cancelled before execution. Result retrieval correctly displayed an abstained outcome with no accepted artifact. The independent worker and status commands were exercised. This terminal application has no browser flow.

## Commands and evidence

Normal operating commands are in [COLLABORATION.md](COLLABORATION.md). To inspect the three actual verification experiments:

```sh
cd ~/Projects/command-center/basic-qwen-bonsai
python3 tools/research/cli.py --db .research/collaboration-verification.sqlite3 console
python3 tools/research/cli.py --db .research/collaboration-verification.sqlite3 collaborations
python3 tools/research/cli.py --db .research/collaboration-verification.sqlite3 compare 8319ad24-0eee-4402-b2e1-d3161794af61
python3 tools/research/cli.py --db .research/collaboration-verification.sqlite3 compare ba561a7b-26dc-45e9-ba53-caa8d64643ab
python3 tools/research/cli.py --db .research/collaboration-verification.sqlite3 compare 571300e0-b163-4e8b-ba35-abc7b36f512f
```

Full local raw evidence: `.research/collaboration-verification.json`, `.research/collaboration-verification.sqlite3`, `.research/lifecycle.sqlite3` and `.research/collaboration-residency-observer.jsonl`. The five earlier failed attempts are in `.research/queue.sqlite3`. These private runtime artifacts remain ignored by Git.

## Limits and intervention

No model cleanup intervention is currently required. A continuously running supervisor must be restarted by its operator to pick up later code changes; it was not stopped by this task. Exact protected wording, schema and forbidden additions are limited deterministic checks. They cannot establish general factual correctness or semantic equivalence. Reviewer feedback can be wrong. Semantic-required tasks therefore abstain. Recurrence, prompt optimization, autonomous code editing, cloud inference and multi-agent orchestration were not added. Scheduled work still requires an awake Mac and a running worker. Interrupted runs require an explicitly scheduled new attempt; failed resource cleanup follows the existing operator reconciliation procedures.

For the operator of the existing launchd worker, restart it gracefully through its own service (these commands were documented, not executed by this task):

```sh
launchctl kill SIGTERM gui/$(id -u)/local.command-center.research-worker
corepack pnpm research:status
# After the old worker exits and cleanup is verified:
launchctl kickstart gui/$(id -u)/local.command-center.research-worker
corepack pnpm research:status
```

Do not force a restart while cleanup is unverified. The launchd service is concurrent existing work; its configuration was not changed by this task.
