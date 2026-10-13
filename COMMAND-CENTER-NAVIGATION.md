# Model and endpoint navigation repair

## Cause and architecture

Previously, Open Endpoint linked directly to the provider's `/v1/models` API listing. Open Model linked to a read-only model diagnostics route. Neither was a conversation workspace. Ollama's root page is a service message; the configured Prism server uses `--no-webui`. No dedicated Qwen/Bonsai agent/chat interface or persistent conversation service existed in this repository.

The existing architecture remains authoritative: registered local providers → durable SQLite queue → exclusive worker/resource lease → provider-specific load/readiness → inference → full result persistence → verified release. Qwen and Bonsai are models. The collaboration runner and opt-in capability runner are orchestration services; neither model becomes an agent merely by opening a chat.

## Implemented behavior

- **Open Model:** integrated workspace preserving exact endpoint and model identity, direct conversation, persistent selectable history, generation status, actual streamed text, provider-reported usage when available, runtime details, and queued/active cancellation according to worker support.
- **Open Endpoint:** configured identity, runtime/base URL, observed connection status, discovered models, provider-reported Ollama residency, measured connection-check duration, actual errors, Test Connection, and Chat with Model. Raw JSON is accessible only through **Developer: raw model listing**.
- All provider-listed installed Ollama models can use the same workspace and immutable task snapshots. Identifiers are discovered, not enumerated in code. Discovered models inherit configured safety/context/output limits but do not inherit Qwen's asserted weight size. Installed does not guarantee a model fits available memory or supports text chat; actual worker readiness/inference may fail and the error is retained.
- Owned Bonsai supports only its exact configured alias/runtime/weights. Its stopped endpoint is distinguished from installed local dependencies. Sending a turn lets the existing worker start/verify its configured owned server; viewing/testing a page never starts it.
- Recorded capability workflow events are exposed separately from direct chat, linked only to actual child requests for the selected model. Prompts, model answers and tool arguments are omitted from this activity summary. The Skills & Tools catalog remains available; direct chat grants no tool execution.

## Persistence and safety

New conversation/turn and bounded generation tables live in the existing queue database. A turn, immutable model snapshot, queue task and progress row are inserted atomically. Full completed assistant messages enter subsequent context only after verified resource release. Historical artifacts are not overwritten. Failed/cancelled turns remain evidence but are not silently treated as successful answers or replayed.

Streaming is opt-in to the exact version-1 interactive-chat contract. Ordinary research, collaboration and Video Factory bridge calls retain their prior behavior. Ollama uses native NDJSON; Bonsai uses OpenAI-compatible SSE. Messages contain user/assistant visible text only. Provider thinking/reasoning channels and inline `<think>` blocks are suppressed, including tags split between network chunks. No reasoning trace is invented or displayed.

The worker writes coalesced progress at most four times per second plus final/error writes; the browser polls active conversations once per second. No WebSocket infrastructure or per-token database rows. Wire data is capped at 8 MiB, individual lines at 1 MiB, visible content at 4 MiB, history at 32 messages/24,000 characters, individual user messages at 8,000 characters. These character limits are safety bounds, not token estimates. Output bounds come from the selected immutable model configuration. Requests retain absolute deadlines, literal loopback-only URLs, no redirects/proxies and mandatory lifecycle cleanup.

Pending cancellation calls the existing queue cancellation service. Active cancellation sets a durable flag; the chat transport closes its socket and the worker performs normal verified cleanup. The canonical task records failure with the cancellation reason under existing queue semantics; the generation record identifies cancellation and retains partial visible content. The partial response is not accepted into subsequent conversation context. A browser disconnect never cancels work. On worker restart, ordinary interrupted-job recovery records unknown outcomes without replay; conversation history/partial output survives.

Feature readiness checks compare the current queue lock owner, PID birth/executable identity and worker feature record. A stale PID or feature record is not proof of current streaming support.

## Operation and activation

```sh
cd ~/Projects/command-center/basic-qwen-bonsai
corepack pnpm research:web --port 8789
corepack pnpm research:status
corepack pnpm research:test
corepack pnpm research:check
```

Open **http://127.0.0.1:8789**. The default web port remains 8787. Native JavaScript modules/Python standard library require no new dependencies or frontend build step.

The existing supervised worker was intentionally left running during implementation and isolated verification. It was subsequently gracefully reloaded after explicit operator approval and an idle/release check. The main workspace now verifies its current process identity and version-1 streaming support. Any legacy or unverified worker remains clearly labeled as completed-response mode. Reloading a production worker requires operator approval, an idle queue, and verification of graceful exit/resource release. Do not kill Ollama, use a force-kill restart, clear a lease or restart while production work is active. Existing `research:worker` duplicate-worker protection remains intact.

