# Command Center web application

The local browser interface operates the existing research queue and Qwen/Bonsai collaboration runner. Python 3.9+ standard library and native browser JavaScript only. No packages, model downloads, cloud connections, fonts from a CDN, or frontend build step are required.

## Start

```sh
cd ~/Projects/command-center/basic-qwen-bonsai
corepack pnpm research:web
```

Open **http://127.0.0.1:8787**. Without pnpm, use `python3 tools/research/web.py`.

The web application does not launch, restart, or replace the independent worker. The installed supervisor was left running. Check it with `corepack pnpm research:status`. If there is no supervised worker, start one in another terminal:

```sh
corepack pnpm research:worker
```

Keep the Mac awake and the worker running for due jobs to execute. The browser and web server can close without losing scheduled jobs or results. A second worker on the same queue refuses to start. Existing console/model/reconciliation commands continue to work.

For another local port:

```sh
corepack pnpm research:web --port 8789
# Or:
python3 tools/research/web.py --port 8789
```

Only IPv4 loopback is supported. An occupied port is reported without stopping another service.

## Operator workflow

- **Overview:** persisted counts, current and scheduled work, worker locks, global resource lease/blocker, recent collaborations and activity.
- **Jobs:** search/filter/sort, select an exact registered Qwen or Bonsai identifier, submit a text prompt, schedule in local time, inspect responses/errors, cancel a pending job, copy or download evidence. Output/timeout bounds may be reduced from their configured defaults. Ordinary running inference has no cancellation endpoint.
- **Collaboration:** submit a brief/source/original, choose text or JSON, use approved synthetic templates or a full specification, set existing bounded limits/constraints/metrics, then inspect stages, critiques, feedback evaluation, validation and provenance. Compare any immutable revisions. Text paragraphs use bounded word diffs; structured output uses line diffs. Very large comparisons use explicitly labelled positional highlighting. Revisions remain readable in full.
- **Models:** exact identifiers, runtimes, endpoints, ownership, installed-file/model availability, live residency observations, recorded lifecycle state, timings, last verified unload and diagnostics. Open Model provides a queue-backed conversation workspace; Open Endpoint provides discovery and connection checks. No unmanaged model start/stop controls or direct browser inference connections exist.
- **Endpoints:** configured local services, Test Connection, real provider model discovery/residency where supported, and selected-model conversation links.
- **Results:** searchable library by name, ID, model and state, opening research responses and collaboration artifacts/evidence. Copy artifacts and download complete JSON evidence.
- **Activity:** latest 200 recorded lifecycle/collaboration events and events reliably derived from task creation/claim/completion timestamps. A newly created STOPPED lifecycle session is distinguished from verified unloading. No invented intermediate events or utilization statistics.

The main `.research/queue.sqlite3` remains authoritative and writable through existing enqueue/cancel services. `.research/collaboration-verification.sqlite3`, when present, appears as **verification**: the earlier successful hardware experiments are browsable and downloadable, with cancellation disabled. Records are not migrated or overwritten. Normal summary lists are bounded to the latest 500 records per table/database; older known IDs remain accessible through record URLs. The activity window is bounded independently.

Accepted artifacts are identified by the persisted accepted revision ID. Revisions that were not retained remain accessible. Abstention displays its best retained candidate without presenting it as accepted. Deterministic validation diagnostics are not quality scores, and model agreement is not factual proof. Required semantic verification causes abstention because this system has no independent semantic verifier.

## Live state and recovery

The visible page polls summaries every five seconds; hidden pages stop polling. Selected evidence refreshes when persisted workflow evidence or lifecycle state changes, and failed detail reads are retried. Forms and filters survive polling. Model observations use the existing read-only lifecycle probe about every 15 seconds, with their own timestamps and bounded health checks. Installed Bonsai files do not prove the server is running; the worker still verifies runtime compatibility and inference before use.

**Worker offline** is different from **web disconnected**. The former means queued work waits. The latter retains the previous view as stale information, marks worker/resources unverified and reconnects automatically. Server restart refreshes the browser security session; mutations are never automatically replayed after a failed response. If a submission response is lost, inspect the queue before retrying.

