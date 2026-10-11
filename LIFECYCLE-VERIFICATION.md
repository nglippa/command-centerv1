# Lifecycle verification — 2026-10-11

Implemented and verified on this Apple M4 Pro Mac mini (24 GiB unified memory). Real hardware runs are separate from controlled failure tests. No model downloads, cloud inference, unrelated process termination, Git commits, pushes or deployments were performed.

## Git revisions and files

Starting revision: **N/A**. Ending revision: **N/A**. This standalone directory has no Git repository (`git rev-parse HEAD` and `--show-toplevel` both fail). No repository was initialized.

Implementation/documentation files changed:

- `README.md`
- `package.json`
- `tools/research/README.md`
- `tools/research/cli.py`
- `tools/research/queue_store.py`
- `tools/research/models.json`
- `tools/research/registry.py`
- `tools/research/lifecycle.py`
- `tools/research/owned_server.py`
- `tools/research/test_research.py`
- `tools/research/test_lifecycle.py`
- `tools/research/test_worker_lifecycle.py`
- `LIFECYCLE-VERIFICATION.md`

Existing `models.py` HTTP transport and pnpm lockfile were reused unchanged; no dependencies were added. Local data changes are limited to nullable queue schema additions, nine appended completed hardware jobs, one appended cancelled console task, lifecycle records/lease, and `.research/hardware-verification.json`. All three original task records, including full stored result/raw JSON, were compared across all 17 original columns and remain exactly unchanged.

Original-history SHA-256: `4c9e96659dc159ca4ab53f89cd9394f602c141a293e9378400cede6914c88c10`.

## Audit and implementation

The old worker directly requested inference and left model residency unmanaged. It had no Bonsai startup/shutdown ownership, no loaded-state verification, no resource lease across different queues, and no resource reconciliation after interruption. Its queue locking, atomic pending claim/cancellation, at-most-once recovery and HTTP safety bounds were retained. The previous Bonsai alias came from historical documentation; the installed model was independently discovered and historical records were preserved.

`registry.py` validates exact local entries and immutable task snapshots: provider, endpoint, ownership, installed runtime/model identity, explicit start/stop/health mechanisms, expected resource estimates, context/output bounds and finite deadlines. `lifecycle.py` implements timestamped transitions, cooperative exclusive heavy-model lease, bounded readiness and cleanup checks, resource blockers, diagnostics and restart reconciliation. `owned_server.py` gates Bonsai exec on durable PID/birth/target-executable registration; worker death before permission closes the pipe and prevents model loading. Readiness checks the actual post-exec process identity. Completed response/raw JSON/session ID are committed atomically before cleanup, and cleanup errors are stored separately.

Ollama daemon PID 11621, version 0.34.4, remains running at `http://127.0.0.1:11434/v1`. Exact model: `qwen3:14b-q8_0` (installed weight size 15,895,155,363 bytes). Native preload, `/api/ps`, tiny readiness inference, task inference, native `keep_alive=0`, and valid complete `/api/ps` absence are used. Other resident Ollama models are not unloaded.

Bonsai exact served alias: `Ternary-Bonsai-2-27B-PQ2_0`, endpoint `http://127.0.0.1:8080/v1`. Runtime: `/Users/nicholaslippa/Projects/Bonsai-demo/bin/mac/llama-server`, Prism `0.2.0-dev`, build 10772, commit `e8fd209c9`. Weights: `/Users/nicholaslippa/Projects/Bonsai-demo/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf` (7,206,168,928 bytes). Both dependencies exist; no missing dependency or substitute was used.

## Real hardware measurements

All nine jobs completed against the actual locally installed model and passed cleanup verification. Cycles 1–5 satisfy the requested three Qwen/two Bonsai sequence; 6–7 validate stricter identity/configuration checks; 8–9 validate the final gated Bonsai launcher and switch back to Qwen. Every completed raw response contains the exact requested model identifier. Durations are measured wall time; load includes readiness inference, and inference includes result persistence. Diagnostic commands run separately. Hardware prompts used 64 output tokens, 4,096-token context and 120-second job timeout.

| Cycle | Model | Load (s) | Inference (s) | Unload (s) | Verified final state |
|---|---|---:|---:|---:|---|
| 1 | Qwen | 14.948 | 0.507 | 0.182 | STOPPED |
| 2 | Bonsai | 6.062 | 2.615 | 0.113 | STOPPED |
| 3 | Qwen | 15.399 | 0.478 | 0.080 | STOPPED |
| 4 | Bonsai | 5.745 | 2.558 | 0.111 | STOPPED |
| 5 | Qwen | 13.999 | 0.479 | 0.065 | STOPPED |
| 6 | Bonsai | 5.749 | 2.415 | 0.174 | STOPPED |
| 7 | Qwen | 15.379 | 0.479 | 0.075 | STOPPED |
| 8 | Bonsai | 5.768 | 2.446 | 0.163 | STOPPED |
| 9 | Qwen | 15.564 | 0.479 | 0.075 | STOPPED |

