from __future__ import annotations

import fcntl
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from collaboration_store import CollaborationStore
from lifecycle import Manager, SessionStore
from queue_store import QueueStore, utc_now
from registry import Registry
from web import DEFAULT_CONFIG, LocalServer, MAX_BODY, WebApplication


def basic_spec(name="Editorial fixture"):
    return {
        "name": name,
        "instruction": "Improve the artifact within the declared constraints.",
        "source": "Fictional source material.",
        "original": "Pebble draft.",
        "format": "text",
        "requires_semantic_verification": True,
        "constraints": {"required_content": ["Pebble"]},
        "metrics": {"target_words": 20},
        "limits": {"rounds": 2, "calls": 20, "runtime": 900, "output_chars": 12000,
                   "total_output_chars": 80000, "max_tokens": 1024, "retries": 0},
    }


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "queue.sqlite3"
        self.resources = self.root / "resources"
        self.app = self.server = self.thread = None
        self._start()

    def tearDown(self):
        self._stop()
        self.temp.cleanup()

    def _start(self, history=None):
        self.app = WebApplication(self.db, DEFAULT_CONFIG, self.resources,
                                  history_db=history, probe_enabled=False)
        self.server = LocalServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        status, headers, bootstrap = self.get("/api/bootstrap")
        self.assertEqual(status, 200)
        self.token = bootstrap["csrf_token"]
        self.cookie = headers["set-cookie"].split(";", 1)[0]

    def _stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.app.close()
        self.app = self.server = self.thread = None

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def raw(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        merged = {"Host": f"127.0.0.1:{self.server.server_port}", **(headers or {})}
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        payload = response.read()
        result_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, result_headers, payload

    def get(self, path, headers=None):
        status, response_headers, body = self.raw("GET", path, headers=headers)
        return status, response_headers, json.loads(body) if body else None

    def post(self, path, value, *, token=True, cookie=True, origin=True, headers=None):
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["X-CSRF-Token"] = self.token
        if cookie:
            request_headers["Cookie"] = self.cookie
        if origin:
            request_headers["Origin"] = self.origin
        request_headers.update(headers or {})
        status, response_headers, body = self.raw("POST", path, json.dumps(value, allow_nan=True), request_headers)
        return status, response_headers, json.loads(body) if body else None

    def submit_job(self, **overrides):
        model = self.app.registry.entries[0]
        payload = {"model_identifier": model.identifier, "prompt": "Local durable fixture", **overrides}
        status, _, result = self.post("/api/jobs", payload)
        self.assertEqual(status, 201, result)
        return result

    def submit_collaboration(self, spec=None, **overrides):
        status, _, result = self.post("/api/collaborations", {"spec": spec or basic_spec(), **overrides})
        self.assertEqual(status, 201, result)
        return result

    def test_bootstrap_exact_registry_and_session_cookie(self):
        status, headers, data = self.get("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("SameSite=Strict", headers["set-cookie"])
        configured = Registry(DEFAULT_CONFIG).entries
        self.assertEqual([x["identifier"] for x in data["models"]], [x.identifier for x in configured])
        self.assertEqual(data["databases"], [{"key": "research", "label": "Research queue", "writable": True}])

    def test_job_submission_persists_exact_snapshot_and_defaults(self):
        result = self.submit_job(name="Pinned model")
        with QueueStore(self.db) as store:
            row = store.get(result["id"])
        entry = self.app.registry.entries[0]
        self.assertEqual((row["model_identifier"], row["endpoint"]), (entry.identifier, entry.endpoint))
        self.assertEqual(json.loads(row["registry_json"])["identifier"], entry.identifier)
        self.assertEqual((row["max_tokens"], row["timeout_seconds"]), (entry.max_tokens, entry.timeout_seconds))

    def test_timezone_future_schedule_is_normalized(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(timezone(timedelta(hours=-5)))
        result = self.submit_job(scheduled_at=future.isoformat())
        with QueueStore(self.db) as store:
            row = store.get(result["id"])
        self.assertEqual(datetime.fromisoformat(row["scheduled_at"]).utcoffset(), timedelta(0))
        self.assertGreater(datetime.fromisoformat(row["scheduled_at"]), datetime.now(timezone.utc))

    def test_restart_preserves_job_and_result_export(self):
        result = self.submit_job()
        with QueueStore(self.db) as store:
            claimed = store.claim_due()
            self.assertEqual(claimed["id"], result["id"])
            self.assertTrue(store.complete(result["id"], "full result", '{"reasoning":"retained"}', "session-x"))
        old_token, old_cookie = self.token, self.cookie
        self._stop(); self._start()
        self.assertNotEqual(self.token, old_token)
        status, _, detail = self.get(f"/api/jobs/research/{result['id']}")
        self.assertEqual((status, detail["job"]["result"]), (200, "full result"))
        status, headers, exported = self.get(f"/api/export/research/research/{result['id']}")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["content-disposition"])
        self.assertEqual(exported["job"]["raw_response_json"], '{"reasoning":"retained"}')
        status, _, error = self.post("/api/jobs", {"model_identifier": self.app.registry.entries[0].identifier,
                                                    "prompt": "stale"}, headers={"X-CSRF-Token": old_token,
                                                    "Cookie": old_cookie})
        self.assertEqual(status, 403)

    def test_job_input_validation_and_configured_limits(self):
        entry = self.app.registry.entries[0]
        cases = [
            ({"model_identifier": "missing", "prompt": "x"}, "exact configured"),
            ({"model_identifier": entry.identifier, "prompt": 3}, "must be text"),
            ({"model_identifier": entry.identifier, "prompt": "x", "max_tokens": True}, "max_tokens"),
            ({"model_identifier": entry.identifier, "prompt": "x", "max_tokens": entry.max_tokens + 1}, "max_tokens"),
            ({"model_identifier": entry.identifier, "prompt": "x", "timeout_seconds": entry.timeout_seconds + 1}, "timeout_seconds"),
            ({"model_identifier": entry.identifier, "prompt": "x", "scheduled_at": "2026-10-11T10:00:00"}, "timezone"),
            ({"model_identifier": entry.identifier, "prompt": "x", "extra": 1}, "Unsupported"),
        ]
        for payload, phrase in cases:
            with self.subTest(payload=payload):
                status, _, result = self.post("/api/jobs", payload)
                self.assertEqual(status, 400)
                self.assertIn(phrase.lower(), result["error"].lower())

    def test_malformed_duplicate_nonfinite_and_oversized_json(self):
        common = {"Content-Type": "application/json", "Origin": self.origin,
                  "Cookie": self.cookie, "X-CSRF-Token": self.token}
        for body in (b'{"prompt":', b'{"model_identifier":"x","model_identifier":"y","prompt":"z"}',
                     b'{"model_identifier":"x","prompt":"z","timeout_seconds":NaN}'):
            with self.subTest(body=body):
                status, _, result = self.raw("POST", "/api/jobs", body, common)
                self.assertEqual(status, 400)
                self.assertIn("Malformed JSON", json.loads(result)["error"])
        status, _, result = self.raw("POST", "/api/jobs", b"{" + b" " * MAX_BODY, common)
        self.assertEqual(status, 413)
        self.assertIn("128 KiB", json.loads(result)["error"])

    def test_prompt_and_name_store_bounds_are_enforced(self):
        entry = self.app.registry.entries[0]
        for payload in ({"model_identifier": entry.identifier, "prompt": "x" * 32001},
                        {"model_identifier": entry.identifier, "prompt": "x", "name": "n" * 81}):
            with self.subTest(field=list(payload)[-1]):
                status, _, data = self.post("/api/jobs", payload)
                self.assertEqual(status, 400)

    def test_host_origin_fetch_site_csrf_and_cookie_guards(self):
        status, _, _ = self.get("/api/state", {"Host": "attacker.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.post("/api/jobs", {}, headers={"Origin": "http://attacker.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.post("/api/jobs", {}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertEqual(self.post("/api/jobs", {}, token=False)[0], 403)
        self.assertEqual(self.post("/api/jobs", {}, cookie=False)[0], 403)
        self.assertEqual(self.post("/api/jobs", {}, headers={"Cookie": "cc_session=wrong"})[0], 403)

    def test_security_headers_and_method_restrictions(self):
        status, headers, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        status, _, body = self.raw("PUT", "/api/jobs", b"{}", {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["error"]), (405, "Method not supported."))

    def test_path_traversal_unknown_alias_and_unknown_record(self):
        self.assertEqual(self.get("/static/%2e%2e/web.py")[0], 404)
        self.assertEqual(self.get("/api/jobs/unknown/id")[0], 404)
        self.assertEqual(self.get("/api/jobs/research/not-here")[0], 404)

    def test_pending_ordinary_cancel_and_running_conflict(self):
        pending = self.submit_job()
        status, _, cancelled = self.post(f"/api/jobs/research/{pending['id']}/cancel", {})
        self.assertEqual((status, cancelled["state"]), (200, "cancelled"))
        running = self.submit_job()
        with QueueStore(self.db) as store:
            self.assertEqual(store.claim_due()["id"], running["id"])
        status, _, conflict = self.post(f"/api/jobs/research/{running['id']}/cancel", {})
        self.assertEqual(status, 409)
        self.assertIn("no longer eligible", conflict["error"])

    def test_collaboration_pending_and_running_cooperative_cancel(self):
        pending = self.submit_collaboration()
        status, _, result = self.post(f"/api/collaborations/research/{pending['id']}/cancel", {})
        self.assertEqual((status, result["state"], result["cancel_requested"]), (200, "cancelled", True))
        running = self.submit_collaboration(basic_spec("Running fixture"))
        with CollaborationStore(self.db) as store:
            self.assertEqual(store.claim_due()["id"], running["id"])
        status, _, result = self.post(f"/api/collaborations/research/{running['id']}/cancel", {})
        self.assertEqual((status, result["state"], result["cancel_requested"]), (200, "running", True))

    def test_collaboration_normalizes_snapshots_and_full_evidence_export(self):
        result = self.submit_collaboration()
        with CollaborationStore(self.db) as store:
            run = store.claim_due()
            original = store.evidence(result["id"])["revisions"][0]
            revision = store.add_revision(result["id"], "qwen-only", "Pebble revised.", original["id"], {"call": 1})
            store.append_event(result["id"], "review", {"verdict": "approve", "comments": ["clear"]})
            store.finish(result["id"], "abstained", {"accepted": False, "best_revision_id": revision}, "semantic verification absent")
        status, _, detail = self.get(f"/api/collaborations/research/{result['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["run"]["spec"]["limits"]["rounds"], 2)
        self.assertEqual(set(detail["run"]["models"]), {"qwen", "bonsai"})
        self.assertEqual([r["label"] for r in detail["revisions"]], ["original", "qwen-only"])
        self.assertTrue(any(e["kind"] == "review" for e in detail["events"]))
        status, headers, exported = self.get(f"/api/export/collaboration/research/{result['id']}")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["content-disposition"])
        self.assertEqual(exported["revisions"][0]["artifact"], "Pebble draft.")

    def test_collaboration_validation_errors_are_bounded(self):
        for value in ({}, {"spec": {"name": "missing"}}, {"spec": basic_spec(), "unexpected": True}):
            with self.subTest(value=value):
                status, _, body = self.post("/api/collaborations", value)
                self.assertEqual(status, 400)
                self.assertTrue(body["error"])

    def test_history_alias_is_read_only_and_evidence_unchanged(self):
        history = self.root / "verification.sqlite3"
        with QueueStore(history) as queue:
            queue.enqueue(model_family="qwen", model_identifier="historic", endpoint="http://127.0.0.1:11434/v1",
                          prompt="historical", scheduled_at=utc_now())
        with CollaborationStore(history) as store:
            run_id = store.enqueue(basic_spec("Verified history"), {"qwen": {}, "bonsai": {}})
            before = store.evidence(run_id)
        self._stop(); self._start(history)
        status, _, state = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(any(x["database"] == "verification" for x in state["jobs"]))
        status, _, detail = self.get(f"/api/collaborations/verification/{run_id}")
        self.assertEqual(detail["run"]["original"], "Pebble draft.")
        self.assertEqual(self.post(f"/api/collaborations/verification/{run_id}/cancel", {})[0], 403)
        with CollaborationStore(history) as store:
            after = store.evidence(run_id)
        self.assertEqual(before, after)

    def test_workers_and_lease_are_truthful_without_rewriting_owner_files(self):
        worker = Path(str(self.db.resolve()) + ".worker.lock")
        lease = self.resources / "heavy-model.lease"
        worker.write_text("worker-owner\n"); self.resources.mkdir(exist_ok=True); lease.write_text("lease-owner\n")
        status, _, state = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(state["workers"][0]["active"])
        self.assertFalse(state["lease"]["held"])
        self.assertEqual((worker.read_text(), lease.read_text()), ("worker-owner\n", "lease-owner\n"))
        with worker.open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(self.get("/api/state")[2]["workers"][0]["active"])
        self.assertEqual(worker.read_text(), "worker-owner\n")

    def test_stale_recorded_session_is_separate_from_live_observation(self):
        entry = self.app.registry.entries[0]
        store = SessionStore(self.resources)
        try:
            sid = store.create(entry, "old-task")
            store.db.execute("UPDATE sessions SET state='READY',load_ms=123,updated_at=? WHERE id=?", (utc_now(), sid))
            store.db.execute("UPDATE current SET session_id=?,blocker='operator check required' WHERE singleton=1", (sid,))
            store.db.commit()
        finally:
            store.close()
        status, _, state = self.get("/api/state")
        model = next(x for x in state["models"] if x["identifier"] == entry.identifier)
        self.assertEqual(model["last_session"]["state"], "READY")
        self.assertIsNone(model["observed_active"])
        self.assertEqual(model["availability"], "unknown")
        self.assertEqual(state["lifecycle"]["blocker"], "operator check required")
        self.app._observation = {"observed_at": utc_now(), "models": [{"model": entry.identifier, "ok": True,
            "value": {"loaded": [entry.identifier]}, "availability": "available", "availability_reason": "fixture"}]}
        state = self.get("/api/state")[2]
        model = next(x for x in state["models"] if x["identifier"] == entry.identifier)
        self.assertTrue(model["observed_active"])
        self.assertEqual(model["last_session"]["state"], "READY")

    def test_state_summaries_exclude_prompts_and_full_raw(self):
        result = self.submit_job(prompt="SECRET-PROMPT-FIXTURE")
        with QueueStore(self.db) as store:
            task = store.claim_due(); store.complete(task["id"], "SECRET-RESULT-FIXTURE", "SECRET-RAW-FIXTURE")
        _, _, state = self.get("/api/state")
        encoded = json.dumps(state)
        self.assertNotIn("SECRET-PROMPT-FIXTURE", encoded)
        self.assertNotIn("SECRET-RESULT-FIXTURE", encoded)
        self.assertNotIn("SECRET-RAW-FIXTURE", encoded)
        summary = next(x for x in state["jobs"] if x["id"] == result["id"])
        self.assertTrue(summary["has_result"])

    def test_static_routes_csp_and_no_external_assets(self):
        for path, content_type in (("/", "text/html"), ("/static/app.js", "text/javascript"),
                                   ("/static/styles.css", "text/css")):
            with self.subTest(path=path):
                status, headers, body = self.raw("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith(content_type))
                self.assertIn("script-src 'self'", headers["content-security-policy"])
                self.assertNotIn(b"https://", body)
                self.assertNotIn(b"http://", body)
        html = self.raw("GET", "/")[2].decode()
        self.assertIn('/static/app.js', html)
        self.assertNotIn('<script>', html)

    def test_web_submission_never_dispatches_or_reconciles_models(self):
        with patch.object(Manager, "execute", side_effect=AssertionError("execute called")) as execute, \
             patch.object(Manager, "reconcile", side_effect=AssertionError("reconcile called")) as reconcile, \
             patch("subprocess.Popen", side_effect=AssertionError("process started")) as popen:
            self.submit_job()
            self.submit_collaboration()
        execute.assert_not_called(); reconcile.assert_not_called(); popen.assert_not_called()

    def test_state_counts_activity_and_truncation_shape(self):
        job = self.submit_job(name="Activity fixture")
        collaboration = self.submit_collaboration()
        status, _, state = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(state["counts"]["pending"], 2)
        self.assertTrue(any(x["record_id"] == job["id"] for x in state["activity"]))
        self.assertTrue(any(x["record_id"] == collaboration["id"] for x in state["activity"]))
        self.assertIsInstance(state["truncated"], bool)
        self.assertLessEqual(len(state["activity"]), 200)

    def test_historical_job_id_collision_does_not_attach_primary_lifecycle(self):
        result = self.submit_job()
        entry = self.app.registry.entries[0]
        with SessionStoreFixture(self.resources) as store:
            store.create(entry, result['id'])
        history = self.root / 'history.sqlite3'
        with QueueStore(history) as store:
            store.enqueue(model_family=entry.family, model_identifier=entry.identifier, endpoint=entry.endpoint,
                          prompt='Historical input', scheduled_at=utc_now(), task_id=result['id'])
        self._stop(); self._start(history)
        status, _, detail = self.get('/api/jobs/verification/' + result['id'])
        self.assertEqual(status, 200)
        self.assertIsNone(detail['lifecycle'])
        lifecycle_events = [a for a in self.get('/api/state')[2]['activity'] if a['source'] == 'lifecycle']
        self.assertTrue(lifecycle_events)
        self.assertTrue(all(a['record_id'] is None for a in lifecycle_events))

    def test_collaboration_summary_changes_when_stage_evidence_is_appended(self):
        result = self.submit_collaboration()
        before = next(j for j in self.get('/api/state')[2]['jobs'] if j['id'] == result['id'])
        with CollaborationStore(self.db) as store:
            store.append_event(result['id'], 'validation', {'label': 'original', 'validation': {'valid': True}})
        after = next(j for j in self.get('/api/state')[2]['jobs'] if j['id'] == result['id'])
        self.assertEqual(before['state'], after['state'])
        self.assertGreater(after['event_seq'], before['event_seq'])


    def test_live_progress_is_read_only_and_token_usage_arrives_after_response(self):
        created = self.submit_job()
        with QueueStore(self.db) as queue:
            job = queue.claim_due()
        with SessionStoreFixture(self.resources) as store:
            sid = store.create(self.app.registry.entries[0], job['id'])
            store.transition(sid, 'LOADING')
            store.transition(sid, 'READY')
            store.transition(sid, 'BUSY')
        _, _, state = self.get('/api/state')
        progress = next(j for j in state['jobs'] if j['id'] == created['id'])['progress']
        self.assertEqual(progress['stage']['key'], 'generating')
        self.assertTrue(progress['indeterminate'])
        self.assertEqual(progress['usage']['status'], 'unavailable')
        self.assertGreaterEqual(progress['inference_elapsed_ms'], 0)
        raw = json.dumps({'prompt_eval_count': 12, 'eval_count': 30, 'eval_duration': 2000000000, 'response': 'never send generated text on summary polls'})
        with QueueStore(self.db) as queue:
            queue.complete(job['id'], 'response', raw, sid)
        _, _, state = self.get('/api/state')
        row = next(j for j in state['jobs'] if j['id'] == created['id'])
        self.assertEqual(row['progress']['usage']['output_tokens'], 30)
        self.assertEqual(row['progress']['output_budget']['label'], 'Output budget used')
        self.assertNotIn('never send generated text', json.dumps(state))
        _, _, detail = self.get('/api/jobs/research/' + created['id'])
        self.assertTrue(detail['stage_history'])
        self.assertEqual(detail['progress']['usage']['output_tokens_per_second'], 15)
        self._stop(); self._start()
        _, _, restored = self.get('/api/jobs/research/' + created['id'])
        self.assertEqual(restored['progress']['usage']['output_tokens'], 30)
        self.assertEqual(restored['stage_history'], detail['stage_history'])

    def test_collaboration_progress_projects_usage_without_prompt_or_raw_on_polls(self):
        created = self.submit_collaboration()
        with CollaborationStore(self.db) as store:
            run = store.claim_due()
            store.append_event(run['id'], 'call_started', {'call':1,'attempt':0,'role':'bonsai','purpose':'review',
                'model':{'identifier':self.app.registry.entries[1].identifier},'prompt':'secret prompt not for summaries'})
            raw = json.dumps({'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30},'choices':[{'message':{'content':'generated private content'}}]})
            store.append_event(run['id'], 'call_received', {'call':1,'session_id':'fixture','raw_transport_json':raw})
        _, _, state = self.get('/api/state')
        progress = next(j for j in state['jobs'] if j['id']==created['id'])['progress']
        self.assertEqual(progress['current_call']['usage']['output_tokens'],20)
        self.assertNotIn('secret prompt',json.dumps(state))
        self.assertNotIn('generated private content',json.dumps(state))
        self.assertTrue(progress['plan']['not_guaranteed'])
        _, _, detail = self.get('/api/collaborations/research/' + created['id'])
        self.assertEqual(detail['progress']['usage_totals']['output_tokens'],20)

    def test_collaboration_lifecycle_links_only_the_matching_research_record(self):
        created=self.submit_collaboration()
        with CollaborationStore(self.db) as store: store.claim_due()
        with SessionStoreFixture(self.resources) as store:
            sid=store.create(self.app.registry.entries[0], 'collab:'+created['id']+':1')
            store.transition(sid,'LOADING')
        _,_,state=self.get('/api/state')
        row=next(j for j in state['jobs'] if j['id']==created['id'])
        self.assertTrue(row['lifecycle_active'])
        linked=[e for e in state['activity'] if e['source']=='lifecycle']
        self.assertTrue(linked)
        self.assertTrue(all(e['record_id']==created['id'] and e['kind']=='collaboration' and e['database']=='research' for e in linked))
        self.assertIsNone(self.app.lifecycle_job('collab:'+created['id']+':not-a-call',state['jobs']))

    def test_model_actions_use_registry_and_never_dispatch(self):
        with patch.object(Manager,'execute',side_effect=AssertionError('dispatch')), patch.object(Manager,'reconcile',side_effect=AssertionError('reconcile')):
            _, _, state = self.get('/api/state')
            for model in state['models']:
                self.assertIsNone(model['access']['browser_url'])
                self.assertEqual(model['access']['endpoint_url'],model['endpoint'])
                self.assertTrue(model['access']['details_url'].startswith('#/models/registered/'))


class SessionStoreFixture:
    def __init__(self, path):
        self.store = SessionStore(path)
    def __enter__(self):
        return self.store
    def __exit__(self, *_):
        self.store.close()


if __name__ == "__main__":
    unittest.main()