Web restart never recovers or replays interrupted inference itself. Recovery stays with the existing worker: interrupted jobs retain unknown-outcome evidence; collaboration history is preserved without automatic replay. Failed cleanup blocks conflicting execution. Inspect Models/Activity and the existing status command. Only use `corepack pnpm research:reconcile` for explicit operator reconciliation according to the existing lifecycle guide; the web server never invokes it. Do not kill Ollama or unrelated model servers.

## Local API protections

The small Python HTTP server serves only an explicit static-file allowlist and application routes. Database names are fixed aliases, never client filesystem paths. Reads use query-only SQLite connections; writes call `QueueStore.enqueue/cancel` or `CollaborationStore.enqueue/cancel` with exact registry snapshots. The server does not claim jobs, acquire execution leases, run shell commands, or invoke model execution/reconciliation.

Mutations require same-origin Origin/Host, an HttpOnly SameSite=Strict session cookie, a CSRF header and bounded JSON. Foreign origins, DNS-rebinding Host values, duplicate/non-finite JSON, unsupported fields, invalid limits and traversal paths are rejected. Request bodies are capped at 128 KiB and socket I/O at ten seconds. CSP restricts scripts/styles/connections to this origin; model and user content is rendered as text. No arbitrary file browsing, process operations or historical editing API exists. Lock status briefly probes existing locks without creating or rewriting ownership records.

This is a local operator application, not a network deployment/authentication system. Anyone with access to this local browser session can operate its research queue.

## Development and checks

```sh
corepack pnpm research:check
node --check tools/research/web_static/app.js
corepack pnpm research:test
# Focused HTTP integration tests:
python3 -m unittest discover -s tools/research -p 'test_web.py' -v
```

Edit `tools/research/web_static/{index.html,styles.css,app.js}` and reload the browser. Restart only the web command for `web.py` changes. No frontend dependency installation is needed. Backend tests use temporary databases and controlled observations without inference or model shutdown. Existing lifecycle/collaboration tests remain intact.

## Browser verification (2026-10-11)

Verified against the real local HTTP server with Playwright browser interactions:

- Submitted Bonsai job `d449393c-52fe-4bd0-af8e-fc9396f7ef03`, scheduled for 2099-01-01 18:00 UTC. Read it back from SQLite through the API, downloaded JSON and cancelled it while pending. Literal HTML in the prompt remained text.
- Submitted collaboration `9a0ef699-bfc0-4c92-a088-a55a0e37efbb`, scheduled for 2099-01-02 18:00 UTC. Confirmed exact model snapshots and original revision; restarted only the web server, observed disconnection/reconnection and persistent records, then cancelled the experiment.
- Opened existing real hardware research output (`Hardware lifecycle cycle 9: qwen` → `lifecycle OK.`) and the verified collaboration experiments. Compared Qwen-only with round 1, inspected validation/provenance, and copied the actual revision. Abstention remained explicit.
- On a 390×844 mobile viewport, rejected excessive output tokens, corrected the bounds, scheduled Qwen job `1fa3c59b-f829-4db3-8800-fec7a1fdeecf` for 2099-01-03 18:00 UTC and cancelled it.
- Exercised all six sections, filters, preserved draft input, desktop/mobile/tablet navigation and responsive layout. A separate temporary queue/server on port 8788 verified unavailable-worker presentation; it was stopped afterward.

No new inference was requested by these future-scheduled tests. Existing running worker/model observations and previously recorded hardware outputs were real. The production worker/supervisor and its ongoing jobs were left running. Synthetic verification cancellations remain in research history. The full suite passed: 114 tests, including all 91 baseline tests and 23 new web tests. Python syntax checks, JavaScript syntax checks and `git diff --check` passed. All existing collaboration runs, events and revisions matched the baseline snapshot exactly. All 134 previously terminal research jobs were unchanged; the one previously running job completed through the existing worker. The web tests never started inference. A local verification record is preserved in `.research/web-verification.json`.

