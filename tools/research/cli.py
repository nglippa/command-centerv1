#!/usr/bin/env python3
"""CLI and independent worker for the local Qwen/Bonsai research queue."""

from __future__ import annotations

import argparse
import json
import fcntl
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from models import LocalHTTP, ModelError, discover, load_config  # type: ignore
    from queue_store import QueueStore, utc_now  # type: ignore
else:
    from .models import LocalHTTP, ModelError, discover, load_config
    from .queue_store import QueueStore, utc_now


sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle import Manager, ResourceBlocked, HeavyLease
from whodunnit_notice import print_notice
from registry import Registry, RegistryError
from collaboration_store import CollaborationStore
from collaboration import CollaborationRunner, comparison_text

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "models.json"
DEFAULT_DB = HERE.parents[1] / ".research" / "queue.sqlite3"
DEFAULT_RESOURCES = DEFAULT_DB.parent
CONTROL = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)


def display(value: object) -> str:
    return CONTROL.sub("", str(value))


def parse_time(value: str) -> str:
    if not value.strip():
        return utc_now()
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Use an ISO 8601 date and time with a timezone.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("A timezone is required (for example, +00:00 or Z).")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def lock_path(db_path: str | Path) -> Path:
    resolved = Path(db_path).expanduser().resolve()
    return Path(str(resolved) + ".worker.lock")


