from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from progress import collaboration_progress, provider_usage, research_progress


NOW = datetime(2026, 10, 11, 12, 0, 10, tzinfo=timezone.utc)


def event(kind, at, **data):
    return {"kind": kind, "at": at, "data": data}


class UsageTests(unittest.TestCase):
    def test_ollama_provider_duration_and_counts(self):
        value = provider_usage(json.dumps({"prompt_eval_count": 12, "eval_count": 30, "eval_duration": 2_000_000_000}), 2500)
        self.assertEqual((value["status"], value["provider"], value["input_tokens"], value["output_tokens"], value["total_tokens"]),
                         ("provider_reported", "ollama", 12, 30, 42))
        self.assertEqual(value["output_tokens_per_second"], 15)
        self.assertEqual(value["throughput_source"], "provider_eval_duration")
        self.assertEqual(value["total_source"], "calculated_from_provider_counts")
        self.assertEqual(value["durations"], {"provider_output_ms": 2000, "inference_interval_ms": 2500.0})

    def test_openai_counts_use_only_interval_average(self):
        raw = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        value = provider_usage(raw, 4000)
        self.assertEqual(value["output_tokens_per_second"], 5)
        self.assertEqual(value["throughput_source"], "calculated_from_provider_counts_and_infer_ms")
        self.assertEqual(value["total_source"], "provider_reported")
        without_time = provider_usage(raw)
        self.assertIsNone(without_time["output_tokens_per_second"])
        self.assertIsNone(without_time["throughput_source"])

    def test_invalid_counts_are_rejected_without_text_estimate(self):
        cases = [
            {"prompt_eval_count": True, "eval_count": 2, "eval_duration": 1},
            {"prompt_eval_count": 1, "eval_count": -1, "eval_duration": 1},
            {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 99}},
            {"usage": {"prompt_tokens": 1.0, "completion_tokens": 2, "total_tokens": 3}},
            {"content": "many words must not become estimated tokens"},
            "not-json",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                value = provider_usage(raw, float("nan"))
                self.assertEqual(value["status"], "unavailable")
                self.assertIsNone(value["output_tokens"])
                self.assertIsNone(value["output_tokens_per_second"])

    def test_zero_duration_keeps_counts_without_rate(self):
        value = provider_usage({"prompt_eval_count": 0, "eval_count": 0, "eval_duration": 0})
        self.assertEqual(value["status"], "provider_reported")
        self.assertIsNone(value["output_tokens_per_second"])


class AdditionalUsageTests(unittest.TestCase):
    def test_partial_counts_preserve_available_output_without_estimation(self):
        for raw in ({"eval_count":9,"eval_duration":1000000000},{"usage":{"completion_tokens":9}}):
            value=provider_usage(raw)
            self.assertEqual(value["output_tokens"],9)
            self.assertIsNone(value["input_tokens"])
            self.assertIsNone(value["total_tokens"])
            self.assertEqual(value["output_source"],"provider_reported")

    def test_missing_total_is_a_calculated_sum(self):
        value=provider_usage({"usage":{"prompt_tokens":2,"completion_tokens":3}})
        self.assertEqual(value["total_tokens"],5)
        self.assertEqual(value["total_source"],"calculated_from_provider_counts")

    def test_extreme_telemetry_cannot_create_nonfinite_rates(self):
        for raw in ({"prompt_eval_count":2,"eval_count":3,"eval_duration":10**400},
                    {"prompt_eval_count":2,"eval_count":3,"eval_duration":1e-300},
                    {"prompt_eval_count":2,"eval_count":10**400,"eval_duration":1}):
            value=provider_usage(raw)
            self.assertIsNone(value["output_tokens_per_second"])
            json.dumps(value,allow_nan=False)

    def test_completed_response_still_tracks_cleanup(self):
        value=research_progress({"state":"completed","max_tokens":100}, {"state":"UNLOADING"})
        self.assertEqual(value["stage"]["key"],"verifying_unload")
        self.assertTrue(value["indeterminate"])


class InterruptedProjectionTests(unittest.TestCase):
    def test_terminal_collaboration_stale_busy_timer_freezes_at_recorded_end(self):
        run={"id":"r","state":"interrupted","started_at":"2026-10-11T12:00:00+00:00","completed_at":"2026-10-11T12:00:05+00:00","spec":{"limits":{"rounds":1,"calls":10}}}
        values=[event("call_started","2026-10-11T12:00:01+00:00",call=1,attempt=0,purpose="initial_candidate",role="qwen",model={"identifier":"q"})]
        sessions={"history":[{"id":"s","task_id":"collab:r:1","state":"BUSY","infer_ms":None}],"events":[{"session_id":"s","to_state":"BUSY","at":"2026-10-11T12:00:02+00:00"}]}
        projection=collaboration_progress(run,values,sessions,NOW)
        self.assertIsNone(projection["current_call"])
        self.assertEqual(projection["calls"][0]["inference_elapsed_ms"],3000)
        self.assertEqual(projection["calls"][0]["status"],"interrupted")
        self.assertFalse(projection["indeterminate"])

