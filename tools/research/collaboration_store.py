"""Append-only SQLite evidence store for bounded model collaboration."""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime,timezone
from pathlib import Path
from typing import Any

from validation import normalize_spec

STATES=("pending","running","completed","failed","cancelled","interrupted","abstained","rejected")

def utc_now()->str:return datetime.now(timezone.utc).isoformat(timespec="microseconds")

def _finite(value:Any,depth:int=0)->None:
    if depth>20:raise ValueError("JSON nesting is too deep.")
    if isinstance(value,float) and not math.isfinite(value):raise ValueError("JSON numbers must be finite.")
    if isinstance(value,dict):
        if any(not isinstance(k,str) for k in value):raise ValueError("JSON keys must be strings.")
        for item in value.values():_finite(item,depth+1)
    elif isinstance(value,list):
        for item in value:_finite(item,depth+1)
    elif value is not None and type(value) not in (str,int,float,bool):raise ValueError("Value is not JSON serializable.")

def _json(value:Any)->str:_finite(value);return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)

class CollaborationStore:
    def __init__(self,path:str|Path):
        self.path=Path(path).expanduser().resolve();parent_existed=self.path.parent.exists();existed=self.path.exists()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if not parent_existed:self.path.parent.chmod(0o700)
        self.db=sqlite3.connect(self.path,timeout=5);self.db.row_factory=sqlite3.Row
        if not existed:self.path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL");self.db.execute("PRAGMA busy_timeout=5000");self.db.execute("PRAGMA foreign_keys=ON")
        states=",".join(repr(x) for x in STATES)
        self.db.executescript(f"""
        CREATE TABLE IF NOT EXISTS collaboration_runs(
          id TEXT PRIMARY KEY,state TEXT NOT NULL CHECK(state IN ({states})),created_at TEXT NOT NULL,
          scheduled_at TEXT NOT NULL,started_at TEXT,completed_at TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,
          spec_json TEXT NOT NULL,models_json TEXT NOT NULL,original_json TEXT NOT NULL,
          outcome_json TEXT,stop_reason TEXT,accepted_revision_id TEXT);
        CREATE TABLE IF NOT EXISTS collaboration_events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL REFERENCES collaboration_runs(id),
          at TEXT NOT NULL,kind TEXT NOT NULL,data_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS collaboration_revisions(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES collaboration_runs(id),at TEXT NOT NULL,
          label TEXT NOT NULL,artifact_json TEXT NOT NULL,parent_id TEXT,provenance_json TEXT,
          FOREIGN KEY(parent_id) REFERENCES collaboration_revisions(id));
        CREATE INDEX IF NOT EXISTS collaboration_due ON collaboration_runs(state,scheduled_at,created_at);
        CREATE TRIGGER IF NOT EXISTS collab_immutable_run BEFORE UPDATE OF spec_json,models_json,original_json,created_at ON collaboration_runs
          BEGIN SELECT RAISE(ABORT,'immutable collaboration inputs'); END;
        CREATE TRIGGER IF NOT EXISTS collab_no_event_update BEFORE UPDATE ON collaboration_events BEGIN SELECT RAISE(ABORT,'events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS collab_no_event_delete BEFORE DELETE ON collaboration_events BEGIN SELECT RAISE(ABORT,'events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS collab_no_revision_update BEFORE UPDATE ON collaboration_revisions BEGIN SELECT RAISE(ABORT,'revisions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS collab_no_revision_delete BEFORE DELETE ON collaboration_revisions BEGIN SELECT RAISE(ABORT,'revisions are immutable'); END;
        """);self.db.commit()
    def close(self):self.db.close()
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def _decode(self,row):
        if not row:return None
        out=dict(row)
        for key in ("spec_json","models_json","original_json","outcome_json","data_json","artifact_json","provenance_json"):
            if key in out:
                out[key[:-5] if key.endswith("_json") else key]=json.loads(out[key]) if out[key] is not None else None
        return out
    def enqueue(self,spec:dict,models:dict,scheduled_at:str|None=None)->str:
        spec=normalize_spec(spec)
        if not isinstance(models,dict) or set(models)!={"qwen","bonsai"}:raise ValueError("models must contain exact qwen and bonsai snapshots.")
        for value in models.values():
            if not isinstance(value,dict):raise ValueError("model snapshots must be objects.")
        run_id=str(uuid.uuid4());now=utc_now();scheduled=scheduled_at or now
        try:
            parsed=datetime.fromisoformat(scheduled.replace("Z","+00:00"))
            if parsed.tzinfo is None:raise ValueError
            scheduled=parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
        except (ValueError,AttributeError) as exc:raise ValueError("scheduled_at requires an ISO timezone.") from exc
        with self.db:
            self.db.execute("""INSERT INTO collaboration_runs(id,state,created_at,scheduled_at,spec_json,models_json,original_json)
              VALUES(?,'pending',?,?,?,?,?)""",(run_id,now,scheduled,_json(spec),_json(models),_json(spec["original"])))
            revision=str(uuid.uuid4())
            self.db.execute("""INSERT INTO collaboration_revisions(id,run_id,at,label,artifact_json,parent_id,provenance_json)
              VALUES(?,?,?,'original',?,NULL,?)""",(revision,run_id,now,_json(spec["original"]),_json({"kind":"immutable_input"})))
            self.db.execute("INSERT INTO collaboration_events(run_id,at,kind,data_json) VALUES(?,?,?,?)",
                            (run_id,now,"input",_json({"original_revision_id":revision,"spec":spec,"models":models})))
        return run_id
    def get(self,run_id:str)->dict|None:return self._decode(self.db.execute("SELECT * FROM collaboration_runs WHERE id=?",(run_id,)).fetchone())
    def list(self,pending:bool|None=None)->list[dict]:
        where="" if pending is None else (" WHERE state IN ('pending','running')" if pending else " WHERE state NOT IN ('pending','running')")
        return [self._decode(x) for x in self.db.execute("SELECT * FROM collaboration_runs"+where+" ORDER BY created_at")]
    def claim_due(self,now:str|None=None)->dict|None:
        cutoff=now or utc_now()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row=self.db.execute("SELECT id FROM collaboration_runs WHERE state='pending' AND scheduled_at<=? ORDER BY scheduled_at,created_at LIMIT 1",(cutoff,)).fetchone()
            if not row:self.db.commit();return None
            changed=self.db.execute("UPDATE collaboration_runs SET state='running',started_at=? WHERE id=? AND state='pending'",(utc_now(),row[0])).rowcount
            self.db.commit();return self.get(row[0]) if changed==1 else None
        except BaseException:self.db.rollback();raise
    def cancel(self,run_id:str)->bool:
        with self.db:
            changed=self.db.execute("""UPDATE collaboration_runs SET state=CASE WHEN state='pending' THEN 'cancelled' ELSE state END,
              completed_at=CASE WHEN state='pending' THEN ? ELSE completed_at END,
              stop_reason=CASE WHEN state='pending' THEN 'cancelled before dispatch' ELSE stop_reason END,cancel_requested=1
              WHERE id=? AND state IN ('pending','running')""",(utc_now(),run_id)).rowcount
            if changed:self.db.execute("INSERT INTO collaboration_events(run_id,at,kind,data_json) VALUES(?,?,?,?)",
                (run_id,utc_now(),'cancel_requested',_json({'requested':True})))
        return changed==1
    def cancelled(self,run_id:str)->bool:
        row=self.db.execute("SELECT cancel_requested FROM collaboration_runs WHERE id=?",(run_id,)).fetchone();return bool(row and row[0])
    def recover_running(self)->int:
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            rows=self.db.execute("SELECT id FROM collaboration_runs WHERE state='running'").fetchall()
            count=self.db.execute("""UPDATE collaboration_runs SET state='interrupted',completed_at=?,
                outcome_json='{"accepted":false,"reason":"unknown interrupted outcome"}',
                stop_reason='worker interrupted; no automatic retry or replay' WHERE state='running'""",(utc_now(),)).rowcount
            for row in rows:self.db.execute("INSERT INTO collaboration_events(run_id,at,kind,data_json) VALUES(?,?,?,?)",
                (row[0],utc_now(),'recovery',_json({'state':'interrupted','replayed':False})))
            return count
    def append_event(self,run_id:str,kind:str,data:Any)->int:
        if not isinstance(kind,str) or not kind:raise ValueError("event kind required")
        with self.db:cursor=self.db.execute("INSERT INTO collaboration_events(run_id,at,kind,data_json) VALUES(?,?,?,?)",(run_id,utc_now(),kind,_json(data)))
        return int(cursor.lastrowid)
    def add_revision(self,run_id:str,label:str,artifact:Any,parent_id:str|None=None,provenance:Any=None)->str:
        revision=str(uuid.uuid4())
        with self.db:
            if parent_id and not self.db.execute("SELECT 1 FROM collaboration_revisions WHERE id=? AND run_id=?",(parent_id,run_id)).fetchone():
                raise ValueError("Parent revision must belong to this collaboration.")
            self.db.execute("INSERT INTO collaboration_revisions(id,run_id,at,label,artifact_json,parent_id,provenance_json) VALUES(?,?,?,?,?,?,?)",
                (revision,run_id,utc_now(),label,_json(artifact),parent_id,_json(provenance or {})))
        return revision
    def evidence(self,run_id:str)->dict:
        run=self.get(run_id)
        if not run:raise KeyError(run_id)
        events=[self._decode(x) for x in self.db.execute("SELECT * FROM collaboration_events WHERE run_id=? ORDER BY seq",(run_id,))]
        revisions=[self._decode(x) for x in self.db.execute("SELECT * FROM collaboration_revisions WHERE run_id=? ORDER BY rowid",(run_id,))]
        return {"run":run,"events":events,"revisions":revisions}
    def finish(self,run_id:str,status:str,outcome:Any,stop_reason:str,accepted_revision_id:str|None=None)->None:
        if status not in STATES or status in ("pending","running"):raise ValueError("invalid final status")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row=self.db.execute("SELECT cancel_requested FROM collaboration_runs WHERE id=? AND state='running'",(run_id,)).fetchone()
            if row and row[0] and status!='cancelled':
                status='cancelled';outcome={'accepted':False,'reason':'cancellation won final decision'}
                stop_reason='cancelled';accepted_revision_id=None
            if accepted_revision_id and not self.db.execute("SELECT 1 FROM collaboration_revisions WHERE id=? AND run_id=?",(accepted_revision_id,run_id)).fetchone():
                raise ValueError("Accepted revision must belong to this collaboration.")
            changed=self.db.execute("""UPDATE collaboration_runs SET state=?,completed_at=?,outcome_json=?,stop_reason=?,accepted_revision_id=?
              WHERE id=? AND state='running'""",(status,utc_now(),_json(outcome),stop_reason,accepted_revision_id,run_id)).rowcount
            if changed!=1:raise RuntimeError("collaboration run is not running")
            self.db.execute("INSERT INTO collaboration_events(run_id,at,kind,data_json) VALUES(?,?,?,?)",
                (run_id,utc_now(),"finished",_json({'state':status,'outcome':outcome,'stop_reason':stop_reason,'accepted_revision_id':accepted_revision_id})))
