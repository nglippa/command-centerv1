"""Controlled localhost worker interruptions; these never touch real models."""
import json
import subprocess
import time
import unittest
from pathlib import Path
import test_research as fixtures
FakeHandler=fixtures.FakeHandler
Server=fixtures.Server
from queue_store import QueueStore
from lifecycle import Manager, LifecycleError
from registry import ModelEntry, RegistryError
from test_lifecycle import FakeDeps, entries


class WorkerLifecycleTests(unittest.TestCase):
    setUp=fixtures.QueueTests.setUp
    tearDown=fixtures.QueueTests.tearDown
    enqueue=fixtures.QueueTests.enqueue
    config=fixtures.QueueTests.config
    worker_args=fixtures.QueueTests.worker_args
    worker=fixtures.QueueTests.worker
    def wait_for(self,predicate,timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.01)
        self.fail("Controlled fixture condition did not arrive")

    def row(self,tid):
        with QueueStore(self.db) as store: return store.get(tid)

    def launch(self,server,**changes):
        with QueueStore(self.db) as store: tid=self.enqueue(store,endpoint=server.endpoint,**changes)
        proc=subprocess.Popen(self.worker_args(server)+["--once"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(),proc.wait(timeout=3)))
        return tid,proc

    def test_sigterm_during_loading_unloads_without_inference(self):
        server=Server(); self.addCleanup(server.close); FakeHandler.load_delay=.25
        tid,proc=self.launch(server)
        self.wait_for(lambda:FakeHandler.loading); proc.terminate()
        self.assertEqual(proc.wait(timeout=4),0)
        self.assertEqual(self.row(tid)['state'],'failed')
        self.assertFalse(FakeHandler.loaded); self.assertFalse(FakeHandler.requests)

    def test_sigterm_during_unloading_keeps_result_and_finishes_cleanup(self):
        server=Server(); self.addCleanup(server.close); FakeHandler.unload_delay=.25
        tid,proc=self.launch(server)
        self.wait_for(lambda:FakeHandler.unloading)
        self.assertEqual(self.row(tid)['state'],'completed')
        proc.terminate(); self.assertEqual(proc.wait(timeout=4),0)
        self.assertFalse(FakeHandler.loaded); self.assertEqual(self.row(tid)['result'],'complete answer')

    def test_sigkill_during_unloading_result_survives_and_restart_reconciles(self):
        server=Server(); self.addCleanup(server.close); FakeHandler.unload_delay=.6
        tid,proc=self.launch(server)
        self.wait_for(lambda:FakeHandler.unloading)
        before=self.row(tid); self.assertEqual(before['state'],'completed'); self.assertTrue(before['lifecycle_session_id'])
        proc.kill(); proc.wait(timeout=3)
        FakeHandler.unload_delay=0
        self.assertEqual(self.worker(server),0)
        after=self.row(tid)
        for key in ('state','result','raw_response_json','completed_at'): self.assertEqual(before[key],after[key])
        self.assertFalse(FakeHandler.loaded); self.assertEqual(len(FakeHandler.requests),1)
        manager=Manager(Path(self.temp.name)/'resources',self.config(server))
        try:
            self.assertEqual(manager.store.status()['history'][0]['state'],'STOPPED')
            self.assertTrue(any(e['detail_json'].get('recovery_verified') for e in manager.store.status()['events']))
        finally: manager.store.close()

    def test_real_transport_inference_timeout_still_unloads(self):
        server=Server(); self.addCleanup(server.close); FakeHandler.delay=.2
        with QueueStore(self.db) as store: tid=self.enqueue(store,endpoint=server.endpoint,timeout_seconds=.05)
        self.assertEqual(self.worker(server),0)
        self.assertIn('timed out',self.row(tid)['error']); self.assertFalse(FakeHandler.loaded)

    def test_duplicate_worker_returns_two(self):
        from cli import acquire_lock
        handle=acquire_lock(self.db)
        try:
            from cli import run_worker
            self.assertEqual(run_worker(str(self.db),once=True),2)
        finally: handle.close()


class AdditionalLifecycleTests(unittest.TestCase):
    def test_registry_rejects_nonfinite_and_duplicate_flags(self):
        q,b=entries()
        for value in (float('nan'),float('inf')):
            q['load_timeout']=value
            with self.assertRaises(RegistryError): ModelEntry.from_dict(q)
        b['startup_command'] += ['--host','0.0.0.0']
        with self.assertRaises(RegistryError): ModelEntry.from_dict(b)

    def test_bonsai_crash_during_job_and_cleanup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            q,b=entries(); config=Path(temp)/'models.json'; config.write_text(json.dumps({'models':[q,b]}))
            d=FakeDeps(); original=d.complete
            def complete(e,prompt,max_tokens):
                if prompt!='Reply OK':
                    d.process.code=1; raise LifecycleError('Bonsai server crashed during inference')
                return original(e,prompt,max_tokens)
            d.complete=complete
            mgr=Manager(Path(temp)/'state',config,deps=d)
            try:
                with mgr.lease() as acquired:
                    self.assertTrue(acquired); mgr.reconcile()
                    outcome=mgr.execute({'registry_json':json.dumps(b),'prompt':'job'})
                self.assertIn('crashed during inference',outcome.error); self.assertIsNone(outcome.cleanup_error)
                self.assertEqual(mgr.store.status()['history'][0]['state'],'STOPPED')
            finally: mgr.store.close()

    def test_inference_saved_before_failed_unload_and_next_task_stays_pending(self):
        import tempfile
        from cli import run_worker
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            q,b=entries(); config=Path(temp)/'models.json'; config.write_text(json.dumps({'models':[q,b]}))
            d=FakeDeps(); d.unload_fail=True; db=Path(temp)/'queue.sqlite3'
            with QueueStore(db) as queue:
                ids=[queue.enqueue(model_family=e['family'],model_identifier=e['identifier'],endpoint=e['endpoint'],
                     prompt='job',scheduled_at='2000-01-01T00:00:00Z',registry_json=json.dumps(e)) for e in (q,b)]
            with patch('cli.Manager',side_effect=lambda root,conf,**kw:Manager(root,conf,deps=d,**kw)):
                self.assertEqual(run_worker(str(db),once=True,registry_path=config,resources_dir=Path(temp)/'state'),3)
            with QueueStore(db) as queue:
                self.assertEqual(queue.get(ids[0])['state'],'completed')
                self.assertTrue(queue.get(ids[0])['lifecycle_error'])
                self.assertEqual(queue.get(ids[1])['state'],'pending')


