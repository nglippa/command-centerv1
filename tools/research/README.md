# Operating the local console

Run from `~/Projects/command-center/basic-qwen-bonsai`. No dependencies need installing. The implementation uses Python 3.9+ standard library, with pnpm 12.4.1 package scripts.

```sh
corepack pnpm research:console
corepack pnpm research:worker
corepack pnpm research:status
corepack pnpm research:models
corepack pnpm research:reconcile
corepack pnpm research:check
corepack pnpm research:test
corepack pnpm research:worker --once
```

Direct equivalents:

```sh
python3 tools/research/cli.py console
python3 tools/research/cli.py worker
python3 tools/research/cli.py status
python3 tools/research/cli.py models
python3 tools/research/cli.py reconcile
python3 tools/research/cli.py worker --once
python3 -m unittest discover -s tools/research -p 'test_*.py' -v
```

The console retains six choices: create, queue, finished tasks, result, pending cancellation, exit. Select the actual listed model identifier. Qwen discovery can list additional installed Qwen identifiers at its configured endpoint; it never substitutes a model automatically. Name is optional (80 characters maximum). Enter a prompt (32,000 characters maximum) ending with a line containing only `.`. Blank run time means now; a scheduled time requires a timezone, for example `2026-10-12T09:00:00-05:00`. Queue view includes worker/lifecycle status. Full result and raw JSON are stored; only terminal control codes are removed from displayed text.

The console may close while the independent worker runs. A stopped worker or sleeping Mac cannot execute scheduled work. Normal worker polling is about one second. `--once` drains only work due when it started. Cancellation remains pending-only; cancelling an already running task returns false. Ctrl-C/SIGTERM requests graceful shutdown: finish the bounded current request, persist its outcome, clean up, then exit. Loading checks the stop request before dispatching the job. The request can take up to its configured timeout.

## Registry and available models

`tools/research/models.json` records verified local configuration. Each newly queued task snapshots the registry entry and its exact model/endpoint; subsequent configuration edits do not redirect it. Historical tasks without a snapshot require an exact registry model/endpoint match. Old completed, failed and cancelled records remain unchanged, including the old Bonsai alias.

| Model | Provider / endpoint | Ownership / mechanism |
|---|---|---|
| `qwen3:14b-q8_0` | Ollama 0.34.4, `http://127.0.0.1:11434/v1` | Shared service; native preload, tiny readiness inference, job, `keep_alive=0`, verified `/api/ps` absence |
| `Ternary-Bonsai-2-27B-PQ2_0` | Installed Prism fork, `http://127.0.0.1:8080/v1` | Start exact installed server, verify PID listener, health, alias, weight path and inference; terminate/wait owned child and verify endpoint release |

Actual Bonsai installation discovered on this Mac:

- Runtime: `/Users/nicholaslippa/Projects/Bonsai-demo/bin/mac/llama-server`
- Runtime: `0.2.0-dev`, build `10772`, commit `e8fd209c9` (`prism-b10772-e8fd209`)
- Weights: `/Users/nicholaslippa/Projects/Bonsai-demo/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf` (7,206,168,928 bytes)

Bonsai is installed here. Availability is checked again for each job: missing weights, missing/nonexecutable runtime, wrong fork, offline server, and readiness/inference errors are reported honestly. The generic upstream llama.cpp binary is not substituted. No model pulls/downloads, model URLs, remote backends, shell invocation or cloud credentials are used. Startup argv is explicitly allowlisted and binds localhost. Only simple text tasks are supported; no vision projector is loaded.

Both models use a 4,096-token context, up to 1,024 generated tokens by default, 600-second inference timeout, 180-second load/readiness budget, 30-second unload/shutdown budget and 3-second health requests, plus separately bounded diagnostic commands. Registry bounds: output 1–8,192 tokens, context 256–1,048,576, request/load/unload up to 3,600 seconds, health up to 30 seconds. Do not increase context/output without considering the 24 GiB memory limit. Responses have an 8 MiB transport cap. Readiness uses a small extra inference; it is included in load duration. Bonsai may return `<think>` text in content; the full response is retained unchanged.

Ollama must already be serving locally. If stopped, start it through your existing Ollama application/service or `ollama serve` in another terminal. The worker never kills or restarts that shared daemon.

