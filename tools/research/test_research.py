from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cli import acquire_lock, run_worker, worker_active
from models import LocalHTTP, ModelError, ModelConfig, discover, safe_endpoint
from queue_store import QueueStore, utc_now


class FakeHandler(BaseHTTPRequestHandler):
    models = ["qwen3:14b-q8_0", "qwen3-coder:30b", "not-this-family"]
    response_content = "complete answer"
    raw_extra = {"reasoning_content": "full private local reasoning record"}
    post_status = 200
    delay = 0.0
    redirect = False
    slow_drip = False
    requests: list[dict] = []
    loaded = []
    unload_delay = 0.0
    load_delay = 0.0
    unloading = False
    loading = False

    def log_message(self, *_: object) -> None:
        pass

    def respond(self, payload, status=200):
        raw=json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        try:
            if self.slow_drip and self.path=="/v1/chat/completions":
                for byte in raw:
                    self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.02)
            else: self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError): pass

    def do_GET(self):
        if self.path=="/v1/models": self.respond({"data":[{"id":x} for x in self.models]})
        elif self.path=="/api/tags": self.respond({"models":[{"name":x} for x in ["qwen3:test","good",*self.models]]})
        elif self.path=="/api/ps": self.respond({"models":[{"name":x,"context_length":4096} for x in self.loaded]})
        else: self.send_error(404)

    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
        if self.path=="/api/generate":
            unload=body.get("keep_alive")==0
            if unload:
                type(self).unloading=True
                if self.unload_delay: time.sleep(self.unload_delay)
                type(self).loaded=[]
            else:
                type(self).loading=True
                if self.load_delay: time.sleep(self.load_delay)
                type(self).loaded=[body["model"]]
            self.respond({"model":body["model"],"done":True,"done_reason":"unload" if unload else "stop"})
            return
        warmup=self.path=="/api/chat" and body["messages"][0]["content"]=="Reply OK"
        if not warmup:
            type(self).requests.append(body)
            if self.delay: time.sleep(self.delay)
        if self.redirect:
            self.send_response(307); self.send_header("Location","http://example.com/steal"); self.end_headers(); return
        if self.path=="/api/chat":
            payload={"model":body["model"],"done":True,"message":{"content":"OK" if warmup else self.response_content,**self.raw_extra}}
        else:
            payload={"choices":[{"message":{"content":self.response_content,**self.raw_extra}}]}
        self.respond(payload,self.post_status)