class ResearchProgressTests(unittest.TestCase):
    def job(self, **values):
        return {"id": "job", "state": "running", "model_identifier": "qwen", "started_at": "2026-10-11T12:00:00+00:00",
                "completed_at": None, "max_tokens": 100, "raw_response_json": None, **values}

    def test_pending_has_no_fake_elapsed_or_inference(self):
        value = research_progress(self.job(state="pending", started_at=None), now=NOW)
        self.assertEqual(value["stage"]["key"], "queued")
        self.assertIsNone(value["elapsed_ms"])
        self.assertIsNone(value["inference_elapsed_ms"])
        self.assertTrue(value["indeterminate"])

    def test_busy_uses_busy_transition_not_job_start(self):
        session = {"id": "s", "model": "qwen", "state": "BUSY", "infer_ms": None}
        events = [{"session_id": "s", "at": "2026-10-11T12:00:07+00:00", "to_state": "BUSY", "from_state": "READY"}]
        value = research_progress(self.job(), session, events, NOW)
        self.assertEqual(value["stage"]["key"], "generating")
        self.assertEqual(value["elapsed_ms"], 10_000)
        self.assertEqual(value["inference_elapsed_ms"], 3_000)
        self.assertEqual(value["inference_timing_source"], "busy_transition_interval")

    def test_busy_without_transition_is_unknown(self):
        value = research_progress(self.job(), {"id": "s", "model": "qwen", "state": "BUSY", "infer_ms": None}, [], NOW)
        self.assertIsNone(value["inference_elapsed_ms"])
        self.assertIsNone(value["inference_timing_source"])

    def test_terminal_freezes_elapsed_and_uses_recorded_inference(self):
        job = self.job(state="completed", completed_at="2026-10-11T12:00:08+00:00",
                       raw_response_json=json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 25, "total_tokens": 30}}))
        session = {"id": "s", "model": "bonsai", "state": "STOPPED", "infer_ms": 2500}
        value = research_progress(job, session, now=NOW)
        self.assertEqual(value["elapsed_ms"], 8000)
        self.assertEqual(value["inference_elapsed_ms"], 2500)
        self.assertEqual(value["output_budget"], {"status": "available", "label": "Output budget used", "used": 25, "limit": 100, "ratio": .25})
        self.assertFalse(value["indeterminate"])

    def test_cleanup_stage_remains_truthful_after_result_persisted(self):
        job = self.job(state="completed", completed_at="2026-10-11T12:00:08+00:00")
        value = research_progress(job, {"state": "UNLOADING", "model": "qwen", "infer_ms": 1000}, now=NOW)
        self.assertEqual(value["stage"]["key"], "verifying_unload")
        self.assertEqual(value["elapsed_ms"], 8000)

    def test_unknown_session_does_not_claim_lease_waiting(self):
        value = research_progress(self.job(), None, now=NOW)
        self.assertEqual(value["stage"]["key"], "claimed")
        self.assertNotIn("lease", value["stage"]["label"].lower())

    def test_running_with_verified_stopped_session_is_finalizing(self):
        session = {"state": "STOPPED", "last_verified_unload": "2026-10-11T12:00:09+00:00"}
        self.assertEqual(research_progress(self.job(), session, now=NOW)["stage"]["key"], "finalizing")


