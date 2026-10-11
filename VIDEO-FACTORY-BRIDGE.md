# Video Factory local bridge v1

`tools/research/bridge.py` is a bounded JSON subprocess interface to the existing
Command Center queue and lifecycle manager. It does not execute inference, create
a model scheduler, import TypeScript, start another dashboard or bypass leases.

Request on stdin: `{"version":1,"action":"status"}`. Other actions are `submit`
(with `job`), `get` and `cancel` (with `jobId`). Output is a single JSON envelope;
errors exit nonzero. Inputs are capped at 256 KiB. Model family resolves to one
exact registered model and captures its immutable registry snapshot.

A submitted job has jobId, projectId, jobType, prompt, inputPayload,
approvedSources, model (`qwen` or `bonsai`), outputSchema (object schema or null),
maxTokens (1–8192), timeoutSeconds (1–600) and retryBudget (0–2). Job IDs are
idempotent only for the same canonical complete contract; conflicting reuse fails.
Retries are bounded content-workflow decisions by the caller, not a second model
executor. No caller-supplied endpoint, runtime command or model substitution is
accepted. Existing research rows have nullable contract_json and remain unchanged.

```sh
/usr/bin/python3 tools/research/bridge.py <<'JSON'
{"version":1,"action":"status"}
JSON
/usr/bin/python3 tools/research/cli.py status
/usr/bin/python3 tools/research/cli.py worker
python3 -m unittest discover -s tools/research -p 'test_*.py' -v
```

The worker persists inference before unloading. The bridge returns result/raw
only after the task's associated session is STOPPED with last_verified_unload and
no cleanup error. Status exposes worker, queue and lifecycle metadata, not every
private research prompt/result. Cancel reports whether the pending task was
cancelled; active inference remains bounded and follows normal owned cleanup.

Structured Qwen final inference uses the job schema as native Ollama format.
Warmup/legacy requests keep their existing semantics. Structured Bonsai final
inference uses Prism's enable_thinking=false request option and temperature=0;
warmup and legacy research behavior are preserved. The factory independently
checks complete JSON, source evidence and returned model identity.

`--render-lease` acquires the same HeavyLease, refuses any recoverable model
session, checks native residency/listener observations, then returns ready and
holds the lease until stdin receives `release` or EOF. It never performs model
recovery, unloads models or signals a process. The factory aborts rendering if
the lease subprocess exits unexpectedly.

## Durable worker and safe recovery

`tools/research/supervise.py` calls the existing worker. The companion launchd
plist is installed at `~/Library/LaunchAgents/local.command-center.research-worker.plist`.
RunAtLoad keeps queued work available after login; unexpected errors can restart,
while resource-blocked exit 3 maps to a successful stop. No automatic destructive
recovery or model-daemon termination is performed.

```sh
launchctl print gui/$(id -u)/local.command-center.research-worker
launchctl kickstart gui/$(id -u)/local.command-center.research-worker
```

On a cleanup blocker: inspect `cli.py status` and live native observations first,
preserve outputs, resolve only the recorded owned resource, then use the existing
`cli.py reconcile`. Do not kill Ollama or unrelated model processes. Restart only
after reconciliation reports verified release. A lost lease or uncertain listener
is a blocker, not permission to bypass safeguards. Include `/usr/sbin` in PATH so
macOS lsof can verify listener ownership.

The existing independent collaboration implementation and original research
results are preserved; Video Factory submits ordinary queued structured stages
and shares that same worker's coordination. The factory's own campaign validation
and human approval remain separate from any Command Center editorial judgment.
