"""Bounded, read-only inventory of locally documented capabilities.

The catalog describes what is installed or configured.  It does not execute
skills, MCP servers, media tools, or model services.  Only ``ffprobe`` is
identified as a candidate for a future fixed, read-only executor.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ast
from urllib.parse import urlsplit
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


MAX_SKILL_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 256 * 1024
MAX_ROUTE_RESULTS = 8

FIELDS = (
    "id", "name", "kind", "description", "primary_use_cases",
    "secondary_use_cases", "when_to_use", "when_not_to_use",
    "supported_operations", "execution_transport", "availability",
    "configuration_status", "required_dependencies", "resource_requirements",
    "side_effects", "security_constraints", "provenance_requirements",
    "relevant_projects", "relevant_workflow_phases", "documentation_path",
    "version_or_source", "last_verified",
)

VIDEO_METADATA = {
    "video-editing": {
        "primary": ["Edit supplied video and audio into a validated deliverable", "Design and render explicit editorial timelines"],
        "secondary": ["Create review contact sheets and transition strips", "Build FFmpeg, Remotion, or hybrid compositions"],
        "operations": ["inspect media", "plan timeline", "edit and composite", "render review artifacts", "validate deliverable"],
        "not": ["Do not infer media properties from filenames", "Do not publish or acquire paid media without authorization"],
        "dependencies": ["ffprobe or equivalent inspection tooling", "ffmpeg for FFmpeg rendering", "Node.js for Remotion workflows"],
        "resources": ["local source media", "render storage", "CPU/GPU capacity appropriate to the requested export"],
        "side": ["May create workspaces, intermediates, and rendered media when an agent follows the skill"],
        "provenance": ["Preserve source observations, timing decisions, review artifacts, validation report, and checksum"],
        "phases": ["source inspection", "editorial planning", "assembly", "review", "master validation"],
    },
    "analyze-video-editing": {
        "primary": ["Reverse-engineer editing, pacing, sound, titles, and shot grammar from supplied video"],
        "secondary": ["Produce an evidence-backed reconstruction plan"],
        "operations": ["inspect source", "analyze timeline", "analyze sound and graphics", "document editing grammar"],
        "not": ["Do not treat inferred intent as observed fact", "Do not render or publish without a separate editing workflow"],
        "dependencies": ["local media inspection tools"], "resources": ["local reference media"],
        "side": ["May create analysis and review artifacts when an agent follows the skill"],
        "provenance": ["Tie findings to observed frames, timestamps, audio, or metadata"],
        "phases": ["reference inspection", "forensic analysis", "reconstruction planning"],
    },
    "find-broll": {
        "primary": ["Plan, source, vet, and place B-roll against exact spoken meaning"],
        "secondary": ["Build provenance and approval manifests", "Create restrained concept graphics when appropriate"],
        "operations": ["analyze transcript", "plan beats", "research candidates", "record provenance", "place approved cutaways"],
        "not": ["Do not infer rights from availability", "Do not acquire paid media, publish, or sign in without authorization"],
        "dependencies": ["transcript or source media", "authorized research/download tooling when sourcing is requested"],
        "resources": ["source media", "candidate storage", "review time"],
        "side": ["May download public candidates only within user authorization when an agent follows the skill"],
        "provenance": ["Record canonical URL, creator, title, date, capture time, rights note, and source interval"],
        "phases": ["transcript analysis", "beat planning", "sourcing", "approval", "placement", "render verification"],
    },
}

REMOTION_METADATA = {
    "remotion-best-practices": ("Route a Remotion task to the narrow installed guidance it needs", ["select Remotion workflow", "preserve user edits", "route to composition, media, rendering, or Studio guidance"], ["task routing", "implementation planning"]),
    "remotion-captions": ("Transcribe, import, structure, display, and animate timed captions", ["create Caption JSON", "transcribe captions", "display captions", "animate caption pages"], ["transcription", "caption authoring", "caption animation"]),
    "remotion-create": ("Create a Remotion project or composition with an early Studio preview", ["inspect project", "scaffold project", "author composition", "open preview"], ["project setup", "composition", "preview"]),
    "remotion-docs": ("Find and read current Remotion documentation for a concrete implementation question", ["search documentation index", "fetch documentation markdown", "route to authoritative reference"], ["documentation research", "implementation planning"]),
    "remotion-interactivity": ("Structure Remotion components so Studio can select, move, resize, rotate, and edit them", ["define interactive schema", "expose editable props", "structure component timelines"], ["component architecture", "Studio interaction", "authoring"]),
    "remotion-maps": ("Choose and implement a documented static or animated geographic technique", ["render static map", "animate routes and markers", "build Mapbox, MapLibre, MapTiler, GeoJSON, or Cesium scenes"], ["map technique selection", "map composition", "map animation"]),
    "remotion-markup": ("Author deterministic frame-driven React markup for Remotion scenes and effects", ["author scene markup", "drive frame animation", "compose sequences", "preserve editable structure"], ["visual implementation", "animation", "composition"]),
    "remotion-multimedia": ("Inspect and transform browser media with the documented Mediabunny APIs", ["read duration", "read dimensions", "trim or transform browser media", "extract media metadata"], ["media inspection", "browser preprocessing", "composition inputs"]),
    "remotion-render": ("Export Remotion video, stills, frame sets, or transparent media", ["render video", "render still", "render selected frames", "render transparent output"], ["render planning", "export", "output validation"]),
    "remotion-saas": ("Design an application that embeds Remotion preview and rendering workflows", ["embed Player", "choose application framework", "choose client or server rendering", "design editor workflow"], ["application architecture", "preview integration", "render integration"]),
    "remotion-studio": ("Start and verify Remotion Studio for interactive preview and user steering", ["start Studio", "open exact preview URL", "verify composition preview", "manage Studio port"], ["preview startup", "interactive review", "iteration"]),
    "remotion-upgrade": ("Upgrade all Remotion packages and skills coherently while preserving project changes", ["inspect manifests", "upgrade aligned packages", "update lockfile", "verify installed versions"], ["dependency audit", "upgrade", "post-upgrade verification"]),
}


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return value[:100] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _read_bounded(path: Path, limit: int, trusted_root: Path) -> Optional[str]:
    try:
        resolved = path.resolve(strict=True)
        root = trusted_root.resolve(strict=True)
        if not _inside(resolved, root) or not resolved.is_file() or resolved.stat().st_size > limit:
            return None
        data = resolved.read_bytes()
        if len(data) > limit:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        return None


def _read_preview(path: Path, limit: int, trusted_root: Path) -> Tuple[Optional[str], Optional[str], Optional[Path]]:
    """Read at most limit bytes and report why complete instruction access is unavailable."""
    try:
        resolved = path.resolve(strict=True)
        root = trusted_root.resolve(strict=True)
        if not _inside(resolved, root):
            return None, "symlink_escape", None
        if not resolved.is_file():
            return None, "not_regular_file", resolved
        size = resolved.stat().st_size
        with resolved.open("rb") as handle:
            data = handle.read(limit + 1)
        oversized = size > limit or len(data) > limit
        return data[:limit].decode("utf-8", errors="replace"), ("oversized" if oversized else None), resolved
    except OSError:
        return None, "unreadable", None


def _frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    result: Dict[str, Any] = {}
    for line in text[4:end].splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        if (len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"):
            value = value[1:-1]
        if value.startswith("[") and value.endswith("]"):
            result[key] = [part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip()]
        elif value not in ("|", ">"):
            result[key] = value
    return result, text[end + 4:].lstrip("\r\n")


def _paragraph(body: str) -> str:
    lines: List[str] = []
    in_fence = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith("#"):
            if lines:
                break
            continue
        if line.startswith(("- ", "* ", "> ")):
            if lines:
                break
            continue
        lines.append(line)
        if sum(len(item) for item in lines) >= 600:
            break
    return " ".join(lines)[:800]


def _section_lines(body: str, headings: Sequence[str], limit: int = 6) -> List[str]:
    wanted = tuple(value.lower() for value in headings)
    collecting = False
    values: List[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            collecting = any(value in title for value in wanted)
            continue
        if collecting and line.startswith(("- ", "* ")):
            value = re.sub(r"[`*_]", "", line[2:].strip())
            if value:
                values.append(value[:300])
                if len(values) >= limit:
                    break
    return values


def _skill_roots(repo_root: Path, extra_roots: Sequence[Path], include_defaults: bool) -> List[Tuple[Path, str, bool]]:
    roots: List[Tuple[Path, str, bool]] = []
    if include_defaults:
        home = Path.home()
        active = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
        candidates = [
            (home / ".agents/skills", "shared-agent-skills", True),
            (home / ".codex/skills", "codex-skills", active == home / ".codex"),
            (home / ".claude/skills", "claude-skills", False),
            (active / "skills", "active-profile-skills", True),
            (repo_root / ".agents/skills", "repository-agent-skills", True),
            (repo_root / ".codex/skills", "repository-codex-skills", True),
            (repo_root / ".claude/skills", "repository-claude-skills", True),
            (repo_root / "skills", "repository-skills", True),
        ]
        profiles = sorted([home / ".codex", home / ".claude"] +
                          list(home.glob(".codex-account-*")) + list(home.glob(".claude-account-*")))
        for profile in profiles:
            # Top-level skill roots may be active; cached plugin copies remain
            # discovery evidence only because cache presence does not prove enablement.
            candidates.append((profile / "skills", profile.name + "-skills", profile == active))
            for plugin_skills in sorted((profile / "plugins/cache").glob("**/skills")):
                # A cache directory proves discovery, not that the plugin is enabled.
                candidates.append((plugin_skills, "plugin-cache-skills", False))
        roots.extend(candidates)
    roots.extend((Path(root), "test-or-explicit-skills", True) for root in extra_roots)
    seen = set()
    output = []
    for path, source, configured in roots:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir() or str(resolved) in seen:
            continue
        seen.add(str(resolved)); output.append((resolved, source, configured))
    return output


def _skill_record(path: Path, root: Path, source: str, configured: bool,
                  diagnostics: Dict[str, int]) -> Optional[Dict[str, Any]]:
    text, limitation, resolved = _read_preview(path, MAX_SKILL_BYTES, root)
    if text is None:
        diagnostics[limitation or "unreadable"] = diagnostics.get(limitation or "unreadable", 0) + 1
        return None
    meta, body = _frontmatter(text)
    name = str(meta.get("name") or path.parent.name).strip()
    description = str(meta.get("description") or "").strip() or _paragraph(body)
    if not description or description.lower() == name.lower():
        description = "Skill documentation is present but contains no substantive description in its bounded readable prefix."
    slug = _slug(name)
    key = slug if slug in VIDEO_METADATA else ("openclip" if slug.startswith("openclip-") else None)
    explicit = VIDEO_METADATA.get(key or "")
    if slug in REMOTION_METADATA:
        primary, operations, phases = REMOTION_METADATA[slug]
        explicit = {
            "primary": [primary], "secondary": [], "operations": operations,
            "not": ["Do not treat instruction discovery as proof that project dependencies, credentials, services, or render resources are ready"],
            "dependencies": ["Node.js", "a project-specific Remotion installation when executing the workflow"],
            "resources": ["project workspace", "browser/render resources appropriate to the composition"],
            "side": ["May create projects, install project packages, open previews, and render media when an agent follows the skill"],
            "provenance": ["Preserve source assets, project files, render settings, and validation artifacts"],
            "phases": phases,
        }
    elif key == "openclip":
        explicit = {
            "primary": ["Apply the documented OpenClip workflow named by this skill"],
            "secondary": ["Prepare, transform, or validate media within an agent-operated workflow"],
            "operations": [slug.replace("openclip-", "").replace("-", " "), "inspect and validate media artifacts"],
            "not": ["Do not infer that external services, credentials, models, or project dependencies are ready"],
            "dependencies": ["Dependencies documented by the selected OpenClip workflow"],
            "resources": ["local media and workspace appropriate to the workflow"],
            "side": ["May create or modify media artifacts when an agent follows the skill"],
            "provenance": ["Retain source identity, transformation settings, and output validation"],
            "phases": ["inspection", "transformation", "review", "delivery"],
        }
    elif slug == "cinematic-video":
        explicit = {
            "primary": ["Develop a constrained prompt and review plan for one continuous live-action cinematic shot"],
            "secondary": ["Choose still-first or direct-video planning based on whether framing or motion defines the shot"],
            "operations": ["design shot beat", "specify camera and light", "constrain motion", "plan visual review"],
            "not": ["Do not claim a generation backend is available", "Do not generate, purchase, or publish media without a separately configured transport and authorization"],
            "dependencies": ["An independently configured video-generation transport is required for generation"],
            "resources": ["Generation cost and compute are backend-specific and unverified"],
            "side": ["Instruction retrieval is read-only; generation is unavailable through this catalog"],
            "provenance": ["Record prompt, backend/model identity, source image if any, parameters, and generated artifact provenance"],
            "phases": ["concept", "shot design", "prompt construction", "review planning"],
        }
    use = _section_lines(body, ("when to use", "activates", "use this skill"))
    avoid = _section_lines(body, ("when not", "do not use", "never"))
    status = "CONFIGURED" if configured else "DISCOVERED"
    record = {
        "id": "skill:" + slug, "name": name, "kind": "SKILL", "description": description[:1200],
        "primary_use_cases": explicit["primary"] if explicit else (use[:3] or [description[:300]]),
        "secondary_use_cases": explicit["secondary"] if explicit else use[3:6],
        "when_to_use": use or ([description[:300]] if not explicit else explicit["primary"]),
        "when_not_to_use": (explicit["not"] if explicit else avoid),
        "supported_operations": explicit["operations"] if explicit else _section_lines(body, ("available", "workflow", "capabilities", "operations")),
        "execution_transport": "SKILL_INSTRUCTIONS", "availability": status,
        "configuration_status": status,
        "required_dependencies": explicit["dependencies"] if explicit else [],
        "resource_requirements": explicit["resources"] if explicit else [],
        "side_effects": explicit["side"] if explicit else ["None from catalog discovery; an agent following the documentation may perform the documented actions"],
        "security_constraints": ["Instruction text may be retrieved through read_skill", "Instructions are guidance rather than permission to perform side effects", "User authorization and executor validation still govern actions"],
        "provenance_requirements": explicit["provenance"] if explicit else ["Preserve source and output provenance required by the skill documentation"],
        "relevant_projects": [], "relevant_workflow_phases": explicit["phases"] if explicit else [],
        "documentation_path": str(resolved), "source_paths": [str(resolved)], "trusted_roots": [str(root.resolve())],
        "version_or_source": str(meta.get("version") or source),
        "last_verified": datetime.now(timezone.utc).isoformat(),
        "read_availability": "BOUNDED_PREVIEW_ONLY" if limitation == "oversized" else "FULL",
        "availability_reason": ("Documentation exceeds the 64 KiB full-read limit; metadata comes from its bounded prefix."
                                if limitation == "oversized" else "Documentation was read within the configured bound."),
        "_content_digest": hashlib.sha256(text.encode("utf-8") + str(resolved.stat().st_size).encode("ascii")).hexdigest(),
    }
    return record


CLI_SPECS = {
    "ffprobe": (["-version"], ["inspect local media metadata and stream structure"], "NATIVE_READ_ONLY_CANDIDATE"),
    "ffmpeg": (["-version"], ["transcode, filter, composite, and render media"], "AGENT_ONLY_CLI"),
    "yt-dlp": (["--version"], ["inspect or download supported public media sources under authorization"], "AGENT_ONLY_CLI"),
    "whisper": (["--help"], ["transcribe local speech media"], "AGENT_ONLY_CLI"),
    "whisper-cli": (["--help"], ["transcribe local speech media with a configured local Whisper runtime"], "AGENT_ONLY_CLI"),
    "scenedetect": (["version"], ["detect scene boundaries in local video"], "AGENT_ONLY_CLI"),
    "auto-editor": (["--version"], ["apply automated media editing under explicit operator control"], "AGENT_ONLY_CLI"),
    "python3": (["--version"], ["run project-scoped Python tooling"], "AGENT_ONLY_CLI"),
    "node": (["--version"], ["run JavaScript project tooling"], "AGENT_ONLY_CLI"),
    "npm": (["--version"], ["manage Node.js project dependencies and scripts"], "AGENT_ONLY_CLI"),
    "npx": (["--version"], ["invoke project-scoped Node.js commands"], "AGENT_ONLY_CLI"),
    "git": (["--version"], ["inspect and manage version-controlled project changes"], "AGENT_ONLY_CLI"),
    "sips": (["--version"], ["inspect or transform local image files with the macOS image utility"], "AGENT_ONLY_CLI"),
    "afconvert": (None, ["convert local audio files with the macOS audio utility"], "AGENT_ONLY_CLI"),
    "screencapture": (None, ["capture a screen image only with explicit operator authorization"], "AGENT_ONLY_CLI"),
    "osascript": (None, ["run explicitly authorized AppleScript automation"], "AGENT_ONLY_CLI"),
    "rg": (["--version"], ["search bounded local text and file paths"], "AGENT_ONLY_CLI"),
    "ollama": (["--version"], ["inspect or operate the local Ollama installation under lifecycle controls"], "AGENT_ONLY_CLI"),
    "blender": (None, ["author or render Blender scenes through an explicitly bounded workflow"], "AGENT_ONLY_CLI"),
    "uv": (["--version"], ["run isolated Python project and tool environments"], "AGENT_ONLY_CLI"),
    "uvx": (["--version"], ["run an explicitly selected Python tool in an isolated environment"], "AGENT_ONLY_CLI"),
    "graft": (["--version"], ["inspect supported local source context through the installed Graft CLI"], "AGENT_ONLY_CLI"),
    "magick": (["--version"], ["inspect or transform local images with ImageMagick"], "AGENT_ONLY_CLI"),
    "sox": (["--version"], ["inspect or transform local audio with SoX"], "AGENT_ONLY_CLI"),
}


def _version(argv: Sequence[str]) -> Optional[str]:
    process = None
    selector = None
    try:
        process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   env={"PATH": os.environ.get("PATH", ""), "LANG": "C"})
        if process.stdout is None:
            return None
        selector = selectors.DefaultSelector(); selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 2
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill(); process.wait(); return None
            ready = selector.select(remaining)
            if ready:
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 16 * 1024:
                    process.kill(); process.wait(); return None
            elif process.poll() is not None:
                break
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode != 0:
            return None
        decoded = output.decode("utf-8", errors="replace")
        line = next((line.strip() for line in decoded.splitlines() if line.strip()), "")
        return re.sub(r"[\x00-\x1f\x7f]", "", line)[:300] or None
    except (OSError, subprocess.SubprocessError):
        if process is not None and process.poll() is None:
            process.kill(); process.wait()
        return None
    finally:
        if selector is not None: selector.close()
        if process is not None and process.stdout is not None: process.stdout.close()


def _cli_records() -> List[Dict[str, Any]]:
    records = []
    for name, (args, operations, transport) in CLI_SPECS.items():
        path = shutil.which(name)
        verified = _version([path] + args) if path and args is not None else None
        configured = name == "ffprobe" and verified is not None
        available = path is not None and (verified is not None or args is None)
        health = "verified" if verified else "not_probed" if path and args is None else "probe_failed" if path else "missing"
        records.append({
            "id": "cli:" + name, "name": name, "kind": "CLI_TOOL",
            "description": (("Installed local command-line tool. " if available else "Required command-line tool was not verified on PATH. ") + operations[0] + "."),
            "primary_use_cases": operations, "secondary_use_cases": [],
            "when_to_use": operations, "when_not_to_use": ["Do not invoke directly from model output", "Do not assume project inputs or authorization"],
            "supported_operations": operations,
            "execution_transport": ("AGENT_ONLY" if available else "UNAVAILABLE"),
            "availability": "CONFIGURED" if configured else ("DISCOVERED" if available else "UNVERIFIED" if path else "UNAVAILABLE"),
            "configuration_status": "CONFIGURED" if configured else ("DISCOVERED" if available else "UNVERIFIED" if path else "UNAVAILABLE"),
            "required_dependencies": [], "resource_requirements": ["local files and bounded process resources"],
            "executable_present": bool(path), "probe_status": health,
            "side_effects": (["Fixed ffprobe inspection is read-only"] if name == "ffprobe" else ["May read, download, install, modify, or create files depending on arguments; not callable by Command Center"]),
            "security_constraints": (["Future executor must use a fixed argv shape, local path validation, timeout, and output bound"] if name == "ffprobe" else ["Agent-only discovery; no model-callable executor", "Arguments require independent validation and authorization"]),
            "provenance_requirements": ["Record executable identity, version, arguments, and source/output paths if later executed"],
            "relevant_projects": [], "relevant_workflow_phases": ["inspection" if name == "ffprobe" else "agent workflow"],
            "documentation_path": path or "", "source_paths": [path] if path else [], "trusted_roots": [],
            "version_or_source": verified or ("executable present; safe version probe unavailable" if path else "executable not found on PATH"),
            "last_verified": datetime.now(timezone.utc).isoformat(),
        })
    return records


def _config_paths(repo_root: Path, extra: Sequence[Path], include_defaults: bool) -> List[Tuple[Path, str]]:
    home = Path.home()
    active = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    values = []
    if include_defaults:
        values = [(active / "config.toml", "active codex profile"), (home / ".claude.json", "claude"),
                  (repo_root / ".mcp.json", "repository")]
        for profile in [home / ".codex", home / ".claude"] + sorted(home.glob(".codex-account-*")) + sorted(home.glob(".claude-account-*")):
            values.extend([(profile / "config.toml", "configured profile"), (profile / ".claude.json", "configured profile")])
            # Installed plugin configurations are discovered, never assumed enabled.
            values.extend((p, "installed plugin configuration") for p in (profile / "plugins/cache").glob("**/.mcp.json"))
    values.extend((Path(path), "explicit") for path in extra)
    output, seen = [], set()
    for path, source in values:
        try:
            path = path.resolve(strict=True)
            if path not in seen and path.is_file():
                seen.add(path); output.append((path, source))
        except OSError:
            pass
    return output


def _mcp_configs(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            return {}
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    if path.suffix.lower() == ".json":
        try:
            value = json.loads(text)
            servers = {}
            def visit(obj, depth=0):
                if depth > 6 or not isinstance(obj, dict):
                    return
                for key, child in obj.items():
                    if key == "mcpServers" and isinstance(child, dict):
                        servers.update({name: config for name, config in child.items()
                                        if isinstance(name, str) and isinstance(config, dict)})
                    elif isinstance(child, dict):
                        visit(child, depth+1)
            visit(value)
            return servers
        except (ValueError, TypeError):
            return {}
    servers, section, env_section = {}, None, None
    for line in text.splitlines():
        header = re.match(r'^\s*\[mcp_servers\.(?:"([^"\n]+)"|([A-Za-z0-9_-]+))\]\s*$', line)
        if header:
            section = header.group(1) or header.group(2); env_section=None; servers.setdefault(section, {}); continue
        env_header=re.match(r'^\s*\[mcp_servers\.([A-Za-z0-9_-]+)\.env\]\s*$',line)
        if env_header and env_header.group(1) in servers:
            env_section=env_header.group(1); section=None; continue
        if line.strip().startswith('['):
            section = None; env_section=None; continue
        if env_section:
            key=re.match(r'^\s*([A-Za-z0-9_]+)\s*=',line)
            if key:servers[env_section].setdefault('env',{})[key.group(1)]='[REDACTED]'
            continue
        if section:
            match = re.match(r'^\s*(command|args|url|type)\s*=\s*(.+)$', line)
            if match:
                try:
                    servers[section][match.group(1)] = ast.literal_eval(match.group(2))
                except (ValueError, SyntaxError):
                    pass
    return servers


def _mcp_names(path: Path) -> List[str]:
    return sorted(_mcp_configs(path))


def _safe_mcp_metadata(config):
    command = config.get('command')
    url = config.get('url') or config.get('httpUrl')
    transport = 'MCP_HTTP' if isinstance(url, str) else 'MCP_STDIO' if isinstance(command, str) else 'UNAVAILABLE'
    endpoint = None
    if isinstance(url, str):
        try:
            parsed = urlsplit(url)
            # Origins and known protocol paths only; never query/userinfo/path tokens.
            host = parsed.hostname or ''
            authority = ('['+host+']' if ':' in host else host) + (':'+str(parsed.port) if parsed.port else '')
            endpoint = parsed.scheme + '://' + authority + (parsed.path if parsed.path in ('', '/', '/mcp', '/sse') else '')
        except ValueError:
            pass
    credentials = sorted(set(str(k) for k in config.get('env', {}) if re.search(r'key|token|secret|password', str(k), re.I)))
    credentials.extend(str(k) for k in config.get('headers', {}))
    return {'configured_transport': transport, 'configured_endpoint': endpoint,
            'configured_executable': Path(command).name if isinstance(command, str) and re.fullmatch(r'[A-Za-z0-9_./-]+', command) else None,
            'required_credential_names': credentials, 'configured_disabled': config.get('disabled') is True}


def _mcp_records(repo_root: Path, extra: Sequence[Path], include_defaults: bool) -> List[Dict[str, Any]]:
    records = {}
    for path, source in _config_paths(repo_root, extra, include_defaults):
        for name, config in _mcp_configs(path).items():
            key = _slug(name)
            if key in records:
                records[key]['source_paths'].append(str(path)); continue
            metadata = _safe_mcp_metadata(config)
            disabled = metadata['configured_disabled']
            records[key] = {
                "id": "mcp:" + key, "name": name, "kind": "MCP_SERVER",
                "description": "Agent-configured MCP server. Its purpose and operations require runtime tool definitions; Command Center has not verified a callable transport for this server.",
                "primary_use_cases": ["Inspect server tool definitions before selecting an authorized operation"], "secondary_use_cases": [],
                "when_to_use": ["After verifying the server identity, tool schema, transport and operation permission"],
                "when_not_to_use": ["Do not assume agent-host configuration grants local models execution access"],
                "supported_operations": [], "execution_transport": "AGENT_ONLY",
                "availability": "DISABLED" if disabled else "DISCOVERED" if source == "installed plugin configuration" else "CONFIGURED",
                "configuration_status": "DISABLED" if disabled else "DISCOVERED" if source == "installed plugin configuration" else "CONFIGURED",
                "configuration_source": source,
                "required_dependencies": ["Configured MCP host and server-specific dependencies"],
                "resource_requirements": ["Unknown until actual runtime verification"],
                "side_effects": ["Unknown; server descriptions and annotations do not grant permission"],
                "security_constraints": ["Credentials are never exposed", "No server is started by catalog discovery", "Executable operations require trusted adapters and independent permission checks"],
                "provenance_requirements": ["Preserve server identity, exact tool schemas, arguments and returned evidence"],
                "relevant_projects": [], "relevant_workflow_phases": ["discovery"],
                "documentation_path": str(path), "source_paths": [str(path)], "trusted_roots": [str(path.parent)],
                "version_or_source": source, "last_verified": datetime.now(timezone.utc).isoformat(), **metadata,
            }
    return list(records.values())


def _comfy_record(repo_root: Path) -> Dict[str, Any]:
    candidates = [Path.home() / "ComfyUI", Path.home() / "Applications/ComfyUI", repo_root / "ComfyUI"]
    candidates.extend(sorted((Path.home() / "ComfyUI-Installs").glob("*/ComfyUI")))
    install = next((path.resolve() for path in candidates if path.is_dir()), None)
    models: List[str] = []
    if install:
        model_root = install / "models"
        if model_root.is_dir():
            for path in model_root.glob("*/*"):
                try:
                    if path.is_file() and path.suffix.lower() in (".safetensors", ".ckpt", ".pt", ".pth", ".gguf") and path.stat().st_size:
                        models.append(path.name)
                        if len(models) >= 20:
                            break
                except OSError:
                    pass
    listeners = []
    for port in (8188, 8189, 8000):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                listeners.append(port)
        except OSError:
            pass
    listener = bool(listeners)
    version = None
    if install:
        pyproject = install / "pyproject.toml"
        text = _read_bounded(pyproject, 64 * 1024, install) if pyproject.is_file() else None
        match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', text or "", re.MULTILINE)
        version = match.group(1)[:100] if match else None
    available = bool(install or listener)
    return {
        "id": "service:comfyui", "name": "ComfyUI", "kind": "LOCAL_SERVICE",
        "description": "Local ComfyUI installation or listener observation; workflow readiness and model compatibility are unverified.",
        "primary_use_cases": ["Agent-operated node-based image or media workflows"], "secondary_use_cases": [],
        "when_to_use": ["After a workflow, model compatibility, and operator authorization are verified"],
        "when_not_to_use": ["Do not infer generation readiness from an installation, listener, template, or model file alone"],
        "supported_operations": [], "execution_transport": "AGENT_ONLY" if available else "UNAVAILABLE",
        "availability": "CONFIGURED" if install else ("DISCOVERED" if listener else "UNAVAILABLE"),
        "configuration_status": "CONFIGURED" if install else ("DISCOVERED" if listener else "UNAVAILABLE"),
        "required_dependencies": ["compatible ComfyUI runtime, workflow, and models"],
        "resource_requirements": ["model-specific memory and compute requirements are unverified"],
        "side_effects": ["No generation or workflow submission occurs during discovery"],
        "security_constraints": ["Loopback listener check only", "No HTTP request, server start, workflow submission, or model load", "Not model-callable"],
        "provenance_requirements": ["Record workflow, model identity, seed, inputs, and outputs if later executed"],
        "relevant_projects": [], "relevant_workflow_phases": ["image generation", "media generation"],
        "documentation_path": str(install) if install else "http://127.0.0.1:8188",
        "source_paths": [str(install)] if install else [], "trusted_roots": [str(install)] if install else [],
        "version_or_source": version or "local installation/listener observation; version unverified",
        "last_verified": datetime.now(timezone.utc).isoformat(),
        "observation": {"installation_present": bool(install), "model_file_count": len(models),
                        "model_filenames": models, "loopback_listener": listener,
                        "listener_ports": listeners},
    }


def _merge(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    skills: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for record in records:
        key = record["id"]
        if record.get("kind") == "SKILL":
            digest = record.get("_content_digest", "")
            by_digest = skills.setdefault(key, {})
            prior = by_digest.get(digest)
            if prior is None:
                by_digest[digest] = record
            else:
                prior["source_paths"] = sorted(set(prior.get("source_paths", [])) | set(record.get("source_paths", [])))
                prior["trusted_roots"] = sorted(set(prior.get("trusted_roots", [])) | set(record.get("trusted_roots", [])))
                if record["configuration_status"] == "CONFIGURED":
                    prior["configuration_status"] = prior["availability"] = "CONFIGURED"
            continue
        prior = merged.get(key)
        if prior is None:
            merged[key] = record
            continue
        paths = sorted(set(prior.get("source_paths", [])) | set(record.get("source_paths", [])))
        choose = record if record["configuration_status"].startswith("CONFIGURED") and not prior["configuration_status"].startswith("CONFIGURED") else prior
        choose["source_paths"] = paths
        merged[key] = choose
    for base, by_digest in skills.items():
        collision = len(by_digest) > 1
        for digest, record in by_digest.items():
            record["id"] = base + (":" + digest[:12] if collision else "")
            record.pop("_content_digest", None)
            merged[record["id"]] = record
    return sorted(merged.values(), key=lambda value: (value["kind"], value["name"].lower(), value["id"]))


def build_catalog(repo_root: Optional[Path] = None, extra_skill_roots: Sequence[Path] = (),
                  config_paths: Sequence[Path] = (), include_defaults: bool = True,
                  include_cli: bool = True, include_services: bool = True,
                  diagnostics: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """Build a bounded catalog without starting or invoking any capability."""
    repo = Path(repo_root or Path.cwd()).expanduser().resolve()
    records: List[Dict[str, Any]] = []
    observed_diagnostics: Dict[str, int] = {}
    for root, source, configured in _skill_roots(repo, extra_skill_roots, include_defaults):
        try:
            paths = root.rglob("SKILL.md")
            for path in paths:
                record = _skill_record(path, root, source, configured, observed_diagnostics)
                if record:
                    records.append(record)
        except OSError:
            continue
    if include_cli:
        records.extend(_cli_records())
    records.extend(_mcp_records(repo, config_paths, include_defaults))
    if include_services:
        records.append(_comfy_record(repo))
    if diagnostics is not None:
        diagnostics.clear(); diagnostics.update(observed_diagnostics)
    return _merge(records)


def read_skill(capability_id: str, catalog: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Return bounded documentation for a discovered skill, rejecting path escapes."""
    if not isinstance(capability_id, str) or not capability_id.startswith("skill:"):
        raise ValueError("A skill capability id is required.")
    records = list(catalog) if catalog is not None else build_catalog()
    matches = [record for record in records if record.get("id") == capability_id and record.get("kind") == "SKILL"]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous skill capability.")
    record = matches[0]
    documented = Path(record["documentation_path"])
    allowed = [Path(value) for value in record.get("trusted_roots", [])]
    text = next((_read_bounded(documented, MAX_SKILL_BYTES, root) for root in allowed
                 if _inside(documented.resolve(), root.resolve())), None)
    if text is None:
        raise ValueError("Skill documentation is unavailable or outside its trusted root.")
    return {"capability": record, "content": text}


