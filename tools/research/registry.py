"""Strict model registry and immutable task snapshots."""
from __future__ import annotations

import json
import ipaddress
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ModelEntry:
    family: str
    identifier: str
    endpoint: str
    provider: str
    max_tokens: int = 1024
    timeout_seconds: float = 600
    context_window: int = 4096
    load_timeout: float = 180
    unload_timeout: float = 30
    health_timeout: float = 3
    owner_mode: str = "owned"
    reuse_external: bool = False
    expected_resources: dict[str, Any] = field(default_factory=dict)
    startup_command: tuple[str, ...] = ()
    model_path: str | None = None
    runtime_path: str | None = None
    runtime_version_contains: str | None = None
    startup_mechanism: str = ""
    shutdown_mechanism: str = ""
    health_check: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelEntry":
        if not isinstance(value, dict):
            raise RegistryError("Model entry must be an object.")
        required = ("family", "identifier", "endpoint", "provider")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise RegistryError("Model entry is missing required string fields.")
        provider = value["provider"]
        if provider not in ("ollama", "bonsai"):
            raise RegistryError(f"Unsupported provider {provider!r}.")
        owner_mode = str(value.get("owner_mode", "shared" if provider=="ollama" else "owned"))
        if owner_mode not in ("owned", "external", "shared"):
            raise RegistryError("owner_mode must be owned, external, or shared.")
        command = value.get("startup_command", [])
        if not isinstance(command, list) or any(not isinstance(x, str) or not x for x in command):
            raise RegistryError("startup_command must be an argument array.")
        if not isinstance(value.get("reuse_external", False), bool):
            raise RegistryError("reuse_external must be boolean.")
        entry = cls(
            family=value["family"], identifier=value["identifier"], endpoint=value["endpoint"],
            provider=provider, max_tokens=int(value.get("max_tokens", 1024)),
            timeout_seconds=float(value.get("timeout_seconds", 600)),
            context_window=int(value.get("context_window", 4096)),
            load_timeout=float(value.get("load_timeout", 180)),
            unload_timeout=float(value.get("unload_timeout", 30)),
            health_timeout=float(value.get("health_timeout", 3)), owner_mode=owner_mode,
            reuse_external=bool(value.get("reuse_external", False)),
            expected_resources=dict(value.get("expected_resources", {})),
            startup_command=tuple(command), model_path=value.get("model_path"),
            runtime_path=value.get("runtime_path"),
            runtime_version_contains=value.get("runtime_version_contains"),
            startup_mechanism=str(value.get("startup_mechanism","native preload" if provider=="ollama" else "exact installed runtime argv")),
            shutdown_mechanism=str(value.get("shutdown_mechanism","keep_alive=0 then verify /api/ps" if provider=="ollama" else "wait for current owned child exit and verify listener")),
            health_check=str(value.get("health_check","native loaded listing and tiny inference" if provider=="ollama" else "health, exact model alias/path, listener identity and tiny inference")),
        )
        if not 1 <= entry.max_tokens <= 8192 or not 256 <= entry.context_window <= 1_048_576:
            raise RegistryError("Invalid token/context bounds.")
        if not all(math.isfinite(x) for x in (entry.timeout_seconds, entry.load_timeout, entry.unload_timeout, entry.health_timeout)):
            raise RegistryError("Lifecycle timeouts must be finite.")
        if min(entry.timeout_seconds, entry.load_timeout, entry.unload_timeout, entry.health_timeout) <= 0 \
                or max(entry.timeout_seconds,entry.load_timeout,entry.unload_timeout)>3600 \
                or entry.health_timeout>30:
            raise RegistryError("Lifecycle timeouts must be positive.")
        parsed=urlsplit(entry.endpoint)
        if parsed.path not in ("","/v1"):
            raise RegistryError("Registry endpoints must use the origin or /v1, without a trailing slash.")
        try: loopback=ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError: loopback=False
        if parsed.scheme!="http" or not loopback or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RegistryError("Endpoint must be credential-free literal loopback HTTP.")
        if provider == "bonsai" and not all((entry.startup_command, entry.model_path,
                                               entry.runtime_path, entry.runtime_version_contains)):
            raise RegistryError("Bonsai requires explicit command, model/runtime paths, and version pin.")
        if provider=="ollama" and owner_mode!="shared":
            raise RegistryError("Ollama must use shared service ownership.")
        if provider=="bonsai":
            if owner_mode=="shared" or (owner_mode=="external" and not entry.reuse_external):
                raise RegistryError("Bonsai external ownership requires explicit reuse_external; shared is unsupported.")
            command=list(entry.startup_command); port=str(parsed.port or 80)
            if any(x.startswith(("--host=","--port=","--model=","--alias=")) for x in command):
                raise RegistryError("Model/host/port/alias flags must use separate arguments.")
            def after(*flags: str) -> str | None:
                for flag in flags:
                    if flag in command and command.index(flag)+1<len(command): return command[command.index(flag)+1]
                return None
            for flags in (("-m","--model"),("--alias",),("--host",),("--port",)):
                if sum(command.count(flag) for flag in flags) != 1:
                    raise RegistryError("Model, alias, host and port flags must occur exactly once.")
            pairs={"-m","--model","--alias","--host","--port","-c","--ctx-size","-ngl","--gpu-layers",
                   "-np","--parallel","-b","--batch-size","-ub","--ubatch-size","--reasoning-budget","--reasoning-format"}
            switches={"--jinja","--no-webui","--log-disable"}
            index=1
            while index<len(command):
                flag=command[index]
                if flag in pairs and index+1<len(command): index+=2
                elif flag in switches: index+=1
                else: raise RegistryError(f"Unsupported server argument {flag!r}; downloads, plugins and remote backends are prohibited.")
            context=after("-c","--ctx-size")
            if context is not None and context!=str(entry.context_window):
                raise RegistryError("Bonsai context flag must match context_window.")
            forbidden=("--hf-repo","--model-url","--url","--download","-hf","--hf-file","-hff","--rpc")
            if command[0]!=entry.runtime_path or after("-m","--model")!=entry.model_path \
                    or after("--alias")!=entry.identifier or after("--host") not in ("127.0.0.1","::1") \
                    or after("--port")!=port or any(x.split("=",1)[0] in forbidden for x in command) \
                    or any("http://" in x or "https://" in x for x in command):
                raise RegistryError("Bonsai startup command is not the exact configured local invocation.")
        return entry

    def export(self) -> dict[str, Any]:
        value = asdict(self)
        value["startup_command"] = list(self.startup_command)
        return value

    def snapshot_json(self) -> str:
        return json.dumps(self.export(), sort_keys=True, separators=(",", ":"))


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            values = raw["models"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RegistryError(f"Invalid registry: {exc}") from exc
        if not isinstance(values, list):
            raise RegistryError("Registry models must be a list.")
        self.entries = [ModelEntry.from_dict(item) for item in values]

    def resolve_task(self, task: dict[str, Any]) -> ModelEntry:
        snapshot = task.get("registry_json")
        if snapshot:
            try:
                entry=ModelEntry.from_dict(json.loads(snapshot))
                for field, attr in (("model_identifier","identifier"),("endpoint","endpoint")):
                    if field in task and task[field] != getattr(entry,attr):
                        raise RegistryError("Task identity differs from its immutable model snapshot.")
                return entry
            except (json.JSONDecodeError, TypeError) as exc:
                raise RegistryError("Task registry snapshot is invalid.") from exc
        matches = [e for e in self.entries if e.identifier == task.get("model_identifier")
                   and e.endpoint == task.get("endpoint")]
        if len(matches) != 1:
            raise RegistryError("Historical task model/endpoint is unavailable; no substitution was made.")
        return matches[0]

    def clone_discovered(self, configured: ModelEntry, identifier: str, endpoint: str) -> ModelEntry:
        if endpoint != configured.endpoint or not identifier:
            raise RegistryError("Discovered models may only clone at the configured endpoint.")
        value = configured.export()
        value["identifier"] = identifier
        return ModelEntry.from_dict(value)