For an unsupervised deployment, stop the current worker through its own controls after idle and verified release, then run `corepack pnpm research:worker`. For the installed launchd deployment, use its existing `local.command-center.research-worker` service with graceful SIGTERM and an explicit start after verified exit; it does not necessarily auto-relaunch after a successful graceful stop. This document does not authorize that production operation.

## Real verification — 13 October 2026

A separate verification queue used the **same worker implementation and same global resource directory/lease**. Production was checked idle before each one-shot worker invocation. The existing supervisor was not stopped or replaced. These were actual local inference calls, not mocked outputs:

| Model / outcome | Load | Inference | Verified unload | Observed usage |
|---|---:|---:|---:|---|
| Qwen `qwen3:14b-q8_0`, complete | 42.992s | 6.802s | 0.073s | 29 input / 48 output; 8.5 output tokens/s from provider timing |
| Bonsai `Ternary-Bonsai-2-27B-PQ2_0`, complete | 5.925s | 4.647s | 0.160s | 24 input / 71 output, provider reported |
| Bonsai, browser Stop during actual stream | 6.582s | interrupted; no completed duration claimed | 0.163s | 118 partial visible characters retained; no token estimate |

Qwen browser samples showed successive partial sentences before the final response:

> Spring brings blooming flowers and warmer days. Summer is filled with long days and sunny skies. Autumn leaves turn colorful and the air cools down. Winter covers the ground in snow and brings a quiet stillness.

Bonsai completed a fictional-workshop paragraph and emitted an empty thinking wrapper; the added filter removes it from workspace presentation without modifying that historical record. A later real Bonsai stream showed visible inventory text without the wrapper. Clicking Stop changed the generation record to cancelled, retained partial text, closed the owned runtime and verified release. After testing, Ollama `/api/ps` reported an empty model list and no Bonsai listener remained.

Playwright exercised both actions, real chat submission/streaming, history restored after the owned test web server restarted, connection testing, discovery/selection of a nonconfigured Gemma model (navigation only; no Gemma inference), pending cancellation, and actual active Bonsai cancellation. Desktop 1440px, tablet 1024px and mobile 390px had no horizontal document overflow. Final interaction checks recorded no JavaScript errors or failed requests. Test-server restarts produced expected disconnected polling during the restart, separate from final checks.

Automated tests cover provider wire formats, bounded/invalid/malformed responses, cancellation of stalled sockets, timeout, usage sources, hidden reasoning, feature identity, exact navigation, dynamic model discovery, endpoint validation, CSRF, queue atomicity, pending cancellation, cleanup-gated history and restart persistence. Full results and any deployment activation decision are recorded in COMMAND-CENTER-WEB.md.

## Limitations

Live accurate token counts usually arrive only in the terminal provider frame. Visible text streams earlier; characters/words/chunks are never presented as tokens. Actual token throughput is shown only when a supported timing source exists. Context capacity is configured; live occupancy is unavailable. Endpoint latency measures the connection check, not inference. Bonsai throughput uses the recorded Command Center inference interval when available; it is an interval average, not a provider-timed metric. Recorded capability activity refreshes on the existing five-second polling cycle and preserves expanded details and typed input. Provider-wide request logs, exact GPU-memory reclamation and other applications' agent events are unavailable. Model/endpoint workspaces show only recorded Command Center activity.

No inference service was launched for navigation. No model download, cloud inference or new dependency. No commit or push was performed during implementation; Git publication requires an explicit operator request. No Video Factory source or workflow was changed by this task; concurrently modified files in that repository were left untouched.

Files for this repair: `chat_sessions.py`, `chat_stream.py`, `test_chat_stream.py`, `test_model_workspaces.py`, `web_static/workspaces.js` (new); `model_access.py`, `models.py`, `registry.py`, `queue_store.py`, `cli.py`, `lifecycle.py`, `web.py`, `web_static/app.js`, `web_static/styles.css`, and the web documentation (modified). All paths are under `tools/research` unless stated otherwise. Earlier capability/UI working-tree changes remain uncommitted and preserved.

### Main-queue activation verification

After operator-approved activation, real Bonsai chat on the standard port 8787 returned `Main queue chat is live.` Task `chat-turn-5935ef7e-aa3f-45d1-9431-be0377e288ec` completed with 21 input and 7 output tokens reported by the provider. Recorded load/inference/unload times were 9.854s / 1.209s / 0.269s. STOPPED and verified release were confirmed, with no blocker; Ollama reported no resident models and no Bonsai listener remained. The main queue contains this additional synthetic verification record; original records are preserved. The stale port-8787 web process was the cause of the reported module/API 404s and was gracefully reloaded. Final browser console had zero errors or warnings.