def acquire_lock(db_path: str | Path):
    path = lock_path(db_path)
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        path.parent.chmod(0o700)
    lock_existed = path.exists()
    handle = path.open("a+", encoding="utf-8")
    if not lock_existed:
        path.chmod(0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def worker_active(db_path: str | Path) -> bool:
    handle = acquire_lock(db_path)
    if handle is None:
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()
    return False


def run_worker(db_path: str, *, once: bool = False, poll_seconds: float = 1.0,
               registry_path: str | Path = DEFAULT_CONFIG,
               resources_dir: str | Path = DEFAULT_RESOURCES) -> int:
    handle = acquire_lock(db_path)
    if handle is None:
        print("Another worker already holds this queue's exclusive lock.", file=sys.stderr)
        return 2
    stopping = False
    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    manager = None
    try:
        manager = Manager(resources_dir, registry_path, stopping=lambda:stopping)
        with QueueStore(db_path) as store, CollaborationStore(db_path) as collaborations:
            recovered = store.recover_running()
            if recovered:
                print(f"Recovered {recovered} interrupted task(s) as failed.")
            interrupted = collaborations.recover_running()
            if interrupted: print(f"Recovered {interrupted} interrupted collaboration(s); no replay.")
            once_cutoff = utc_now() if once else None
            while not stopping:
                with manager.lease() as acquired:
                    if not acquired:
                        if once:
                            print("Heavy-model resources are leased by another worker.", file=sys.stderr)
                            return 2
                    else:
                        try:
                            manager.reconcile()
                        except ResourceBlocked as exc:
                            manager.store.db.execute("UPDATE current SET blocker=? WHERE singleton=1",(str(exc),))
                            manager.store.db.commit()
                            print(f"Model resources blocked: {display(exc)}",file=sys.stderr)
                            return 3
                        if stopping: break
                        cutoff = once_cutoff or utc_now()
                        plain = store.db.execute("SELECT scheduled_at,created_at FROM tasks WHERE state='pending' AND scheduled_at<=? ORDER BY scheduled_at,created_at LIMIT 1",(cutoff,)).fetchone()
                        collaborative = collaborations.db.execute("SELECT scheduled_at,created_at FROM collaboration_runs WHERE state='pending' AND scheduled_at<=? ORDER BY scheduled_at,created_at LIMIT 1",(cutoff,)).fetchone()
                        if collaborative and (not plain or tuple(collaborative)<tuple(plain)):
                            run = collaborations.claim_due(cutoff)
                            if run:
                                code = CollaborationRunner(collaborations,manager,stopping=lambda:stopping).run(run)
                                if code==3: return 3
                                continue
                        task = store.claim_due(once_cutoff)
                        if task is None:
                            if once: return 0
                        else:
                            if stopping:
                                store.fail(task["id"],"Worker interrupted before model dispatch; no inference was started.")
                                return 0
                            print(f"Running {task['id']} with {display(task['model_identifier'])}",flush=True)
                            def save_result(result: str, raw: str, sid: str) -> None:
                                # Persist before cleanup; SIGKILL during unload must not lose results.
                                if not store.complete(task["id"],result,raw,sid):
                                    raise RuntimeError("Running task changed before completion.")
                            outcome = manager.execute(task,on_result=save_result)
                            if outcome.error:
                                if store.fail(task["id"],outcome.error,outcome.raw_json):
                                    print(f"Task {task['id']} failed: {display(outcome.error)}",file=sys.stderr)
                                else:
                                    print(f"Post-inference persistence error; worker stopped: {display(outcome.error)}",file=sys.stderr)
                                    return 1
                            store.record_lifecycle(task["id"],outcome.session_id,outcome.cleanup_error)
                            if outcome.cleanup_error:
                                print(f"Cleanup unverified; worker stopped: {display(outcome.cleanup_error)}",file=sys.stderr)
                                return 3
                            continue
                # Waiting here schedules polling; successful state checks prove readiness/cleanup.
                time.sleep(poll_seconds)
            return 0
    except Exception as exc:
        print(f"Worker stopped: {display(exc)}",file=sys.stderr)
        return 1
    finally:
        if manager: manager.store.close()
        for sig, old in previous.items(): signal.signal(sig,old)
        handle.close()


def print_rows(rows: list[dict]) -> None:
    if not rows:
        print("No tasks.")
        return
    for row in rows:
        name = f" — {display(row['name'])}" if row.get("name") else ""
        print(
            f"{row['id']}  {row['state']:<9}  {display(row['model_identifier'])}  "
            f"{row['scheduled_at']}{name}"
        )


def model_availability(manager: Manager, entry) -> tuple[bool,str]:
    try:
        if entry.provider == "ollama":
            value=manager.d.native(entry,"GET","/api/tags",None,entry.health_timeout)
            if not isinstance(value,dict) or not isinstance(value.get("models"),list):
                raise ValueError("Malformed installed-model listing.")
            names=[x.get("name") or x.get("model") for x in value["models"] if isinstance(x,dict)]
            return (entry.identifier in names,"installed" if entry.identifier in names else "model missing; no download attempted")
        missing=[]
        if not manager.d.path_file(entry.model_path or ""): missing.append(f"weights: {entry.model_path}")
        if not manager.d.path_file(entry.runtime_path or "",True): missing.append(f"executable runtime: {entry.runtime_path}")
        if missing: return False,"missing " + "; ".join(missing)
        version=manager.d.run([entry.runtime_path,"--version"],5)
        if version.returncode or entry.runtime_version_contains not in version.stdout+version.stderr:
            return False,"runtime fork/version mismatch"
        return True,"installed locally; inference readiness unverified until load"
    except Exception as exc:
        return False,str(exc)


def select_model(config_path: str, resources_dir: str | Path = DEFAULT_RESOURCES):
    registry=Registry(config_path)
    manager=Manager(resources_dir,config_path)
    try:
        models=list(registry.entries)
        # Only configured families and endpoints, using the existing discovery filter.
        seeds=load_config(config_path)
        discovered,_=discover([x for x in seeds if x.family.lower()=="qwen"])
        for model in discovered:
            if any(e.identifier==model.identifier and e.endpoint==model.endpoint for e in models): continue
            seed=next((e for e in registry.entries if e.provider=="ollama" and e.endpoint==model.endpoint),None)
            if seed: models.append(registry.clone_discovered(seed,model.identifier,model.endpoint))
        print("Models:")
        for index,model in enumerate(models,1):
            available,reason=model_availability(manager,model)
            print(f"  {index}. {display(model.family)} / {display(model.identifier)} — {display(model.endpoint)} "
                  f"[{'available' if available else 'unavailable'}: {display(reason)}]")
        raw=input("Model number: ").strip()
        try:
            index=int(raw)
            if not 1<=index<=len(models): raise ValueError
            return models[index-1]
        except (ValueError,IndexError) as exc:
            raise ValueError("Choose one of the listed model numbers.") from exc
    finally: manager.store.close()


def add_interactive(store: QueueStore, config_path: str, resources_dir: str | Path = DEFAULT_RESOURCES) -> None:
    model = select_model(config_path, resources_dir)
    name = input("Optional name: ").strip() or None
    print("Prompt (finish with a line containing only '.'):")
    lines: list[str] = []
    while True:
        line = input()
        if line == ".":
            break
        lines.append(line)
    prompt = "\n".join(lines)
    scheduled = parse_time(input("Run time (blank for now, or timezone-aware ISO 8601): "))
    task_id = store.enqueue(
        model_family=model.family,
        model_identifier=model.identifier,
        endpoint=model.endpoint,
        prompt=prompt,
        scheduled_at=scheduled,
        max_tokens=model.max_tokens,
        timeout_seconds=model.timeout_seconds,
        name=name,
        registry_json=model.snapshot_json(),
    )
    print(f"Queued {task_id}.")


def run_console(db_path: str, config_path: str, resources_dir: str | Path = DEFAULT_RESOURCES) -> int:
    print("QWEN / BONSAI RESEARCH CONSOLE")
    try:
        with QueueStore(db_path) as store:
            while True:
                print_notice(resources_dir, display)
                print(
                    "\n1. Add research task\n2. View pending queue\n"
                    "3. View completed/failed/cancelled\n4. View result by ID\n"
                    "5. Cancel task/collaboration\n6. Exit\n"
                    "7. Schedule collaboration from JSON file\n8. Compare collaboration"
                )
                try:
                    choice = input("Choice: ").strip()
                    if choice == "1":
                        add_interactive(store, config_path, resources_dir)
                    elif choice == "2":
                        print_rows(store.list_pending())
                        with CollaborationStore(db_path) as c: print_collaborations(c.list(True))
                        print_status(db_path,config_path,resources_dir)
                    elif choice == "3":
                        print_rows(store.list_finished())
                        with CollaborationStore(db_path) as c: print_collaborations(c.list(False))
                    elif choice == "4":
                        result_id=input("Task or collaboration ID: ").strip()
                        row = store.get(result_id)
                        if row is None:
                            with CollaborationStore(db_path) as c:
                                if c.get(result_id): print(comparison_text(c.evidence(result_id)))
                                else: print("Task or collaboration not found.")
                        else:
                            print(f"State: {row['state']}")
                            print(f"Name: {display(row['name'] or '(unnamed)')}")
                            print(f"Model: {display(row['model_identifier'])} at {display(row['endpoint'])}")
                            for label, field in (("Created", "created_at"), ("Scheduled", "scheduled_at"),
                                                 ("Started", "started_at"), ("Completed", "completed_at")):
                                print(f"{label}: {row[field] or 'none'}")
                            if row.get("result") is not None:
                                print(display(row["result"]))
                            if row.get("lifecycle_error"):
                                print(f"Cleanup unverified: {display(row['lifecycle_error'])}")
                            if row.get("error") is not None:
                                print(f"Error: {display(row['error'])}")
                    elif choice == "5":
                        cancel_id=input("Task or collaboration ID: ").strip()
                        if store.cancel(cancel_id): print("Cancelled.")
                        else:
                            with CollaborationStore(db_path) as c: ok=c.cancel(cancel_id)
                            print("Collaboration cancellation recorded; an in-flight call requires cleanup." if ok else "Task is not pending or was not found.")
                    elif choice == "6":
                        return 0
                    elif choice == "7":
                        spec_path=input("Collaboration JSON file: ").strip()
                        scheduled=parse_time(input("Run time (blank for now, or timezone-aware ISO 8601): "))
                        print("Queued " + enqueue_collaboration(db_path,config_path,spec_path,scheduled))
                    elif choice == "8":
                        with CollaborationStore(db_path) as c:
                            print(comparison_text(c.evidence(input("Collaboration ID: ").strip())))
                    else:
                        print("Choose 1 through 8.")
                except (ValueError, EOFError, OSError, KeyError) as exc:
                    if isinstance(exc, EOFError):
                        print()
                        return 0
                    print(f"Error: {display(exc)}")
                except KeyboardInterrupt:
                    print()
                    return 0
    except Exception as exc:
        print(f"Could not open queue: {display(exc)}", file=sys.stderr)
        return 1



def enqueue_collaboration(db_path,config_path,spec_path,scheduled=None):
    path=Path(spec_path).expanduser()
    with path.open('rb') as handle: raw=handle.read(131073)
    if len(raw)>131072: raise ValueError("Spec file exceeds 128 KiB.")
    spec=json.loads(raw.decode('utf-8'),parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
    models={}
    for family in ('qwen','bonsai'):
        entries=[e for e in Registry(config_path).entries if e.family==family]
        if len(entries)!=1: raise ValueError(f"Expected one configured {family} model; no substitution.")
        models[family]=entries[0].export()
    with CollaborationStore(db_path) as c: return c.enqueue(spec,models,scheduled)


def print_collaborations(rows):
    if not rows: print("No collaborations.")
    for row in rows:
        print(f"{row['id']}  {row['state']:<11} {display(row['spec']['name'])}  {row['scheduled_at']}  {display(row['stop_reason'] or '')}")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Qwen/Bonsai research queue")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite queue path")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="model config JSON")
    parser.add_argument("--resources-dir", default=str(DEFAULT_RESOURCES), help="shared heavy-model coordination directory; all workers must agree")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("console")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true", help="drain currently due work and exit")
    sub.add_parser("status")
    sub.add_parser("models")
    sub.add_parser("reconcile")
    collaborate=sub.add_parser("collaborate",help="schedule bounded collaboration from a JSON spec")
    collaborate.add_argument("spec")
    collaborate.add_argument("--at",default="")
    compare=sub.add_parser("compare"); compare.add_argument("id")
    sub.add_parser("collaborations")
    cancel=sub.add_parser("cancel-collaboration"); cancel.add_argument("id")
    return parser


