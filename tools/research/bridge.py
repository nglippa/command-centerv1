"""Version 1 local JSON queue interface. Model execution belongs to cli.run_worker."""
from __future__ import annotations
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from cli import DEFAULT_CONFIG, DEFAULT_DB, DEFAULT_RESOURCES, worker_active
from queue_store import QueueStore, utc_now
from registry import Registry
from lifecycle import Manager
from whodunnit_notice import read_notice

LIMIT = 256 * 1024

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

def validate_job(value):
    if not isinstance(value,dict): raise ValueError('job must be an object')
    allowed={'jobId','projectId','jobType','prompt','inputPayload','approvedSources','model','outputSchema','maxTokens','timeoutSeconds','retryBudget'}
    if set(value)-allowed: raise ValueError('Unknown job contract fields')
    job=dict(value)
    for field,limit in [('jobId',200),('projectId',100),('jobType',100),('prompt',32000)]:
        if not isinstance(job.get(field),str) or not job[field].strip() or len(job[field])>limit: raise ValueError('Invalid '+field)
    if job.get('model') not in ('qwen','bonsai'): raise ValueError('Model must be registered qwen or bonsai')
    if not isinstance(job.get('inputPayload',{}),dict) or not isinstance(job.get('approvedSources',[]),list): raise ValueError('Invalid payload or approvedSources')
    schema=job.get('outputSchema')
    if schema is not None and (not isinstance(schema,dict) or schema.get('type')!='object'): raise ValueError('outputSchema must be an object schema')
    job.setdefault('inputPayload',{});job.setdefault('approvedSources',[]);job.setdefault('outputSchema',None)
    for field,default,lo,hi,integer in [('maxTokens',2400,1,8192,True),('timeoutSeconds',180,1,600,False),('retryBudget',1,0,2,True)]:
        number=job.setdefault(field,default)
        if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not lo<=number<=hi or (integer and not isinstance(number,int)): raise ValueError('Invalid '+field)
    canonical(job)
    return job

def task_result(store,manager,task):
    if task is None: raise ValueError('Unknown jobId')
    session=None
    if task.get('lifecycle_session_id'):
        row=manager.store.db.execute('SELECT * FROM sessions WHERE id=?',(task['lifecycle_session_id'],)).fetchone()
        if row: session=dict(row)
    blocker=session.get('blocker') if session else None
    cleanup_verified=bool(session and session['state']=='STOPPED' and session['last_verified_unload'] and not task.get('lifecycle_error') and not blocker)
    ready=bool(task['state']=='completed' and cleanup_verified)
    return {'version':1,'jobId':task['id'],'state':task['state'],'readyForConsumer':ready,'result':task.get('result') if ready else None,'raw':task.get('raw_response_json') if cleanup_verified else None,'error':task.get('error'),'contract':json.loads(task['contract_json']) if task.get('contract_json') else None,'createdAt':task['created_at'],'completedAt':task.get('completed_at'),'lifecycle':{'sessionId':task.get('lifecycle_session_id'),'state':session['state'] if session else None,'verifiedUnloadAt':session.get('last_verified_unload') if session else None,'cleanupError':task.get('lifecycle_error'),'blocker':blocker,'loadMs':session.get('load_ms') if session else None,'inferMs':session.get('infer_ms') if session else None,'unloadMs':session.get('unload_ms') if session else None}}

def handle(request,db,config,resources):
    if not isinstance(request,dict) or type(request.get('version')) is not int or request.get('version')!=1: raise ValueError('Unsupported interface version')
    action=request.get('action')
    with QueueStore(db) as store:
        manager=Manager(resources,config)
        try:
            if action=='submit':
                job=validate_job(request.get('job'));snapshot=canonical(job)
                existing=store.get(job['jobId'])
                if existing:
                    if existing.get('contract_json')!=snapshot: raise ValueError('Idempotency conflict: jobId has a different contract')
                    return task_result(store,manager,existing)
                entries=[entry for entry in Registry(config).entries if entry.family==job['model']]
                if len(entries)!=1: raise ValueError('Model family must resolve to exactly one registered entry')
                entry=entries[0]
                try: store.enqueue(model_family=entry.family,model_identifier=entry.identifier,endpoint=entry.endpoint,prompt=job['prompt'],scheduled_at=utc_now(),max_tokens=job['maxTokens'],timeout_seconds=job['timeoutSeconds'],name=job['jobType'][:80],task_id=job['jobId'],registry_json=entry.snapshot_json(),contract_json=snapshot)
                except sqlite3.IntegrityError:
                    existing=store.get(job['jobId'])
                    if not existing or existing.get('contract_json')!=snapshot: raise
                return task_result(store,manager,store.get(job['jobId']))
            if action in ('get','cancel'):
                task=store.get(request.get('jobId'))
                if task is None: raise ValueError('Unknown jobId')
                if action=='cancel':
                    cancelled=store.cancel(task['id']);task=store.get(task['id'])
                    return {**task_result(store,manager,task),'cancelled':cancelled,'active':task['state']=='running'}
                return task_result(store,manager,task)
            if action=='status':
                current=dict(manager.store.db.execute('SELECT * FROM current WHERE singleton=1').fetchone())
                session=manager.store.db.execute('SELECT model,state,task_id,updated_at FROM sessions WHERE id=?',(current['session_id'],)).fetchone()
                last=store.db.execute("SELECT id,state,completed_at,lifecycle_error FROM tasks WHERE state IN ('completed','failed','cancelled') ORDER BY completed_at DESC LIMIT 1").fetchone()
                return {'notification':read_notice(resources),'version':1,'workerActive':worker_active(db),'queue':store.summary(),'resource':current,'activeSession':dict(session) if session else None,'lastCompletedJob':dict(last) if last else None,'models':[{'family':e.family,'identifier':e.identifier,'provider':e.provider} for e in manager.registry.entries]}
            raise ValueError('Unsupported action')
        finally: manager.store.close()

def render_lease(config,resources):
    manager=Manager(resources,config)
    try:
        with manager.lease() as acquired:
            if not acquired: raise ValueError('Command Center resources are leased')
            # Rendering must never perform model recovery or signal a process.
            if manager.store.recovery_candidate(): raise ValueError('Model reconciliation required before rendering')
            manager.reconcile()
            live=manager.probe_live()
            for observation in live['models']:
                value=observation.get('value',{})
                if not observation.get('ok') or value.get('loaded') or value.get('listener_pid') is not None or value.get('serving_model'): raise ValueError('Model resources are not verified free for rendering')
            print(canonical({'version':1,'ready':True}),flush=True)
            for line in sys.stdin:
                if line.strip()=='release': break
    finally: manager.store.close()

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--db',default=str(DEFAULT_DB));parser.add_argument('--config',default=str(DEFAULT_CONFIG));parser.add_argument('--resources-dir',default=str(DEFAULT_RESOURCES));parser.add_argument('--render-lease',action='store_true');args=parser.parse_args(argv)
    try:
        if args.render_lease: render_lease(args.config,args.resources_dir);return 0
        raw=sys.stdin.buffer.read(LIMIT+1)
        if len(raw)>LIMIT: raise ValueError('Request exceeds 256 KiB')
        request=json.loads(raw.decode('utf-8'),parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
        result=handle(request,args.db,args.config,args.resources_dir);print(canonical(result));return 0
    except Exception as exc:
        print(canonical({'version':1,'error':str(exc)}),flush=True);return 1

if __name__=='__main__': raise SystemExit(main())
