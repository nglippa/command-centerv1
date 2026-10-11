from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lifecycle import Dependencies, HeavyLease, LifecycleError, Manager, ResourceBlocked
from registry import ModelEntry, Registry, RegistryError


class FakeProcess:
    def __init__(self,pid=4321,crashed=False,stubborn=False):
        self.pid=pid; self.code=1 if crashed else None; self.stubborn=stubborn
        self.terminated=False; self.killed=False
    def poll(self): return self.code
    def terminate(self): self.terminated=True; self.code=None if self.stubborn else 0
    def kill(self): self.killed=True; self.code=-9
    def wait(self,timeout=None):
        if self.code is None: raise subprocess.TimeoutExpired("fake",timeout)
        return self.code


class FakeDeps(Dependencies):
    def __init__(self):
        self.loaded=[]; self.calls=[]; self.clock=0.; self.files=True; self.version_ok=True
        self.listener=None; self.process=FakeProcess(); self.health=True; self.props_ok=True
        self.malformed_ps=False; self.unload_fail=False; self.unload_stuck=False
        self.chat_fail=False; self.crash=False; self.identity_value=("birth","runtime")
        self.replace_dead=True
    def native(self,e,method,path,payload,timeout):
        self.calls.append((method,path,payload))
        if path=="/api/tags": return {"models":[{"name":e.identifier}]}
        if path=="/api/ps":
            if self.malformed_ps:return {}
            return {"models":[{"name":x,"context_length":e.context_window} for x in self.loaded]}
        if path=="/api/generate":
            if payload.get("keep_alive")==0:
                if self.unload_fail:return {"done":False,"model":e.identifier}
                if not self.unload_stuck:self.loaded=[x for x in self.loaded if x!=e.identifier]
                return {"done":True,"model":e.identifier,"done_reason":"unload"}
            self.loaded=[e.identifier]; return {"done":True,"model":e.identifier}
        if path=="/api/chat":
            if self.chat_fail and payload["messages"][0]["content"]!="Reply OK": raise LifecycleError("inference interrupted")
            return {"done":True,"model":e.identifier,"message":{"content":"OK"}}
        if path=="/health":
            if not self.health: raise LifecycleError("unresponsive")
            return {"status":"ok"}
        if path=="/v1/models": return {"data":[{"id":e.identifier}]}
        if path=="/props": return {"model_path":e.model_path if self.props_ok else "/wrong"}
        raise AssertionError(path)
    def complete(self,e,prompt,max_tokens):
        self.calls.append(("COMPLETE",prompt,max_tokens)); return "answer",json.dumps({"answer":"answer"})
    def path_file(self,path,executable=False): return self.files
    def run(self,argv,timeout):
        text="build e8fd209c9" if self.version_ok else "wrong"
        return subprocess.CompletedProcess(argv,0,text,"")
    def popen(self,argv,env):
        self.calls.append(("POPEN",argv,env)); self.identity_value=("birth",argv[0])
        if self.replace_dead and self.process.poll() is not None:self.process=FakeProcess(self.process.pid+1)
        return self.process
    def release_start(self,process): self.calls.append(("START_ALLOWED",process.pid))
    def lsof_pid(self,port):
        if self.process and self.process.poll() is None and any(c[0]=="POPEN" for c in self.calls): return self.process.pid
        return self.listener
    def identity(self,pid): return self.identity_value
    def diagnostics(self,pid): return {"rss_kb":"123"}
    def sleep(self,s): self.clock += max(s,.1)
    def monotonic(self): return self.clock


def entries(reuse=False):
    q={"family":"qwen","identifier":"qwen3:14b-q8_0","endpoint":"http://127.0.0.1:11434/v1",
       "provider":"ollama","context_window":4096,"load_timeout":1,"unload_timeout":1,"health_timeout":.1}
    path="/models/Ternary-Bonsai-2-27B-PQ2_0.gguf"; runtime="/bin/llama-server"
    b={"family":"bonsai","identifier":"Ternary-Bonsai-2-27B-PQ2_0","endpoint":"http://127.0.0.1:8080/v1",
       "provider":"bonsai","context_window":4096,"load_timeout":1,"unload_timeout":1,"health_timeout":.1,
       "reuse_external":reuse,"model_path":path,"runtime_path":runtime,"runtime_version_contains":"e8fd209",
       "startup_command":[runtime,"-m",path,"--alias","Ternary-Bonsai-2-27B-PQ2_0","--host","127.0.0.1","--port","8080"]}
    return q,b


