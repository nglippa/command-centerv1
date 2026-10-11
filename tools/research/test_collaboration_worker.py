"""Worker integration uses the real lifecycle manager with controlled effects."""
from __future__ import annotations
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli import run_worker
from collaboration import parse_role_json, ParseError, CollaborationRunner, CollaborationTransport
from collaboration_store import CollaborationStore,utc_now
from lifecycle import Manager,ResourceBlocked,LifecycleError,Dependencies
from queue_store import QueueStore
from registry import Registry,ModelEntry
from test_lifecycle import FakeDeps,entries
from test_collaboration import spec,candidate,review,evaluation,FakeManager,model
from validation import normalize_spec,validate_artifact
from models import LocalHTTP,ModelError

class Responses(FakeDeps):
    def __init__(self,responses):super().__init__();self.responses=list(responses);self.overlaps=[]
    def native(self,e,method,path,payload,timeout):
        if self.loaded and any(c[0]=='POPEN' for c in self.calls) and self.process.poll() is None:self.overlaps.append(True)
        if path=='/api/chat' and payload['messages'][0]['content']!='Reply OK':
            value=super().native(e,method,path,payload,timeout)
            value['message']['content']=self.responses.pop(0);return value
        return super().native(e,method,path,payload,timeout)
    def complete(self,e,prompt,max_tokens):
        if self.loaded:self.overlaps.append(True)
        return ('OK' if prompt=='Reply OK' else self.responses.pop(0)),json.dumps({'model':e.identifier})

class WorkerCollaborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=self.root/'queue.sqlite3'
        q,b=entries();self.config=self.root/'models.json';self.config.write_text(json.dumps({'models':[q,b]}))
        self.models={e.family:e.export() for e in Registry(self.config).entries}
    def tearDown(self):self.tmp.cleanup()
    def worker(self,deps):
        with patch('cli.Manager',side_effect=lambda root,conf,**kw:Manager(root,conf,deps=deps,**kw)),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return run_worker(str(self.db),once=True,registry_path=self.config,resources_dir=self.root/'state')
    def test_worker_executes_both_models_through_real_lifecycle_and_preserves_plain_history(self):
        with QueueStore(self.db) as q:
            tid=q.enqueue(model_family='qwen',model_identifier=self.models['qwen']['identifier'],endpoint=self.models['qwen']['endpoint'],prompt='old',scheduled_at=utc_now(),max_tokens=1,timeout_seconds=1)
            q.cancel(tid);before=q.get(tid)
        with CollaborationStore(self.db) as c:rid=c.enqueue(spec(),self.models)
        d=Responses([candidate('Pebble text'),review(),evaluation(),candidate('Pebble text'),review('approve')])
        self.assertEqual(self.worker(d),0)
        with CollaborationStore(self.db) as c:
            ev=c.evidence(rid);self.assertEqual(ev['run']['state'],'completed')
            sessions=[e['data']['lifecycle'] for e in ev['events'] if e['kind']=='call_finished']
            self.assertEqual(len(sessions),5)
            self.assertTrue(all(s['state']=='STOPPED' and s['last_verified_unload'] for s in sessions))
        with QueueStore(self.db) as q:self.assertEqual(before,q.get(tid))
        self.assertFalse(d.overlaps);self.assertFalse(d.loaded);self.assertIsNotNone(d.process.poll())
    def test_future_run_survives_worker_restart_then_runs_when_due(self):
        with CollaborationStore(self.db) as c:rid=c.enqueue(spec(),self.models,'2099-01-01T00:00:00Z')
        d=Responses([]);self.assertEqual(self.worker(d),0)
        with CollaborationStore(self.db) as c:
            self.assertEqual(c.get(rid)['state'],'pending')
            c.db.execute('UPDATE collaboration_runs SET scheduled_at=? WHERE id=?',(utc_now(),rid));c.db.commit()
        d=Responses([candidate('Pebble text'),review(),evaluation(),candidate('Pebble text'),review('approve')])
        self.assertEqual(self.worker(d),0)
        with CollaborationStore(self.db) as c:self.assertEqual(c.get(rid)['state'],'completed')
    def test_failed_unload_keeps_second_workflow_pending(self):
        with CollaborationStore(self.db) as c:first=c.enqueue(spec(),self.models);second=c.enqueue(spec(),self.models)
        d=Responses([candidate('Pebble text')]);d.unload_fail=True
        self.assertEqual(self.worker(d),3)
        with CollaborationStore(self.db) as c:
            self.assertEqual(c.get(first)['state'],'abstained');self.assertEqual(c.get(second)['state'],'pending')
            self.assertTrue(any(e['kind']=='call_received' for e in c.evidence(first)['events']))
    def test_cancel_during_call_does_not_dispatch_reviewer_and_cleanup_finishes(self):
        with CollaborationStore(self.db) as c:
            rid=c.enqueue(spec(),self.models);run=c.claim_due()
            manager=FakeManager([candidate('Pebble text')]);old=manager.execute
            def cancel_after_result(task,on_result=None):
                out=old(task,on_result);c.cancel(rid);return out
            manager.execute=cancel_after_result
            self.assertEqual(CollaborationRunner(c,manager).run(run),1)
            self.assertEqual((manager.calls,c.get(rid)['state']),(1,'cancelled'))
    def test_final_cancellation_wins_over_acceptance(self):
        with CollaborationStore(self.db) as c:
            rid=c.enqueue(spec(),self.models);c.claim_due();revision=c.evidence(rid)['revisions'][0]['id']
            c.cancel(rid);c.finish(rid,'completed',{'accepted':True},'valid',revision)
            row=c.get(rid);self.assertEqual(row['state'],'cancelled');self.assertIsNone(row['accepted_revision_id']);self.assertFalse(row['outcome']['accepted'])
    def test_strict_parser_rejects_duplicate_and_nonfinite_json(self):
        for raw in ('{"artifact":"a","artifact":"b"}','{"artifact":NaN}'):
            with self.assertRaises(ParseError):parse_role_json(raw)
    def test_text_metric_field_rejected_at_admission(self):
        with self.assertRaises(ValueError):normalize_spec(spec(metrics={'field':'summary'}))
    def test_bonsai_template_option_is_transmitted_without_overriding_identity(self):
        entry=ModelEntry.from_dict(self.models['bonsai']);seen=[]
        payload={'model':entry.identifier,'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]}
        def request(client,method,path,body):
            seen.append(json.loads(body));return 200,json.dumps(payload).encode()
        with patch.object(LocalHTTP,'_request',request):
            result,raw=CollaborationTransport(Dependencies()).complete(entry,'Reply OK',8)
        self.assertEqual(result,'OK');self.assertEqual(seen[0]['model'],entry.identifier)
        self.assertEqual(seen[0]['chat_template_kwargs'],{'enable_thinking':False});self.assertEqual(seen[0]['temperature'],0)
        with self.assertRaises(ModelError):LocalHTTP(entry.endpoint,1).complete(entry.identifier,'x',8,request_options={'model':'other'})
    def test_truncated_bonsai_reply_is_an_error_with_full_raw_evidence(self):
        entry=ModelEntry.from_dict(self.models['bonsai'])
        payload={'model':entry.identifier,'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}
        with patch.object(LocalHTTP,'_request',return_value=(200,json.dumps(payload).encode())):
            with self.assertRaises(LifecycleError) as caught:CollaborationTransport(Dependencies()).complete(entry,'x',8)
        self.assertIn('partial',caught.exception.raw_json)
    def test_call_budget_counts_readiness_and_role_inference(self):
        raw=spec();raw['limits']['calls']=2
        with CollaborationStore(self.db) as c:
            rid=c.enqueue(raw,self.models);run=c.claim_due();manager=FakeManager([candidate('Pebble text')])
            self.assertEqual(CollaborationRunner(c,manager).run(run),1);self.assertEqual(manager.calls,1)
            self.assertIn('model call budget exhausted',c.get(rid)['stop_reason'])
    def test_schema_repair_budget_is_global(self):
        raw=spec();raw['limits']['retries']=1
        bad_review=json.dumps({'verdict':'approve','comments':'string not array','suggestions':[],'unsupported_changes':[]})
        with CollaborationStore(self.db) as c:
            rid=c.enqueue(raw,self.models);run=c.claim_due();manager=FakeManager(['not JSON',candidate('Pebble text'),bad_review])
            self.assertEqual(CollaborationRunner(c,manager).run(run),1);self.assertEqual(manager.calls,3)
            self.assertEqual(len([e for e in c.evidence(rid)['events'] if e['kind']=='schema_rejected']),2)

    def test_schema_constant_change_is_a_protected_violation(self):
        raw={'name':'strict','instruction':'preserve','source':{'storage':'on-device'},'original':{'storage':'on-device'},'format':'json',
            'constraints':{'schema':{'type':'object','required':['storage'],'additionalProperties':False,'properties':{'storage':{'type':'string','const':'on-device'}}}}}
        result=validate_artifact({'storage':'cloud'},normalize_spec(raw))
        self.assertFalse(result['valid']);self.assertTrue(result['violations'][0]['protected'])

    def test_finished_run_cannot_be_replayed(self):
        with CollaborationStore(self.db) as c:
            rid=c.enqueue(spec(),self.models);run=c.claim_due();c.finish(rid,'interrupted',{'accepted':False},'test interruption')
            manager=FakeManager([])
            self.assertEqual(CollaborationRunner(c,manager).run(run),1);self.assertEqual(manager.calls,0)
    def test_revision_parent_cannot_cross_workflows(self):
        with CollaborationStore(self.db) as c:
            first=c.enqueue(spec(),self.models);second=c.enqueue(spec(),self.models)
            parent=c.evidence(first)['revisions'][0]['id']
            with self.assertRaises(ValueError):c.add_revision(second,'round-1','text',parent)

    def test_sigkill_keeps_received_output_and_restart_never_replays(self):
        with CollaborationStore(self.db) as c:rid=c.enqueue(spec(),self.models)
        code='''import sys,time
from collaboration_store import CollaborationStore
from collaboration import CollaborationRunner
from test_collaboration import FakeManager,candidate
with CollaborationStore(sys.argv[1]) as c:
    run=c.claim_due()
    class Paused(FakeManager):
        def execute(self,task,on_result=None):
            out=super().execute(task,on_result)
            print("RESULT_SAVED",flush=True)
            time.sleep(20)
            return out
    CollaborationRunner(c,Paused([candidate("Pebble text")])).run(run)
'''
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent),PYTHONPYCACHEPREFIX=str(self.root/'pycache'))
        child=subprocess.Popen([sys.executable,'-u','-c',code,str(self.db)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env)
        try:
            import select,time
            deadline=time.monotonic()+10;ready=False;captured=b''
            while time.monotonic()<deadline:
                if select.select([child.stdout],[],[],.1)[0]:
                    captured+=os.read(child.stdout.fileno(),4096)
                    if b'RESULT_SAVED' in captured:ready=True;break
                    if child.poll() is not None:break
            self.assertTrue(ready,'Controlled child failed to reach durable response callback: '+captured.decode(errors='replace'))
            child.send_signal(signal.SIGKILL);child.wait(timeout=3)
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=3)
            child.stdout.close()
        with CollaborationStore(self.db) as c:
            self.assertEqual(c.recover_running(),1);evidence=c.evidence(rid)
            self.assertEqual(evidence['run']['state'],'interrupted');self.assertIsNone(c.claim_due())
            received=[e for e in evidence['events'] if e['kind']=='call_received']
            self.assertEqual(received[0]['data']['result'],candidate('Pebble text'))
            self.assertEqual(evidence['revisions'][0]['artifact'],'Draft text')

if __name__=='__main__':unittest.main()
