"""Deterministic collaboration specification and artifact validation."""
from __future__ import annotations

import json
import math
import re
from typing import Any

FORMATS={"text","json"}
SCHEMA_KEYS={"type","required","properties","additionalProperties","const","enum","minimum","maximum",
             "items","minItems","maxItems","minLength","maxLength"}
LIMIT_DEFAULTS={"rounds":2,"calls":20,"runtime":900,"output_chars":12000,
                "total_output_chars":80000,"max_tokens":1024,"retries":0}
LIMIT_CAPS={"rounds":4,"calls":48,"runtime":3600,"output_chars":100000,
            "total_output_chars":800000,"max_tokens":4096,"retries":2}
CONSTRAINT_KEYS={"min_words","max_words","min_chars","max_chars","required_content","protected_claims",
                 "forbidden_content","allowed_numbers","required_sections","allowed_sections","schema"}
METRIC_KEYS={"avoid_phrases","sentence_max_words","target_words","field"}
SPEC_KEYS={"name","instruction","source","original","format","limits","constraints","metrics",
           "requires_semantic_verification"}
MAX_INPUT_CHARS=24000

def _strict_int(value:Any,name:str,minimum:int,maximum:int)->int:
    if type(value) is not int or not minimum<=value<=maximum: raise ValueError(f"{name} must be an integer in {minimum}..{maximum}.")
    return value

def _strings(value:Any,name:str,limit:int=100)->list[str]:
    if value is None:return []
    if not isinstance(value,list) or len(value)>limit or any(not isinstance(x,str) or not x or len(x)>2000 for x in value):
        raise ValueError(f"{name} must be a bounded list of nonempty strings.")
    return list(value)

def _schema(schema:Any,depth:int=0)->dict[str,Any]:
    if not isinstance(schema,dict) or depth>8 or len(schema)>40:raise ValueError("schema must be a bounded object.")
    unknown=set(schema)-SCHEMA_KEYS
    if unknown:raise ValueError(f"Unsupported schema keyword(s): {sorted(unknown)}")
    kind=schema.get("type")
    if kind not in ("object","array","string","integer","number","boolean","null"):raise ValueError("schema type is required and unsupported.")
    out=dict(schema)
    applicable={
        "object":{"required","properties","additionalProperties"},
        "array":{"items","minItems","maxItems"},
        "string":{"minLength","maxLength"},
        "integer":{"minimum","maximum"},"number":{"minimum","maximum"},
        "boolean":set(),"null":set(),
    }[kind]|{"type","const","enum"}
    invalid=set(out)-applicable
    if invalid:raise ValueError(f"Schema keyword(s) do not apply to {kind}: {sorted(invalid)}")
    if "required" in out:out["required"]=_strings(out["required"],"schema.required")
    if "properties" in out:
        if kind!="object" or not isinstance(out["properties"],dict) or len(out["properties"])>100:raise ValueError("Invalid schema properties.")
        out["properties"]={str(k):_schema(v,depth+1) for k,v in out["properties"].items()}
    if "items" in out:out["items"]=_schema(out["items"],depth+1)
    if "additionalProperties" in out and type(out["additionalProperties"]) is not bool:raise ValueError("additionalProperties must be boolean.")
    if "enum" in out and (not isinstance(out["enum"],list) or len(out["enum"])>100):raise ValueError("enum is invalid.")
    for key in ("minimum","maximum"):
        if key in out and (type(out[key]) not in (int,float) or not math.isfinite(out[key])):raise ValueError(f"{key} is invalid.")
    for key in ("minItems","maxItems","minLength","maxLength"):
        if key in out:out[key]=_strict_int(out[key],key,0,100000)
    return out