## Lifecycle, persistence and recovery

Bonsai launch uses a small pipe-gated local launcher. The child cannot execute the model runtime until the worker has committed its PID, birth time and expected executable. If the worker dies before permission, pipe EOF makes the launcher exit without loading. Readiness verifies the post-exec process identity.

The queue remains `.research/queue.sqlite3`. Schema changes add only nullable task snapshot/session/cleanup fields. Lifecycle sessions, timestamped state transitions, residency evidence, durations, blockers and memory diagnostics are in `.research/lifecycle.sqlite3`. States are `UNAVAILABLE`, `STOPPED`, `STARTING`, `LOADING`, `READY`, `BUSY`, `UNLOADING`, `STOPPING`, `ERROR`. Actual completed inference is persisted **before** cleanup; cleanup failure is a separate task field, so it cannot erase a successful result.

A per-queue lock prevents duplicate workers. A second OS file lock at `.research/heavy-model.lease` protects the entire reconcile → load/start → readiness → inference → cleanup sequence across different queue databases. Use one coordination directory for every worker managing these endpoints. Custom paths go before the subcommand:

```sh
python3 tools/research/cli.py --db /path/queue.sqlite3 --config /path/models.json \
  --resources-dir /path/shared-model-resources worker
```

Changing the resources directory creates a different lease and recovery store. Do not use independent directories for the same actual endpoints; isolated tests use controlled fixture endpoints.

No job retries automatically. On restart, running queue rows become failed with unknown inference outcome, preserving existing at-most-once dispatch semantics. Completed results survive restart. Before claiming another task, the worker checks actual resource state rather than trusting an old state record. A stale managed Ollama load/unload intent is reconciled with native unload and valid `/api/ps` verification. An empty or malformed HTTP response is never evidence of unloading.

An external resident Ollama model is not unloaded by the worker. An occupied Bonsai endpoint is refused by default. Setting `reuse_external: true` explicitly allows reuse only after the endpoint serves the exact alias and installed weight path and passes readiness inference; it is never killed. Because that external process remains resident, completion records a cleanup blocker and the worker stops before a conflicting job. `owner_mode: external` also forbids spawning an absent external server.

Cleanup uses actual state checks and bounded polling. Failure leaves an `ERROR` blocker and exit code 3; pending conflicting jobs remain queued. A worker restart or explicit `research:reconcile` performs one bounded recovery attempt. Exit 2 means duplicate worker/resource lease contention, exit 1 an unexpected worker/configuration/database error. There are no infinite cleanup retries.

After SIGKILL or a machine crash during Bonsai execution, the live current-child handle is gone. The worker checks recorded PID **and birth/executable identity** plus the current listener, but conservatively refuses to signal an orphan or reused PID. It blocks until an operator releases that resource. A legacy unguarded startup record without a durable PID also blocks; the new gated launcher prevents creating that condition. This is safe recovery with operator intervention, rather than guessing ownership from a stale PID.

## Operator intervention

First run `research:status`, read the precise blocker and task cleanup error, and stop any active worker normally. Do not delete the lifecycle database or reset recorded state to bypass a blocker.

For an external model, release it through the application that owns it. For an orphaned Bonsai process, inspect the recorded PID, creation time, executable, current listener and actual model identity before manually stopping that verified orphan; never kill a process based only on an old PID. The worker intentionally does not provide a blind PID-kill command. Then run `research:reconcile` and `research:status`, and restart the worker once observations show no conflict. If the model server is unreachable, restore that local service first so absence can be verified.

Memory diagnostics use existing macOS `memory_pressure -Q`, `vm_stat` and process RSS. They are observations, not proof of exact physical/GPU memory reclamation. `STOPPED` means valid Ollama residency absence or a waited owned-process exit plus endpoint release. Other applications can still load models outside this cooperative lease; preflight checks detect observed conflicts, but the console cannot control unrelated applications.

Tests use controlled HTTP fixtures and fake process effects for destructive cases. Real hardware measurements are separately documented in `LIFECYCLE-VERIFICATION.md`; original history was compared across every original column. No browser verification applies to this terminal-only application. There are no TypeScript sources/configuration: `research:check` checks the actual Python implementation.
