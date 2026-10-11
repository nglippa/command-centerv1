"""Read-only, evidence-based progress projections for persisted research records.

This module never contacts a provider, changes lifecycle state, or estimates tokens
from text. Active generation remains indeterminate because the current adapters are
non-streaming. Provider token counters become available only after raw response JSON
has been durably recorded.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Any, Iterable


TERMINAL = {"completed", "failed", "cancelled", "abstained", "interrupted", "rejected"}
ACTIVE_LIFECYCLE = {"STARTING", "LOADING", "READY", "BUSY", "UNLOADING", "STOPPING"}


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _duration(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _raw_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def provider_usage(raw_response: Any, infer_ms: Any = None) -> dict[str, Any]:
    """Extract validated provider counters and measured average output throughput.

    ``output_tokens_per_second`` is provider-timed for Ollama when ``eval_duration``
    is valid. OpenAI-compatible responses have no provider duration, so a rate is an
    interval average only when a positive persisted ``infer_ms`` is supplied.
    """
    interval_ms = _duration(infer_ms)
    base = {
        "status": "unavailable", "provider": None, "input_tokens": None,
        "output_tokens": None, "total_tokens": None,
        "input_source": None, "output_source": None, "total_source": None,
        "output_tokens_per_second": None, "throughput_source": None,
        "durations": {"provider_output_ms": None, "inference_interval_ms": interval_ms},
        "reason": "Persisted provider usage is unavailable.",
    }
    raw = _raw_object(raw_response)
    if raw is None:
        return base

    if any(key in raw for key in ("prompt_eval_count", "eval_count", "eval_duration")):
        prompt = _count(raw.get("prompt_eval_count"))
        output = _count(raw.get("eval_count"))
        duration_ns = _duration(raw.get("eval_duration"))
        if (("prompt_eval_count" in raw and prompt is None) or ("eval_count" in raw and output is None)
                or prompt is None and output is None):
            return {**base, "provider": "ollama", "reason": "Ollama token counters are invalid or absent."}
        rate = None
        provider_ms = duration_ns / 1_000_000 if duration_ns is not None else None
        source = None
        if output is not None and duration_ns is not None and duration_ns > 0:
            seconds = duration_ns / 1_000_000_000
            rate = output / seconds if seconds > 0 else None
            if rate is not None and not math.isfinite(rate): rate = None
            source = "provider_eval_duration" if rate is not None else None
        return {
            **base, "status": "provider_reported", "provider": "ollama",
            "input_tokens": prompt, "output_tokens": output,
            "total_tokens": prompt + output if prompt is not None and output is not None else None,
            "input_source": "provider_reported" if prompt is not None else None,
            "output_source": "provider_reported" if output is not None else None,
            "total_source": "calculated_from_provider_counts" if prompt is not None and output is not None else None,
            "output_tokens_per_second": rate, "throughput_source": source,
            "durations": {"provider_output_ms": provider_ms, "inference_interval_ms": interval_ms},
            "reason": None,
        }

    if "usage" in raw:
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            return {**base, "provider": "openai_compatible", "reason": "OpenAI-compatible usage is not an object."}
        prompt = _count(usage.get("prompt_tokens"))
        output = _count(usage.get("completion_tokens"))
        total = _count(usage.get("total_tokens"))
        if (any(key in usage and _count(usage[key]) is None for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
                or prompt is None and output is None and total is None
                or total is not None and prompt is not None and output is not None and total != prompt + output):
            return {**base, "provider": "openai_compatible", "reason": "OpenAI-compatible token counters are invalid, absent, or inconsistent."}
        total_source = "provider_reported" if total is not None else None
        if total is None and prompt is not None and output is not None:
            total = prompt + output
            total_source = "calculated_from_provider_counts"
        seconds = interval_ms / 1000 if interval_ms is not None else None
        rate = output / seconds if output is not None and seconds is not None and seconds > 0 else None
        if rate is not None and not math.isfinite(rate): rate = None
        return {
            **base, "status": "provider_reported", "provider": "openai_compatible",
            "input_tokens": prompt, "output_tokens": output, "total_tokens": total,
            "input_source": "provider_reported" if prompt is not None else None,
            "output_source": "provider_reported" if output is not None else None,
            "total_source": total_source,
            "output_tokens_per_second": rate,
            "throughput_source": "calculated_from_provider_counts_and_infer_ms" if rate is not None else None,
            "reason": None,
        }
    return base


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _elapsed(start: Any, end: Any) -> int | None:
    first, last = _instant(start), _instant(end)
    if first is None or last is None or last < first:
        return None
    return round((last - first).total_seconds() * 1000)


def _now(value: Any) -> datetime:
    return _instant(value) or datetime.now(timezone.utc)


def _data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("data")
    if isinstance(value, dict):
        return value
    value = event.get("data_json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _lifecycle_events(events: Iterable[dict[str, Any]], session_id: str | None) -> list[dict[str, Any]]:
    values = [event for event in events if not session_id or event.get("session_id") == session_id]
    return sorted(values, key=lambda event: event.get("at") or "")


def _inference_elapsed(session: dict[str, Any] | None, events: Iterable[dict[str, Any]], now: datetime,
                       terminal_at: Any = None) -> tuple[int | None, str | None]:
    if not session:
        return None, None
    recorded = _duration(session.get("infer_ms"))
    if recorded is not None and session.get("state") != "BUSY":
        return round(recorded), "recorded_infer_ms"
    ordered = _lifecycle_events(events, session.get("id"))
    busy_index = next((index for index in range(len(ordered) - 1, -1, -1)
                       if ordered[index].get("to_state") == "BUSY"), None)
    if busy_index is None:
        return None, None
    start = ordered[busy_index].get("at")
    end = None
    for event in ordered[busy_index + 1:]:
        if event.get("from_state") == "BUSY":
            end = event.get("at")
            break
    if end is None:
        end = terminal_at if terminal_at else now
    return _elapsed(start, end), "busy_transition_interval"


def _research_stage(job: dict[str, Any], session: dict[str, Any] | None) -> dict[str, str]:
    state = str(job.get("state") or "unknown")
    lifecycle = str(session.get("state")) if session else None
    if lifecycle in ("UNLOADING", "STOPPING"):
        key = "verifying_unload" if lifecycle == "UNLOADING" else "stopping_owned_server"
        return {"key": key, "label": "Verifying model unload" if lifecycle == "UNLOADING" else "Stopping owned model server"}
    if lifecycle == "ERROR" and session and session.get("blocker"):
        return {"key": "cleanup_blocked", "label": "Cleanup requires operator attention"}
    if state == "completed" and lifecycle in ("BUSY", "READY"):
        return {"key": "response_recorded", "label": "Response recorded; lifecycle release pending"}
    if state in TERMINAL:
        return {"key": state, "label": state.replace("_", " ").title()}
    if state == "pending":
        return {"key": "queued", "label": "Queued"}
    if lifecycle == "STOPPED" and session and session.get("last_verified_unload"):
        return {"key": "finalizing", "label": "Model unloaded; finalizing durable result"}
    mapping = {
        "STARTING": ("starting_server", "Starting owned model server"),
        "LOADING": ("loading_model", "Loading model"),
        "READY": ("model_ready", "Model ready"),
        "BUSY": ("generating", "Generating response"),
        "UNAVAILABLE": ("unavailable", "Model unavailable"),
        "ERROR": ("lifecycle_error", "Lifecycle error"),
        "STOPPED": ("preparing", "Preparing model lifecycle"),
    }
    key, label = mapping.get(lifecycle, ("claimed", "Claimed; lifecycle evidence unavailable"))
    return {"key": key, "label": label}


def research_progress(job: dict[str, Any], session: dict[str, Any] | None = None,
                      events: Iterable[dict[str, Any]] = (), now: Any = None) -> dict[str, Any]:
    """Project one durable research task without implying streaming token progress."""
    current = _now(now)
    terminal = job.get("state") in TERMINAL
    elapsed_end = job.get("completed_at") if terminal else current
    inference_ms, inference_source = _inference_elapsed(session, events, current,
                                                         job.get("completed_at") if terminal else None)
    usage = provider_usage(job.get("raw_response_json"), session.get("infer_ms") if session else None)
    limit = _count(job.get("max_tokens"))
    used = usage["output_tokens"]
    budget = ({"status": "available", "label": "Output budget used", "used": used, "limit": limit,
               "ratio": used / limit if limit else None}
              if used is not None and limit is not None and limit > 0 else
              {"status": "unavailable", "label": "Output budget used", "used": None, "limit": limit, "ratio": None})
    return {
        "kind": "research", "as_of": current.isoformat(), "state": job.get("state"), "terminal": terminal,
        "stage": _research_stage(job, session),
        "model": (session or {}).get("model") or job.get("model_identifier"),
        "session_id": (session or {}).get("id"),
        "elapsed_ms": _elapsed(job.get("started_at"), elapsed_end),
        "inference_elapsed_ms": inference_ms, "inference_timing_source": inference_source,
        "usage": usage, "output_budget": budget,
        "indeterminate": not terminal or (session or {}).get("state") in ("UNLOADING", "STOPPING")
            or job.get("state")=="completed" and (session or {}).get("state") in ("BUSY", "READY"),
    }


def _sessions(values: Any) -> list[dict[str, Any]]:
    if isinstance(values, dict):
        if "history" in values and isinstance(values["history"], list):
            return [x for x in values["history"] if isinstance(x, dict)]
        return [values]
    return [x for x in (values or ()) if isinstance(x, dict)]


def _session_events(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, dict) or not isinstance(values.get("events"), list):
        return []
    return [event for event in values["events"] if isinstance(event, dict)]


def _call_records(run: dict[str, Any], events: list[dict[str, Any]], sessions: Any,
                  now: datetime) -> list[dict[str, Any]]:
    calls: dict[int, dict[str, Any]] = {}
    for event in events:
        data = _data(event)
        number = _count(data.get("call"))
        if number is None:
            continue
        record = calls.setdefault(number, {"call": number, "attempt": None, "role": None, "purpose": None,
                                           "model": None, "started_at": None, "received_at": None,
                                           "finished_at": None, "session_id": None, "status": "unknown",
                                           "error": None, "cleanup_error": None, "usage": provider_usage(None)})
        if event.get("kind") == "call_started":
            model = data.get("model")
            record.update(attempt=_count(data.get("attempt")), role=data.get("role"), purpose=data.get("purpose"),
                          model=model.get("identifier") if isinstance(model, dict) else model,
                          started_at=event.get("at"), status="dispatched")
        elif event.get("kind") == "call_received":
            record.update(received_at=event.get("at"), session_id=data.get("session_id") or record["session_id"],
                          status="response_recorded")
            record["raw"] = data.get("raw_transport_json")
        elif event.get("kind") == "call_finished":
            record.update(finished_at=event.get("at"), session_id=data.get("session_id") or record["session_id"],
                          error=data.get("error"), cleanup_error=data.get("cleanup_error"))
            if "raw" not in record:
                record["raw"] = data.get("raw_transport_json")
            record["status"] = "failed" if record["error"] or record["cleanup_error"] else "completed"
            lifecycle = data.get("lifecycle")
            if isinstance(lifecycle, dict):
                record["session"] = lifecycle
    all_sessions = _sessions(sessions)
    lifecycle_events = _session_events(sessions)
    for record in calls.values():
        task_id = f"collab:{run.get('id')}:{record['call']}"
        session = record.get("session") or next((s for s in all_sessions if s.get("id") == record.get("session_id")), None) \
            or next((s for s in all_sessions if s.get("task_id") == task_id), None)
        if session:
            record["session_id"] = record.get("session_id") or session.get("id")
            record["lifecycle_state"] = session.get("state")
            if record["status"] in ("unknown", "dispatched"):
                record["status"] = "generating" if session.get("state") == "BUSY" else str(session.get("state") or "unknown").lower()
        infer_ms = session.get("infer_ms") if session else None
        record["usage"] = provider_usage(record.get("raw"), infer_ms)
        frozen_at = record.get("finished_at") or record.get("received_at") or (
            run.get("completed_at") if run.get("state") in TERMINAL else None)
        elapsed, timing_source = _inference_elapsed(session, lifecycle_events, now, frozen_at)
        record["inference_elapsed_ms"] = elapsed
        record["inference_timing_source"] = timing_source
        record.pop("raw", None)
        record.pop("session", None)
    return [calls[key] for key in sorted(calls)]


def _logical_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    logical: list[dict[str, Any]] = []
    last_for_role_purpose: dict[tuple[Any, Any], dict[str, Any]] = {}
    for call in calls:
        key = (call.get("role"), call.get("purpose"))
        if call.get("attempt", 0) and key in last_for_role_purpose:
            target = last_for_role_purpose[key]
            target["calls"].append(call["call"])
            target["status"] = call["status"]
            if call["status"] == "completed":
                target["completed"] = True
            continue
        target = {"role": call.get("role"), "purpose": call.get("purpose"), "calls": [call["call"]],
                  "status": call["status"], "completed": call["status"] == "completed"}
        logical.append(target)
        last_for_role_purpose[key] = target
    return logical


def collaboration_progress(run: dict[str, Any], events: Iterable[dict[str, Any]], sessions: Any = (),
                           now: Any = None) -> dict[str, Any]:
    """Project persisted collaboration calls and a non-guaranteed adaptive plan."""
    event_list = sorted([event for event in events if isinstance(event, dict)], key=lambda x: x.get("at") or "")
    current = _now(now)
    calls = _call_records(run, event_list, sessions, current)
    terminal = run.get("state") in TERMINAL
    if terminal:
        terminal_call_state = "cancelled" if run.get("state") == "cancelled" else (
            "interrupted" if run.get("state") == "interrupted" else "failed")
        for call in calls:
            if call["status"] not in ("completed", "failed"):
                call["status"] = terminal_call_state
    logical = _logical_calls(calls)
    rounds = _count(((run.get("spec") or {}).get("limits") or {}).get("rounds")) or 0
    planned = [("initial_candidate", "qwen", None), ("review", "bonsai", 0)]
    for number in range(1, rounds + 1):
        planned.extend([("evaluate_feedback", "qwen", number), ("revision", "qwen", number),
                        ("review", "bonsai", number)])
    calls_limit = _count(((run.get("spec") or {}).get("limits") or {}).get("calls"))
    max_role_calls = calls_limit // 2 if calls_limit is not None else len(planned)
    planned = planned[:max_role_calls]
    queues: dict[str, list[dict[str, Any]]] = {}
    for value in logical:
        queues.setdefault(str(value.get("purpose")), []).append(value)
    stages = []
    original_validations = [event for event in event_list if event.get("kind") == "validation"
                            and _data(event).get("label") == "original"]
    stages.append({"key": "original_validation", "label": "Validate original", "role": "deterministic",
                   "round": None, "status": "completed" if original_validations else ("not_run" if terminal else "upcoming")})
    indexes: dict[str, int] = {}
    for purpose, role, round_number in planned:
        index = indexes.get(purpose, 0); indexes[purpose] = index + 1
        observed = queues.get(purpose, [])
        logical_call = observed[index] if index < len(observed) else None
        status = logical_call["status"] if logical_call else ("not_run" if terminal else "upcoming")
        stages.append({"key": purpose, "label": purpose.replace("_", " ").title(), "role": role,
                       "round": round_number, "status": status,
                       "calls": logical_call["calls"] if logical_call else []})
    current_call = None if terminal else next((call for call in reversed(calls) if call["status"] not in ("completed", "failed")), None)
    usage_values = [call["usage"] for call in calls if call["usage"]["status"] == "provider_reported"]
    usage_totals = {
        "status": "provider_reported" if usage_values else "unavailable",
        "input_tokens": sum(x["input_tokens"] for x in usage_values) if usage_values and all(x["input_tokens"] is not None for x in usage_values) else None,
        "output_tokens": sum(x["output_tokens"] for x in usage_values) if usage_values and all(x["output_tokens"] is not None for x in usage_values) else None,
        "total_tokens": sum(x["total_tokens"] for x in usage_values) if usage_values and all(x["total_tokens"] is not None for x in usage_values) else None,
        "total_source": "calculated_sum_of_role_response_counts" if usage_values else None,
        "calls_with_usage": len(usage_values), "completed_calls": sum(x["status"] == "completed" for x in calls),
    }
    state = str(run.get("state") or "unknown")
    if terminal:
        stage = {"key": state, "label": state.replace("_", " ").title()}
    elif current_call:
        stage = {"key": current_call["status"], "label": f"{str(current_call.get('purpose') or 'Model call').replace('_', ' ').title()} · {current_call['status'].replace('_', ' ').title()}"}
    elif state == "pending":
        stage = {"key": "queued", "label": "Queued"}
    else:
        stage = {"key": "between_calls", "label": "Between persisted model calls"}
    return {
        "kind": "collaboration", "as_of": current.isoformat(), "state": run.get("state"), "terminal": terminal, "stage": stage,
        "cancel_requested": bool(run.get("cancel_requested")),
        "current_call": current_call, "calls": calls, "stages": stages,
        "plan": {"adaptive": True, "not_guaranteed": True, "max_rounds": rounds,
                 "max_role_calls": max_role_calls,
                 "description": "The workflow may stop early after validation, disagreement, cancellation, failure, or cleanup error."},
        "usage_totals": usage_totals,
        "elapsed_ms": _elapsed(run.get("started_at"), run.get("completed_at") if terminal else current),
        "indeterminate": not terminal,
    }