def print_status(db_path,config_path,resources_dir) -> None:
    print_notice(resources_dir, display)
    print("worker active" if worker_active(db_path) else "worker not running")
    with HeavyLease(resources_dir).acquire() as free:
        print("heavy-model lease: free" if free else "heavy-model lease: held")
    with QueueStore(db_path) as store: summary=store.summary()
    print("tasks: " + ", ".join(f"{state}={summary['counts'].get(state,0)}" for state in
                              ("pending","running","completed","failed","cancelled")))
    print(f"next due: {summary['next_due'] or 'none'}")
    running=summary["running"]
    print(f"current job: {running['id']}" if running else "current job: none")
    with CollaborationStore(db_path) as c:
        rows=c.list()
        counts={state:sum(r['state']==state for r in rows) for state in sorted({r['state'] for r in rows})}
        print("collaborations: " + (", ".join(f"{k}={v}" for k,v in counts.items()) or "none"))
        running=next((r for r in rows if r['state']=='running'),None)
        if running: print(f"current collaboration: {running['id']} — {display(running['spec']['name'])}")
    manager=Manager(resources_dir,config_path)
    try:
        status=manager.store.status(); history=status["history"]
        current=next((r for r in history if r["id"]==status["current"]["session_id"]),None)
        if current:
            active_session=current['state'] not in ('STOPPED','UNAVAILABLE','ERROR')
            print(f"requested model: {display(current['model']) if active_session else 'none'}")
            print(f"last/session model: {display(current['model'])}")
            for label,key in (("provider","provider"),("endpoint","endpoint"),("recorded lifecycle state","state"),
                              ("ownership","owner_mode"),("load duration (ms)","load_ms"),
                              ("inference duration (ms)","infer_ms"),("unload duration (ms)","unload_ms")):
                print(f"{label}: {display(current[key])}")
            print(f"managed model/start intent: {bool(current['owned_intent'])}")
            if current['provider']=='bonsai':
                print(f"recorded owned PID: {current['pid'] or 'none'}; birth: {current['pid_birth'] or 'none'}")
                print(f"recorded executable: {display(current['executable'] or 'none')}")
            if current['state'] in ('UNLOADING','STOPPING'):
                print(f"cleanup job: {current['task_id']}")
            for phase,diagnostics in current['diagnostics_json'].items():
                pressure=diagnostics.get('memory_pressure','unavailable').strip().splitlines()
                print(f"memory diagnostic {phase}: {display(pressure[-1] if pressure else 'unavailable')}")
        else: print("recorded lifecycle state: none")
        for key in ("last_successful_inference","last_verified_unload"):
            stamps=[r[key] for r in history if r[key]]
            print(f"{key.replace('_',' ')}: {max(stamps) if stamps else 'none'}")
        print(f"current blocker: {display(status['current']['blocker'] or 'none')}")
        for entry in manager.registry.entries:
            ok,reason=model_availability(manager,entry)
            print(f"availability {display(entry.identifier)}: {'available' if ok else 'UNAVAILABLE'} ({display(reason)})")
        live=manager.probe_live()
        print(f"live observation: {live['observed_at']}")
        for model in live['models']:
            print(f"active model/resource {display(model['model'])}: " + display(json.dumps(model.get('value') if model['ok'] else {'unverified':model['error']})))
    finally: manager.store.close()