def normalize_spec(raw:dict[str,Any])->dict[str,Any]:
    if not isinstance(raw,dict):raise ValueError("spec must be an object.")
    if set(raw)-SPEC_KEYS:raise ValueError(f"Unknown spec field(s): {sorted(set(raw)-SPEC_KEYS)}")
    required=("name","instruction","source","original")
    if any(k not in raw for k in required):raise ValueError("name, instruction, source and original are required.")
    if not isinstance(raw["name"],str) or not raw["name"].strip() or len(raw["name"])>120:raise ValueError("name is invalid.")
    if not isinstance(raw["instruction"],str) or not raw["instruction"].strip() or len(raw["instruction"])>16000:raise ValueError("instruction is invalid.")
    if not isinstance(raw["source"],(str,dict)) or not isinstance(raw["original"],(str,dict)):raise ValueError("source/original must be text or objects.")
    if len(json.dumps(raw["source"],ensure_ascii=False))+len(json.dumps(raw["original"],ensure_ascii=False))>MAX_INPUT_CHARS:
        raise ValueError("source and original exceed the input size limit.")
    fmt=raw.get("format","text")
    if fmt not in FORMATS:raise ValueError("format must be text or json.")
    if fmt=="text" and not isinstance(raw["original"],str):raise ValueError("text format requires a text original.")
    if fmt=="json" and not isinstance(raw["original"],dict):raise ValueError("json format requires an object original.")
    limits=dict(LIMIT_DEFAULTS); supplied=raw.get("limits",{})
    if not isinstance(supplied,dict) or set(supplied)-set(limits):raise ValueError("Unknown limit.")
    limits.update(supplied)
    for key,value in list(limits.items()):limits[key]=_strict_int(value,f"limits.{key}",0 if key=="retries" else 1,LIMIT_CAPS[key])
    constraints=raw.get("constraints",{})
    if not isinstance(constraints,dict) or set(constraints)-CONSTRAINT_KEYS:raise ValueError("Unknown constraint.")
    c={}
    for key in ("min_words","max_words","min_chars","max_chars"):
        if key in constraints:c[key]=_strict_int(constraints[key],key,0,1_000_000)
    for key in ("required_content","protected_claims","forbidden_content","required_sections"):
        c[key]=_strings(constraints.get(key),key)
    for key in ("allowed_numbers","allowed_sections"):
        if key in constraints:c[key]=_strings(constraints[key],key)
    if "schema" in constraints:
        if fmt!='json':raise ValueError("JSON schema requires JSON artifact format.")
        c["schema"]=_schema(constraints["schema"])
    metrics=raw.get("metrics",{})
    if not isinstance(metrics,dict) or set(metrics)-METRIC_KEYS:raise ValueError("Unknown metric.")
    m={"avoid_phrases":_strings(metrics.get("avoid_phrases"),"avoid_phrases")}
    for key in ("sentence_max_words","target_words"):
        if key in metrics:m[key]=_strict_int(metrics[key],key,1,100000)
    if "field" in metrics:
        if not isinstance(metrics["field"],str) or not metrics["field"]:raise ValueError("metric field is invalid.")
        if fmt!="json": raise ValueError("metric field requires JSON format.")
        m["field"]=metrics["field"]
    semantic=raw.get("requires_semantic_verification",True)
    if type(semantic) is not bool:raise ValueError("requires_semantic_verification must be boolean.")
    return {"name":raw["name"].strip(),"instruction":raw["instruction"],"source":raw["source"],
            "original":raw["original"],"format":fmt,"limits":limits,"constraints":c,"metrics":m,
            "requires_semantic_verification":semantic}

def _render_full(artifact:Any,spec:dict[str,Any])->str:
    if spec["format"]=="text":
        if not isinstance(artifact,str):raise ValueError("Artifact must be text.")
        return artifact
    if not isinstance(artifact,dict):raise ValueError("Artifact must be an object.")
    return json.dumps(artifact,ensure_ascii=False,sort_keys=True)

def _render_metric(artifact:Any,spec:dict[str,Any])->str:
    full=_render_full(artifact,spec)
    field=spec["metrics"].get("field")
    if field:
        value=artifact.get(field)
        if not isinstance(value,str):raise ValueError("Selected metric field must be a string.")
        return value
    return full

def _same_json_value(a:Any,b:Any)->bool:
    if type(a) is not type(b):return False
    if isinstance(a,dict):return set(a)==set(b) and all(_same_json_value(a[k],b[k]) for k in a)
    if isinstance(a,list):return len(a)==len(b) and all(_same_json_value(x,y) for x,y in zip(a,b))
    return a==b

