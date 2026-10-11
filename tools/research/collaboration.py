"""Bounded, evidence-preserving Qwen/Bonsai collaboration runner."""
from __future__ import annotations

import json
import re
import time
import shutil
import textwrap
from itertools import zip_longest
from dataclasses import replace
from difflib import unified_diff
from typing import Any,Callable

from collaboration_store import CollaborationStore
from registry import ModelEntry
from lifecycle import Dependencies, LifecycleError
from models import LocalHTTP
from validation import better,validate_artifact

CONTROL=re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")

class CollaborationTransport:
    """Keep lifecycle effects unchanged; use the installed Prism thinking switch."""
    options={"chat_template_kwargs":{"enable_thinking":False},"temperature":0}
    def __init__(self,base):self.base=base
    def __getattr__(self,key):return getattr(self.base,key)
    def complete(self,entry,prompt,max_tokens):
        result,raw=LocalHTTP(entry.endpoint,entry.timeout_seconds).complete(entry.identifier,prompt,max_tokens,request_options=self.options)
        value=json.loads(raw)
        if value.get('model')!=entry.identifier:raise LifecycleError("Bonsai returned a different model.",raw)
        if value['choices'][0].get('finish_reason')=='length':raise LifecycleError("Bonsai output token limit reached.",raw)
        return result,raw

class ParseError(ValueError):pass

def _strip_think(text:str)->str:
    value=text.lstrip()
    if not value.startswith("<think>"):return value
    depth=0;end=None
    for match in re.finditer(r"</?think>",value):
        depth += -1 if match.group()=="</think>" else 1
        if depth==0:end=match.end();break
        if depth<0:break
    if end is None:raise ParseError("Unbalanced leading think block.")
    return value[end:].lstrip()

