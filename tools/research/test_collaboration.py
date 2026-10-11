from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from collaboration import CollaborationRunner,ParseError,comparison_text,parse_role_json
from collaboration_store import CollaborationStore
from validation import better,normalize_spec,validate_artifact

def model(identifier):
    return {"family":identifier,"identifier":identifier,"endpoint":"http://127.0.0.1:11434/v1","provider":"ollama",
            "owner_mode":"shared","max_tokens":128,"timeout_seconds":5,"context_window":4096,
            "load_timeout":5,"unload_timeout":2,"health_timeout":1}

def spec(semantic=False,rounds=1,**changes):
    value={"name":"test","instruction":"Improve exactly within constraints.","source":"fictional",
           "original":"Draft text","format":"text","requires_semantic_verification":semantic,
           "limits":{"rounds":rounds,"calls":12,"runtime":120,"output_chars":12000,"total_output_chars":80000,"max_tokens":128,"retries":0},
           "constraints":{"required_content":["Pebble"]},"metrics":{"target_words":2}}
    value.update(changes);return value

def candidate(text):return json.dumps({"artifact":text})
def review(verdict="revise"):return json.dumps({"verdict":verdict,"comments":["c"],"suggestions":["s"],"unsupported_changes":[]})
def evaluation(accept=True):return json.dumps({"accepted_feedback":["s"] if accept else [],"rejected_feedback":[] if accept else ["s"],"rationale":"bounded"})

class FakeLifecycleStore:
    def status(self):return {"history":[{"id":"session","state":"STOPPED","load_ms":1,"infer_ms":2,"unload_ms":1,"last_verified_unload":"now","observations":{},"diagnostics":{}}]}

class FakeManager:
    def __init__(self,responses,cleanup_at=None,error_at=None):
        self.responses=list(responses);self.cleanup_at=cleanup_at;self.error_at=error_at;self.calls=0;self.reconciles=0
        self.stopping=lambda:False;self.store=FakeLifecycleStore()
    def reconcile(self):self.reconciles+=1
    def execute(self,task,on_result=None):
        self.calls+=1
        if self.error_at==self.calls:return SimpleNamespace(result=None,raw_json=None,error="unavailable",cleanup_error=None,session_id="session")
        result=self.responses.pop(0);raw=json.dumps({"model":task["model_identifier"],"content":result})
        if on_result:on_result(result,raw,"session")
        return SimpleNamespace(result=result,raw_json=raw,error=None,
                               cleanup_error="unload failed" if self.cleanup_at==self.calls else None,session_id="session")

