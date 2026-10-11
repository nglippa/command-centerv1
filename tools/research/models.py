"""Safe local OpenAI-compatible model discovery and completion calls."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ModelError(Exception):
    def __init__(self, message: str, raw_json: str | None = None):
        super().__init__(message)
        self.raw_json = raw_json


@dataclass(frozen=True)
class ModelConfig:
    family: str
    identifier: str
    endpoint: str
    max_tokens: int = 1024
    timeout_seconds: float = 600.0
    source: str = ""
    discovered: bool = False


def safe_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        raise ValueError("Endpoint must be credential-free local HTTP.")
    if parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("Endpoint may not contain a query or fragment.")
    host = parsed.hostname.lower()
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("Endpoint host must be 127.0.0.1, localhost, or ::1.") from exc
    if not address.is_loopback:
        raise ValueError("Remote model endpoints are not allowed.")
    if parsed.path not in ("", "/") and not parsed.path.rstrip("/").endswith("/v1"):
        raise ValueError("Endpoint path must be empty, /, or end in /v1.")
    port = parsed.port
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    path = parsed.path.rstrip("/")
    return f"http://{authority}{path}"


def load_config(path: str | Path) -> list[ModelConfig]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read model configuration: {exc}") from exc
    items = data.get("models") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("Model configuration must contain a non-empty models list.")
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid model configuration entry.")
        try:
            max_tokens = int(item.get("max_tokens", 1024))
            timeout = float(item.get("timeout_seconds", 600))
            if not 1 <= max_tokens <= 8192 or not 0 < timeout <= 3600:
                raise ValueError
            out.append(
                ModelConfig(
                    family=str(item["family"]),
                    identifier=str(item["identifier"]),
                    endpoint=safe_endpoint(str(item["endpoint"])),
                    max_tokens=max_tokens,
                    timeout_seconds=timeout,
                    source=str(item.get("source", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid model configuration entry.") from exc
    return out


class LocalHTTP:
    """HTTP client limited to loopback, with no proxy or redirect support."""

    def __init__(self, endpoint: str, timeout_seconds: float):
        self.endpoint = safe_endpoint(endpoint)
        self.parts = urlsplit(self.endpoint)
        self.timeout = timeout_seconds

    def _request(self, method: str, suffix: str, body: bytes | None = None) -> tuple[int, bytes]:
        started = time.monotonic()
        deadline = started + self.timeout
        connection = http.client.HTTPConnection(
            self.parts.hostname,
            self.parts.port or 80,
            timeout=self.timeout,
        )
        path = (self.parts.path.rstrip("/") + suffix) or suffix
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        expired = threading.Event()
        socket_ref: list[socket.socket | None] = [None]

        def expire() -> None:
            expired.set()
            sock = socket_ref[0]
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(self.timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            socket_ref[0] = connection.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            connection.request(method, path, body=body, headers=headers)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            # Redirects are deliberately returned as errors, never followed.
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read(min(65_536, MAX_RESPONSE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ModelError("Model response exceeded the 8 MiB safety limit.")
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError
            return response.status, b"".join(chunks)
        except (TimeoutError, socket.timeout) as exc:
            raise ModelError(f"Model request timed out after {self.timeout:g} seconds.") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            if expired.is_set() or time.monotonic() >= deadline:
                raise ModelError(f"Model request timed out after {self.timeout:g} seconds.") from exc
            raise ModelError(
                f"Could not reach local model server at {self.endpoint}: {type(exc).__name__}."
            ) from exc
        finally:
            timer.cancel()
            connection.close()

    def models(self) -> list[str]:
        status, raw = self._request("GET", "/models")
        if status != 200:
            raise ModelError(f"Model discovery returned HTTP {status}.")
        try:
            payload = json.loads(raw)
            return [str(item["id"]) for item in payload["data"] if item.get("id")]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ModelError("Model discovery returned malformed JSON.") from exc

    def complete(self, model: str, prompt: str, max_tokens: int, *, request_options: dict | None = None) -> tuple[str, str]:
        request_options = request_options or {}
        if set(request_options)-{"chat_template_kwargs","temperature"}:
            raise ModelError("Unsupported local request option.")
        if "chat_template_kwargs" in request_options:
            kwargs=request_options["chat_template_kwargs"]
            if not isinstance(kwargs,dict) or set(kwargs)!={"enable_thinking"} or type(kwargs["enable_thinking"]) is not bool:
                raise ModelError("Invalid local chat template options.")
        if "temperature" in request_options and (type(request_options["temperature"]) not in (int,float) or not 0<=request_options["temperature"]<=2):
            raise ModelError("Invalid local temperature.")
        request = json.dumps(
            {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                **(request_options or {}),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        status, raw = self._request("POST", "/chat/completions", request)
        raw_text = raw.decode("utf-8", errors="replace")
        if 300 <= status < 400:
            raise ModelError(f"Model server redirect (HTTP {status}) was refused.", raw_text)
        if status != 200:
            raise ModelError(f"Model server returned HTTP {status}.", raw_text)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelError("Model server returned malformed JSON.", raw_text) from exc
        try:
            content = payload["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ModelError("Model response did not contain an answer.", raw_text) from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelError("Model returned reasoning-only or empty content.", raw_text)
        return content, raw_text


def discover(configured: list[ModelConfig]) -> tuple[list[ModelConfig], dict[str, str]]:
    """Return configured plus family-matching discovered IDs and endpoint errors."""
    found: list[ModelConfig] = []
    errors: dict[str, str] = {}
    by_endpoint: dict[str, list[ModelConfig]] = {}
    for item in configured:
        by_endpoint.setdefault(item.endpoint, []).append(item)
    seen: set[tuple[str, str]] = set()
    for endpoint, seeds in by_endpoint.items():
        try:
            ids = LocalHTTP(endpoint, min(3.0, *(seed.timeout_seconds for seed in seeds))).models()
        except ModelError as exc:
            errors[endpoint] = str(exc)
            found.extend(seeds)
            seen.update((seed.endpoint, seed.identifier) for seed in seeds)
            continue
        id_set = set(ids)
        for seed in seeds:
            found.append(ModelConfig(
                seed.family, seed.identifier, seed.endpoint, seed.max_tokens,
                seed.timeout_seconds, seed.source, seed.identifier in id_set,
            ))
            seen.add((seed.endpoint, seed.identifier))
        for identifier in ids:
            lower = identifier.lower()
            for seed in seeds:
                if seed.family.lower() in lower and (endpoint, identifier) not in seen:
                    found.append(
                        ModelConfig(
                            seed.family,
                            identifier,
                            endpoint,
                            seed.max_tokens,
                            seed.timeout_seconds,
                            seed.source,
                            True,
                        )
                    )
                    seen.add((endpoint, identifier))
                    break
    return found, errors
