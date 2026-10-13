"""Fail-closed, ownership-aware lifecycle management for local heavy models."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from registry import ModelEntry, Registry, RegistryError

STATES = ("UNAVAILABLE", "STOPPED", "STARTING", "LOADING", "READY", "BUSY",
          "UNLOADING", "STOPPING", "ERROR")
TRANSITIONS = {
    None: set(STATES), "UNAVAILABLE": {"STOPPED", "ERROR"},
    "STOPPED": {"STARTING", "LOADING", "ERROR", "UNAVAILABLE"},
    "STARTING": {"LOADING", "READY", "STOPPING", "ERROR"},
    "LOADING": {"READY", "UNLOADING", "STOPPING", "ERROR"},
    "READY": {"BUSY", "UNLOADING", "STOPPING", "ERROR"},
    "BUSY": {"READY", "UNLOADING", "STOPPING", "ERROR"},
    "UNLOADING": {"STOPPED", "ERROR"}, "STOPPING": {"STOPPED", "ERROR"},
    "ERROR": {"STOPPED", "UNLOADING", "STOPPING", "ERROR"},
}


class ResourceBlocked(RuntimeError):
    pass


class LifecycleError(RuntimeError):
    def __init__(self,message: str,raw_json: str | None = None):
        super().__init__(message)
        self.raw_json=raw_json

class UnavailableError(LifecycleError):
    pass


@dataclass
class Outcome:
    result: str | None = None
    raw_json: str | None = None
    error: str | None = None
    cleanup_error: str | None = None
    session_id: str | None = None


class HeavyLease:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir).resolve() / "heavy-model.lease"

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        existed=self.path.parent.exists(); self.path.parent.mkdir(parents=True, exist_ok=True)
        if not existed: self.path.parent.chmod(0o700)
        existed=self.path.exists(); handle = self.path.open("a+")
        if not existed: self.path.chmod(0o600)
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            handle.seek(0); handle.truncate(); handle.write(f"{os.getpid()}\n"); handle.flush()
            yield True
        finally:
            handle.close()


class SessionStore:
    def __init__(self, state_dir: str | Path):
        root = Path(state_dir).resolve(); existed=root.exists(); root.mkdir(parents=True, exist_ok=True)
        if not existed: root.chmod(0o700)
        db_path=root/"lifecycle.sqlite3"; existed=db_path.exists()
        self.db = sqlite3.connect(db_path)
        if not existed: db_path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS sessions(
            id TEXT PRIMARY KEY, task_id TEXT, model TEXT NOT NULL, provider TEXT NOT NULL,
            endpoint TEXT NOT NULL, owner_mode TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
              ('UNAVAILABLE','STOPPED','STARTING','LOADING','READY','BUSY','UNLOADING','STOPPING','ERROR')),
            entry_json TEXT NOT NULL, owned_intent INTEGER NOT NULL DEFAULT 0,
            pid INTEGER, pid_birth TEXT, executable TEXT, started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, load_ms INTEGER, infer_ms INTEGER, unload_ms INTEGER,
            last_successful_inference TEXT, last_verified_unload TEXT, blocker TEXT,
            observations_json TEXT NOT NULL DEFAULT '{}', diagnostics_json TEXT NOT NULL DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            at TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, detail_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS current(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), session_id TEXT, blocker TEXT);
          INSERT OR IGNORE INTO current(singleton,session_id,blocker) VALUES(1,NULL,NULL);
        """); self.db.commit()

    @staticmethod
    def now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def create(self, entry: ModelEntry, task_id: str | None) -> str:
        sid = str(uuid.uuid4()); now = self.now()
        self.db.execute("""INSERT INTO sessions(id,task_id,model,provider,endpoint,owner_mode,state,
          entry_json,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (sid,task_id,entry.identifier,entry.provider,entry.endpoint,entry.owner_mode,"STOPPED",
           entry.snapshot_json(),now,now))
        self.db.execute("UPDATE current SET session_id=?,blocker=NULL WHERE singleton=1", (sid,))
        self.db.execute("INSERT INTO events(session_id,at,from_state,to_state,detail_json) VALUES(?,?,?,?,?)",
                        (sid,now,None,"STOPPED","{}")); self.db.commit(); return sid

    def transition(self, sid: str, state: str, detail: dict[str, Any] | None = None, **fields: Any) -> None:
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        old = row["state"] if row else None
        if state not in STATES or state not in TRANSITIONS.get(old, set()):
            raise LifecycleError(f"Invalid lifecycle transition {old!r} -> {state!r}.")
        allowed = {"pid","pid_birth","executable","load_ms","infer_ms","unload_ms",
                   "last_successful_inference","last_verified_unload","blocker",
                   "observations_json","diagnostics_json","owner_mode","owned_intent"}
        if any(key not in allowed for key in fields): raise LifecycleError("Invalid session field.")
        for key in ("observations_json","diagnostics_json"):
            if key in fields and row:
                prior=json.loads(row[key]); added=fields[key]
                prior.update(json.loads(added) if isinstance(added,str) else added)
                fields[key]=prior
        now = self.now(); columns = ["state=?","updated_at=?"] + [f"{k}=?" for k in fields]
        values = [state,now] + [json.dumps(v, sort_keys=True) if k.endswith("_json") and not isinstance(v,str) else v
                               for k,v in fields.items()] + [sid]
        self.db.execute(f"UPDATE sessions SET {','.join(columns)} WHERE id=?", values)
        self.db.execute("INSERT INTO events(session_id,at,from_state,to_state,detail_json) VALUES(?,?,?,?,?)",
                        (sid,now,old,state,json.dumps(detail or {},sort_keys=True)))
        if fields.get("blocker"):
            self.db.execute("UPDATE current SET session_id=?,blocker=? WHERE singleton=1",
                            (sid,fields["blocker"]))
        elif "blocker" in fields:
            self.db.execute("UPDATE current SET blocker=NULL WHERE singleton=1 AND session_id=?",(sid,))
        self.db.commit()

    def blocker(self) -> tuple[str | None, dict[str, Any] | None]:
        row = self.db.execute("SELECT session_id,blocker FROM current WHERE singleton=1").fetchone()
        if not row or not row["blocker"]: return None, None
        session = self.db.execute("SELECT * FROM sessions WHERE id=?", (row["session_id"],)).fetchone()
        return row["blocker"], dict(session) if session else None

    def recovery_candidate(self) -> dict[str,Any] | None:
        row=self.db.execute("""SELECT * FROM sessions WHERE blocker IS NOT NULL OR owned_intent=1
          OR state NOT IN ('STOPPED','UNAVAILABLE','ERROR') ORDER BY started_at DESC LIMIT 1""").fetchone()
        return dict(row) if row else None

    def clear_blocker(self, sid: str, observation: dict[str, Any]) -> None:
        # Recovery uses observed live absence, never a stale state as proof.
        state=self.state(sid)
        if state not in ("UNLOADING","STOPPING","ERROR","UNAVAILABLE"):
            self.transition(sid,"ERROR",{"recovery":"interrupted lifecycle reconciled against live resources"})
        self.transition(sid,"STOPPED",{"recovery_verified":observation},blocker=None,
                        owned_intent=0,last_verified_unload=self.now(),observations_json=observation)
        self.db.execute("UPDATE current SET blocker=NULL WHERE singleton=1 AND session_id=?", (sid,))
        self.db.commit()

    def status(self) -> dict[str, Any]:
        current = self.db.execute("SELECT * FROM current WHERE singleton=1").fetchone()
        history = [dict(r) for r in self.db.execute("SELECT * FROM sessions ORDER BY started_at DESC")]
        events = [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY seq")]
        for row in history:
            for key in ("entry_json","observations_json","diagnostics_json"):
                row[key]=json.loads(row[key])
        for row in events: row["detail_json"]=json.loads(row["detail_json"])
        return {"current": dict(current), "history": history, "events": events}

    def close(self) -> None: self.db.close()

    def state(self,sid:str) -> str:
        row=self.db.execute("SELECT state FROM sessions WHERE id=?",(sid,)).fetchone()
        if not row: raise LifecycleError("Unknown lifecycle session.")
        return row[0]


class Dependencies:
    """Production defaults; tests replace every effectful method."""
    def native(self, entry: ModelEntry, method: str, path: str, payload: Any, timeout: float) -> Any:
        from models import LocalHTTP, ModelError
        endpoint = entry.endpoint[:-3] if entry.endpoint.endswith("/v1") else entry.endpoint
        client = LocalHTTP(endpoint, timeout)
        body = None if payload is None else json.dumps(payload).encode()
        status, raw = client._request(method, path, body)
        if status != 200: raise ModelError(f"{path} returned HTTP {status}.", raw.decode("utf-8",errors="replace"))
        try: return json.loads(raw)
        except (json.JSONDecodeError,UnicodeDecodeError) as exc: raise ModelError(f"{path} returned malformed JSON.",raw.decode("utf-8",errors="replace")) from exc

    def complete(self, entry: ModelEntry, prompt: str, max_tokens: int) -> tuple[str,str]:
        from models import LocalHTTP, ModelError
        result,raw=LocalHTTP(entry.endpoint, entry.timeout_seconds).complete(entry.identifier,prompt,max_tokens,request_options=getattr(self,"contract_request_options",None))
        if json.loads(raw).get("model")!=entry.identifier:
            raise ModelError("Bonsai inference returned a different or missing model identity.",raw)
        return result,raw

    def path_file(self, path: str, executable: bool = False) -> bool:
        p=Path(path); return p.is_file() and p.stat().st_size > 0 and (not executable or os.access(p,os.X_OK))
    def run(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv,capture_output=True,text=True,timeout=timeout,check=False)
    def popen(self, argv: list[str], env: dict[str,str]) -> subprocess.Popen[Any]:
        wrapper=Path(__file__).with_name("owned_server.py")
        return subprocess.Popen([sys.executable,str(wrapper),json.dumps(argv)],env=env,
                                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,start_new_session=True)
    def release_start(self, process) -> None:
        process.stdin.write(b"START\n"); process.stdin.flush(); process.stdin.close()
    def lsof_pid(self, port: int) -> int | None:
        out=self.run(["lsof","-nP",f"-iTCP:{port}","-sTCP:LISTEN","-t"],2)
        lines=[x for x in out.stdout.splitlines() if x]
        if out.returncode==1 and not lines and not out.stderr.strip():return None
        if out.returncode!=0 or len(lines)!=1 or not lines[0].isdigit():
            raise LifecycleError("Listener ownership could not be determined with lsof.")
        return int(lines[0])
    def identity(self, pid: int) -> tuple[str,str] | None:
        out=self.run(["ps","-p",str(pid),"-o","lstart=,comm="],2)
        text=out.stdout.strip()
        return (text[:24],text[24:].strip()) if out.returncode==0 and len(text)>24 else None
    def diagnostics(self, pid: int | None) -> dict[str,Any]:
        data={}
        for key,argv in (("memory_pressure",["memory_pressure","-Q"]),
                         ("vm_memory_pressure",["sysctl","vm.memory_pressure"])):
            try:
                result=self.run(argv,2)
                data[key]=result.stdout[-4096:] if result.returncode==0 else "unavailable"
            except Exception: data[key]="unavailable"
        try:
            sample=self.run(["ps","-axo","pid=,ppid=,rss=,comm="],2)
            rows=[]
            for line in sample.stdout.splitlines():
                parts=line.split(None,3)
                if len(parts)==4 and ("ollama" in parts[3].lower() or "llama-server" in parts[3].lower()):
                    rows.append({"pid":int(parts[0]),"ppid":int(parts[1]),"rss_kb":int(parts[2]),"executable":parts[3]})
            data["inference_processes"]=rows if sample.returncode==0 else "unavailable"
        except Exception: data["inference_processes"]="unavailable"
        try:
            sample=self.run(["vm_stat"],2)
            data["vm_stat"]=sample.stdout[-8192:] if sample.returncode==0 else "unavailable"
        except Exception: data["vm_stat"]="unavailable"
        if pid:
            try: data["rss_kb"]=self.run(["ps","-p",str(pid),"-o","rss="],2).stdout.strip()
            except Exception: data["rss_kb"]="unavailable"
        return data
    def sleep(self, seconds: float) -> None: time.sleep(seconds)
    def monotonic(self) -> float: return time.monotonic()


class Manager:
    def __init__(self, state_dir: str|Path, registry_path: str|Path,
                 stopping: Callable[[],bool]=lambda:False, deps: Dependencies|None=None):
        self.state_dir=Path(state_dir).resolve(); self.registry=Registry(registry_path)
        self.store=SessionStore(self.state_dir); self.stopping=stopping; self.d=deps or Dependencies()
        self._process: Any=None; self._session: str|None=None; self._entry: ModelEntry|None=None
        self._ollama_owned_intent=False; self._ollama_external=False
        self._bonsai_external=False; self._bonsai_owned_intent=False
        self._lease_held=False; self._reconciled=False
        self._last_ps_raw:dict[str,Any]={}; self._infer_started=0.; self._load_started=0.
        self._loaded_before_raw:dict[str,Any]={}

    @contextmanager
    def lease(self):
        with HeavyLease(self.state_dir).acquire() as acquired:
            self._lease_held=acquired
            try: yield acquired
            finally: self._lease_held=False; self._reconciled=False

    def _ps(self, entry: ModelEntry, timeout:float|None=None) -> list[str]:
        value=self.d.native(entry,"GET","/api/ps",None,timeout or entry.health_timeout)
        self._last_ps_raw=value if isinstance(value,dict) else {"invalid":type(value).__name__}
        if not isinstance(value,dict) or not isinstance(value.get("models"),list):
            raise LifecycleError("Ollama /api/ps did not return a models list.")
        names=[]
        for item in value["models"]:
            if not isinstance(item,dict): raise LifecycleError("Ollama /api/ps model is malformed.")
            name=item.get("name") or item.get("model")
            if not isinstance(name,str) or not name: raise LifecycleError("Ollama /api/ps model has no exact name.")
            context=item.get("context_length")
            if name==entry.identifier and context is not None and context != entry.context_window:
                raise LifecycleError("Loaded Ollama context length differs from the configured value.")
            names.append(name)
        return names

    def reconcile(self) -> None:
        if not self._lease_held: raise LifecycleError("Global heavy-model lease is required.")
        session=self.store.recovery_candidate()
        if not session: self._reconciled=True; return
        blocker=session.get("blocker") or f"Interrupted lifecycle state {session['state']}"
        try: entry=ModelEntry.from_dict(json.loads(session["entry_json"]))
        except Exception as exc: raise ResourceBlocked(f"{blocker}; persisted model snapshot is invalid") from exc
        self._preflight(entry)
        if entry.provider=="ollama":
            try:
                loaded=self._ps(entry)
                if session["owned_intent"]:
                    self._ollama_unload(entry); loaded=self._ps(entry)
                if loaded: raise ResourceBlocked(f"Loaded external model(s): {loaded!r}")
            except ResourceBlocked: raise
            except Exception as exc: raise ResourceBlocked(f"Ollama state remains unverified: {exc}") from exc
            self.store.clear_blocker(session["id"],{"reconciled_ps":self._last_ps_raw}); self.reconcile(); return
        pid=session.get("pid"); live=self.d.identity(pid) if pid else None
        observations=json.loads(session['observations_json'])
        if not pid and session['owned_intent'] and not observations.get('launch_guarded'):
            raise ResourceBlocked("Interrupted unguarded Bonsai start has no durable PID; operator must resolve possible orphan.")
        port=urlsplit(entry.endpoint).port or 80; listener=self.d.lsof_pid(port)
        expected=(session.get("pid_birth"),session.get("executable"))
        if live and (not expected[0] or live[0]==expected[0]) and live!=expected:
            raise ResourceBlocked("Bonsai process is live with incomplete or changing executable identity; no signal was sent.")
        if live and live != expected:
            if listener: raise ResourceBlocked("Stale PID was reused; endpoint ownership is unverified.")
            self.store.clear_blocker(session["id"],{"pid_reused":True,"listener":None}); self.reconcile(); return
        if live or listener: raise ResourceBlocked("Verified owned Bonsai orphan requires operator cleanup.")
        self.store.clear_blocker(session["id"],{"pid_gone":True,"listener":None}); self.reconcile()

    def execute(self, task: dict[str,Any], on_result: Callable[[str,str,str],None] | None = None) -> Outcome:
        if not self._lease_held or not self._reconciled:
            raise LifecycleError("Acquire the global lease and reconcile before execute.")
        self._reconciled=False
        out=Outcome()
        self._infer_started=0.; self._load_started=0.
        self._loaded_before_raw={}; self._last_ps_raw={}
        self._process=None; self._ollama_owned_intent=False
        self._ollama_external=False; self._bonsai_external=False; self._bonsai_owned_intent=False
        try: entry=self.registry.resolve_task(task)
        except RegistryError as exc:
            out.error=str(exc)
            out.raw_json=getattr(exc,"raw_json",out.raw_json); self._reconciled=False; return out
        sid=self.store.create(entry,str(task.get("id") or "")); out.session_id=sid
        self._session=sid; self._entry=entry; started=self.d.monotonic()
        try:
            if self.stopping(): raise LifecycleError("Worker interrupted before model dispatch.")
            if entry.provider=="ollama": result,raw=self._ollama(entry,task)
            else: result,raw=self._bonsai(entry,task)
            out.result=result; out.raw_json=raw
            if on_result: on_result(result,raw,sid)
            infer_start=self._infer_started or started
            self.store.transition(sid,"READY",infer_ms=int((self.d.monotonic()-infer_start)*1000),
                                  last_successful_inference=self.store.now())
        except Exception as exc:
            out.error=str(exc)
            out.raw_json=getattr(exc,"raw_json",out.raw_json)
            if isinstance(exc,UnavailableError):
                if self.store.state(sid)!="UNAVAILABLE": self.store.transition(sid,"UNAVAILABLE",{"error":str(exc)})
            else:
                fields={"blocker":str(exc)} if isinstance(exc,ResourceBlocked) else {}
                self.store.transition(sid,"ERROR",{"error":str(exc)},**fields)
        finally:
            try:
                if entry.provider=="ollama": self._cleanup_ollama(entry)
                else: self._cleanup_bonsai(entry)
            except Exception as exc:
                out.cleanup_error=str(exc)
                self.store.transition(sid,"ERROR",{"cleanup_error":str(exc)},blocker=str(exc))
        self._reconciled=False
        return out

    def _preflight(self, entry:ModelEntry) -> None:
        if entry.provider=="ollama":
            for other in self.registry.entries:
                if other.provider=="bonsai":
                    try: listener=self.d.lsof_pid(urlsplit(other.endpoint).port or 80)
                    except Exception as exc: raise ResourceBlocked(f"Bonsai listener state is unverified: {exc}") from exc
                    if listener is not None: raise ResourceBlocked(f"External Bonsai listener PID {listener} blocks Ollama load.")
        else:
            for other in self.registry.entries:
                if other.provider=="ollama":
                    try: loaded=self._ps(other)
                    except Exception as exc: raise ResourceBlocked(f"Ollama residency is unverified: {exc}") from exc
                    if loaded: raise ResourceBlocked(f"Loaded Ollama model(s) block Bonsai start: {loaded!r}")

    def probe_live(self) -> dict[str,Any]:
        """Read-only live observations for status; never starts, loads, or unloads."""
        observations=[]
        for entry in self.registry.entries:
            try:
                if entry.provider=="ollama":
                    value={"loaded":self._ps(entry),"raw":self._last_ps_raw}
                else:
                    listener=self.d.lsof_pid(urlsplit(entry.endpoint).port or 80)
                    value={"listener_pid":listener,"serving_model":None}
                    if listener:
                        health=self.d.native(entry,"GET","/health",None,entry.health_timeout)
                        models=self.d.native(entry,"GET","/v1/models",None,entry.health_timeout)
                        props=self.d.native(entry,"GET","/props",None,entry.health_timeout)
                        ids=[x.get("id") for x in models.get("data",[]) if isinstance(x,dict)]
                        if entry.identifier in ids and Path(props.get("model_path","")).resolve()==Path(entry.model_path).resolve():
                            value["serving_model"]=entry.identifier
                        value["health"]=health.get("status")
                        value["ownership"]="external or recorded owned process; see session PID/birth"
                        if not value["serving_model"]: value["identity"]="unverified"
                observations.append({"model":entry.identifier,"provider":entry.provider,"ok":True,"value":value})
            except Exception as exc:
                observations.append({"model":entry.identifier,"provider":entry.provider,"ok":False,"error":str(exc)})
        return {"observed_at":self.store.now(),"models":observations}

    def _ollama(self, e:ModelEntry, task:dict[str,Any]) -> tuple[str,str]:
        tags=self.d.native(e,"GET","/api/tags",None,e.health_timeout)
        if not isinstance(tags,dict) or not isinstance(tags.get("models"),list):
            raise LifecycleError("Ollama /api/tags did not return a models list.")
        available=[]
        for item in tags["models"]:
            if not isinstance(item,dict) or not isinstance(item.get("name") or item.get("model"),str):
                raise LifecycleError("Ollama /api/tags model is malformed.")
            available.append(item.get("name") or item.get("model"))
        if e.identifier not in available: raise UnavailableError("Exact Ollama model is not installed; no pull attempted.")
        self._preflight(e)
        loaded=self._ps(e)
        self._loaded_before_raw=self._last_ps_raw
        if loaded:
            self._ollama_external=True
            raise LifecycleError(f"External loaded model(s) block managed load: {loaded!r}")
        self.store.transition(self._session,"LOADING",{"owned_intent_persisted":True},owned_intent=1,
                              observations_json={"loaded_before":self._loaded_before_raw},
                              diagnostics_json={"before":self.d.diagnostics(None)})
        self._ollama_owned_intent=True
        self._load_started=self.d.monotonic()
        deadline=self._load_started+e.load_timeout
        def remaining():
            left=deadline-self.d.monotonic()
            if left<=0: raise LifecycleError("Ollama load deadline expired.")
            return left
        payload={"model":e.identifier,"prompt":"","keep_alive":600,"stream":False,
                 "options":{"num_ctx":e.context_window}}
        value=self.d.native(e,"POST","/api/generate",payload,remaining())
        if not isinstance(value,dict) or value.get("done") is not True or value.get("model")!=e.identifier:
            raise LifecycleError("Ollama load response was not a completed exact-model response.")
        loaded=self._ps(e,min(e.health_timeout,remaining()))
        if loaded != [e.identifier]: raise LifecycleError(f"Unexpected loaded model set: {loaded!r}")
        if self.stopping(): raise LifecycleError("Stopped during Ollama load.")
        warm={"model":e.identifier,"messages":[{"role":"user","content":"Reply OK"}],"think":False,
              "stream":False,"keep_alive":600,"options":{"num_ctx":e.context_window,"num_predict":8}}
        ready=self.d.native(e,"POST","/api/chat",warm,remaining())
        message=ready.get("message",{}).get("content") if isinstance(ready,dict) else None
        if not isinstance(ready,dict) or ready.get("done") is not True or ready.get("model")!=e.identifier or not isinstance(message,str) or not message:
            raise LifecycleError("Ollama warmup did not prove exact-model readiness.")
        self.store.transition(self._session,"READY",load_ms=int((self.d.monotonic()-self._load_started)*1000),
                              observations_json={"loaded_before":self._loaded_before_raw,
                                                 "loaded_ready":self._last_ps_raw},
                              diagnostics_json={"ready":self.d.diagnostics(None)})
        if self.stopping(): raise LifecycleError("Stopped before inference.")
        self.store.transition(self._session,"BUSY")
        self._infer_started=self.d.monotonic()
        from chat_stream import is_interactive_chat, run_chat
        if is_interactive_chat(task):
            db_path=getattr(self.d,"generation_db_path",None)
            if not db_path: raise LifecycleError("Interactive chat generation store is not configured.")
            return run_chat(e,task,db_path,self.stopping)
        request={"model":e.identifier,"messages":[{"role":"user","content":task["prompt"]}],"think":False,
                 "stream":False,"keep_alive":600,"options":{"num_ctx":e.context_window,
                 "num_predict":int(task.get("max_tokens",e.max_tokens))}}
        if task.get("contract_json"):
            contract=json.loads(task["contract_json"])
            schema=contract.get("outputSchema")
            if schema is not None:
                if not isinstance(schema,dict) or schema.get("type")!="object":
                    raise LifecycleError("Structured output schema must be an object schema.")
                request["format"]=schema
                request["options"].update({"temperature":0,"seed":42})
        value=self.d.native(e,"POST","/api/chat",request,float(task.get("timeout_seconds",e.timeout_seconds)))
        content=value.get("message",{}).get("content") if isinstance(value,dict) else None
        if not isinstance(value,dict) or value.get("done") is not True or value.get("model")!=e.identifier or not isinstance(content,str) or not content:
            raise LifecycleError("Ollama inference response was incomplete, wrong-model, or empty.",json.dumps(value,ensure_ascii=False))
        return content,json.dumps(value,ensure_ascii=False)

    def _ollama_unload(self,e:ModelEntry) -> None:
        deadline=self.d.monotonic()+e.unload_timeout
        value=self.d.native(e,"POST","/api/generate",{"model":e.identifier,"prompt":"","keep_alive":0,"stream":False},e.unload_timeout)
        if not isinstance(value,dict) or value.get("done") is not True \
                or value.get("model")!=e.identifier \
                or (value.get("done_reason") is not None and value.get("done_reason")!="unload"):
            raise LifecycleError("Ollama unload was not acknowledged.")
        while self.d.monotonic()<deadline:
            loaded=self._ps(e,min(e.health_timeout,max(.001,deadline-self.d.monotonic())))
            if e.identifier not in loaded:
                if loaded: raise LifecycleError(f"External model appeared during unload: {loaded!r}")
                return
            self.d.sleep(.05)
        raise LifecycleError("Ollama model remained loaded after unload deadline.")

    def _cleanup_ollama(self,e:ModelEntry) -> None:
        if not self._session: return
        if self._ollama_external:
            raise LifecycleError("Externally loaded Ollama model remains resident; managed switching is blocked.")
        if not self._ollama_owned_intent: return
        self.store.transition(self._session,"UNLOADING")
        started=self.d.monotonic()
        self._ollama_unload(e)
        self.store.transition(self._session,"STOPPED",last_verified_unload=self.store.now(),owned_intent=0,
                              unload_ms=int((self.d.monotonic()-started)*1000),
                              observations_json={"loaded_before":self._loaded_before_raw,
                                                 "loaded_after":self._last_ps_raw},
                              diagnostics_json={"after":self.d.diagnostics(None)})

    def _bonsai(self,e:ModelEntry,task:dict[str,Any]) -> tuple[str,str]:
        if not self.d.path_file(e.model_path or "") or not self.d.path_file(e.runtime_path or "",True):
            self.store.transition(self._session,"UNAVAILABLE")
            raise UnavailableError(f"Bonsai unavailable (missing dependency): weights {e.model_path!r} must exist and be nonempty; runtime {e.runtime_path!r} must exist and be executable.")
        version=self.d.run([e.runtime_path,"--version"],5)
        if version.returncode or e.runtime_version_contains not in (version.stdout+version.stderr):
            raise UnavailableError("Bonsai runtime version does not contain the configured fork pin.")
        self._preflight(e)
        command=list(e.startup_command); port=str(urlsplit(e.endpoint).port or 80)
        def arg_after(*flags:str) -> str|None:
            for flag in flags:
                if flag in command and command.index(flag)+1<len(command): return command[command.index(flag)+1]
            return None
        if command[0] != e.runtime_path or arg_after("-m","--model") != e.model_path \
                or arg_after("--alias") != e.identifier or arg_after("--host") != "127.0.0.1" \
                or arg_after("--port") != port or any("http://" in x or "https://" in x for x in command):
            raise LifecycleError("Bonsai startup command does not exactly bind the configured local runtime/model/alias/endpoint.")
        port=urlsplit(e.endpoint).port or 80; listener=self.d.lsof_pid(port)
        self._load_started=self.d.monotonic()
        if not listener and e.owner_mode=="external":
            raise UnavailableError("Configured external Bonsai server is unavailable; worker will not start it.")
        if listener:
            self._bonsai_external=True
            if not e.reuse_external: raise ResourceBlocked("Bonsai endpoint is externally occupied; reuse is disabled.")
            self.store.transition(self._session,"STARTING",owner_mode="external")
        else:
            self.store.transition(self._session,"STARTING",{"owned_intent":True,"launch_guarded":True},
                                  owned_intent=1,observations_json={"launch_guarded":True})
            self._bonsai_owned_intent=True
            env={k:os.environ[k] for k in ("PATH","TMPDIR","LANG") if k in os.environ}
            env.update({"HF_HUB_OFFLINE":"1","TRANSFORMERS_OFFLINE":"1"})
            self._process=self.d.popen(list(e.startup_command),env)
            identity=self.d.identity(self._process.pid)
            if not identity: raise LifecycleError("Could not record Bonsai process identity.")
            self._load_started=self.d.monotonic()
            # The wrapper cannot exec/load until this transaction has committed.
            self.store.transition(self._session,"LOADING",pid=self._process.pid,pid_birth=identity[0],owned_intent=1,
                                  executable=e.runtime_path)
            self.d.release_start(self._process)
            self.store.db.execute("UPDATE sessions SET diagnostics_json=? WHERE id=?",
                                  (json.dumps({"before":self.d.diagnostics(self._process.pid)}),self._session))
            self.store.db.commit()
        deadline=self.d.monotonic()+e.load_timeout
        while self.d.monotonic()<deadline:
            if self.stopping(): raise LifecycleError("Stopped during Bonsai startup.")
            if self._process and self._process.poll() is not None: raise LifecycleError(f"Bonsai server crashed during startup (exit {self._process.poll()}).")
            if self._process and self.d.lsof_pid(port) not in (self._process.pid,): self.d.sleep(.05); continue
            try:
                def remaining() -> float:
                    left=deadline-self.d.monotonic()
                    if left<=0: raise LifecycleError("Bonsai readiness deadline expired.")
                    return min(e.health_timeout,left)
                health=self.d.native(e,"GET","/health",None,remaining())
                models=self.d.native(e,"GET","/v1/models",None,remaining())
                props=self.d.native(e,"GET","/props",None,remaining())
                ids=[x.get("id") for x in models.get("data",[])]
                if health.get("status")=="ok" and e.identifier in ids and Path(props.get("model_path","")).resolve()==Path(e.model_path).resolve(): break
            except Exception: pass
            self.d.sleep(.05)
        else: raise LifecycleError("Bonsai readiness deadline expired.")
        left=deadline-self.d.monotonic()
        if left<=0: raise LifecycleError("Bonsai readiness deadline expired.")
        if self._process:
            identity=self.d.identity(self._process.pid)
            recorded=self.store.db.execute("SELECT pid_birth,executable FROM sessions WHERE id=?",(self._session,)).fetchone()
            if not identity or identity[0]!=recorded['pid_birth'] or Path(identity[1]).resolve()!=Path(e.runtime_path).resolve():
                raise LifecycleError("Owned Bonsai process identity changed before readiness.")
        left=deadline-self.d.monotonic()
        if left<=0: raise LifecycleError("Bonsai readiness deadline expired.")
        warm, _=self.d.complete(replace(e,timeout_seconds=left),"Reply OK",8)
        if not warm.strip(): raise LifecycleError("Bonsai warmup returned empty content.")
        self.store.transition(self._session,"READY",load_ms=int((self.d.monotonic()-self._load_started)*1000),
                              observations_json={"health":health,"models":models,"props":props},
                              diagnostics_json={"ready":self.d.diagnostics(self._process.pid if self._process else listener)})
        if self.stopping(): raise LifecycleError("Stopped before inference.")
        self.store.transition(self._session,"BUSY")
        self._infer_started=self.d.monotonic()
        from chat_stream import is_interactive_chat, run_chat
        if is_interactive_chat(task):
            db_path=getattr(self.d,"generation_db_path",None)
            if not db_path: raise LifecycleError("Interactive chat generation store is not configured.")
            return run_chat(e,task,db_path,self.stopping)
        prior_options=getattr(self.d,"contract_request_options",None)
        if task.get("contract_json"):
            self.d.contract_request_options={"chat_template_kwargs":{"enable_thinking":False},"temperature":0}
        try:
            return self.d.complete(replace(e,timeout_seconds=float(task.get("timeout_seconds",e.timeout_seconds))),task["prompt"],int(task.get("max_tokens",e.max_tokens)))
        finally:
            self.d.contract_request_options=prior_options

    def _cleanup_bonsai(self,e:ModelEntry) -> None:
        if not self._session: return
        if self._bonsai_external: # external reuse is never stopped and remains a blocker
            raise LifecycleError("Externally owned Bonsai remains resident; managed switching is blocked.")
        if not self._process:
            if not self._bonsai_owned_intent:return
            listener=self.d.lsof_pid(urlsplit(e.endpoint).port or 80)
            if listener is not None: raise LifecycleError("Bonsai start failed and endpoint ownership is unverified.")
            self.store.transition(self._session,"STOPPED",owned_intent=0,last_verified_unload=self.store.now())
            return
        self.store.transition(self._session,"STOPPING")
        started=self.d.monotonic()
        process=self._process
        if process.poll() is None:
            deadline=self.d.monotonic()+e.unload_timeout
            process.terminate()
            try: process.wait(timeout=max(.001,e.unload_timeout*.75))
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=max(.001,deadline-self.d.monotonic()))
        listener=self.d.lsof_pid(urlsplit(e.endpoint).port or 80)
        if listener is not None: raise LifecycleError(f"Bonsai endpoint remains occupied by PID {listener}; model switching is blocked.")
        self.store.transition(self._session,"STOPPED",last_verified_unload=self.store.now(),owned_intent=0,
                              unload_ms=int((self.d.monotonic()-started)*1000),
                              observations_json={"process_returncode":process.poll(),"listener_after":listener},
                              diagnostics_json={"after":self.d.diagnostics(process.pid)})