class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/"queue.sqlite3";self.store=CollaborationStore(self.db)
        self.models={"qwen":model("qwen-test"),"bonsai":model("bonsai-test")}
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def claimed(self,raw=None):
        rid=self.store.enqueue(raw or spec(),self.models);return self.store.claim_due(),rid
    def good(self):return [candidate("Pebble text"),review(),evaluation(),candidate("Pebble text"),review("approve")]

    def test_success_and_evidence(self):
        run,rid=self.claimed();manager=FakeManager(self.good());code=CollaborationRunner(self.store,manager).run(run)
        row=self.store.get(rid);self.assertEqual((code,row["state"]),(0,"completed"));self.assertEqual(manager.reconciles,manager.calls)
        evidence=self.store.evidence(rid);self.assertTrue(any(e["kind"]=="call_received" for e in evidence["events"]));self.assertIn("Qwen-Only",comparison_text(evidence).title())
        prompt=next(e["data"]["prompt"] for e in evidence["events"] if e["kind"]=="call_started")
        self.assertIn("RETURN ONLY JSON",prompt);self.assertIn('"source"',prompt);self.assertIn('"output_schema"',prompt)

    def test_unavailable(self):
        run,rid=self.claimed();code=CollaborationRunner(self.store,FakeManager([],error_at=1)).run(run)
        self.assertEqual(code,1);self.assertEqual(self.store.get(rid)["state"],"failed")

    def test_repeated_disagreement_stops(self):
        responses=[candidate("Pebble text has lots of filler words indeed"),review(),evaluation(False),candidate("Pebble text has filler"),review(),evaluation(False)]
        run,rid=self.claimed(spec(rounds=2));manager=FakeManager(responses);self.assertEqual(CollaborationRunner(self.store,manager).run(run),0)
        decisions=[e for e in self.store.evidence(rid)["events"] if e["kind"]=="decision"]
        self.assertEqual(decisions[-1]["data"]["reason"],"repeated_disagreement")

    def test_protected_invalid_revision_stops(self):
        raw=spec();raw["original"]="Pebble KEEP";raw["constraints"]={"required_content":["Pebble"],"protected_claims":["KEEP"]}
        responses=[candidate("Pebble KEEP"),review(),evaluation(),candidate("Pebble changed")]
        run,rid=self.claimed(raw);CollaborationRunner(self.store,FakeManager(responses)).run(run)
        self.assertTrue(any(e["kind"]=="decision" and e["data"].get("reason")=="protected_requirement_violation" for e in self.store.evidence(rid)["events"]))

    def test_no_improvement_keeps_valid_incumbent(self):
        raw=spec();raw["original"]="Pebble text"
        run,rid=self.claimed(raw);CollaborationRunner(self.store,FakeManager(self.good())).run(run)
        self.assertEqual(self.store.get(rid)["outcome"]["artifact"],"Pebble text")

    def test_timeout_before_call(self):
        ticks=iter([0,1000]);run,rid=self.claimed();runner=CollaborationRunner(self.store,FakeManager([]),clock=lambda:next(ticks))
        self.assertEqual(runner.run(run),1);self.assertEqual(self.store.get(rid)["state"],"failed")

    def test_cancel(self):
        run,rid=self.claimed();self.store.cancel(rid)
        self.assertEqual(CollaborationRunner(self.store,FakeManager([])).run(run),1);self.assertEqual(self.store.get(rid)["state"],"cancelled")

    def test_failed_unload_preserves_raw_before_abstain(self):
        run,rid=self.claimed();self.assertEqual(CollaborationRunner(self.store,FakeManager(self.good(),cleanup_at=1)).run(run),3)
        evidence=self.store.evidence(rid);self.assertEqual(evidence["run"]["state"],"abstained")
        self.assertTrue(any(e["kind"]=="call_received" and e["data"]["result"]=="{\"artifact\": \"Pebble text\"}" for e in evidence["events"]))

    def test_interrupted(self):
        run,rid=self.claimed();self.assertEqual(CollaborationRunner(self.store,FakeManager([]),stopping=lambda:True).run(run),1)
        self.assertEqual(self.store.get(rid)["state"],"interrupted")

    def test_recover_no_replay(self):
        run,rid=self.claimed();self.assertEqual(self.store.recover_running(),1);self.assertEqual(self.store.get(rid)["state"],"interrupted");self.assertIsNone(self.store.claim_due())

    def test_original_and_evidence_immutable(self):
        run,rid=self.claimed()
        with self.assertRaises(sqlite3.IntegrityError):self.store.db.execute("UPDATE collaboration_runs SET original_json='null' WHERE id=?",(rid,))
        event=self.store.evidence(rid)["events"][0]
        with self.assertRaises(sqlite3.IntegrityError):self.store.db.execute("DELETE FROM collaboration_events WHERE seq=?",(event["seq"],))
        self.assertEqual(self.store.get(rid)["original"],"Draft text")

    def test_semantic_required_abstains_with_best_preserved(self):
        run,rid=self.claimed(spec(semantic=True));self.assertEqual(CollaborationRunner(self.store,FakeManager(self.good())).run(run),0)
        row=self.store.get(rid);self.assertEqual(row["state"],"abstained");self.assertIsNotNone(row["outcome"]["best_revision_id"])

    def test_validator_strict_schema_and_pareto(self):
        raw={"name":"json","instruction":"return","source":{},"original":{"x":1},"format":"json","requires_semantic_verification":False,
             "constraints":{"schema":{"type":"object","required":["x"],"properties":{"x":{"type":"integer","const":1}},"additionalProperties":False}}}
        normalized=normalize_spec(raw);valid=validate_artifact({"x":1},normalized);invalid=validate_artifact({"x":2},normalized)
        self.assertTrue(valid["valid"]);self.assertFalse(invalid["valid"]);self.assertTrue(better(valid,invalid));self.assertFalse(valid["semantic_verified"])
        raw["constraints"]["schema"]["pattern"]="x"
        with self.assertRaises(ValueError):normalize_spec(raw)

    def test_empty_whitelists_and_json_metric_field_do_not_hide_constraints(self):
        raw={"name":"json","instruction":"return","source":{},"original":{"summary":"clean"},"format":"json",
             "requires_semantic_verification":False,"constraints":{"allowed_numbers":[],"allowed_sections":[],
             "forbidden_content":["hidden"]},"metrics":{"field":"summary"}}
        normalized=normalize_spec(raw)
        checked=validate_artifact({"summary":"clean","details":"hidden 7"},normalized)
        self.assertEqual({v["code"] for v in checked["violations"]},{"forbidden_content","unauthorized_number"})

    def test_schema_keyword_applicability_and_boolean_integer_distinction(self):
        raw={"name":"json","instruction":"return","source":{},"original":{"x":1},"format":"json",
             "constraints":{"schema":{"type":"object","properties":{"x":{"type":"integer","const":1}}}}}
        normalized=normalize_spec(raw)
        self.assertFalse(validate_artifact({"x":True},normalized)["valid"])
        raw["constraints"]["schema"]={"type":"string","minimum":1}
        with self.assertRaises(ValueError):normalize_spec(raw)

    def test_output_parser_exact_only(self):
        self.assertEqual(parse_role_json('<think>a<think>b</think></think>\n```json\n{"artifact":"x"}\n```')["artifact"],"x")
        with self.assertRaises(ParseError):parse_role_json('comment {"artifact":"x"}')
        with self.assertRaises(ParseError):parse_role_json('<think>broken {"artifact":"x"}')

    def test_output_overflow_still_preserves_full_received(self):
        raw=spec();raw["limits"]["output_chars"]=5
        run,rid=self.claimed(raw);result=candidate("Pebble text");CollaborationRunner(self.store,FakeManager([result])).run(run)
        received=next(e for e in self.store.evidence(rid)["events"] if e["kind"]=="call_received")
        self.assertEqual(received["data"]["result"],result);self.assertEqual(self.store.get(rid)["state"],"failed")

if __name__=="__main__":unittest.main()
