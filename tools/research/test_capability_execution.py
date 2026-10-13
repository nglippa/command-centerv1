import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from capabilities import build_catalog, _mcp_names, _safe_mcp_metadata, route_capabilities
from capability_run import CapabilityCatalog, CapabilityRun, EvidenceStore, select_for_task
from capability_tools import ToolExecutor, StdioMCP, bounded_command, validate_input, augment_catalog, mcp_health


def envelope(action, identifier='', arguments=None, text=''):
    return json.dumps({'action':action, 'capability_id':identifier, 'arguments':arguments or {}, 'text':text})


class CapabilityExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'queue.sqlite3'
        self.skill = self.root / 'guidance' / 'SKILL.md'
        self.skill.parent.mkdir()
        self.skill.write_text('---\nname: cinematic-video\ndescription: Plan cinematic shots and visual storytelling.\n---\n# Direction\nUse observed evidence; do not claim to generate pixels.\n')
        self.records = build_catalog(self.root, extra_skill_roots=[self.root], include_defaults=False,
                                     include_cli=False, include_services=False)
        self.records.append({'id':'native:repo-status', 'name':'Repository status', 'kind':'NATIVE_TOOL',
            'description':'Inspect repository status.', 'primary_use_cases':['repository status'],
            'secondary_use_cases':[], 'supported_operations':['inspect_revision_and_changes'],
            'execution_transport':'SUBPROCESS','availability':'CONFIGURED','configuration_status':'CONFIGURED',
            'security_constraints':['read-only'],'documentation_path':'fixed','callable':True,
            'when_to_use':['repository status'],'when_not_to_use':[],
            'invocation':{'arguments':{'project':'command-center'}}})
        self.catalog = CapabilityCatalog(self.root, records=self.records)
        self.skill_id = self.records[0]['id']
        self.submitted, self.cancelled = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_bridge(self, responses, *, cleanup_error=None, worker=True, polls=0):
        iterator = iter(responses)
        current = [None]
        remaining = [polls]
        def call(value):
            action = value['action']
            if action == 'status':return {'workerActive':worker, 'resource':{'blocker':None}}
            if action == 'submit':
                self.submitted.append(value['job']);current[0] = next(iterator)
                return {'jobId':value['job']['jobId'], 'state':'pending', 'readyForConsumer':False, 'lifecycle':{}}
            if action == 'cancel':
                self.cancelled.append(value['jobId']);return {'state':'running','cancelled':False}
            if remaining[0]:
                remaining[0]-=1
                return {'state':'running','readyForConsumer':False,'lifecycle':{}}
            return {'state':'completed','readyForConsumer':not bool(cleanup_error), 'result':current[0],
                'contract':self.submitted[-1], 'lifecycle':{'state':'STOPPED','verifiedUnloadAt':'verified',
                'cleanupError':cleanup_error,'blocker':None}}
        return call

    def runner(self, responses, **kwargs):
        class E:
            calls=[]
            def invoke(self, identifier, arguments):
                self.calls.append((identifier,arguments));return {'status':'completed','output':{'revision':'verified'}}
        executor = E()
        return CapabilityRun(self.db, resources=self.root, catalog=self.catalog,
                             request=self.fake_bridge(responses, **kwargs), executor=executor, sleep=lambda _:None), executor

    def test_existing_bridge_contract_tool_loop_and_provenance(self):
        original='Cinematic task: read the skill then inspect repository status.'
        runner, executor=self.runner([envelope('read_skill',self.skill_id),
            envelope('invoke_tool','native:repo-status',{'project':'command-center'}),envelope('finish',text='Evidence-based final')])
        result=runner.run(original,'qwen',max_calls=3)
        self.assertEqual(result['run']['state'],'completed')
        self.assertEqual(result['run']['task'],original)
        self.assertEqual(len(executor.calls),1)
        self.assertTrue(all(j['outputSchema']['type']=='object' for j in self.submitted))
        kinds=[e['kind'] for e in result['events']]
        self.assertIn('read_skill',kinds);self.assertIn('invoke_tool',kinds)
        self.assertEqual(kinds.count('model_response'),3)
        self.assertIn('not permission grants',self.submitted[0]['prompt'])
        store=EvidenceStore(self.db)
        try:self.assertEqual(store.get(result['run']['id'])['run']['result'],'Evidence-based final')
        finally:store.close()

    def test_skill_request_matching_redundant_id_is_validated(self):
        runner,_=self.runner([envelope('read_skill',self.skill_id,{'capability_id':self.skill_id}),envelope('finish',text='done')])
        value=runner.run('cinematic guidance','qwen')
        self.assertEqual(value['run']['state'],'completed')
        self.assertIn('read_skill',[e['kind'] for e in value['events']])

    def test_skill_request_mismatched_redundant_id_is_rejected(self):
        runner,_=self.runner([envelope('read_skill',self.skill_id,{'capability_id':'skill:other'}),envelope('finish',text='done')])
        value=runner.run('cinematic guidance','qwen')
        self.assertNotIn('read_skill',[e['kind'] for e in value['events']])
        self.assertIn('tool_rejected',[e['kind'] for e in value['events']])

    def test_bonsai_role_and_capability_selection(self):
        runner,_=self.runner([envelope('finish',text='Editorial evidence')])
        self.assertEqual(runner.run('Cinematic clarity review','bonsai')['run']['state'],'completed')
        self.assertIn('Review editorial clarity',self.submitted[0]['prompt'])
        self.assertIn('cinematic-video',self.submitted[0]['prompt'])
        self.assertNotIn('Role: Generate plans',self.submitted[0]['prompt'])

    def test_bonsai_leading_thinking_marker_uses_existing_strict_parser(self):
        runner,_=self.runner(['<think>\n\n</think>\n'+envelope('finish',text='Verified editorial reply')])
        self.assertEqual(runner.run('editorial review','bonsai')['run']['state'],'completed')

    def test_recovery_records_interruption_without_replaying_children(self):
        store=EvidenceStore(self.db)
        try:
            store.db.execute('INSERT INTO capability_runs(id,model,task,created_at,state,settings_json,owner_pid,owner_identity) VALUES(?,?,?,?,?,?,?,?)',
                ('stale','qwen','original','timestamp','running','{}','123','["old birth","other process"]'))
            store.db.commit()
            with patch('capability_run.Dependencies.identity',return_value=('new birth','unrelated')):
                self.assertEqual(store.recover(),['stale'])
            value=store.get('stale')
            self.assertEqual(value['run']['state'],'interrupted')
            self.assertEqual(value['run']['task'],'original')
            self.assertEqual([e['kind'] for e in value['events']],['recovery'])
            self.assertFalse(self.submitted)
        finally:store.close()

    def test_failed_unload_blocks_tool_execution(self):
        runner,executor=self.runner([envelope('invoke_tool','native:repo-status',{'project':'command-center'})],cleanup_error='unload unverified')
        result=runner.run('repository status','qwen')
        self.assertEqual(result['run']['state'],'failed')
        self.assertIn('cleanup unverified',result['run']['error']);self.assertFalse(executor.calls)
        self.assertEqual(len(self.cancelled),1)

    def test_worker_offline_does_not_enqueue(self):
        runner,_=self.runner([],worker=False)
        with self.assertRaisesRegex(ValueError,'offline'):runner.run('repository status','qwen')
        self.assertFalse(self.submitted)

    def test_unknown_or_unavailable_tool_is_not_executable(self):
        runner,executor=self.runner([envelope('invoke_tool','service:comfyui',{}),envelope('finish',text='Unavailable; no generation')])
        result=runner.run('cinematic task','qwen')
        self.assertFalse(executor.calls)
        self.assertIn('tool_rejected',[e['kind'] for e in result['events']])

    def test_invalid_model_envelope_is_preserved_as_failure(self):
        runner,_=self.runner(['not JSON'])
        result=runner.run('repository status','qwen')
        self.assertEqual(result['run']['state'],'failed')
        self.assertIn('model_response',[e['kind'] for e in result['events']])

    def test_call_budget_ends_without_infinite_retry(self):
        runner,_=self.runner([envelope('describe_capability',self.skill_id)])
        result=runner.run('cinematic task','qwen',max_calls=1)
        self.assertIn('Maximum model-call',result['run']['error']);self.assertEqual(len(self.submitted),1)

    def test_cancelled_run_never_dispatches_tool(self):
        runner,executor=self.runner([envelope('invoke_tool','native:repo-status',{'project':'command-center'})])
        checks=[False,True]
        runner.stopping=lambda: checks.pop(0) if checks else True
        result=runner.run('repository status','qwen')
        self.assertEqual(result['run']['state'],'cancelled');self.assertFalse(executor.calls)

    def test_runtime_timeout_retains_child_and_requests_pending_cancellation(self):
        runner,executor=self.runner([envelope('finish',text='late')],polls=100)
        t=[0]
        def clock():t[0]+=1;return t[0]
        runner.clock=clock
        result=runner.run('repository status','qwen',runtime=3)
        self.assertEqual(result['run']['state'],'failed');self.assertFalse(executor.calls)
        self.assertIn('runtime budget',result['run']['error']);self.assertTrue(self.cancelled)

    def test_metadata_pagination_and_search(self):
        page=self.catalog.page('cinematic',kind='SKILL',limit=1)
        self.assertEqual(page['total'],1);self.assertEqual(page['items'][0]['id'],self.skill_id)
        with self.assertRaises(ValueError):self.catalog.page(limit=1000)

    def test_no_executable_from_skill_or_raw_model_arguments(self):
        executor=ToolExecutor(self.records)
        with self.assertRaises(PermissionError):executor.invoke(self.skill_id,{})
        with self.assertRaises(ValueError):executor.invoke('native:repo-status',{'project':'command-center','command':'rm'})
        with self.assertRaises(ValueError):executor.invoke('native:repo-status',{'project':'../../secret'})

    def test_ffprobe_unapproved_path_never_spawns(self):
        records=[{'id':'cli:ffprobe','callable':True,'execution_transport':'SUBPROCESS'}]
        with patch('capability_tools.bounded_command') as command:
            with self.assertRaises(PermissionError):ToolExecutor(records).invoke('cli:ffprobe',{'source_id':'secret'})
            command.assert_not_called()

    def test_command_timeout_and_output_limit_owned_children(self):
        with self.assertRaises(TimeoutError):bounded_command([sys.executable,'-c','import time;time.sleep(5)'],timeout=.05)
        with self.assertRaisesRegex(ValueError,'byte limit'):bounded_command([sys.executable,'-c','print("x"*100000)'],limit=1000)

    def test_mcp_subtables_not_miscounted_as_servers_and_credentials_hidden(self):
        path=self.root/'config.toml'
        path.write_text('[mcp_servers.safe]\ncommand="echo"\n[mcp_servers.safe.env]\nTOKEN="private"\n[mcp_servers.safe.tools.read]\napproval_mode="approve"\n')
        self.assertEqual(_mcp_names(path),['safe'])
        metadata=_safe_mcp_metadata({'url':'https://user:private@example.com/mcp?token=private','headers':{'Authorization':'private'}})
        self.assertNotIn('private',json.dumps(metadata));self.assertEqual(metadata['configured_transport'],'MCP_HTTP')

    def test_strict_schema_types_and_unknown_constructs(self):
        for value in (True,1.5,'3'):
            with self.assertRaises(ValueError):validate_input(value,{'type':'integer'})
        with self.assertRaises(ValueError):validate_input({}, {'type':'object','anyOf':[]})
        with self.assertRaises(ValueError):validate_input({'unknown':1},{'type':'object','properties':{}})

    def test_mcp_permission_is_independent_of_server_annotations(self):
        schema={'type':'object','properties':{'limit':{'type':'integer','minimum':1,'maximum':500}}}
        client=object.__new__(StdioMCP)
        client.tools=lambda:[{'name':'list_projects','inputSchema':schema,'annotations':{'readOnlyHint':False}}]
        client.server_info={'name':'codebase-memory-mcp'}
        sent=[]
        client.request=lambda method,params: sent.append((method,params)) or {'content':[]}
        with self.assertRaises(PermissionError):client.call('delete_project',{})
        with self.assertRaises(PermissionError):client.call('list_projects',{'limit':500})
        result=client.call('list_projects',{'limit':5})
        self.assertEqual(result['input_schema'],schema)
        self.assertEqual(sent[-1][1]['arguments']['metadata_only'],True)

    def test_unrelated_video_capabilities_not_routed_for_transcription(self):
        names=['whisper-cli','ffmpeg','cinematic-video','remotion-maps']
        records=[{'id':('cli:' if i<2 else 'skill:')+name,'name':name,'kind':'CLI_TOOL' if i<2 else 'SKILL',
                  'description':'video transcription','primary_use_cases':[]} for i,name in enumerate(names)]
        self.assertEqual([r['id'] for r in route_capabilities('Transcribe this video',catalog=records)],['cli:whisper-cli','cli:ffmpeg'])


if __name__=='__main__':unittest.main()