class CollaborationProgressTests(unittest.TestCase):
    def run_record(self, **values):
        base = {"id": "run", "state": "running", "started_at": "2026-10-11T12:00:00+00:00",
                "completed_at": None, "spec": {"limits": {"rounds": 2, "calls": 24}}}
        base.update(values); return base

    def test_adaptive_plan_has_distinct_feedback_stage_and_no_percentage(self):
        value = collaboration_progress(self.run_record(), [], now=NOW)
        self.assertEqual([x["key"] for x in value["stages"]], ["original_validation", "initial_candidate", "review",
                         "evaluate_feedback", "revision", "review", "evaluate_feedback", "revision", "review"])
        self.assertTrue(value["plan"]["not_guaranteed"])
        self.assertNotIn("percent", value)
        self.assertEqual(value["stage"]["key"], "between_calls")

    def test_original_validation_requires_persisted_event(self):
        missing = collaboration_progress(self.run_record(), [], now=NOW)
        present = collaboration_progress(self.run_record(), [event("validation", "2026-10-11T12:00:01+00:00", label="original")], now=NOW)
        self.assertEqual(missing["stages"][0]["status"], "upcoming")
        self.assertEqual(present["stages"][0]["status"], "completed")

    def test_successful_call_restores_from_persisted_events_and_usage(self):
        raw = json.dumps({"prompt_eval_count": 4, "eval_count": 6, "eval_duration": 1_000_000_000})
        events = [event("call_started", "2026-10-11T12:00:01+00:00", call=1, attempt=0, role="qwen", purpose="initial_candidate", model={"identifier": "qwen"}),
                  event("call_received", "2026-10-11T12:00:03+00:00", call=1, raw_transport_json=raw, session_id="s"),
                  event("call_finished", "2026-10-11T12:00:04+00:00", call=1, raw_transport_json=raw, session_id="s", error=None, cleanup_error=None, lifecycle={"state": "STOPPED", "infer_ms": 1000})]
        value = collaboration_progress(self.run_record(), events, now=NOW)
        self.assertEqual(value["calls"][0]["status"], "completed")
        self.assertEqual(value["stages"][1]["status"], "completed")
        self.assertEqual(value["usage_totals"]["total_tokens"], 10)
        self.assertNotIn("raw", value["calls"][0])

    def test_schema_retry_does_not_complete_logical_stage_twice(self):
        events = [event("call_started", "2026-10-11T12:00:01+00:00", call=1, attempt=0, role="qwen", purpose="initial_candidate"),
                  event("call_finished", "2026-10-11T12:00:02+00:00", call=1, error="schema", cleanup_error=None),
                  event("call_started", "2026-10-11T12:00:03+00:00", call=2, attempt=1, role="qwen", purpose="initial_candidate"),
                  event("call_finished", "2026-10-11T12:00:04+00:00", call=2, error=None, cleanup_error=None)]
        value = collaboration_progress(self.run_record(), events, now=NOW)
        self.assertEqual(value["stages"][1]["calls"], [1, 2])
        self.assertEqual(value["stages"][1]["status"], "completed")
        self.assertEqual(value["usage_totals"]["completed_calls"], 1)

    def test_live_session_matches_synthetic_task_before_response(self):
        events = [event("call_started", "2026-10-11T12:00:01+00:00", call=1, attempt=0, role="qwen", purpose="initial_candidate")]
        sessions = [{"id": "s", "task_id": "collab:run:1", "state": "BUSY", "model": "qwen", "infer_ms": None}]
        value = collaboration_progress(self.run_record(), events, sessions, NOW)
        self.assertEqual(value["calls"][0]["status"], "generating")
        self.assertEqual(value["current_call"]["call"], 1)
        self.assertEqual(value["usage_totals"]["status"], "unavailable")

    def test_terminal_early_stop_marks_unrun_plan_without_fake_completion(self):
        run = self.run_record(state="abstained", completed_at="2026-10-11T12:00:05+00:00")
        value = collaboration_progress(run, [], now=NOW)
        self.assertEqual(value["stage"]["key"], "abstained")
        self.assertTrue(all(stage["status"] == "not_run" for stage in value["stages"]))
        self.assertEqual(value["elapsed_ms"], 5000)
        self.assertFalse(value["indeterminate"])

    def test_plan_is_capped_by_role_request_budget(self):
        value = collaboration_progress(self.run_record(spec={"limits": {"rounds": 4, "calls": 10}}), [], now=NOW)
        self.assertEqual(value["plan"]["max_role_calls"], 5)
        self.assertEqual(len(value["stages"]), 6)  # deterministic original plus five possible role requests

    def test_terminal_call_cannot_remain_visually_generating(self):
        events = [event("call_started", "2026-10-11T12:00:01+00:00", call=1, attempt=0,
                        role="qwen", purpose="initial_candidate")]
        sessions = [{"task_id": "collab:run:1", "state": "BUSY", "model": "qwen"}]
        value = collaboration_progress(self.run_record(state="interrupted", completed_at="2026-10-11T12:00:02+00:00"),
                                       events, sessions, NOW)
        self.assertEqual(value["calls"][0]["status"], "interrupted")
        self.assertEqual(value["calls"][0]["lifecycle_state"], "BUSY")

    def test_failed_and_cancelled_calls_are_not_success(self):
        events = [event("call_started", "2026-10-11T12:00:01+00:00", call=1, attempt=0, role="bonsai", purpose="review"),
                  event("call_finished", "2026-10-11T12:00:02+00:00", call=1, error=None, cleanup_error="unload failed")]
        failed = collaboration_progress(self.run_record(state="failed", completed_at="2026-10-11T12:00:02+00:00"), events, now=NOW)
        self.assertEqual(failed["calls"][0]["status"], "failed")
        self.assertEqual(failed["usage_totals"]["completed_calls"], 0)
        cancelled = collaboration_progress(self.run_record(state="cancelled", completed_at="2026-10-11T12:00:02+00:00"), [], now=NOW)
        self.assertEqual(cancelled["stage"]["key"], "cancelled")


if __name__ == "__main__":
    unittest.main()
