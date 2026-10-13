"""Bounded durable streaming for explicitly contracted interactive chat tasks."""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from models import MAX_RESPONSE_BYTES, ModelError, safe_endpoint

MAX_LINE_BYTES = 1024 * 1024
MAX_CONTENT_CHARS = 4 * 1024 * 1024
MAX_MESSAGES = 32
MAX_CONTEXT_CHARS = 24_000
WRITE_INTERVAL = .25
_HIDDEN_KEYS = frozenset({"thinking", "reasoning", "reasoning_content"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def ensure_generation(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("""
        CREATE TABLE IF NOT EXISTS tasks_generation (
          task_id TEXT PRIMARY KEY REFERENCES tasks(id),
          content TEXT NOT NULL DEFAULT '',
          usage_json TEXT,
          updated_at TEXT NOT NULL,
          status TEXT NOT NULL,
          cancel_requested INTEGER NOT NULL DEFAULT 0,
          updates INTEGER NOT NULL DEFAULT 0
        )
    """)
    db.commit()


def record_worker_feature(db: sqlite3.Connection, pid: int, identity: Any) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS worker_features (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          pid INTEGER,
          identity_json TEXT,
          version INTEGER,
          updated_at TEXT
        )
    """)
    encoded = json.dumps(identity, ensure_ascii=False, allow_nan=False) if identity is not None else None
    db.execute("INSERT INTO worker_features(singleton,pid,identity_json,version,updated_at) VALUES(1,?,?,1,?) "
               "ON CONFLICT(singleton) DO UPDATE SET pid=excluded.pid,identity_json=excluded.identity_json,"
               "version=excluded.version,updated_at=excluded.updated_at", (pid, encoded, _now()))
    db.commit()


def is_interactive_chat(task: dict[str, Any]) -> bool:
    raw = task.get("contract_json")
    if not isinstance(raw, str):
        return False
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, dict) and value.get("kind") == "interactive_chat"


def cancellation_requested(db_path: str | Path, task_id: str) -> bool:
    with sqlite3.connect(Path(db_path).resolve(), timeout=.25) as db:
        row = db.execute("SELECT cancel_requested FROM tasks_generation WHERE task_id=?", (task_id,)).fetchone()
        return bool(row and row[0] == 1)


def mark_generation_cancelled(db_path: str | Path, task_id: str) -> None:
    with sqlite3.connect(Path(db_path).resolve(), timeout=2) as db:
        ensure_generation(db)
        db.execute("UPDATE tasks_generation SET status='cancelled',updated_at=?,updates=updates+1 "
                   "WHERE task_id=? AND status!='completed'", (_now(), task_id))
        db.commit()


def _contract(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("contract_json")
    try:
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError("Duplicate interactive chat contract field.")
                value[key] = item
            return value
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON.")))
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ModelError("Interactive chat contract is malformed.") from exc
    if not isinstance(value, dict) or set(value) != {"kind", "version", "messages", "stream"} \
            or value.get("kind") != "interactive_chat" or type(value.get("version")) is not int or value.get("version") != 1 \
            or value.get("stream") is not True:
        raise ModelError("Interactive chat contract must be exact version 1 with streaming enabled.")
    messages = value.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ModelError("Interactive chat requires 1 to 32 messages.")
    total = 0
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"} \
                or message.get("role") not in ("user", "assistant") \
                or not isinstance(message.get("content"), str) or not message["content"].strip():
            raise ModelError("Interactive chat messages must contain only nonempty user or assistant text.")
        total += len(message["content"])
    if total > MAX_CONTEXT_CHARS:
        raise ModelError("Interactive chat context exceeds 24000 characters.")
    return value


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in _HIDDEN_KEYS}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _count(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ModelError(f"Model stream reported invalid {name}.")
    return value


def _usage_ollama(frame: dict[str, Any]) -> dict[str, Any] | None:
    prompt = _count(frame.get("prompt_eval_count"), "prompt token count")
    output = _count(frame.get("eval_count"), "output token count")
    if prompt is None and output is None:
        return None
    value: dict[str, Any] = {"input_tokens": prompt, "output_tokens": output,
                             "input_source": "provider_reported" if prompt is not None else "unavailable",
                             "output_source": "provider_reported" if output is not None else "unavailable"}
    if prompt is not None and output is not None:
        value.update(total_tokens=prompt + output, total_source="calculated_from_provider_counts")
    duration = frame.get("eval_duration")
    if duration is not None:
        if type(duration) is not int or duration < 0:
            raise ModelError("Model stream reported invalid evaluation duration.")
        value["eval_duration_ns"] = duration
    return value


def _usage_openai(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ModelError("Model stream reported malformed usage.")
    prompt = _count(value.get("prompt_tokens"), "prompt token count")
    output = _count(value.get("completion_tokens"), "output token count")
    total = _count(value.get("total_tokens"), "total token count")
    if total is not None and prompt is not None and output is not None and total != prompt+output:
        raise ModelError("Model stream reported inconsistent token counts.")
    if prompt is None and output is None and total is None:
        return None
    return {"input_tokens": prompt, "output_tokens": output, "total_tokens": total,
            "input_source": "provider_reported" if prompt is not None else "unavailable",
            "output_source": "provider_reported" if output is not None else "unavailable",
            "total_source": "provider_reported" if total is not None else "unavailable"}


class _Durable:
    def __init__(self, path: Path, task_id: str):
        self.path, self.task_id = path, task_id
        self.last_write = 0.0
        self.content = ""
        with sqlite3.connect(path, timeout=2) as db:
            ensure_generation(db)
            db.execute("INSERT INTO tasks_generation(task_id,content,usage_json,updated_at,status) "
                       "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET content='',usage_json=NULL,"
                       "updated_at=excluded.updated_at,status='running',updates=tasks_generation.updates+1",
                       (task_id, "", None, _now(), "running"))
            db.commit()

    def write(self, content: str, usage: dict[str, Any] | None = None, status: str = "running", force: bool = False) -> None:
        self.content = content
        now = time.monotonic()
        if not force and now - self.last_write < WRITE_INTERVAL:
            return
        encoded = json.dumps(usage, ensure_ascii=False, allow_nan=False) if usage is not None else None
        with sqlite3.connect(self.path, timeout=2) as db:
            db.execute("UPDATE tasks_generation SET content=?,usage_json=COALESCE(?,usage_json),updated_at=?,"
                       "status=?,updates=updates+1 WHERE task_id=?",
                       (content, encoded, _now(), status, self.task_id))
            db.commit()
        self.last_write = now


class _AbortMonitor:
    def __init__(self, sock: socket.socket, deadline: float, stopping: Callable[[], bool],
                 db_path: Path, task_id: str):
        self.sock, self.deadline, self.stopping = sock, deadline, stopping
        self.db_path, self.task_id = db_path, task_id
        self.done = threading.Event(); self.reason: str | None = None
        self.thread = threading.Thread(target=self._run, name="chat-stream-abort", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.done.wait(.1):
            try:
                cancelled = cancellation_requested(self.db_path, self.task_id)
            except sqlite3.Error:
                self.reason = "Interactive chat cancellation state became unavailable."
                break
            if cancelled:
                self.reason = "Interactive chat cancellation requested."
                break
            try:
                stopped=self.stopping()
            except Exception:
                self.reason="Interactive chat stopping state became unavailable."
                break
            if stopped:
                self.reason = "Worker interrupted during interactive chat."
                break
            if time.monotonic() >= self.deadline:
                self.reason = "Interactive chat request timed out."
                break
        if self.reason:
            try: self.sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass

    def close(self) -> None:
        self.done.set(); self.thread.join(timeout=.3)


def _open(entry, body: bytes, path: str, db_path: Path, task_id: str,
          stopping: Callable[[], bool]):
    endpoint = safe_endpoint(entry.endpoint)
    parts = urlsplit(endpoint)
    deadline = time.monotonic() + float(entry.timeout_seconds)
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=float(entry.timeout_seconds))
    monitor = None
    try:
        connection.connect()
        if connection.sock is None:
            raise ModelError("Local model connection did not provide a socket.")
        monitor = _AbortMonitor(connection.sock, deadline, stopping, db_path, task_id)
        connection.sock.settimeout(max(.001, deadline - time.monotonic()))
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
        connection.sock.settimeout(max(.001, deadline - time.monotonic()))
        response = connection.getresponse()
        if response.status != 200:
            raw = response.read(min(MAX_LINE_BYTES, MAX_RESPONSE_BYTES))
            raise ModelError(f"Model stream returned HTTP {response.status}.", raw.decode("utf-8", errors="replace"))
        return connection, response, monitor, deadline
    except Exception as exc:
        reason=monitor.reason if monitor else None
        if monitor: monitor.close()
        connection.close()
        if reason:raise ModelError(reason) from exc
        if isinstance(exc,socket.timeout) or time.monotonic()>=deadline:
            raise ModelError("Interactive chat request timed out.") from exc
        if isinstance(exc,(ConnectionError,OSError,http.client.HTTPException)):
            raise ModelError("Could not reach the configured local chat service.") from exc
        raise


def _line(response, connection, deadline: float, monitor: _AbortMonitor, total: list[int]) -> bytes:
    if monitor.reason:
        raise ModelError(monitor.reason)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelError("Interactive chat request timed out.")
    if connection.sock is not None:
        connection.sock.settimeout(remaining)
    try:
        line = response.readline(MAX_LINE_BYTES + 1)
    except (OSError, socket.timeout, http.client.HTTPException) as exc:
        message = monitor.reason
        if message is None and (isinstance(exc, socket.timeout) or time.monotonic() >= deadline):
            message = "Interactive chat request timed out."
        raise ModelError(message or "Interactive chat stream connection failed.") from exc
    if monitor.reason:
        raise ModelError(monitor.reason)
    if len(line) > MAX_LINE_BYTES:
        raise ModelError("Model stream line exceeded the 1 MiB safety limit.")
    total[0] += len(line)
    if total[0] > MAX_RESPONSE_BYTES:
        raise ModelError("Model response exceeded the 8 MiB safety limit.")
    return line


class VisibleText:
    """Suppress provider inline thinking blocks, including split tag boundaries."""
    def __init__(self):
        self.pending=""; self.hidden=False

    def feed(self, delta: str, final=False) -> str:
        if not isinstance(delta,str): raise ModelError("Model stream returned non-text content.")
        self.pending += delta
        out=[]
        while self.pending:
            marker="</think>" if self.hidden else "<think>"
            index=self.pending.find(marker)
            if index>=0:
                if not self.hidden:out.append(self.pending[:index])
                self.pending=self.pending[index+len(marker):];self.hidden=not self.hidden
                continue
            if self.hidden:
                self.pending=self.pending[-(len(marker)-1):]
                break
            keep=0
            for length in range(1,min(len(marker),len(self.pending)+1)):
                if self.pending.endswith(marker[:length]):keep=length
            if final:keep=0
            out.append(self.pending[:-keep] if keep else self.pending)
            self.pending=self.pending[-keep:] if keep else ""
            break
        if final and self.hidden:raise ModelError("Model stream ended inside a hidden thinking block.")
        return "".join(out)


def visible_content(text: str) -> str:
    return VisibleText().feed(text,True).strip()


def _terminal_metadata(value):
    if isinstance(value,dict):return {k:_terminal_metadata(v) for k,v in value.items() if k not in _HIDDEN_KEYS and k!='content'}
    if isinstance(value,list):return [_terminal_metadata(v) for v in value]
    return value


def _append(content: str, delta: Any) -> str:
    if delta is None:
        return content
    if not isinstance(delta, str):
        raise ModelError("Model stream returned non-text content.")
    if len(content) + len(delta) > MAX_CONTENT_CHARS:
        raise ModelError("Interactive chat output exceeded the 4 MiB content limit.")
    return content + delta


def _ollama(entry, messages, max_tokens, durable, db_path, task_id, stopping):
    endpoint = safe_endpoint(entry.endpoint)
    base = urlsplit(endpoint).path.rstrip("/")
    if base == "/v1": base = ""
    body = json.dumps({"model": entry.identifier, "messages": messages, "think": False,
                       "stream": True, "keep_alive": 600,
                       "options": {"num_ctx": entry.context_window, "num_predict": max_tokens}},
                      ensure_ascii=False).encode()
    connection, response, monitor, deadline = _open(entry, body, base + "/api/chat", db_path, task_id, stopping)
    content = ""; terminal = None; usage = None; total = [0]; visible=VisibleText()
    try:
        while True:
            line = _line(response, connection, deadline, monitor, total)
            if not line: break
            if not line.strip(): continue
            try: frame = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ModelError("Ollama stream returned malformed NDJSON.") from exc
            if not isinstance(frame, dict) or frame.get("model") != entry.identifier:
                raise ModelError("Ollama stream returned a different or missing model identity.")
            message = frame.get("message")
            if message is not None:
                if not isinstance(message, dict): raise ModelError("Ollama stream message was malformed.")
                content = _append(content, visible.feed(message.get("content") if message.get("content") is not None else ""))
                durable.write(content)
            if frame.get("done") is True:
                terminal = frame; usage = _usage_ollama(frame); break
            if frame.get("done") not in (False, None):
                raise ModelError("Ollama stream returned an invalid completion marker.")
        if terminal is None: raise ModelError("Ollama stream ended without a completed terminal frame.")
        content=_append(content,visible.feed("",True)).strip()
        if not content.strip(): raise ModelError("Model returned reasoning-only or empty content.")
        return content, usage, {"transport": "ollama_ndjson", "terminal_frame": _terminal_metadata(terminal)}
    finally:
        monitor.close(); connection.close()


def _bonsai(entry, messages, max_tokens, durable, db_path, task_id, stopping):
    endpoint = safe_endpoint(entry.endpoint); base = urlsplit(endpoint).path.rstrip("/")
    body = json.dumps({"model": entry.identifier, "messages": messages, "stream": True,
                       "stream_options": {"include_usage": True}, "max_tokens": max_tokens,
                       "chat_template_kwargs": {"enable_thinking": False}}, ensure_ascii=False).encode()
    connection, response, monitor, deadline = _open(entry, body, (base + "/chat/completions") or "/chat/completions", db_path, task_id, stopping)
    content = ""; terminal = None; usage = None; saw_done = False; event: list[str] = []; total = [0]; visible=VisibleText()
    try:
        while True:
            line = _line(response, connection, deadline, monitor, total)
            if not line:
                if event: raise ModelError("Bonsai stream ended with an incomplete SSE event.")
                break
            text = line.decode("utf-8", errors="strict").rstrip("\r\n")
            if text == "":
                if not event: continue
                payload = "\n".join(event); event = []
                if payload == "[DONE]": saw_done = True; break
                try: frame = json.loads(payload)
                except json.JSONDecodeError as exc: raise ModelError("Bonsai stream returned malformed SSE JSON.") from exc
                if not isinstance(frame, dict) or frame.get("model") != entry.identifier:
                    raise ModelError("Bonsai stream returned a different or missing model identity.")
                if "usage" in frame: usage = _usage_openai(frame.get("usage"))
                choices = frame.get("choices")
                if not isinstance(choices, list): raise ModelError("Bonsai stream choices were malformed.")
                for choice in choices:
                    if not isinstance(choice, dict): raise ModelError("Bonsai stream choice was malformed.")
                    delta = choice.get("delta", {})
                    if not isinstance(delta, dict): raise ModelError("Bonsai stream delta was malformed.")
                    content = _append(content, visible.feed(delta.get("content") if delta.get("content") is not None else ""))
                    if choice.get("finish_reason") is not None:
                        if not isinstance(choice["finish_reason"], str) or not choice["finish_reason"]:
                            raise ModelError("Bonsai stream completion marker was malformed.")
                        terminal = frame
                durable.write(content, usage)
                continue
            if text.startswith(":"): continue
            if not text.startswith("data:"):
                raise ModelError("Bonsai stream contained a non-data SSE field.")
            event.append(text[5:].lstrip())
        if not saw_done or terminal is None:
            raise ModelError("Bonsai stream ended without finish_reason and [DONE].")
        content=_append(content,visible.feed("",True)).strip()
        if not content.strip(): raise ModelError("Model returned reasoning-only or empty content.")
        return content, usage, {"transport": "openai_sse", "terminal_frame": _terminal_metadata(terminal)}
    except UnicodeDecodeError as exc:
        raise ModelError("Bonsai stream returned invalid UTF-8.") from exc
    finally:
        monitor.close(); connection.close()


def run_chat(entry, task: dict[str, Any], db_path: str | Path,
             stopping: Callable[[], bool] = lambda: False) -> tuple[str, str]:
    """Run one validated chat stream and durably expose only visible content."""
    path = Path(db_path).expanduser().resolve()
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ModelError("Interactive chat task ID is missing.")
    durable = _Durable(path, task_id)
    content = ""
    try:
        contract = _contract(task)
        if cancellation_requested(path, task_id):
            raise ModelError("Interactive chat cancellation requested.")
        maximum = task.get("max_tokens", entry.max_tokens)
        if type(maximum) is not int or not 1 <= maximum <= entry.max_tokens:
            raise ModelError("Interactive chat max_tokens exceeds the model snapshot.")
        if entry.provider == "ollama":
            content, usage, raw = _ollama(entry, contract["messages"], maximum, durable, path, task_id, stopping)
        elif entry.provider == "bonsai":
            content, usage, raw = _bonsai(entry, contract["messages"], maximum, durable, path, task_id, stopping)
        else:
            raise ModelError("Interactive chat provider is unsupported.")
        raw.update(model=entry.identifier, content=content, stream_usage=usage)
        if entry.provider == 'ollama':
            raw.update(raw['terminal_frame'])
            raw['message'] = {'role':'assistant','content':content}
        else:
            raw['choices'] = [{'message':{'role':'assistant','content':content}}]
            raw['usage'] = {k:usage[v] for k,v in [('prompt_tokens','input_tokens'),('completion_tokens','output_tokens'),('total_tokens','total_tokens')] if usage and usage.get(v) is not None}
        durable.write(content, usage, "completed", True)
        return content, json.dumps(raw, ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        cancelled = False
        try: cancelled = cancellation_requested(path, task_id)
        except sqlite3.Error: pass
        durable.write(durable.content, None, "cancelled" if cancelled else "error", True)
        if isinstance(exc, ModelError): raise
        raise ModelError("Interactive chat stream failed.") from exc
