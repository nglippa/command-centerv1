# Qwen / Bonsai research console

A standalone terminal console and persistent SQLite queue for local text research tasks. The worker runs one heavy model at a time, verifies inference readiness, saves the full response, and verifies cleanup before starting another job. Python 3.9+ standard library only; pnpm scripts are convenience commands.

```sh
cd ~/Projects/command-center/basic-qwen-bonsai
corepack pnpm research:console
# In a separate terminal:
corepack pnpm research:worker
# In either terminal:
corepack pnpm research:status
corepack pnpm research:models
corepack pnpm research:check
corepack pnpm research:test
```

The worker must be running and the Mac awake to execute scheduled tasks. No models are downloaded. Ollama remains running; only its managed model is unloaded. Bonsai uses the installed Prism-compatible runtime and only current worker-owned server processes are stopped.

See [operating details](tools/research/README.md) and [real hardware verification](LIFECYCLE-VERIFICATION.md).
# command-centerv1
