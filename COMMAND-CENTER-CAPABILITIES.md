# Installed capabilities in Command Center

Open **Skills & Tools** in the sidebar (or mobile navigation). Quick filters show Skills and MCP servers; search by purpose, operation or workflow phase. Each entry explains use cases, exclusions, dependencies, transport, permissions and source. Select a skill and **Read instructions** to retrieve its actual bounded documentation. MCP technical details distinguish discovered JSON schemas from permitted operations. Discovery never grants execution permission.

## Commands

```sh
cd ~/Projects/command-center/basic-qwen-bonsai
corepack pnpm research:capabilities
corepack pnpm research:web --port 8789
# Relevant capabilities without inference:
python3 tools/research/capability_run.py route 'Plan a cinematic opening' --model qwen
# Opt-in bounded model/tool workflow (requires existing independent worker):
corepack pnpm research:capability-run --model qwen --task 'Read relevant video guidance and inspect Command Center repository status' --max-calls 4 --runtime 600
corepack pnpm research:capability-run --model bonsai --task 'Read relevant editorial guidance and inspect registered project metadata' --max-calls 4 --runtime 600
# Evidence and interrupted parent reconciliation:
python3 tools/research/capability_run.py show RUN_ID
python3 tools/research/capability_run.py recover
```

For permitted media metadata, explicitly approve a canonical regular media file using `--source /absolute/path/clip.mp4`. The model uses the returned source ID, never arbitrary paths. Existing console, worker, status, models, collaboration and production bridge commands remain unchanged.

## Inventory and actual execution

The verification snapshot contains **1,617 distinct skill documents** across **4,400 source paths**, including shared skills, profile skills, repository guidance and inactive cached plugin versions. Identical documents share one entry with source aliases; different versions remain distinguishable. **1,615** documents fit the bounded reader; two oversized documents remain inventory-only. This is an installed-document inventory, not a claim that every skill is enabled in the active agent profile.

There are **25 MCP server identities**, **24 CLI inventory entries** (20 executable paths found; 15 safe version probes succeeded), three native adapters, one local service, two existing workflows and one validator. Configuration alone is not health. Installed executable paths, probe outcomes and model-callable adapters are reported separately.

The narrow local codebase-memory MCP client verified **17 actual JSON tool schemas** against version 0.11.0. Only **list_projects**, bounded metadata without indexing, deletion or source reads, is permitted. Other configured MCP servers are discovery/agent-only unless independently integrated. An optional agent-host metadata snapshot records operation names and descriptions with absent schemas explicitly identified; it does not grant local-model access.

Four execution adapters are enabled: approved-source FFprobe metadata, fixed Command Center repository status, existing Command Center status, and MCP project metadata. Video Factory status is described but unavailable until its existing fixed local endpoint passes contract verification. Other installed CLIs are not arbitrary model-callable shell commands.

Examples: cinematic-video supplies shot/storytelling guidance; it does not produce video pixels. video-editing supplies editorial guidance. analyze-video-editing concerns reference and render analysis. find-broll requires licensing/source provenance. Each Remotion skill is routed separately: render, captions, maps, upgrades and creation are distinct tasks. Development, design, testing, research and other documented domains are retained; task routing supplies a bounded relevant subset rather than all instructions.

## Model orchestration and boundaries

This is an opt-in producer using the existing verified inference bridge and durable queue. Qwen receives planning/generation instructions; Bonsai receives editorial-review instructions. A bounded structured response requests list/describe/read/invoke/finish. Full skills are retrieved only on demand from registry-controlled paths; symlink escapes, oversized reads and unknown arguments fail closed. Guidance is treated as untrusted reference material, not system policy or authorization.

Every model call remains an ordinary queued job. Tools execute only after the existing bridge confirms verified model release. No inference adapter, scheduler, resource lease or production worker was replaced. The parent tool workflow persists original tasks, prompts, raw model responses, selected capability descriptions, validated requests, tool results and errors in append-only events in the existing SQLite database.

Default limits: four model calls, 600 seconds and 1,024 output tokens per call. Hard limits: eight calls, 1,800 seconds and 2,048 output tokens. Context and instruction slices have bounded character sizes, explicitly not token estimates. Subprocess/MCP requests have timeouts and output limits. Unsupported schemas/operations and destructive, paid, publishing or external-write requests are rejected. No arbitrary model-generated commands or MCP launch arguments are executed.