class TestManager(Manager):
    """Exercise the required lease -> reconcile -> execute caller contract."""
    def execute(self,task):
        with self.lease() as acquired:
            if not acquired: raise AssertionError("test lease unavailable")
            Manager.reconcile(self)
            return Manager.execute(self,task)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self._stores=[]
        q,b=entries(); self.config=self.root/"models.json"
        self.config.write_text(json.dumps({"models":[q,b]})); self.q=q; self.b=b
    def tearDown(self):
        for store in self._stores: store.close()
        self.tmp.cleanup()
    def manager(self,d=None,stopping=lambda:False):
        manager=TestManager(self.root/"state",self.config,stopping,d or FakeDeps())
        self._stores.append(manager.store)
        return manager
    def task(self,e): return {"id":"t","registry_json":json.dumps(e),"prompt":"secret prompt","max_tokens":16,"timeout_seconds":2}

    def test_01_registry_exact_snapshot_and_export(self):
        r=Registry(self.config); e=r.resolve_task(self.task(self.q)); self.assertEqual(e.export()["identifier"],self.q["identifier"])
    def test_02_historical_exact_only_and_no_substitution(self):
        r=Registry(self.config); self.assertEqual(r.resolve_task({"model_identifier":self.q["identifier"],"endpoint":self.q["endpoint"]}).provider,"ollama")
        with self.assertRaises(RegistryError): r.resolve_task({"model_identifier":"other","endpoint":self.q["endpoint"]})
    def test_03_discovered_clone_same_endpoint_only(self):
        r=Registry(self.config); e=r.entries[0]; self.assertEqual(r.clone_discovered(e,"qwen-new",e.endpoint).identifier,"qwen-new")
        with self.assertRaises(RegistryError): r.clone_discovered(e,"q","http://127.0.0.1:1/v1")
    def test_04_successful_qwen_load_infer_unload(self):
        d=FakeDeps(); o=self.manager(d).execute(self.task(self.q)); self.assertEqual(o.result,"OK"); self.assertFalse(d.loaded); self.assertIsNone(o.cleanup_error)
    def test_05_qwen_missing_never_pulls(self):
        d=FakeDeps(); d.native=lambda e,m,p,x,t: {"models":[]} if p=="/api/tags" else {"models":[]}
        o=self.manager(d).execute(self.task(self.q)); self.assertIn("not installed",o.error); self.assertFalse(any(c[1]=="/api/pull" for c in d.calls))
    def test_06_malformed_ps_fails_closed(self):
        d=FakeDeps(); d.malformed_ps=True; o=self.manager(d).execute(self.task(self.q)); self.assertIn("models list",o.error)
    def test_07_failed_unload_persists_blocker(self):
        d=FakeDeps(); d.unload_fail=True; m=self.manager(d); o=m.execute(self.task(self.q)); self.assertIsNotNone(o.cleanup_error)
        with m.lease() as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(ResourceBlocked): Manager.reconcile(m)
    def test_08_unload_timeout_blocks(self):
        d=FakeDeps(); d.unload_stuck=True; o=self.manager(d).execute(self.task(self.q)); self.assertIn("deadline",o.cleanup_error)
    def test_09_external_ollama_model_never_unloaded(self):
        d=FakeDeps(); d.loaded=[self.q["identifier"]]; o=self.manager(d).execute(self.task(self.q)); self.assertIn("External loaded",o.error); self.assertEqual(d.loaded,[self.q["identifier"]]); self.assertIsNotNone(o.cleanup_error)
    def test_10_cross_queue_global_lease(self):
        one=HeavyLease(self.root/"state")
        with one.acquire() as acquired:
            self.assertTrue(acquired)
            with HeavyLease(self.root/"state").acquire() as second:self.assertFalse(second)
            code=("from lifecycle import HeavyLease; import sys; "
                  "c=HeavyLease(sys.argv[1]).acquire(); x=c.__enter__(); c.__exit__(None,None,None); "
                  "raise SystemExit(0 if not x else 9)")
            child=subprocess.run([sys.executable,"-c",code,str(self.root/"state")],cwd=Path(__file__).parent)
            self.assertEqual(child.returncode,0)
    def test_11_bonsai_start_ready_infer_stop(self):
        d=FakeDeps(); o=self.manager(d).execute(self.task(self.b)); self.assertEqual(o.result,"answer"); self.assertTrue(d.process.terminated); self.assertIsNone(o.cleanup_error)
    def test_12_bonsai_missing_is_unavailable_without_start(self):
        d=FakeDeps(); d.files=False; o=self.manager(d).execute(self.task(self.b)); self.assertIn("missing",o.error); self.assertFalse(any(c[0]=="POPEN" for c in d.calls))
    def test_13_bonsai_wrong_runtime_pin_refused(self):
        d=FakeDeps(); d.version_ok=False; o=self.manager(d).execute(self.task(self.b)); self.assertIn("fork pin",o.error)
    def test_14_bonsai_unresponsive_health_times_out_and_stops(self):
        d=FakeDeps(); d.health=False; o=self.manager(d).execute(self.task(self.b)); self.assertIn("deadline",o.error); self.assertTrue(d.process.terminated)
    def test_15_bonsai_server_crash_detected(self):
        d=FakeDeps(); d.process=FakeProcess(crashed=True); d.replace_dead=False
        o=self.manager(d).execute(self.task(self.b)); self.assertIn("crashed",o.error)
    def test_16_bonsai_stubborn_server_killed(self):
        d=FakeDeps(); d.process=FakeProcess(stubborn=True); o=self.manager(d).execute(self.task(self.b)); self.assertEqual(o.result,"answer"); self.assertTrue(d.process.killed)
    def test_17_external_bonsai_reuse_never_stopped_and_blocks(self):
        q,b=entries(True); self.config.write_text(json.dumps({"models":[q,b]})); d=FakeDeps(); d.listener=999
        o=self.manager(d).execute(self.task(b)); self.assertEqual(o.result,"answer"); self.assertIsNotNone(o.cleanup_error); self.assertFalse(d.process.terminated)
    def test_18_stop_during_load_cleans_owned_process(self):
        d=FakeDeps(); o=self.manager(d,lambda:any(c[0]=="POPEN" for c in d.calls)).execute(self.task(self.b)); self.assertIn("Stopped",o.error); self.assertTrue(d.process.terminated)
    def test_19_inference_failure_still_unloads_qwen(self):
        d=FakeDeps(); d.chat_fail=True; o=self.manager(d).execute(self.task(self.q)); self.assertIn("interrupted",o.error); self.assertFalse(d.loaded)
    def test_20_repeated_switch_both_directions(self):
        d=FakeDeps(); m=self.manager(d)
        for e in (self.q,self.b,self.q,self.b): self.assertIsNotNone(m.execute(self.task(e)).result)
    def test_21_stale_pid_reuse_never_signaled(self):
        d=FakeDeps(); m=self.manager(d); e=ModelEntry.from_dict(self.b); sid=m.store.create(e,"t")
        m.store.transition(sid,"STARTING"); m.store.transition(sid,"ERROR",blocker="stale",pid=7,pid_birth="old",executable="old")
        d.identity_value=("new","other"); d.listener=None
        with m.lease() as acquired:
            self.assertTrue(acquired); Manager.reconcile(m)
        self.assertFalse(d.process.terminated)
    def test_22_inference_success_survives_cleanup_failure(self):
        d=FakeDeps(); d.unload_fail=True; o=self.manager(d).execute(self.task(self.q)); self.assertEqual(o.result,"OK"); self.assertIsNotNone(o.cleanup_error); self.assertIsNone(o.error)
    def test_23_history_never_contains_prompt(self):
        d=FakeDeps(); m=self.manager(d); m.execute(self.task(self.q)); blob=json.dumps(m.store.status()); self.assertNotIn("secret prompt",blob)
    def test_24_missing_final_state_unavailable_without_blocker(self):
        d=FakeDeps(); d.files=False; m=self.manager(d); o=m.execute(self.task(self.b)); row=m.store.status()["history"][0]
        self.assertEqual(row["state"],"UNAVAILABLE"); self.assertIsNone(row["blocker"]); self.assertIsNone(o.cleanup_error)
    def test_25_inference_error_then_cleanup_final_stopped(self):
        d=FakeDeps(); d.chat_fail=True; m=self.manager(d); o=m.execute(self.task(self.q)); row=m.store.status()["history"][0]
        self.assertIn("interrupted",o.error); self.assertEqual(row["state"],"STOPPED")
    def test_26_reconcile_owned_qwen_without_preexisting_blocker(self):
        d=FakeDeps(); m=self.manager(d); e=ModelEntry.from_dict(self.q); sid=m.store.create(e,"old")
        m.store.transition(sid,"LOADING",owned_intent=1); d.loaded=[e.identifier]
        with m.lease() as acquired:self.assertTrue(acquired); Manager.reconcile(m)
        self.assertEqual(d.loaded,[]); self.assertEqual(m.store.state(sid),"STOPPED")
    def test_27_execute_requires_lease_and_reconcile(self):
        with self.assertRaises(LifecycleError): Manager.execute(self.manager(),self.task(self.q))
    def test_28_cross_provider_preflight_blocks_without_touching_external(self):
        d=FakeDeps(); d.listener=999; m=self.manager(d); o=m.execute(self.task(self.q))
        self.assertIn("Bonsai listener",o.error); self.assertFalse(any(c[1]=="/api/generate" for c in d.calls if len(c)>1))
    def test_29_readonly_probe_has_no_mutations(self):
        d=FakeDeps(); m=self.manager(d); before=len(d.calls); result=m.probe_live()
        self.assertEqual(len(result["models"]),2); self.assertFalse(any(c[0] in ("POST","POPEN") for c in d.calls[before:]))
    def test_30_stop_after_qwen_load_still_unloads(self):
        d=FakeDeps(); m=self.manager(d,lambda:bool(d.loaded)); o=m.execute(self.task(self.q))
        self.assertIn("Stopped during Ollama load",o.error); self.assertEqual(d.loaded,[]); self.assertIsNone(o.cleanup_error)


if __name__=="__main__": unittest.main()