## Open Model and generation monitoring

**Open Model** opens a functional selected-model conversation workspace inside Command Center. **Open Endpoint** opens its endpoint health/discovery workspace. Neither defaults to an API listing or arbitrary external page. Test Connection is read-only and Chat with Model preserves the exact selection. Raw JSON remains a clearly labeled developer action. No server/model starts until a queued inference request is executed by the existing worker. See [COMMAND-CENTER-NAVIGATION.md](COMMAND-CENTER-NAVIGATION.md) for architecture, conversation persistence, streaming/cancellation, actual runtime verification and activation requirements.

The existing five-second poll now updates compact running-job monitors and generation details. Draft forms remain intact while monitors update. Loading, generation and release states come from persisted lifecycle transitions. Inference elapsed time begins at the recorded `BUSY` transition, not at job claim (which includes loading). Completed/interrupted timing freezes at recorded completion. Response persistence may precede model unloading: the monitor distinguishes a retained response from pending/verified resource release. Missing/stale worker evidence is labelled; an unrelated worker does not animate pending or historical jobs.

Ordinary research/collaboration calls retain their complete-response transport. Interactive chat contracts opt into bounded native Ollama NDJSON or Bonsai SSE streaming through the existing worker; this requires the updated worker process. Accurate mid-generation token counts and instantaneous throughput are therefore unavailable. Stage-level monitoring was implemented without rewriting timeout, cancellation, response persistence or lifecycle cleanup during production use. The UI never converts characters or words to tokens. Counts appear when existing services persist a provider response; the full raw usage remains in the original immutable evidence/result.

Measurement sources:

- Ollama: `prompt_eval_count` and `eval_count` are provider-reported input/output counts. Total is their explicitly labelled calculated sum. Average output rate is output count divided by provider `eval_duration` (nanoseconds), when valid.
- Prism: OpenAI-compatible `usage.prompt_tokens`, `completion_tokens` and `total_tokens` are provider-reported. A missing total can be calculated only from both reported counts. Average output rate uses the persisted inference-request duration and includes prefill; it is not instantaneous generation throughput.
- Partial usage retains available valid counts. Missing, invalid, negative, non-finite or inconsistent telemetry is unavailable. No estimated counts are supplied. A rate requires a valid count and measured positive duration.
- Collaboration totals are calculated sums over recorded role responses, with coverage shown. Readiness requests are excluded and missing call usage is not presented as a complete experiment total.

Progress is indeterminate when total work is unknown. **Output budget used** displays reported output tokens against the actual task token limit; it is never labelled task completion. Collaboration stage sequences derive from configured rounds/call limits and persisted calls, include feedback evaluation, collapse retries for logical-stage display and mark future stages conditional. Early stops mark unexecuted stages `Not Run`. Completed stages survive reload/restart because they are reconstructed from existing evidence. Cancellation remains cooperative: an in-flight call and bounded cleanup must finish before termination. No second scheduler, telemetry database, per-token writes, model polling that triggers loading, or worker restart is introduced.

### Monitoring verification (2026-10-11)

- Full suite: **147 passing tests** (114 preserved baseline tests plus 33 new projection, URL-resolution and HTTP-integration tests). Python syntax, JavaScript syntax and diff checks passed.
- Real browser controls: Open Model from job/model views, both runtime detail views, read-only Ollama endpoint popup, View Details, provenance tabs and collaboration cancellation. Desktop 1440×1000 and mobile 390×844 layouts had no document overflow.
- An isolated temporary queue/server on port 8788 exercised controlled `LOADING → BUSY → response persisted → UNLOADING → STOPPED` transitions. Counts changed from unavailable to 30 output / 12 input / 42 calculated total, with a measured-fixture 15 tokens/s average and 30/100 **output budget used**. Draft input survived polling. These are synthetic state/telemetry fixtures, not inference or a test stream.
- The isolated collaboration fixture preserved completed stages after reload, displayed the current Bonsai review, recorded cancellation through the real API and stopped animation after cancellation. Its completed draft stayed visible. The fixture server was stopped afterward.
- Real historical Qwen hardware cycle 9 displayed 5 output / 23 input / 28 calculated total. The real historical factual-explanation collaboration displayed 434 output tokens across five role responses, including Bonsai's final 98 output / 529 input / 627 reported total. These were previously completed real inference records, not new inference initiated for monitoring verification.
- An already-running Bonsai production job was observed through the existing web API: its inference elapsed display advanced from 4.5 to 9.5 seconds, its matching model details opened, and mid-call counts correctly remained unavailable. No verification inference was submitted.
- No fresh inference was dispatched. Production work was left running. All ten protected backend source fingerprints remained unchanged. All existing collaboration table hashes matched; all 162 previously terminal research tasks were unchanged, and the one previously running task completed through the existing worker. Synthetic monitoring fixtures were isolated outside the production databases.