def parse_role_json(raw:str)->dict[str,Any]:
    if not isinstance(raw,str):raise ParseError("Model output is not text.")
    value=_strip_think(raw).strip()
    fence=re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```",value,re.IGNORECASE)
    if fence:value=fence.group(1).strip()
    def unique(pairs):
        out={}
        for key,val in pairs:
            if key in out: raise ParseError("Duplicate JSON key: "+key)
            out[key]=val
        return out
    try:parsed=json.loads(value,object_pairs_hook=unique,parse_constant=lambda x: (_ for _ in ()).throw(ParseError("Non-finite JSON")))
    except json.JSONDecodeError as exc:raise ParseError("Output must be one complete JSON object.") from exc
    if not isinstance(parsed,dict):raise ParseError("Output must be a JSON object.")
    return parsed

def _candidate(value:dict,spec:dict)->Any:
    if set(value)!={"artifact"}:raise ParseError("Candidate requires only artifact.")
    artifact=value["artifact"]
    if spec["format"]=="text" and not isinstance(artifact,str):raise ParseError("Text artifact required.")
    if spec["format"]=="json" and not isinstance(artifact,dict):raise ParseError("Object artifact required.")
    return artifact

def _review(value:dict)->dict:
    if set(value)!={"verdict","comments","suggestions","unsupported_changes"} or value["verdict"] not in ("approve","revise","abstain"):
        raise ParseError("Review object has an invalid shape.")
    for key in ("comments","suggestions","unsupported_changes"):
        if not isinstance(value[key],list) or len(value[key])>100 or any(not isinstance(x,str) for x in value[key]):raise ParseError("Review lists must contain strings.")
    return value

def _evaluation(value:dict)->dict:
    if set(value)!={"accepted_feedback","rejected_feedback","rationale"} or not isinstance(value["rationale"],str):raise ParseError("Evaluation object has an invalid shape.")
    for key in ("accepted_feedback","rejected_feedback"):
        if not isinstance(value[key],list) or any(not isinstance(x,str) for x in value[key]):raise ParseError("Feedback lists must contain strings.")
    return value

class CollaborationRunner:
    def __init__(self,store:CollaborationStore,manager:Any,stopping:Callable[[],bool]=lambda:False,
                 clock:Callable[[],float]=time.monotonic):
        self.store=store;self.manager=manager;self.operator_stopping=stopping;self.clock=clock
        self.run_row={};self.spec={};self.models={};self.calls=0;self.retries_used=0
        self.total_chars=0;self.deadline=0.;self.cleanup_reserve=45.

    def _stopping(self)->bool:return self.operator_stopping() or self.store.cancelled(self.run_row["id"])
    def _event(self,kind,data):self.store.append_event(self.run_row["id"],kind,data)
    def _remaining(self)->float:return self.deadline-self.clock()

    def _session(self,sid):
        try:
            status=self.manager.store.status()
            return next((x for x in status.get("history",[]) if x.get("id")==sid),None)
        except Exception as exc:return {"status_unavailable":type(exc).__name__}

    def _call(self,role:str,purpose:str,payload:dict,shape:Callable[[dict],Any])->tuple[Any,str|None,dict]:
        limits=self.spec["limits"]
        schemas={
            "initial_candidate":{"artifact":"text when format=text; object when format=json"},
            "revision":{"artifact":"text when format=text; object when format=json"},
            "review":{"verdict":"approve|revise|abstain","comments":["string"],
                      "suggestions":["string"],"unsupported_changes":["string"]},
            "evaluate_feedback":{"accepted_feedback":["string"],"rejected_feedback":["string"],"rationale":"string"},
        }
        list_schema={"type":"array","items":{"type":"string"}}
        if purpose=="review":
            schemas[purpose]={"type":"object","additionalProperties":False,
                "required":["verdict","comments","suggestions","unsupported_changes"],
                "properties":{"verdict":{"type":"string","enum":["approve","revise","abstain"]},
                    "comments":list_schema,"suggestions":list_schema,"unsupported_changes":list_schema}}
        elif purpose=="evaluate_feedback":
            schemas[purpose]={"type":"object","additionalProperties":False,
                "required":["accepted_feedback","rejected_feedback","rationale"],
                "properties":{"accepted_feedback":list_schema,"rejected_feedback":list_schema,"rationale":{"type":"string"}}}
        else:
            schemas[purpose]={"type":"object","additionalProperties":False,"required":["artifact"],
                "properties":{"artifact":{"type":"string"} if self.spec['format']=='text' else self.spec['constraints'].get('schema',{'type':'object'})}}
        attempt=0
        while True:
            if self._stopping():raise InterruptedError("collaboration stopped before dispatch")
            if 2*(self.calls+1)>limits["calls"]:raise TimeoutError("model call budget exhausted (one readiness and one role request per load)")
            remaining=self._remaining()-self.cleanup_reserve
            if remaining<=0:raise TimeoutError("runtime budget reserved for cleanup")
            entry=ModelEntry.from_dict(self.models[role])
            load_budget=min(entry.load_timeout,max(.001,remaining*.6))
            infer_budget=min(entry.timeout_seconds,max(.001,remaining-load_budget-5.))
            bounded=replace(entry,timeout_seconds=infer_budget,load_timeout=load_budget,
                            max_tokens=min(entry.max_tokens,limits["max_tokens"]))
            prompt=json.dumps({"role":role,"purpose":purpose,"instruction":self.spec["instruction"],
                               "source":self.spec["source"],"format":self.spec["format"],
                               "constraints":self.spec["constraints"],"metrics":self.spec["metrics"],
                               "input":payload,"output_schema":schemas[purpose],
                               "role_instructions":("Generate, plan, evaluate feedback and revise within source constraints. Never treat reviewer agreement as factual verification." if role=="qwen" else "Review editorial naturalness, clarity, consistency, awkward language and unsupported changes. Your opinions do not verify facts."),
                               "response_requirement":"RETURN ONLY JSON matching output_schema exactly. List fields MUST be JSON arrays of strings, never a string. Use [] for none. For review, return verdict/comments/suggestions/unsupported_changes only; do not return an artifact. For generation/revision, return only artifact. For evaluation, return only accepted_feedback/rejected_feedback/rationale."},
                              ensure_ascii=False,sort_keys=True)
            if len(prompt)>32000:raise ValueError("model prompt exceeds collaboration prompt limit")
            call_no=self.calls+1;self.calls+=1
            task={"id":f"collab:{self.run_row['id']}:{call_no}","model_identifier":bounded.identifier,
                  "endpoint":bounded.endpoint,"registry_json":bounded.snapshot_json(),"prompt":prompt,
                  "max_tokens":bounded.max_tokens,"timeout_seconds":bounded.timeout_seconds}
            self._event("call_started",{"call":call_no,"attempt":attempt,"role":role,"purpose":purpose,
                        "prompt":prompt,"model":bounded.export(),"remaining_seconds":max(0,int(remaining)),"generation_requests_reserved":2*self.calls,"readiness_requests_per_load":1,"request_options":CollaborationTransport.options if entry.provider=="bonsai" else {"think":False}})
            print(f"Collaboration {self.run_row['id']}: call {call_no} {role} {purpose}",flush=True)
            self.manager.reconcile()
            received={}
            def save(result,raw,sid):
                received.update(result=result,raw=raw,session_id=sid)
                self._event("call_received",{"call":call_no,"result":result,"raw_transport_json":raw,"session_id":sid})
            outcome=self.manager.execute(task,on_result=save)
            sid=outcome.session_id
            self._event("call_finished",{"call":call_no,"session_id":sid,"lifecycle":self._session(sid),
                        "result":outcome.result,"raw_transport_json":outcome.raw_json,
                        "error":outcome.error,"cleanup_error":outcome.cleanup_error})
            if outcome.cleanup_error:raise RuntimeError("CLEANUP_BLOCKED:"+outcome.cleanup_error)
            if self._stopping():raise InterruptedError("collaboration stopped after dispatch")
            if self._remaining()<=0:raise TimeoutError("collaboration runtime exhausted")
            if outcome.error:
                if received:raise RuntimeError("inference failed after a received result: "+outcome.error)
                raise RuntimeError(outcome.error)
            raw_result=outcome.result if outcome.result is not None else received.get("result")
            if not isinstance(raw_result,str):raise RuntimeError("model returned no result")
            self.total_chars+=len(raw_result)
            if len(raw_result)>limits["output_chars"] or self.total_chars>limits["total_output_chars"]:
                self._event("output_overflow",{"call":call_no,"chars":len(raw_result),"total_chars":self.total_chars})
                raise RuntimeError("model output budget exceeded")
            metadata={"call":call_no,"session_id":sid,"role":role,"purpose":purpose,"model":bounded.export()}
            try:
                parsed=parse_role_json(raw_result)
                self._event('parsed_output',{'call':call_no,'role':role,'purpose':purpose,'parsed':parsed})
                return shape(parsed),sid,metadata
            except ParseError as exc:
                error=str(exc);self._event("schema_rejected",{"call":call_no,"error":error,"raw":raw_result})
                if self.retries_used>=limits["retries"]:raise
                self.retries_used+=1;attempt+=1
                payload={"repair_error":error,"previous_raw":raw_result,"original_payload":payload}

    @staticmethod
    def _goals_satisfied(validation:dict)->bool:
        metrics=validation["metrics"]
        return validation["valid"] and all(metrics.get(key,0)==0 for key in
            ("avoid_phrase_hits","long_sentence_count","excess_words"))

    def run(self,run:dict)->int:
        current=self.store.get(run['id'])
        if not current or current['state']!='running':return 1
        self.calls=0;self.retries_used=0;self.total_chars=0
        self.run_row=run;self.spec=run["spec"];self.models=run["models"]
        self.deadline=self.clock()+self.spec["limits"]["runtime"]
        original_deps=getattr(self.manager,"d",None)
        if type(original_deps) is Dependencies:self.manager.d=CollaborationTransport(original_deps)
        old_stopping=getattr(self.manager,"stopping",lambda:False)
        self.manager.stopping=lambda:old_stopping() or self._stopping()
        incumbent=self.spec["original"];inc_validation=validate_artifact(incumbent,self.spec)
        original_rev=self.store.evidence(run["id"])["revisions"][0]["id"]
        best=incumbent;best_rev=original_rev;disagreements=0;stop_reason="max_rounds";protected_stop=False
        self._event("validation",{"label":"original","revision_id":original_rev,"validation":inc_validation})
        try:
            baseline,_,baseline_call=self._call("qwen","initial_candidate",{"original":incumbent},lambda x:_candidate(x,self.spec))
            baseline_validation=validate_artifact(baseline,self.spec)
            baseline_rev=self.store.add_revision(run["id"],"qwen-only",baseline,original_rev,
                                                  {"validation":baseline_validation,"model_call":baseline_call})
            self._event("validation",{"label":"qwen-only","revision_id":baseline_rev,"validation":baseline_validation})
            if any(v["protected"] for v in baseline_validation["violations"]):
                stop_reason="protected_requirement_violation";protected_stop=True
                self._event("decision",{"reason":stop_reason,"rejected_revision_id":baseline_rev})
            if protected_stop:
                self.store.finish(run["id"],"rejected",{"accepted":False,"best_revision_id":best_rev,
                    "validation":inc_validation},stop_reason,None);return 0
            if baseline_validation["valid"] or better(baseline_validation,inc_validation):
                best,best_rev,inc_validation=baseline,baseline_rev,baseline_validation
            review,_,review_call=self._call("bonsai","review",{"artifact":baseline,"validation":baseline_validation},_review)
            self._event("review",{"round":0,"revision_id":baseline_rev,"review":review})
            current=baseline;current_rev=baseline_rev;current_validation=baseline_validation
            for round_no in range(1,self.spec["limits"]["rounds"]+1):
                evaluation,_,evaluation_call=self._call("qwen","evaluate_feedback",{"artifact":current,"review":review},_evaluation)
                self._event("evaluation",{"round":round_no,"revision_id":current_rev,"evaluation":evaluation})
                if review["verdict"]=="revise" and not evaluation["accepted_feedback"]:disagreements+=1
                else:disagreements=0
                if disagreements>=2:
                    stop_reason="repeated_disagreement"
                    self._event("decision",{"reason":stop_reason,"kept_revision_id":best_rev});break
                candidate,_,revision_call=self._call("qwen","revision",{"artifact":current,"review":review,"evaluation":evaluation},lambda x:_candidate(x,self.spec))
                validation=validate_artifact(candidate,self.spec)
                revision=self.store.add_revision(run["id"],f"round-{round_no}",candidate,current_rev,
                    {"review":review,"review_call":review_call,"evaluation":evaluation,
                     "evaluation_call":evaluation_call,"validation":validation,"model_call":revision_call})
                self._event("validation",{"label":f"round-{round_no}","revision_id":revision,"validation":validation})
                if any(v["protected"] for v in validation["violations"]):
                    stop_reason="protected_requirement_violation";protected_stop=True
                    self._event("decision",{"reason":stop_reason,"rejected_revision_id":revision});break
                improved=better(validation,inc_validation)
                self._event("decision",{"revision_id":revision,"kept_as_incumbent":improved,
                    "reason":"measurable_improvement" if improved else ("invalid_revision" if not validation["valid"] else "no_measurable_improvement"),
                    "comparison_revision_id":best_rev,"scope":"declared deterministic counts only"})
                if improved:best,best_rev,inc_validation=candidate,revision,validation
                review,_,review_call=self._call("bonsai","review",{"artifact":candidate,"validation":validation},_review)
                self._event("review",{"round":round_no,"revision_id":revision,"review":review})
                current,current_rev,current_validation=candidate,revision,validation
                if improved and self._goals_satisfied(validation):
                    stop_reason="validation_succeeded";break
                if not improved:
                    stop_reason="no_measurable_improvement" if validation["valid"] else "invalid_revision"
                    self._event("decision",{"reason":stop_reason,"kept_revision_id":best_rev});break
            if self._stopping():raise InterruptedError("collaboration stopped before final decision")
            if self._remaining()<=0:raise TimeoutError("collaboration runtime exhausted before final decision")
            if protected_stop:
                self.store.finish(run["id"],"rejected",{"accepted":False,"best_revision_id":best_rev,
                    "validation":inc_validation},stop_reason,None);return 0
            if self.spec["requires_semantic_verification"]:
                outcome={"accepted":False,"best_revision_id":best_rev,"validation":inc_validation,"scope":"semantic verification absent"}
                self.store.finish(run["id"],"abstained",outcome,stop_reason,None);return 0
            if inc_validation["valid"]:
                outcome={"accepted":True,"artifact":best,"validation":inc_validation,"scope":"deterministic constraints only"}
                self.store.finish(run["id"],"completed",outcome,stop_reason,best_rev);return 0
            self.store.finish(run["id"],"rejected",{"accepted":False,"best_revision_id":best_rev,"validation":inc_validation},stop_reason,None);return 0
        except Exception as exc:
            message=str(exc)
            if message.startswith("CLEANUP_BLOCKED:"):
                self.store.finish(run["id"],"abstained",{"accepted":False},"failed_unload: "+message.split(":",1)[1]);return 3
            cancelled=self.store.cancelled(run["id"])
            status="cancelled" if cancelled else ("interrupted" if isinstance(exc,InterruptedError) else "failed")
            self.store.finish(run["id"],status,{"accepted":False,"error":message},"cancelled" if cancelled else message);return 1
        finally:
            self.manager.stopping=old_stopping
            if original_deps is not None:self.manager.d=original_deps

def comparison_text(evidence:dict)->str:
    def safe(x):return CONTROL.sub("",str(x))
    def render(artifact):return artifact if isinstance(artifact,str) else json.dumps(artifact,ensure_ascii=False,indent=2,sort_keys=True)
    run=evidence["run"]; revisions=evidence["revisions"];events=evidence["events"]
    by_label={r["label"]:r for r in revisions};by_id={r["id"]:r for r in revisions}
    accepted=by_id.get(run.get("accepted_revision_id"))
    width=max(12,min(42,(shutil.get_terminal_size((140,24)).columns-12)//5))
    columns=[]
    for title,label in (("Original","original"),("Qwen-only","qwen-only"),("Round 1","round-1"),("Round 2","round-2"),("Final accepted",None)):
        rev=accepted if label is None else by_label.get(label)
        body=render(rev["artifact"]) if rev else ("None: "+run["state"] if label is None else "Not run")
        wrapped=[title,"-"*width]
        for line in safe(body).splitlines():wrapped.extend(textwrap.wrap(line,width=width,replace_whitespace=False,drop_whitespace=False) or [""])
        columns.append(wrapped)
    lines=[f"Run {run['id']} — {safe(run['state'])}",f"Stop reason: {safe(run.get('stop_reason') or 'in progress')}",
           f"Decision scope: {safe((run.get('outcome') or {}).get('scope','deterministic constraints only; semantic correctness unverified'))}",""]
    lines.extend(" | ".join(value.ljust(width) for value in row) for row in zip_longest(*columns,fillvalue=""))
    for rev in revisions:
        lines.extend(["",f"=== {safe(rev['label'])}: {rev['id']} ===",safe(render(rev['artifact']))])
        prov=rev.get('provenance') or {};model=(prov.get('model_call') or {}).get('model')
        if model:lines.append(f"Generated by {safe(model['identifier'])} / {safe(model['provider'])} at {safe(model['endpoint'])}")
        parent=by_id.get(rev.get('parent_id'))
        if parent:lines.extend(safe(x) for x in unified_diff(render(parent['artifact']).splitlines(),render(rev['artifact']).splitlines(),fromfile=parent['label'],tofile=rev['label'],lineterm=""))
    for event in events:
        if event["kind"] in ("review","validation","evaluation","decision","finished","recovery"):
            lines.extend(["",f"[{event['kind']}]",safe(json.dumps(event["data"],ensure_ascii=False,indent=2,sort_keys=True))])
        elif event["kind"]=="call_started":
            model=event['data']['model'];lines.append(f"Call {event['data']['call']}: {safe(event['data']['purpose'])} — {safe(model['identifier'])} / {safe(model['provider'])}")
    return "\n".join(lines)
