from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from chat_stream import VisibleText, visible_content, ensure_generation, record_worker_feature, run_chat
from lifecycle import Dependencies, Manager
from models import ModelError
from registry import ModelEntry


class StreamHandler(BaseHTTPRequestHandler):
    scenario = "ollama"
    started = threading.Event()
    request_json = None

    def log_message(self, *_): pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_json = json.loads(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if "completions" in self.path else "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers(); type(self).started.set()
        scenario = type(self).scenario
        try:
            if scenario == "ollama":
                self._lines([
                    {"model":"qwen-test","message":{"content":"Hello ","thinking":"hidden"},"done":False},
                    {"model":"qwen-test","message":{"content":"world"},"done":False},
                    {"model":"qwen-test","message":{"content":""},"done":True,
                     "prompt_eval_count":7,"eval_count":2,"eval_duration":10},
                ])
            elif scenario == "bonsai":
                self._sse({"model":"bonsai-test","choices":[{"delta":{"content":"Good "},"finish_reason":None}]})
                self._sse({"model":"bonsai-test","choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]})
                self._sse({"model":"bonsai-test","choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}})
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            elif scenario == "wrong":
                self._lines([{"model":"other","message":{"content":"bad"},"done":True}])
            elif scenario == "malformed":
                self.wfile.write(b"{not-json}\n"); self.wfile.flush()
            elif scenario == "incomplete":
                self._lines([{"model":"qwen-test","message":{"content":"partial"},"done":False}])
            elif scenario == "large-line":
                self.wfile.write(b"x" * (1024 * 1024 + 1) + b"\n"); self.wfile.flush()
            elif scenario in ("stall", "timeout"):
                self._lines([{"model":"qwen-test","message":{"content":"partial"},"done":False}])
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _lines(self, values):
        for value in values:
            self.wfile.write(json.dumps(value).encode() + b"\n"); self.wfile.flush()

    def _sse(self, value):
        self.wfile.write(b"data: " + json.dumps(value).encode() + b"\n\n"); self.wfile.flush()


class FakeDeps(Dependencies):
    def __init__(self):
        self.loaded = False; self.clock = 0
    def monotonic(self): self.clock += .01; return self.clock
    def sleep(self, _): pass
    def diagnostics(self, _): return {}
    def lsof_pid(self, _): return None
    def native(self, entry, method, path, payload, timeout):
        if path == "/api/tags": return {"models":[{"name":entry.identifier}]}
        if path == "/api/ps": return {"models":[{"name":entry.identifier,"context_length":entry.context_window}]} if self.loaded else {"models":[]}
        if path == "/api/generate":
            self.loaded = payload.get("keep_alive") != 0
            return {"model":entry.identifier,"done":True,"done_reason":"unload" if not self.loaded else "load"}
        if path == "/api/chat":
            return {"model":entry.identifier,"done":True,"message":{"content":"OK"}}
        raise AssertionError(path)


class ChatStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.db = self.root / "queue.sqlite3"
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO tasks VALUES('chat-1')")
            ensure_generation(db)
        StreamHandler.scenario = "ollama"; StreamHandler.started.clear(); StreamHandler.request_json = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def entry(self, provider="ollama", timeout=2):
        identifier = "qwen-test" if provider == "ollama" else "bonsai-test"
        return ModelEntry("qwen" if provider=="ollama" else "bonsai", identifier,
                          f"http://127.0.0.1:{self.server.server_port}/v1", provider,
                          max_tokens=128, timeout_seconds=timeout, context_window=4096)

    def task(self, messages=None, **contract):
        value={"kind":"interactive_chat","version":1,"messages":[{"role":"user","content":"Hello"}] if messages is None else messages,"stream":True}
        value.update(contract)
        return {"id":"chat-1","max_tokens":64,"contract_json":json.dumps(value)}

    def generation(self):
        with sqlite3.connect(self.db) as db:
            db.row_factory=sqlite3.Row
            return dict(db.execute("SELECT * FROM tasks_generation WHERE task_id='chat-1'").fetchone())

    def test_inline_reasoning_tags_are_never_forwarded_across_chunk_boundaries(self):
        visible=VisibleText();answer=''
        for chunk in ['<th','ink>private reasoning','</thi','nk>Public ','answer']:
            answer+=visible.feed(chunk)
        answer+=visible.feed('',True)
        self.assertEqual(answer,'Public answer')
        self.assertEqual(visible_content('<think>hidden</think> final'),'final')
        with self.assertRaises(ModelError):visible_content('<think>incomplete')

    def test_ollama_ndjson_visible_content_usage_and_final_persistence(self):
        result, raw = run_chat(self.entry(), self.task(), self.db)
        self.assertEqual(result, "Hello world")
        data=json.loads(raw); self.assertEqual(data["stream_usage"]["output_tokens"],2)
        self.assertEqual(data["stream_usage"]["total_source"],"calculated_from_provider_counts")
        self.assertNotIn("hidden", raw); self.assertFalse(StreamHandler.request_json["think"])
        row=self.generation(); self.assertEqual((row["status"],row["content"]),("completed","Hello world"))
        self.assertEqual(json.loads(row["usage_json"])["input_tokens"],7)

    def test_bonsai_sse_requires_done_and_preserves_provider_usage(self):
        StreamHandler.scenario="bonsai"
        result,raw=run_chat(self.entry("bonsai"),self.task(),self.db)
        self.assertEqual(result,"Good answer")
        self.assertEqual(json.loads(raw)["stream_usage"]["total_source"],"provider_reported")
        self.assertTrue(StreamHandler.request_json["stream"])
        self.assertEqual(StreamHandler.request_json["chat_template_kwargs"],{"enable_thinking":False})

    def test_cancellation_closes_stalled_socket_and_preserves_partial(self):
        StreamHandler.scenario="stall"; errors=[]
        worker=threading.Thread(target=lambda: self._capture(errors, self.entry(timeout=5), self.task()))
        worker.start(); self.assertTrue(StreamHandler.started.wait(1))
        deadline=time.time()+1
        while time.time()<deadline and self.generation()["content"]!="partial": time.sleep(.01)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE tasks_generation SET cancel_requested=1 WHERE task_id='chat-1'");db.commit()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive()); self.assertIsInstance(errors[0],ModelError)
        row=self.generation(); self.assertEqual((row["status"],row["content"]),("cancelled","partial"))

    def _capture(self, errors, entry, task):
        try: run_chat(entry,task,self.db)
        except Exception as exc: errors.append(exc)

    def test_absolute_timeout_closes_stalled_socket(self):
        StreamHandler.scenario="timeout"; started=time.monotonic()
        with self.assertRaisesRegex(ModelError,"timed out"):
            run_chat(self.entry(timeout=.2),self.task(),self.db)
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(self.generation()["status"],"error")

    def test_malformed_wrong_model_incomplete_and_line_bound_fail(self):
        for scenario,phrase in (("malformed","malformed"),("wrong","identity"),
                                ("incomplete","terminal"),("large-line","1 MiB")):
            with self.subTest(scenario=scenario):
                StreamHandler.scenario=scenario
                with self.assertRaisesRegex(ModelError,phrase): run_chat(self.entry(),self.task(),self.db)

    def test_contract_context_roles_shapes_and_max_tokens_are_bounded(self):
        cases=[
            self.task(version=2), self.task(messages=[]),
            self.task(messages=[{"role":"system","content":"x"}]),
            self.task(messages=[{"role":"user","content":"x"*24001}]),
            self.task(messages=[{"role":"user","content":"x"} for _ in range(33)]),
            {**self.task(),"max_tokens":129},
        ]
        for task in cases:
            with self.subTest(task=task):
                with self.assertRaises(ModelError): run_chat(self.entry(),task,self.db)
                self.assertEqual(self.generation()["status"],"error")

    def test_generation_schema_is_idempotent_and_updates_are_coalesced(self):
        with sqlite3.connect(self.db) as db: ensure_generation(db); ensure_generation(db)
        run_chat(self.entry(),self.task(),self.db)
        row=self.generation()
        self.assertGreaterEqual(row["updates"],2)
        self.assertLessEqual(row["updates"],4)

    def test_preexisting_cancel_flag_is_preserved_and_prevents_dispatch(self):
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO tasks_generation(task_id,updated_at,status,cancel_requested) "
                       "VALUES('chat-1','now','pending',1)"); db.commit()
        with self.assertRaisesRegex(ModelError,"cancellation requested"):
            run_chat(self.entry(),self.task(),self.db)
        row=self.generation(); self.assertEqual((row["cancel_requested"],row["status"]),(1,"cancelled"))
        self.assertFalse(StreamHandler.started.is_set())

    def test_worker_feature_heartbeat_records_actual_identity_shape(self):
        with sqlite3.connect(self.db) as db:
            record_worker_feature(db,321,("birth","/python"))
            row=db.execute("SELECT pid,identity_json,version,updated_at FROM worker_features WHERE singleton=1").fetchone()
        self.assertEqual((row[0],json.loads(row[1]),row[2]),(321,["birth","/python"],1))
        self.assertIn("+00:00",row[3])

    def test_lifecycle_only_opts_in_for_interactive_contract(self):
        config=self.root/"models.json"
        item={"family":"qwen","identifier":"qwen-test","endpoint":"http://127.0.0.1:11434/v1",
              "provider":"ollama","owner_mode":"shared","max_tokens":128,"timeout_seconds":5,
              "context_window":4096,"load_timeout":5,"unload_timeout":5,"health_timeout":1}
        config.write_text(json.dumps({"models":[item]}))
        deps=FakeDeps(); deps.generation_db_path=str(self.db)
        manager=Manager(self.root/"state",config,deps=deps)
        task={**self.task(),"registry_json":json.dumps(item),"prompt":"fallback"}
        try:
            with patch("chat_stream.run_chat",return_value=("streamed","{}")) as streamed:
                with manager.lease() as acquired:
                    self.assertTrue(acquired);manager.reconcile();out=manager.execute(task)
                self.assertEqual(out.result,"streamed");streamed.assert_called_once()
            ordinary={"id":"ordinary","registry_json":json.dumps(item),"prompt":"plain","max_tokens":16}
            with patch("chat_stream.run_chat",side_effect=AssertionError("nonchat must not stream")):
                with manager.lease() as acquired:
                    self.assertTrue(acquired);manager.reconcile();out=manager.execute(ordinary)
                self.assertEqual(out.result,"OK")
        finally: manager.store.close()


if __name__ == "__main__": unittest.main()