def _schema_errors(value:Any,schema:dict[str,Any],path:str="$",out:list[str]|None=None)->list[str]:
    out=[] if out is None else out; kind=schema["type"]
    checks={"object":lambda:type(value)is dict,"array":lambda:type(value)is list,"string":lambda:type(value)is str,
            "integer":lambda:type(value)is int,"number":lambda:type(value) in (int,float) and math.isfinite(value),
            "boolean":lambda:type(value)is bool,"null":lambda:value is None}
    if not checks[kind]():out.append(f"{path} must be {kind}");return out
    if "const" in schema and not _same_json_value(value,schema["const"]):out.append(f"{path} differs from const")
    if "enum" in schema and not any(_same_json_value(value,x) for x in schema["enum"]):out.append(f"{path} is outside enum")
    if kind=="object":
        for key in schema.get("required",[]):
            if key not in value:out.append(f"{path}.{key} is required")
        props=schema.get("properties",{})
        if schema.get("additionalProperties") is False:
            for key in set(value)-set(props):out.append(f"{path}.{key} is not allowed")
        for key,sub in props.items():
            if key in value:_schema_errors(value[key],sub,f"{path}.{key}",out)
    if kind=="array":
        if len(value)<schema.get("minItems",0) or len(value)>schema.get("maxItems",10**9):out.append(f"{path} item count is invalid")
        if "items" in schema:
            for i,item in enumerate(value):_schema_errors(item,schema["items"],f"{path}[{i}]",out)
    if kind=="string" and (len(value)<schema.get("minLength",0) or len(value)>schema.get("maxLength",10**9)):out.append(f"{path} length is invalid")
    if kind in ("integer","number") and (value<schema.get("minimum",-math.inf) or value>schema.get("maximum",math.inf)):out.append(f"{path} is outside bounds")
    return out

def validate_artifact(artifact:Any,spec:dict[str,Any])->dict[str,Any]:
    violations=[]
    def add(code:str,detail:str,protected:bool=False):violations.append({"code":code,"detail":detail,"protected":protected})
    try:
        full_text=_render_full(artifact,spec);metric_text=_render_metric(artifact,spec)
    except ValueError as exc:full_text=metric_text="";add("type",str(exc))
    words=re.findall(r"\b[\w'-]+\b",metric_text); c=spec["constraints"]
    for key,actual in (("min_words",len(words)),("min_chars",len(metric_text))):
        if key in c and actual<c[key]:add(key,f"{actual} < {c[key]}")
    for key,actual in (("max_words",len(words)),("max_chars",len(metric_text))):
        if key in c and actual>c[key]:add(key,f"{actual} > {c[key]}")
    lower=full_text.casefold()
    for item in c["required_content"]:
        if item.casefold() not in lower:add("required_content",f"Missing literal {item!r}")
    for item in c["protected_claims"]:
        if item not in full_text:add("protected_claim",f"Protected literal changed or missing: {item!r}",True)
    for item in c["forbidden_content"]:
        if item.casefold() in lower:add("forbidden_content",f"Forbidden literal present: {item!r}")
    numbers=re.findall(r"(?<!\w)[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?!\w)",full_text)
    if "allowed_numbers" in c:
        allowed=set(c["allowed_numbers"])
        for number in numbers:
            if number not in allowed:add("unauthorized_number",f"Number {number!r} is not allowed",True)
    sections=[x.strip() for x in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$",full_text)]
    for section in c["required_sections"]:
        if section not in sections:add("required_section",f"Missing section {section!r}")
    if "allowed_sections" in c:
        for section in sections:
            if section not in c["allowed_sections"]:add("unauthorized_section",f"Section {section!r} is not allowed")
    if "schema" in c:
        for error in _schema_errors(artifact,c["schema"]):add("schema",error,"differs from const" in error)
    metric_lower=metric_text.casefold()
    metrics={"word_count":len(words),"char_count":len(metric_text)}; goals=spec["metrics"]
    metrics["avoid_phrase_hits"]=sum(metric_lower.count(x.casefold()) for x in goals["avoid_phrases"])
    sentence_lengths=[len(re.findall(r"\b[\w'-]+\b",x)) for x in re.split(r"(?<=[.!?])\s+",metric_text) if x.strip()]
    metrics["long_sentence_count"]=sum(x>goals.get("sentence_max_words",10**9) for x in sentence_lengths)
    metrics["excess_words"]=max(0,len(words)-goals["target_words"]) if "target_words" in goals else 0
    return {"valid":not violations,"violations":violations,"metrics":metrics,
            "semantic_verified":False,"scope":"deterministic constraints only"}

def better(candidate_validation:dict[str,Any],incumbent_validation:dict[str,Any])->bool:
    def vector(v):
        return (sum(x["protected"] for x in v["violations"]),len(v["violations"]),
                v["metrics"].get("avoid_phrase_hits",0),v["metrics"].get("long_sentence_count",0),
                v["metrics"].get("excess_words",0))
    candidate,incumbent=vector(candidate_validation),vector(incumbent_validation)
    return all(a<=b for a,b in zip(candidate,incumbent)) and any(a<b for a,b in zip(candidate,incumbent))