class Server:
    def __init__(self):
        FakeHandler.requests = []
        FakeHandler.loaded = []
        FakeHandler.load_delay=FakeHandler.unload_delay=0
        FakeHandler.loading=FakeHandler.unloading=False
        FakeHandler.delay = 0
        FakeHandler.redirect = False
        FakeHandler.slow_drip = False
        FakeHandler.post_status = 200
        FakeHandler.response_content = "complete answer"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "private" / "queue.sqlite3"

    def config(self,server):
        path=Path(self.temp.name)/"models.json"
        path.write_text(json.dumps({"models":[{"family":"qwen","identifier":"qwen3:test",
            "endpoint":server.endpoint,"provider":"ollama","owner_mode":"shared",
            "load_timeout":3,"unload_timeout":3,"health_timeout":1}]}))
        return path

    def worker_args(self,server):
        return [sys.executable,str(HERE/"cli.py"),"--db",str(self.db),"--config",str(self.config(server)),
                "--resources-dir",str(Path(self.temp.name)/"resources"),"worker"]

    def worker(self,server):
        return run_worker(str(self.db),once=True,registry_path=self.config(server),resources_dir=Path(self.temp.name)/"resources")

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, store: QueueStore, **changes) -> str:
        values = dict(
            model_family="qwen",
            model_identifier="qwen3:test",
            endpoint="http://127.0.0.1:9/v1",
            prompt="research this",
            scheduled_at=utc_now(),
            max_tokens=1024,
        )
        values.update(changes)
        return store.enqueue(**values)

    def test_creation_reopen_persistence_due_and_future(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with QueueStore(self.db) as store:
            immediate = self.enqueue(store, name="now")
            later = self.enqueue(store, name="later", scheduled_at=future)
        with QueueStore(self.db) as reopened:
            self.assertEqual(reopened.claim_due()["id"], immediate)
            self.assertIsNone(reopened.claim_due())
            self.assertEqual(reopened.get(later)["state"], "pending")
            self.assertTrue(reopened.get(later)["scheduled_at"].endswith("+00:00"))
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db.parent.stat().st_mode & 0o777, 0o700)

    def test_cancel_only_pending_and_full_result(self):
        with QueueStore(self.db) as store:
            cancelled = self.enqueue(store)
            self.assertTrue(store.cancel(cancelled))
            self.assertFalse(store.cancel(cancelled))
            task_id = self.enqueue(store)
            self.assertEqual(store.claim_due()["id"], task_id)
            self.assertFalse(store.cancel(task_id))
            large = "x" * 200_000
            raw = json.dumps({"answer": large, "reasoning": "kept"})
            self.assertTrue(store.complete(task_id, large, raw))
        with QueueStore(self.db) as reopened:
            row = reopened.get(task_id)
            self.assertEqual(row["result"], large)
            self.assertEqual(row["raw_response_json"], raw)

    def test_atomic_claim_and_cancel(self):
        with QueueStore(self.db) as store:
            task_id = self.enqueue(store)
        barrier = threading.Barrier(3)
        results = []

        def claim():
            with QueueStore(self.db) as store:
                barrier.wait()
                item = store.claim_due()
                results.append(("claim", item is not None))

        def cancel():
            with QueueStore(self.db) as store:
                barrier.wait()
                results.append(("cancel", store.cancel(task_id)))

        threads = [threading.Thread(target=claim), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(ok for _, ok in results), 1)

    def test_lock_is_canonical_and_exclusive(self):
        self.db.parent.mkdir()
        alias = self.db.parent / ".." / "private" / self.db.name
        first = acquire_lock(self.db)
        self.assertIsNotNone(first)
        try:
            self.assertTrue(worker_active(alias))
        finally:
            first.close()
        self.assertFalse(worker_active(self.db))

    def test_killed_worker_row_is_recovered_without_retry(self):
        server = Server()
        try:
            FakeHandler.delay = 10
            with QueueStore(self.db) as store:
                task_id = self.enqueue(store, endpoint=server.endpoint)
            process = subprocess.Popen(
                self.worker_args(server)+["--once"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with QueueStore(self.db) as store:
                    if store.get(task_id)["state"] == "running" and FakeHandler.requests:
                        break
                time.sleep(0.03)
            else:
                self.fail("worker did not dispatch task")
            process.kill()
            process.wait(timeout=3)
            self.assertEqual(self.worker(server), 0)
            with QueueStore(self.db) as store:
                row = store.get(task_id)
            self.assertEqual(row["state"], "failed")
            self.assertIn("outcome is unknown", row["error"])
            self.assertEqual(len(FakeHandler.requests), 1)
        finally:
            # Avoid waiting for the deliberately sleeping request thread.
            server.httpd.shutdown()
            server.httpd.server_close()

    def test_sigterm_during_inference_persists_returned_success(self):
        server = Server()
        try:
            FakeHandler.delay = 0.25
            with QueueStore(self.db) as store:
                task_id = self.enqueue(store, endpoint=server.endpoint)
            process = subprocess.Popen(
                self.worker_args(server),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not FakeHandler.requests:
                time.sleep(0.01)
            self.assertTrue(FakeHandler.requests)
            process.terminate()
            self.assertEqual(process.wait(timeout=3), 0)
            with QueueStore(self.db) as store:
                row = store.get(task_id)
            self.assertEqual(row["state"], "completed")
            self.assertEqual(row["result"], "complete answer")
        finally:
            server.close()

    def test_future_task_runs_when_due_with_console_closed(self):
        server = Server()
        process = None
        try:
            due = datetime.now(timezone.utc) + timedelta(seconds=1)
            with QueueStore(self.db) as store:
                task_id = self.enqueue(store, endpoint=server.endpoint, scheduled_at=due.isoformat())
            process = subprocess.Popen(
                self.worker_args(server),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.2)
            with QueueStore(self.db) as store:
                self.assertEqual(store.get(task_id)["state"], "pending")
            self.assertEqual(FakeHandler.requests, [])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with QueueStore(self.db) as store:
                    row = store.get(task_id)
                if row["state"] == "completed":
                    break
                time.sleep(0.03)
            else:
                self.fail("future task did not run when due")
            self.assertGreaterEqual(datetime.fromisoformat(row["started_at"]), due)
            self.assertEqual(len(FakeHandler.requests), 1)
            self.assertEqual(row["result"], "complete answer")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
            server.close()


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()

    def tearDown(self):
        self.server.close()

    def test_localhost_safety_proxy_bypass_redirect_and_timeout(self):
        self.assertEqual(
            safe_endpoint(self.server.endpoint.replace("127.0.0.1", "localhost")),
            self.server.endpoint,
        )
        self.assertEqual(safe_endpoint("http://[::1]:8080/v1"), "http://[::1]:8080/v1")
        for bad in (
            "https://127.0.0.1/v1",
            "http://example.com/v1",
            "http://user@127.0.0.1/v1",
            "http://127.0.0.1/v1?q=x",
        ):
            with self.assertRaises(ValueError):
                safe_endpoint(bad)
        old_proxy = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        try:
            answer, _ = LocalHTTP(self.server.endpoint, 2).complete("qwen3:test", "p", 5)
            self.assertEqual(answer, "complete answer")
        finally:
            if old_proxy is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = old_proxy
        FakeHandler.redirect = True
        with self.assertRaisesRegex(ModelError, "redirect"):
            LocalHTTP(self.server.endpoint, 2).complete("qwen3:test", "secret", 5)
        FakeHandler.redirect = False
        FakeHandler.delay = 0.2
        with self.assertRaisesRegex(ModelError, "timed out"):
            LocalHTTP(self.server.endpoint, 0.05).complete("qwen3:test", "p", 5)
        FakeHandler.delay = 0
        FakeHandler.slow_drip = True
        started = time.monotonic()
        with self.assertRaisesRegex(ModelError, "timed out"):
            LocalHTTP(self.server.endpoint, 0.08).complete("qwen3:test", "p", 5)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_discovery_family_filter_routing_and_raw_preservation(self):
        seed = ModelConfig("qwen", "configured-offline", self.server.endpoint, 77, 2, "docs")
        models, errors = discover([seed])
        self.assertFalse(errors)
        self.assertEqual(
            {item.identifier for item in models},
            {"configured-offline", "qwen3:14b-q8_0", "qwen3-coder:30b"},
        )
        answer, raw = LocalHTTP(self.server.endpoint, 2).complete("qwen3-coder:30b", "route", 77)
        self.assertEqual(answer, "complete answer")
        self.assertIn("reasoning_content", raw)
        self.assertEqual(FakeHandler.requests[-1]["model"], "qwen3-coder:30b")
        self.assertEqual(FakeHandler.requests[-1]["max_tokens"], 77)

    def test_model_failure_does_not_prevent_later_task(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "q.sqlite3"
            with QueueStore(db) as store:
                bad = store.enqueue(
                    model_family="qwen", model_identifier="bad", endpoint="http://127.0.0.1:1/v1",
                    prompt="bad", scheduled_at=utc_now(), max_tokens=10,
                )
                good = store.enqueue(
                    model_family="qwen", model_identifier="good", endpoint=self.server.endpoint,
                    prompt="good", scheduled_at=utc_now(), max_tokens=10,
                )
            config=Path(temp)/"models.json"
            config.write_text(json.dumps({"models":[{"family":"qwen","identifier":"good",
                "endpoint":self.server.endpoint,"provider":"ollama","owner_mode":"shared"}]}))
            self.assertEqual(run_worker(str(db), once=True,registry_path=config,resources_dir=Path(temp)/"resources"), 0)
            with QueueStore(db) as store:
                self.assertEqual(store.get(bad)["state"], "failed")
                self.assertEqual(store.get(good)["state"], "completed")


if __name__ == "__main__":
    unittest.main()