## Skills & Tools and readability update

**Skills & Tools** provides searchable, paged access to installed skills, MCP servers, CLI tools and application capabilities. Quick Skills/MCP filters avoid browsing a large catalog. Inspect execution transport and permitted operations separately from discovery; read/copy actual skill instructions without arbitrary file access. See [COMMAND-CENTER-CAPABILITIES.md](COMMAND-CENTER-CAPABILITIES.md) for inventory, safety, model integration, exact commands and actual verification outcomes.

The interface now uses a 20px root scale, 19px body text, 17px data text, 15–16px metadata/labels and at least 44px controls. The existing typography families remain; spacing, native scheduling fields, restrained amber accents and warmer dark surfaces were refined with Impeccable. Tables and the activity timeline scroll within bounded accessible regions. Desktop retains the navigation rail; tablet/mobile use the compact section selector. No fake telemetry, font download or new frontend dependency was added.

Reference research informed hierarchy and restrained chrome: [Linear's design refresh](https://linear.app/now/behind-the-latest-design-refresh) and [Grafana dashboard guidance](https://grafana.com/docs/grafana-cloud/learn-and-build/visualizations/dashboards/build-dashboards/best-practices/). Playwright verified desktop, tablet and mobile rendering, no horizontal document overflow and no JavaScript errors in the final viewport pass. Actual skill-reading and MCP browsing controls were exercised; this is browser verification, separate from inference verification.

The application used for this verification remains at **http://127.0.0.1:8789**. Default startup remains port 8787. Production workers and supervisors were not restarted. Impeccable's source detector could not resolve the runtime `/static/styles.css` mount from static HTML; styles were inspected through the real running browser instead. Its two thick-accent warnings were corrected to thin dividers.

## Navigation repair verification

Actual Qwen and Bonsai inference completed through an isolated verification queue sharing the existing exclusive resources. Playwright observed Qwen text arriving incrementally and tested Stop on an actual Bonsai stream; both completed sessions and the cancelled session reached verified STOPPED state. Pending cancellation, selected-model navigation, discovered Gemma navigation, connection checks and restart-persisted history were exercised. Desktop/tablet/mobile layout checks passed without horizontal overflow. Provider usage and measurements are recorded in [COMMAND-CENTER-NAVIGATION.md](COMMAND-CENTER-NAVIGATION.md).

After explicit operator approval, the idle production worker was gracefully reloaded through its existing launchd service. The main workspace now verifies the current worker identity and interactive streaming/cancellation support. No active job was interrupted. The older web command on port 8787 was also gracefully reloaded after reproducing 404s for its new module/API routes; the standard address now serves matching frontend and backend code.

Final automated regression: **217 tests passed**; Python and both JavaScript module syntax checks and git whitespace checks passed. Original main-queue tasks, collaboration and capability records were preserved. Actual isolated cycle evidence is retained locally in `.research/navigation-verification.json`. No dependencies were added and no production build step exists for this standard-library/native-module application.

For Python route/worker changes, refresh alone cannot update the running Python process. Reload the corresponding service after idle as authorized; serving new JavaScript from an old web process can otherwise produce module/API 404s. Both local addresses 8787 and 8789 were checked after this repair.