All Qwen cycles observed `/api/ps` empty before load, exactly the target model with context 4,096 while ready, and valid `{"models":[]}` after unload. All Bonsai cycles observed the exact owned listener PID, `/health` OK, exact model alias and weight path, successful actual inference, owned child exit code 0, and no listener afterward. Qwen → Bonsai and Bonsai → Qwen switches succeeded repeatedly. No real execution or cleanup error occurred. Bonsai included `<think>` content and responses varied slightly in punctuation; all returned text and raw JSON were preserved unchanged.

## Resource observations

macOS `memory_pressure -Q`, `vm_stat`, and process RSS were sampled at startup, readiness and cleanup. RSS varied with paging/caching and is not model allocation size. Immediate post-unload free-memory percentage can lag verified inference-resource release. The unsupported `sysctl vm.memory_pressure` signal is marked unavailable.

| Cycle | Free percentage startup / ready / after | Ready inference process RSS (MiB) |
|---|---|---:|
| 1 | 80% / 8% / 71% | 15300.8 |
| 2 | 77% / 47% / 78% | 7657.4 |
| 3 | 78% / 11% / 26% | 12341.8 |
| 4 | 77% / 47% / 77% | 7654.5 |
| 5 | 77% / 10% / 12% | 6340.4 |
| 6 | 78% / 46% / 78% | 7653.8 |
| 7 | 78% / 8% / 9% | 8074.3 |
| 8 | 78% / 47% / 78% | 7652.1 |
| 9 | 78% / 7% / 8% | 6819.6 |

Final fresh memory-pressure observation: **78%** free. Final inference-process sample: `[{"pid": 11621, "ppid": 1, "rss_kb": 24400, "executable": "/opt/homebrew/bin/ollama"}]`. Final loaded-model listing is empty and Bonsai has no listener. The Ollama daemon remains alive. These observations do **not** establish exact physical or GPU-memory reclamation.

## Automated and interactive verification

**53 tests passed** on Python 3.9.6 and Python 3.14.7; the ten original tests remain included. Controlled fixtures cover Qwen load/unload, Bonsai start/stop/missing/crash, wrong runtime/identity, inference timeout, malformed residency and wrong-model responses, failed/stuck unload, duplicate workers, a lease across queues/processes, SIGTERM in loading/inference/unloading, SIGKILL in inference/unloading, persistence and result/session durability, both switching directions, external ownership, stale PID reuse, unresponsive health, pending cancellation, repeated cycles and durable launch permission/EOF. Destructive failures use fake process effects or harmless owned test processes and controlled HTTP servers, never unrelated services.

`research:check` passed Python syntax compilation. TypeScript checks are not applicable: this project has no TypeScript sources, tsconfig, compiler dependency or TS build. No browser check applies to a terminal-only application.

The real terminal console was opened, Bonsai selected by its actual ID, a future task created, the console exited/restarted, the persisted task/status viewed, an original research result retrieved, and the pending task cancelled before dispatch. `research:worker --once`, `research:reconcile`, `research:models` and `research:status` passed. Final queue: pending=0, running=0, completed=10, failed=1 (original historical failure), cancelled=2. Worker stopped, lease free, no blocker.

Raw evidence, full state transitions, model identities, measurements, job IDs, live endpoint checks and final original-history comparison are in [.research/hardware-verification.json](.research/hardware-verification.json). Results remain accessible in the console.

## Limitations and manual intervention

No intervention is currently required. Scheduled tasks need a running worker and awake Mac. Running cancellation retains the original pending-only semantics; graceful stop may wait up to the bounded request deadline. Other applications are outside the cooperative lease and can load models; observed conflicts block managed work rather than terminating them. All workers using these endpoints must share one resources directory.

A SIGKILL after an owned Bonsai server has started can leave an orphan. Restart verifies PID/birth/executable/listener state but conservatively blocks rather than signaling a process from an old record. An operator must identify/release that verified orphan, then run `research:reconcile` and restart. Failed/unverified cleanup also stops dispatch and preserves pending jobs. External Bonsai reuse is opt-in and never killed; its continuing residency blocks conflicting execution. Missing-model behavior was verified with controlled tests because Bonsai is genuinely installed. No claim is made about exact GPU-memory reclamation.

## Exact operating commands

```sh
cd ~/Projects/command-center/basic-qwen-bonsai
corepack pnpm research:console
corepack pnpm research:worker
corepack pnpm research:status
corepack pnpm research:models
corepack pnpm research:reconcile
corepack pnpm research:check
corepack pnpm research:test
corepack pnpm research:worker --once
```

Run console and worker in separate terminals. [Operating details](tools/research/README.md) include direct Python equivalents, path configuration, deadlines, cancellation and operator recovery. Whodunnit and the video factory were not modified.
