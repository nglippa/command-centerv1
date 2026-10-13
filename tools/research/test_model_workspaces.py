"""Focused provider, queue, conversation and navigation regressions."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chat_sessions import ModelWorkspaces, WorkspaceError, endpoint_key
from model_access import model_access
from queue_store import QueueStore
from registry import Registry
from web import DEFAULT_CONFIG, WebApplication, LocalServer


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=self.root/'queue.sqlite3';self.registry=Registry(DEFAULT_CONFIG)
        self.service=ModelWorkspaces(self.db,self.registry,self.root/'resources')
        self.entry=self.registry.entries[0];self.key=endpoint_key(self.entry.endpoint)
    def tearDown(self):self.tmp.cleanup()
    def models(self):return patch('chat_sessions.LocalHTTP.models',return_value=[self.entry.identifier,'another-installed:small'])
    def native(self):return patch('chat_sessions.Dependencies.native',return_value={'models':[]})
    def create(self):return self.service.create(self.key,self.entry.identifier)['id']

    def test_navigation_uses_integrated_workspaces_with_exact_selection(self):
        for entry in self.registry.entries:
            access=model_access(entry)
            self.assertTrue(access['workspace_url'].endswith(entry.identifier.replace(':','%3A')))
            self.assertTrue(access['endpoint_workspace_url'].startswith('#/endpoints/'))
            self.assertNotIn('http',access['workspace_url'])
            self.assertNotIn('/v1/models',access['endpoint_workspace_url'])

    def test_discovery_supports_unconfigured_provider_models_without_hardcoding(self):
        with self.models(),self.native():
            entry=self.service.resolve(self.key,'another-installed:small')
            self.assertEqual(entry.identifier,'another-installed:small');self.assertEqual(entry.provider,'ollama')
            self.assertEqual(entry.endpoint,self.entry.endpoint)
            self.assertNotIn('weights_bytes',entry.expected_resources)
            health=self.service.endpoint(self.key)
            self.assertEqual(health['loaded_models'],[])
            self.assertTrue(health['connected'])

    def test_endpoint_and_model_identity_cannot_be_supplied_as_arbitrary_urls(self):
        with self.assertRaises(WorkspaceError):self.service.endpoint('http://evil.test/v1')
        with self.models(),self.native(),self.assertRaises(WorkspaceError):self.service.resolve(self.key,'not-installed')
        bonsai=self.registry.entries[1]
        with self.models(),self.native(),self.assertRaises(WorkspaceError):self.service.resolve(endpoint_key(bonsai.endpoint),'another-installed:small')

    def test_conversation_and_scheduling_are_persisted_in_existing_queue(self):
        identifier=self.create();result=self.service.submit(identifier,'Hello selected model',64)
        restarted=ModelWorkspaces(self.db,self.registry,self.root/'resources')
        value=restarted.conversation(identifier)
        self.assertEqual(value['model_identifier'],self.entry.identifier)
        self.assertEqual(value['turns'][0]['user'],'Hello selected model')
        with QueueStore(self.db) as store:
            task=store.get(result['task_id']);contract=json.loads(task['contract_json'])
            self.assertEqual(task['state'],'pending');self.assertEqual(task['created_at'][:10],task['scheduled_at'][:10])
            self.assertEqual(contract['messages'],[{'role':'user','content':'Hello selected model'}])
            self.assertEqual(task['model_identifier'],self.entry.identifier)

    def test_parallel_turns_are_rejected_and_pending_cancellation_reuses_queue(self):
        identifier=self.create();first=self.service.submit(identifier,'First')
        with self.assertRaises(WorkspaceError):self.service.submit(identifier,'Second')
        self.service.cancel(identifier,first['task_id'])
        self.assertEqual(self.service.conversation(identifier)['turns'][0]['state'],'cancelled')
        self.service.submit(identifier,'Next')

    def test_pending_cancel_rolls_back_if_generation_write_fails(self):
        import sqlite3
        identifier=self.create();first=self.service.submit(identifier,'cancel fixture')
        with QueueStore(self.db) as store:
            store.db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON tasks_generation BEGIN SELECT RAISE(ABORT,'fixture'); END")
            store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):self.service.cancel(identifier,first['task_id'])
        with QueueStore(self.db) as store:
            self.assertEqual(store.get(first['task_id'])['state'],'pending')
            self.assertEqual(store.db.execute('SELECT cancel_requested FROM tasks_generation').fetchone()[0],0)

    def test_stale_feature_pid_cannot_claim_another_worker_lock(self):
        from chat_stream import record_worker_feature
        from cli import lock_path
        with QueueStore(self.db) as store:record_worker_feature(store.db,123,('birth','worker'))
        lock_path(self.db).write_text('456')
        with patch('cli.worker_active',return_value=True),patch('chat_sessions.Dependencies.identity',return_value=('birth','worker')):
            self.assertFalse(self.service.worker_support()['ready'])
        lock_path(self.db).write_text('123')
        with patch('cli.worker_active',return_value=True),patch('chat_sessions.Dependencies.identity',return_value=('birth','worker')):
            self.assertTrue(self.service.worker_support()['ready'])

    def test_cleanup_gates_history_and_retains_original_turns(self):
        identifier=self.create();first=self.service.submit(identifier,'Original')
        with QueueStore(self.db) as store:
            store.claim_due();store.complete(first['task_id'],'Actual fixture response','{}','fixture')
        with self.assertRaises(WorkspaceError):self.service.submit(identifier,'Follow up')
        with patch.object(self.service,'_released',return_value=True):
            second=self.service.submit(identifier,'Follow up')
            value=self.service.conversation(identifier)
            self.assertEqual(value['turns'][0]['response'],'Actual fixture response')
        with QueueStore(self.db) as store:
            messages=json.loads(store.get(second['task_id'])['contract_json'])['messages']
        self.assertEqual([m['role'] for m in messages],['user','assistant','user'])
        self.assertEqual(messages[0]['content'],'Original')

    def test_submission_is_atomic_on_queue_failure(self):
        identifier=self.create()
        with patch('chat_sessions.QueueStore.enqueue',side_effect=ValueError('failure')),self.assertRaises(ValueError):self.service.submit(identifier,'safe')
        self.assertEqual(self.service.conversation(identifier)['turns'],[])

    def test_legacy_worker_active_cancel_is_not_faked(self):
        identifier=self.create();first=self.service.submit(identifier,'test')
        with QueueStore(self.db) as store:store.claim_due()
        with patch.object(self.service,'worker_support',return_value={'ready':False}),self.assertRaises(WorkspaceError):self.service.cancel(identifier,first['task_id'])
        with QueueStore(self.db) as store:self.assertEqual(store.get(first['task_id'])['state'],'running')

    def test_incomplete_provider_model_listing_is_rejected(self):
        from models import LocalHTTP, ModelError
        for value in [{'data':{}},{'data':[{'id':2}]},{'data':[{}]},[]]:
            with self.subTest(value=value),patch('models.LocalHTTP._request',return_value=(200,json.dumps(value).encode())),self.assertRaises(ModelError):
                LocalHTTP(self.entry.endpoint,1).models()

    def test_provider_connection_failure_is_truthful_and_bounded(self):
        from models import ModelError
        with patch('chat_sessions.LocalHTTP.models',side_effect=ModelError('server offline')),patch('chat_sessions.Dependencies.native',side_effect=ModelError('no residency')):
            result=self.service.endpoint(self.key,True)
        self.assertFalse(result['connected']);self.assertIsNone(result['loaded_models']);self.assertIn('server offline',result['errors'])

    def test_invalid_content_and_token_limits_are_rejected(self):
        identifier=self.create()
        for content,limit in [('',64),('x'*8001,64),('Hi',True),('Hi',0),('Hi',self.entry.max_tokens+1)]:
            with self.subTest(content_length=len(content),limit=limit),self.assertRaises(WorkspaceError):self.service.submit(identifier,content,limit)

    def test_tool_activity_redacts_prompts_outputs_and_arguments(self):
        with QueueStore(self.db) as store:
            child=store.enqueue(model_family=self.entry.family,model_identifier=self.entry.identifier,endpoint=self.entry.endpoint,prompt='PRIVATE',scheduled_at='2026-01-01T00:00:00+00:00')
            store.db.executescript('CREATE TABLE capability_runs(id TEXT,model TEXT,state TEXT,created_at TEXT,completed_at TEXT); CREATE TABLE capability_events(seq INTEGER PRIMARY KEY,run_id TEXT,at TEXT,kind TEXT,data_json TEXT);')
            store.db.execute('INSERT INTO capability_runs VALUES(?,?,?,?,?)',('run','qwen','running','timestamp',None))
            for kind,value in [('capabilities_selected',{'capabilities':[{'id':'skill:video-editing','name':'video-editing','kind':'SKILL'}]}),('model_queued',{'job_id':child,'prompt':'PRIVATE','contract':{'auth':'PRIVATE'}}),('invoke_tool',{'requested':{'capability_id':'cli:ffprobe','arguments':{'source':'PRIVATE'}},'result':'PRIVATE'})]:
                store.db.execute('INSERT INTO capability_events(run_id,at,kind,data_json) VALUES(?,?,?,?)',('run','timestamp',kind,json.dumps(value)))
            store.db.commit()
        with self.models(),self.native():value=self.service.workspace(self.key,self.entry.identifier)
        self.assertNotIn('PRIVATE',json.dumps(value))
        self.assertEqual(value['agent_runs'][0]['capabilities'][0]['id'],'skill:video-editing')
        self.assertFalse(value['tool_execution'])

    def test_generation_usage_uses_provider_counts_not_text_length(self):
        identifier=self.create();first=self.service.submit(identifier,'test')
        with QueueStore(self.db) as store:
            store.db.execute('UPDATE tasks_generation SET content=?,usage_json=?,status=? WHERE task_id=?',('longer visible text',json.dumps({'input_tokens':5,'output_tokens':3,'total_tokens':8}), 'running', first['task_id']));store.db.commit()
        usage=self.service.conversation(identifier)['turns'][0]['usage']
        self.assertEqual(usage['output_tokens'],3);self.assertEqual(usage['output_source'],'provider_reported')


class WorkspaceHTTPTests(unittest.TestCase):
    def test_routes_security_static_module_and_connection_test(self):
        with tempfile.TemporaryDirectory() as root:
            app=WebApplication(Path(root)/'queue.sqlite3',DEFAULT_CONFIG,Path(root)/'resources',history_db=None,probe_enabled=False)
            server=LocalServer(('127.0.0.1',0),app)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def call(path,method='GET',payload=None,guard=False):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
                headers={'Content-Type':'application/json'}
                if guard:headers.update({'X-CSRF-Token':app.token,'Cookie':'cc_session='+app.token,'Origin':f'http://127.0.0.1:{server.server_port}'})
                connection.request(method,path,json.dumps(payload) if payload is not None else None,headers)
                r=connection.getresponse();body=r.read();status=r.status;connection.close();return status,body
            try:
                self.assertEqual(call('/static/workspaces.js')[0],200)
                key=endpoint_key(app.registry.entries[0].endpoint)
                self.assertEqual(call('/api/endpoints/'+key+'/test','POST',{})[0],403)
                with patch('chat_sessions.LocalHTTP.models',return_value=['provider-model']),patch('chat_sessions.Dependencies.native',return_value={'models':[]}):
                    status,body=call('/api/endpoints/'+key+'/test','POST',{},True)
                self.assertEqual(status,200);self.assertTrue(json.loads(body)['connected'])
                self.assertEqual(call('/api/conversations','POST',{'endpoint_id':key,'model_identifier':app.registry.entries[0].identifier},True)[0],201)
                self.assertEqual(call('/api/model-workspace?endpoint=evil&model=x')[0],404)
            finally:server.shutdown();server.server_close();thread.join();app.close()

if __name__=='__main__':unittest.main()