Parent recovery verifies owner process identity and marks stale runs interrupted without replaying actions or killing processes. Child jobs follow the existing queue's recovery semantics. Cancelling a parent requests existing child cancellation where eligible; active inference is not forcibly killed. Tool workflow submission/evidence currently uses the CLI; browser capability browsing is read-only. Existing autonomous Video Factory prompts are not silently rewritten to opt into tool use.

## Runtime observations

- Qwen: `qwen3:14b-q8_0`, local Ollama at `http://127.0.0.1:11434/v1`.
- Bonsai: `Ternary-Bonsai-2-27B-PQ2_0`, the existing Prism-compatible owned llama-server runtime at port 8080. No generic runtime substitution was made.
- ComfyUI: `/Users/nicholaslippa/ComfyUI-Installs/Nicholas Lippa/ComfyUI`, version 0.39.2. No model weights or active generation endpoint were found; workflow templates do not prove runnable generation. No generation operation is executable. A future verified generation adapter must acquire the same heavy-resource lease and verify release. No Comfy service, weights or benchmarks were started/downloaded.
- Apify: installed agent plugin/configuration; no callable paid/API transport for local models. No Actors or paid requests executed.
- FFmpeg/FFprobe 9.0.2: installed; actual FFprobe execution verified on a synthetic one-second H264 fixture.
- Whisper CLI 1.9.5: installed; no local transcription weights found in inspected paths, no callable transcription adapter. yt-dlp: absent.

## Verification — 13 October 2026

**190 tests passed**, including the original 147 tests and 43 new discovery/execution/web tests. Python syntax, JavaScript syntax and git whitespace checks passed. Controlled tests cover safe reads, routing, schemas, unavailable tools, permission and output bounds, both roles, bridge compatibility, cleanup failure, time budgets, cancellation, stale-owner recovery and persisted provenance.

Real hardware produced **eight successful inference calls** and one Qwen loading failure through the unchanged supervised worker. Qwen actually read installed remotion-render instructions and requested FFprobe; FFprobe returned H264, 128×72, 25fps, duration 1 second. That first run exhausted its three-call budget without a final answer. A later attempt exposed a redundant skill-ID argument (now safely validated), followed by an Ollama HTTP 500 loading timeout. No complete Qwen parent run is claimed.

Bonsai completed a three-call **read skill → request actual MCP project metadata → final response** workflow in 152.232 seconds. Its leading thinking marker required reuse of the existing strict collaboration parser. Project metadata retrieval did not read or modify the listed repositories. Every successful model call and the failed Qwen startup finished with verified release; no resource blocker remained. No successful concurrent heavy-model residency is claimed or needed.

Example Qwen durations (load / inference / unload): 113.519 / 17.091 / 0.053 seconds, 23.670 / 14.337 / 0.047 seconds, 9.708 / 21.234 / 0.135 seconds. A separate Bonsai call measured 10.005 / 19.238 / 0.543 seconds. These are observed cycles, not expected performance guarantees. Qwen retry memory-pressure diagnostics showed approximately 8% free memory; further inference was stopped rather than repeatedly competing for memory. Diagnostic pressure is not exact GPU-memory reclamation evidence.

Real run IDs remain retrievable with `show`: Qwen `cap-5e2be029-f7a8-4c9a-b7a0-692af34faf0a`; completed Bonsai `cap-2c0934c9-4fb2-4f2e-ac97-76c09bb5e104`; Qwen retry `cap-578e7edb-709d-4cb7-9efe-3d8e5a2d0a72`.

Browser verification exercised search, filters, entry selection, skill reading and MCP details. Desktop 1440px, tablet 1024px and mobile 390px layouts were inspected in Playwright. Existing research and collaboration records remain accessible. All 228 original task rows, all original collaboration records and eight protected backend files match their baseline hashes. Video Factory remains clean. Capability verification used baseline `3f814f3372e77fe03f1b9b0fb5ac933fe6d3e026`; no commit or push was performed during that implementation phase.

Missing models/services, extra MCP executors and production-stage adoption require explicit configuration and scoped adapters. The registry makes these gaps visible; it does not claim universal execution merely from installed documentation.