def route_capabilities(task: Any, domain: Any = None, model: Any = None,
                       catalog: Optional[Sequence[Dict[str, Any]]] = None,
                       limit: int = MAX_ROUTE_RESULTS) -> List[Dict[str, Any]]:
    """Return a deterministic, bounded descriptive selection; never execute it."""
    if type(limit) is not int or not 1 <= limit <= MAX_ROUTE_RESULTS:
        raise ValueError("limit must be between 1 and 8.")
    query = " ".join(str(value or "") for value in (task, domain, model)).lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9+.-]{2,}", query))
    if not tokens:
        return []
    # Intent rules are exclusive where inappropriate capabilities would distract.
    wanted = []
    if any(word in query for word in ("transcribe", "transcription", "speech-to-text")):
        wanted = ["cli:whisper-cli", "cli:ffmpeg"]
    elif any(word in query for word in ("slideshow", "analyze video", "video analysis", "editorial-analysis", "reference film")):
        wanted = ["skill:analyze-video-editing", "skill:video-editing", "cli:ffprobe", "cli:ffmpeg"]
    elif any(word in query for word in ("cinematic", "generated shot", "generate video", "visual metaphor")):
        wanted = ["skill:cinematic-video", "service:comfyui", "cli:ffmpeg", "native:factory-status"]
    elif "remotion" in query:
        operation = next((name for word, name in (("caption", "captions"), ("map", "maps"),
            ("upgrade", "upgrade"), ("render", "render"), ("preview", "studio"),
            ("interactive", "interactivity"), ("media", "multimedia"), ("saas", "saas")) if word in query), "create")
        wanted = ["skill:remotion-" + operation, "skill:remotion-best-practices"]
    elif any(word in query for word in ("edit video", "video edit", "rough cut", "pacing")):
        wanted = ["skill:video-editing", "cli:ffprobe", "cli:ffmpeg"]
    explicit = [r["id"] for r in (catalog or []) if r["id"].lower() in query]
    if "repository status" in query or "git status" in query:
        explicit.insert(0, "native:repo-status")
    if "mcp project" in query or "list_projects" in query:
        explicit.insert(0, "mcp:codebase-memory-mcp")
    if any(word in query for word in ("worker status", "queue status", "resource lease")):
        explicit.insert(0, "native:center-status")
    if "comfyui" in query:
        explicit.append("service:comfyui")
    wanted = explicit + wanted
    scored = []
    for record in (list(catalog) if catalog is not None else build_catalog()):
        text = " ".join(str(record.get(key) or "") for key in
                        ("name", "description", "primary_use_cases", "secondary_use_cases",
                         "supported_operations", "relevant_projects", "relevant_workflow_phases")).lower()
        preferred = next((i for i, prefix in enumerate(wanted)
                          if record["id"] == prefix or (prefix.startswith("skill:") and record["id"].startswith(prefix + ":"))), None)
        if wanted and preferred is None:
            continue
        score = (1000 - preferred * 10 if preferred is not None else
                 sum(4 if token in str(record.get("name", "")).lower() else 1 for token in tokens if token in text))
        if score:
            score += 2 if record.get("availability") == "CONFIGURED" else 0
            scored.append((score, record["id"], record))
    scored.sort(key=lambda value: (-value[0], value[1]))
    keys = ("id", "name", "kind", "description", "supported_operations", "execution_transport",
            "availability", "configuration_status", "security_constraints", "documentation_path")
    # At most one installed document version per logical name in prompt menus.
    results, names = [], set()
    for _, _, record in scored:
        key = (record["kind"], record["name"])
        if key in names:
            continue
        names.add(key); results.append({key: record.get(key) for key in keys})
        if len(results) == limit:
            break
    return results