def main(argv: list[str] | None = None) -> int:
    args=build_parser().parse_args(argv); command=args.command or "console"
    if command=="console": return run_console(args.db,args.config,args.resources_dir)
    if command=="worker": return run_worker(args.db,once=args.once,registry_path=args.config,resources_dir=args.resources_dir)
    if command=="status":
        try: print_status(args.db,args.config,args.resources_dir); return 0
        except Exception as exc: print(f"Status error: {display(exc)}",file=sys.stderr); return 1
    if command in ("collaborate","compare","collaborations","cancel-collaboration"):
        try:
            if command=="collaborate":
                print(enqueue_collaboration(args.db,args.config,args.spec,parse_time(args.at)))
            else:
                with CollaborationStore(args.db) as c:
                    if command=="compare": print(comparison_text(c.evidence(args.id)))
                    elif command=="collaborations": print_collaborations(c.list())
                    elif not c.cancel(args.id): raise ValueError("Collaboration is not pending/running or was not found.")
                    else: print("Cancellation recorded; an in-flight call still requires bounded cleanup.")
            return 0
        except Exception as exc:
            print(f"Collaboration error: {display(exc)}",file=sys.stderr);return 1
    manager=Manager(args.resources_dir,args.config)
    try:
        if command=="models":
            for entry in manager.registry.entries:
                ok,reason=model_availability(manager,entry)
                print(f"{display(entry.identifier)}: {'available' if ok else 'UNAVAILABLE'} — {display(reason)}")
            return 0
        if command=="reconcile":
            with manager.lease() as acquired:
                if not acquired: print("Resources in use; reconciliation skipped."); return 2
                try: manager.reconcile(); print("Live resources reconciled; no unresolved owned lifecycle."); return 0
                except ResourceBlocked as exc:
                    manager.store.db.execute("UPDATE current SET blocker=? WHERE singleton=1",(str(exc),)); manager.store.db.commit()
                    print(f"Resources blocked: {display(exc)}",file=sys.stderr); return 3
    finally: manager.store.close()
    raise AssertionError(command)


if __name__ == "__main__":
    raise SystemExit(main())