class LaunchProtocolTests(unittest.TestCase):
    def test_unregistered_child_exits_without_starting_runtime(self):
        import os,sys,tempfile
        from lifecycle import Dependencies
        with tempfile.TemporaryDirectory() as temp:
            marker=Path(temp)/"executed"
            d=Dependencies(); proc=d.popen([sys.executable,"-c",f"from pathlib import Path; Path({str(marker)!r}).write_text('started')"],{"PATH":os.environ["PATH"]})
            proc.stdin.close()  # worker dies before permission; pipe EOF
            self.assertEqual(proc.wait(timeout=3),0); self.assertFalse(marker.exists())

    def test_registered_child_starts_only_after_permission(self):
        import os,sys,tempfile
        from lifecycle import Dependencies
        with tempfile.TemporaryDirectory() as temp:
            marker=Path(temp)/"executed"
            d=Dependencies(); proc=d.popen([sys.executable,"-c",f"from pathlib import Path; Path({str(marker)!r}).write_text('started')"],{"PATH":os.environ["PATH"]})
            self.assertIsNone(proc.poll()); self.assertFalse(marker.exists())
            d.release_start(proc); self.assertEqual(proc.wait(timeout=3),0); self.assertEqual(marker.read_text(),'started')

    def test_unguarded_untracked_start_blocks_recovery(self):
        import tempfile
        from lifecycle import ResourceBlocked
        with tempfile.TemporaryDirectory() as temp:
            q,b=entries(); config=Path(temp)/'models.json'; config.write_text(json.dumps({'models':[q,b]}))
            d=FakeDeps(); d.identity_value=None
            mgr=Manager(Path(temp)/'state',config,deps=d)
            try:
                sid=mgr.store.create(ModelEntry.from_dict(b),'old')
                mgr.store.transition(sid,'STARTING',owned_intent=1)
                with mgr.lease() as acquired:
                    self.assertTrue(acquired)
                    with self.assertRaisesRegex(ResourceBlocked,'no durable PID'): mgr.reconcile()
            finally: mgr.store.close()

    def test_gate_permission_requires_durable_exact_pid_identity(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            q,b=entries(); config=Path(temp)/'models.json'; config.write_text(json.dumps({'models':[q,b]}))
            d=FakeDeps(); mgr=Manager(Path(temp)/'state',config,deps=d)
            def release(process):
                row=mgr.store.status()['history'][0]
                self.assertEqual(row['state'],'LOADING'); self.assertEqual(row['pid'],process.pid)
                self.assertEqual(row['pid_birth'],'birth'); self.assertEqual(row['executable'],b['runtime_path'])
                self.assertTrue(row['observations_json']['launch_guarded'])
            d.release_start=release
            try:
                with mgr.lease() as acquired:
                    self.assertTrue(acquired); mgr.reconcile()
                    out=mgr.execute({'registry_json':json.dumps(b),'prompt':'job'})
                self.assertIsNone(out.error); self.assertIsNone(out.cleanup_error)
            finally: mgr.store.close()


class ResponseEvidenceTests(unittest.TestCase):
    def test_rejected_qwen_response_is_retained_and_unloaded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            q,b=entries(); config=Path(temp)/'models.json'; config.write_text(json.dumps({'models':[q,b]}))
            d=FakeDeps(); original=d.native
            def native(e,method,path,payload,timeout):
                if path=='/api/chat' and payload['messages'][0]['content']!='Reply OK':
                    return {'done':True,'model':'wrong-model','message':{'content':'untrusted generated response'}}
                return original(e,method,path,payload,timeout)
            d.native=native; mgr=Manager(Path(temp)/'state',config,deps=d)
            try:
                with mgr.lease() as acquired:
                    self.assertTrue(acquired); mgr.reconcile()
                    out=mgr.execute({'registry_json':json.dumps(q),'prompt':'job'})
                self.assertIn('wrong-model',out.error); self.assertIn('untrusted generated response',out.raw_json)
                self.assertFalse(d.loaded); self.assertIsNone(out.cleanup_error)
            finally: mgr.store.close()
