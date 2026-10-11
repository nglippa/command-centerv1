import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import bridge
from queue_store import QueueStore
from lifecycle import Manager
from test_lifecycle import FakeDeps, entries

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.config=self.root/'models.json';self.config.write_text(json.dumps({'models':list(entries())}));self.db=self.root/'queue.sqlite3';self.resources=self.root/'state'
        self.job={'jobId':'factory-test','projectId':'gigwise','jobType':'draft','model':'qwen','prompt':'fixture','outputSchema':{'type':'object','properties':{'x':{'type':'string'}}}}
    def tearDown(self):self.tmp.cleanup()
    def call(self,**request):return bridge.handle({'version':1,**request},self.db,self.config,self.resources)
    def test_idempotent_reopen_and_mismatch(self):
        first=self.call(action='submit',job=self.job);second=self.call(action='submit',job=self.job);self.assertEqual(first['jobId'],second['jobId']);self.assertFalse(second['readyForConsumer'])
        with QueueStore(self.db) as store:self.assertEqual(store.summary()['counts']['pending'],1)
        with self.assertRaisesRegex(ValueError,'Idempotency'):self.call(action='submit',job={**self.job,'prompt':'changed'})
    def test_result_hidden_until_verified_cleanup(self):
        self.call(action='submit',job=self.job)
        with QueueStore(self.db) as store:
            task=store.claim_due();manager=Manager(self.resources,self.config,deps=FakeDeps())
            try:
                with manager.lease() as acquired:
                    self.assertTrue(acquired);manager.reconcile()
                    def persist(result,raw,sid):
                        store.complete(task['id'],result,raw,sid);self.assertIsNone(bridge.task_result(store,manager,store.get(task['id']))['result'])
                    result=manager.execute(task,on_result=persist);store.record_lifecycle(task['id'],result.session_id,result.cleanup_error)
                ready=bridge.task_result(store,manager,store.get(task['id']));self.assertTrue(ready['readyForConsumer']);self.assertEqual(ready['result'],'OK');store.db.execute("UPDATE tasks SET state='failed',error='inference failed' WHERE id=?",(task['id'],));store.db.commit();failed=bridge.task_result(store,manager,store.get(task['id']));self.assertFalse(failed['readyForConsumer']);self.assertIsNotNone(failed['raw']);store.db.execute("UPDATE tasks SET state='completed',error=NULL WHERE id=?",(task['id'],));store.db.commit();manager.store.db.execute("UPDATE current SET blocker='later unrelated blocker' WHERE singleton=1");manager.store.db.commit();self.assertTrue(bridge.task_result(store,manager,store.get(task['id']))['readyForConsumer'])
                requests=[x[2] for x in manager.d.calls if len(x)>2 and x[1]=='/api/chat'];self.assertEqual(requests[-1]['format'],self.job['outputSchema']);self.assertNotIn('format',requests[0])
            finally:manager.store.close()
    def test_contract_bounds_cancel_and_versions(self):
        for bad in ({'endpoint':'http://evil'}, {'timeoutSeconds':float('nan')},{'retryBudget':3},{'maxTokens':True},{'model':'missing'}):
            with self.assertRaises(ValueError):bridge.validate_job({**self.job,**bad})
        with self.assertRaises(ValueError):self.call(version=2,action='status')
        self.call(action='submit',job=self.job);cancel=self.call(action='cancel',jobId=self.job['jobId']);self.assertTrue(cancel['cancelled']);self.assertEqual(cancel['state'],'cancelled')
    def test_render_lease_inspects_probe_envelope(self):
        fake=Manager(self.resources,self.config,deps=FakeDeps())
        with patch.object(bridge,'Manager',return_value=fake),patch.object(bridge.sys,'stdin',io.StringIO('release\n')),contextlib.redirect_stdout(io.StringIO()) as output:
            bridge.render_lease(self.config,self.resources)
        self.assertTrue(json.loads(output.getvalue())['ready'])

    def test_contract_bonsai_disables_thinking_only_for_final_inference(self):
        class D(FakeDeps):
            def __init__(self):super().__init__();self.options=[]
            def complete(self,entry,prompt,max_tokens):
                self.options.append(getattr(self,'contract_request_options',None));return 'OK',json.dumps({'model':entry.identifier})
        deps=D();manager=Manager(self.resources,self.config,deps=deps);b=entries()[1]
        try:
            with manager.lease() as acquired:
                self.assertTrue(acquired);manager.reconcile();result=manager.execute({'id':'bonsai-fixture','registry_json':json.dumps(b),'contract_json':json.dumps(self.job),'prompt':'Review','max_tokens':32,'timeout_seconds':2})
            self.assertIsNone(result.error);self.assertIsNone(result.cleanup_error);self.assertEqual(deps.options,[None,{'chat_template_kwargs':{'enable_thinking':False},'temperature':0}]);self.assertIsNone(deps.contract_request_options)
        finally:manager.store.close()
    def test_render_refuses_loaded_models_without_unloading(self):
        deps=FakeDeps();deps.loaded=['qwen3:14b-q8_0'];manager=Manager(self.resources,self.config,deps=deps)
        with patch.object(bridge,'Manager',return_value=manager):
            with self.assertRaisesRegex(ValueError,'not verified free'):bridge.render_lease(self.config,self.resources)
        self.assertFalse(any(call[1]=='/api/generate' for call in deps.calls if len(call)>1))
