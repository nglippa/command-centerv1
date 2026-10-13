"""Persistent model conversations: a producer/reader of the existing durable queue."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from lifecycle import Dependencies
from models import LocalHTTP, ModelError
from model_access import model_access
from chat_stream import visible_content
from progress import provider_usage
from queue_store import QueueStore, utc_now


class WorkspaceError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status=status


def endpoint_key(url):
    return hashlib.sha256(url.encode()).hexdigest()[:20]


def ensure_sessions(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL, model_identifier TEXT NOT NULL,
        entry_json TEXT NOT NULL, created_at TEXT NOT NULL, name TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS conversation_turns (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id), conversation_id TEXT NOT NULL REFERENCES conversations(id),
        ordinal INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(conversation_id, ordinal));
    ''')


class ModelWorkspaces:
    def __init__(self, db, registry, resources):
        self.db=Path(db); self.registry=registry; self.resources=Path(resources)
        self.endpoints={endpoint_key(e.endpoint): e for e in registry.entries}
        self._lock=threading.Lock();self._health={}
        with QueueStore(self.db) as store:
            ensure_sessions(store.db)
            from chat_stream import ensure_generation
            ensure_generation(store.db)
            store.db.commit()

    def endpoint(self, key, refresh=False):
        entry=self.endpoints.get(key)
        if not entry:raise WorkspaceError('Unknown configured endpoint.',404)
        with self._lock:
            cached=self._health.get(key)
            if not refresh and cached and time.monotonic()-cached[0]<15:
                return cached[1]
            start=time.monotonic(); errors=[];models=[];loaded=None
            try:
                ids=LocalHTTP(entry.endpoint,min(entry.health_timeout,3)).models()
                if len(ids)>500 or any(not isinstance(x,str) or not 1<=len(x)<=200 or any(ord(c)<32 for c in x) for x in ids):
                    raise ModelError('Invalid or oversized model listing.')
                models=list(dict.fromkeys(ids));connected=True
            except (ModelError,ValueError) as exc:
                connected=False;errors.append(str(exc))
            if entry.provider=='ollama':
                try:
                    payload=Dependencies().native(entry,'GET','/api/ps',None,min(entry.health_timeout,3))
                    rows=payload.get('models') if isinstance(payload,dict) else None
                    if not isinstance(rows,list) or any(not isinstance(r,dict) or not isinstance(r.get('name'),str) for r in rows):
                        raise ModelError('Incomplete loaded-model listing.')
                    loaded=[r['name'] for r in rows]
                except (ModelError,ValueError) as exc:errors.append(str(exc))
            configured=[e for e in self.registry.entries if e.endpoint==entry.endpoint]
            for e in configured:
                if e.identifier not in models:models.append(e.identifier)
            options=[]
            for identifier in models:
                exact=next((e for e in configured if e.identifier==identifier),None)
                installed=(Path(entry.model_path or '').is_file() and Path(entry.runtime_path or '').is_file()) if entry.provider=='bonsai' else None
                available=(connected and identifier in ids) if connected else False
                options.append({'identifier':identifier,'reported_by_provider':available,
                    'configured':bool(exact),'chat_allowed':entry.provider=='ollama' and available or bool(exact),
                    'availability':'available' if available else 'installed_server_stopped' if exact and installed else 'unavailable' if connected else 'unknown',
                    'reason':'Provider-listed model; worker still verifies readiness.' if available else
                        'Configured local weights/runtime are present. Sending a turn lets the worker verify and start its owned runtime.' if exact and installed else
                        'Test connection and check local service/model configuration; no model is substituted.'})
            value={'id':key,'endpoint':entry.endpoint,'provider':entry.provider,
                'runtime':'Ollama' if entry.provider=='ollama' else 'Prism (configured compatible runtime)',
                'connected':connected,'observed_at':utc_now(),'latency_ms':round((time.monotonic()-start)*1000),
                'latency_source':'Command Center connection-check round trip; not inference latency',
                'loaded_models':loaded,'loaded_source':'provider /api/ps' if loaded is not None else 'unavailable',
                'models':options,'errors':errors,'inspection_url':model_access(entry)['inspection_url'],
                'limitations':['No provider-wide request log or GPU-memory metric is available.','Connection checks never load or start a model.']}
            self._health[key]=(time.monotonic(),value)
            return value

    def resolve(self, key, identifier):
        seed=self.endpoints.get(key)
        if not seed:raise WorkspaceError('Unknown configured endpoint.',404)
        exact=next((e for e in self.registry.entries if e.endpoint==seed.endpoint and e.identifier==identifier),None)
        if exact:return exact
        health=self.endpoint(key)
        if seed.provider!='ollama' or not any(m['identifier']==identifier and m['reported_by_provider'] for m in health['models']):
            raise WorkspaceError('The selected model is not reported at this configured endpoint. Test connection; no substitution was made.',404)
        return self.registry.clone_discovered(seed,identifier,seed.endpoint)

    def workspace(self, key, identifier):
        entry=self.resolve(key,identifier)
        with QueueStore(self.db) as store:
            sessions=[dict(r) for r in store.db.execute('SELECT id,name,created_at FROM conversations WHERE endpoint_id=? AND model_identifier=? ORDER BY created_at DESC LIMIT 30',(key,identifier))]
            agent_runs=[]
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='capability_runs'").fetchone():
                for row in store.db.execute('SELECT id,model,state,created_at,completed_at FROM capability_runs r WHERE EXISTS (SELECT 1 FROM capability_events e JOIN tasks t ON t.id=json_extract(e.data_json,"$.job_id") WHERE e.run_id=r.id AND e.kind="model_queued" AND t.model_identifier=?) ORDER BY created_at DESC LIMIT 10',(entry.identifier,)):
                    r=dict(row)
                    selected=store.db.execute('SELECT data_json FROM capability_events WHERE run_id=? AND kind="capabilities_selected" ORDER BY seq LIMIT 1',(r['id'],)).fetchone()
                    choices=json.loads(selected['data_json']).get('capabilities',[]) if selected else []
                    r['capabilities']=[{'id':c.get('id'),'name':c.get('name'),'kind':c.get('kind')} for c in choices[:20] if isinstance(c,dict) and isinstance(c.get('id'),str)]
                    # Metadata-only: private prompts, raw answers and tool arguments stay out of this view.
                    r['events']=[{'at':e['at'],'kind':e['kind'],'capability_id':json.loads(e['data_json']).get('capability_id') or json.loads(e['data_json']).get('requested',{}).get('capability_id')}
                         for e in store.db.execute('SELECT at,kind,data_json FROM capability_events WHERE run_id=? ORDER BY seq DESC LIMIT 20',(r['id'],))]
                    agent_runs.append(r)
        return {'model':{k:getattr(entry,k) for k in ('family','identifier','provider','endpoint','context_window','max_tokens','timeout_seconds')},
                'endpoint':self.endpoint(key),'sessions':sessions,'agent_runs':agent_runs,
                'mode':'direct_model','tool_execution':False,'worker_streaming':self.worker_support(),
                'capabilities_url':'#/capabilities', 'context_note':'Configured context limit is shown; exact live context occupancy is unavailable.'}

    def worker_support(self):
        # Do not mistake a stale feature record for the current worker.
        from cli import worker_active, lock_path
        if not worker_active(self.db):return {'ready':False,'reason':'Worker offline. Turns remain queued until the existing worker starts.'}
        with QueueStore(self.db) as store:
            present=store.db.execute("SELECT 1 FROM sqlite_master WHERE name='worker_features'").fetchone()
            row=store.db.execute('SELECT * FROM worker_features WHERE singleton=1').fetchone() if present else None
        if row:
            try:
                identity=Dependencies().identity(row['pid'])
                owner=lock_path(self.db).read_text()[:100].strip()
                if owner==str(row['pid']) and identity and list(identity)==json.loads(row['identity_json']) and row['version']==1:
                    return {'ready':True,'reason':'Current worker advertises persisted interactive streaming support.'}
            except (ValueError,TypeError,OSError):pass
        return {'ready':False,'reason':'Current worker streaming support is unverified. Chat uses the durable queue and returns completed responses. Reload after idle to enable streaming and Stop generation.'}

    def create(self, key, identifier):
        entry=self.resolve(key,identifier);identifier_id='chat-'+str(uuid.uuid4())
        with QueueStore(self.db) as store:
            store.db.execute('INSERT INTO conversations VALUES(?,?,?,?,?,?)',(identifier_id,key,entry.identifier,entry.snapshot_json(),utc_now(),'New conversation'))
            store.db.commit()
        return {'id':identifier_id,'endpoint_id':key,'model_identifier':entry.identifier}

    def _session(self, store, identifier):
        row=store.db.execute('SELECT * FROM conversations WHERE id=?',(identifier,)).fetchone()
        if not row:raise WorkspaceError('Conversation not found.',404)
        return dict(row)

    def _session_lifecycle(self, task):
        if not task.get('lifecycle_session_id'):return None
        path=self.resources/'lifecycle.sqlite3'
        if not path.is_file():return None
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            row=db.execute('SELECT task_id,state,load_ms,infer_ms,unload_ms,last_verified_unload,blocker,updated_at FROM sessions WHERE id=? AND task_id=?',(task['lifecycle_session_id'],task['id'])).fetchone()
            return dict(row) if row else None
        finally:db.close()

    def _released(self, task, lifecycle=None):
        if task.get('lifecycle_error'):return False
        life=lifecycle or self._session_lifecycle(task)
        return bool(life and life['state']=='STOPPED' and life['last_verified_unload'] and not life['blocker'])

    def conversation(self, identifier):
        support=self.worker_support()
        with QueueStore(self.db) as store:
            session=self._session(store,identifier)
            turns=[]
            rows=store.db.execute('SELECT t.*,j.* FROM conversation_turns t JOIN tasks j ON j.id=t.task_id WHERE conversation_id=? ORDER BY ordinal LIMIT 100',(identifier,)).fetchall()
            for r in rows:
                task=dict(r);generation=store.db.execute('SELECT * FROM tasks_generation WHERE task_id=?',(task['id'],)).fetchone()
                generation=dict(generation) if generation else None
                life=self._session_lifecycle(task)
                released=self._released(task,life)
                final=task['state']=='completed' and released
                infer_ms=None
                if task.get('started_at') and task.get('completed_at'):
                    from datetime import datetime
                    infer_ms=(datetime.fromisoformat(task['completed_at'])-datetime.fromisoformat(task['started_at'])).total_seconds()*1000
                usage=provider_usage(task.get('raw_response_json'),life.get('infer_ms') if life else None)
                if generation and generation.get('usage_json') and not final:
                    live=json.loads(generation['usage_json']);
                    counts={k:live[v] for k,v in [('prompt_tokens','input_tokens'),('completion_tokens','output_tokens'),('total_tokens','total_tokens')] if live.get(v) is not None}
                    usage=provider_usage({'usage':counts})
                turns.append({'task_id':task['id'],'ordinal':task['ordinal'],'user':task['content'],
                    'response':visible_content(task.get('result') or '') if final else generation.get('content','') if generation else '',
                    'state':task['state'],'generation':{k:generation.get(k) for k in ('status','updated_at','cancel_requested','updates')} if generation else None,
                    'final':final,'released':released,'lifecycle':life,'error':task.get('error'),'cleanup_error':task.get('lifecycle_error'),
                    'created_at':task['created_at'],'started_at':task.get('started_at'),'completed_at':task.get('completed_at'),
                    'usage':usage,'can_cancel':task['state']=='pending' or task['state']=='running' and support['ready']})
            return {'id':session['id'],'endpoint_id':session['endpoint_id'],'model_identifier':session['model_identifier'],
                    'name':session['name'],'created_at':session['created_at'],'turns':turns,'worker_streaming':support}

    def submit(self, identifier, content, max_tokens=None):
        if not isinstance(content,str) or not content.strip() or len(content)>8000:
            raise WorkspaceError('Message must contain 1–8,000 characters.')
        with QueueStore(self.db) as store:
            session=self._session(store,identifier)
            from registry import ModelEntry
            entry=ModelEntry.from_dict(json.loads(session['entry_json']))
            tokens=entry.max_tokens if max_tokens is None else max_tokens
            if type(tokens)!=int or not 1<=tokens<=entry.max_tokens:raise WorkspaceError('Invalid output token limit.')
            with store.transaction():
                previous=store.db.execute('SELECT t.content,j.* FROM conversation_turns t JOIN tasks j ON j.id=t.task_id WHERE conversation_id=? ORDER BY ordinal',(identifier,)).fetchall()
                messages=[]
                for r in previous:
                    t=dict(r)
                    if t['state'] in ('pending','running') or t['state']=='completed' and not self._released(t):
                        raise WorkspaceError('Previous turn or resource cleanup is still active. Wait before sending again.',409)
                    if t['state']=='completed':messages.extend([{'role':'user','content':t['content']},{'role':'assistant','content':visible_content(t['result'])}])
                messages.append({'role':'user','content':content})
                if len(messages)>32 or sum(len(m['content']) for m in messages)>24000:
                    raise WorkspaceError('Conversation exceeds the bounded history size. Start a new conversation; history is retained.')
                task_id='chat-turn-'+str(uuid.uuid4());now=utc_now()
                prompt='Conversation history (respond to the final user message):\n'+json.dumps(messages,ensure_ascii=False)
                store.enqueue(model_family=entry.family,model_identifier=entry.identifier,endpoint=entry.endpoint,
                    prompt=prompt,scheduled_at=now,name='Chat '+entry.identifier[:70],max_tokens=tokens,
                    timeout_seconds=entry.timeout_seconds,task_id=task_id,registry_json=entry.snapshot_json(),
                    contract_json=json.dumps({'kind':'interactive_chat','version':1,'messages':messages,'stream':True},ensure_ascii=False),commit=False)
                store.db.execute('INSERT INTO conversation_turns VALUES(?,?,?,?,?)',(task_id,identifier,len(previous)+1,content,now))
                store.db.execute("INSERT INTO tasks_generation(task_id,content,updated_at,status,cancel_requested,updates) VALUES(?,'',?,'queued',0,0)",(task_id,now))
                if not previous:store.db.execute('UPDATE conversations SET name=? WHERE id=?',(content[:80],identifier))
        return {'id':identifier,'task_id':task_id}

    def cancel(self, identifier, task_id):
        support=self.worker_support()
        with QueueStore(self.db) as store:
            self._session(store,identifier)
            with store.transaction():
                row=store.db.execute('SELECT j.state FROM conversation_turns t JOIN tasks j ON j.id=t.task_id WHERE conversation_id=? AND task_id=?',(identifier,task_id)).fetchone()
                if not row:raise WorkspaceError('Turn not found in this conversation.',404)
                state=row['state']
                if state=='pending' and store.cancel(task_id,commit=False):
                    store.db.execute("UPDATE tasks_generation SET cancel_requested=1,status='cancelled',updated_at=? WHERE task_id=?",(utc_now(),task_id))
                elif state=='running':
                    if not support['ready']:
                        raise WorkspaceError('Current worker cannot stop active generation. Wait for bounded completion; reload it after idle to enable streaming cancellation.',409)
                    store.db.execute('UPDATE tasks_generation SET cancel_requested=1,updated_at=? WHERE task_id=?',(utc_now(),task_id))
                else:raise WorkspaceError('Turn is already terminal.',409)
        return {'task_id':task_id,'cancel_requested':True}
