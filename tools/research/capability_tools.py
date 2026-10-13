"""Trusted, bounded read-only adapters. Model text never becomes an argv or URL."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time

from capabilities import FIELDS
from models import LocalHTTP, ModelError

ROOT = Path(__file__).resolve().parents[2]
FACTORY = Path.home() / 'Projects/video-editing/video-edits'
MCP_BINARY = Path.home() / '.local/bin/codebase-memory-mcp'
MCP_NAME = 'codebase-memory-mcp'
MCP_ID = 'mcp:codebase-memory-mcp'
MAX_OUTPUT = 128 * 1024
PERMITTED_MCP_TOOLS = frozenset({'list_projects'})


def now():
    return datetime.now(timezone.utc).isoformat()


def finite_json(text):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError('Duplicate JSON key')
            obj[key] = value
        return obj
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))


def validate_input(value, schema, depth=0):
    """Fail closed on unsupported schema constructs; retain original MCP schemas."""
    if depth > 12 or not isinstance(schema, dict):
        raise ValueError('Unsupported input schema')
    allowed = {'type', 'properties', 'required', 'additionalProperties', 'enum', 'items',
               'minimum', 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems',
               'description', 'title', 'default', '$schema', 'examples'}
    if set(schema) - allowed:
        raise ValueError('This schema requires a validator not enabled by this adapter')
    kind = schema.get('type')
    good = {'object': isinstance(value, dict), 'array': isinstance(value, list),
            'string': isinstance(value, str), 'integer': type(value) is int,
            'number': type(value) in (int, float) and math.isfinite(value),
            'boolean': type(value) is bool, 'null': value is None}
    if kind not in good or not good[kind]:
        raise ValueError('Invalid argument type: ' + str(kind))
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('Argument is outside the permitted enum')
    if kind == 'object':
        props = schema.get('properties', {})
        if not isinstance(props, dict) or set(value) - set(props):
            raise ValueError('Unknown tool arguments')
        if set(schema.get('required', [])) - set(value):
            raise ValueError('Missing required tool arguments')
        for key, child in value.items():
            validate_input(child, props[key], depth + 1)
    elif kind == 'array':
        if len(value) > schema.get('maxItems', 100) or len(value) < schema.get('minItems', 0):
            raise ValueError('Argument array exceeds bounds')
        for child in value:
            validate_input(child, schema.get('items', {}), depth + 1)
    elif kind == 'string':
        if len(value) > schema.get('maxLength', 4000) or len(value) < schema.get('minLength', 0):
            raise ValueError('Argument text exceeds bounds')
    elif kind in ('number', 'integer'):
        if value < schema.get('minimum', -1e12) or value > schema.get('maximum', 1e12):
            raise ValueError('Numeric argument exceeds bounds')
    if len(json.dumps(value, allow_nan=False)) > 16 * 1024:
        raise ValueError('Tool arguments exceed 16 KiB')


def safe_env():
    # No credentials inherited by tool children.
    return {k: v for k, v in os.environ.items() if k in ('HOME', 'PATH', 'TMPDIR', 'LANG')}


def bounded_command(argv, cwd=None, timeout=8, limit=MAX_OUTPUT):
    process = subprocess.Popen(argv, cwd=cwd, env=safe_env(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    for handle in (process.stdout, process.stderr):
        selector.register(handle, selectors.EVENT_READ)
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Tool request timed out')
            for key, _ in selector.select(min(.1, remaining)):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = out if key.fileobj is process.stdout else err
                target.extend(chunk)
                if len(out) + len(err) > limit:
                    raise ValueError('Tool output exceeded its byte limit')
        process.wait(timeout=max(.01, deadline - time.monotonic()))
        if process.returncode:
            # Server stderr may contain private paths or credentials; do not return it.
            raise ValueError('Tool exited with code ' + str(process.returncode))
        return bytes(out).decode('utf-8', errors='strict')
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=1)
        process.stdout.close(); process.stderr.close()


class StdioMCP:
    """MCP JSON-RPC stdio for one fixed, locally configured metadata server."""
    def __init__(self, executable=MCP_BINARY, timeout=8):
        self.executable = Path(executable).resolve(strict=True)
        if self.executable != MCP_BINARY.resolve(strict=True) or not os.access(self.executable, os.X_OK):
            raise ValueError('MCP executable is not the trusted installed server')
        self.timeout = timeout
        self.process = None
        self.buffer = b''
        self.total = 0
        self.sequence = 0
        self.selector = selectors.DefaultSelector()

    def __enter__(self):
        self.process = subprocess.Popen([str(self.executable)], cwd=ROOT, env=safe_env(),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for handle in (self.process.stdout, self.process.stderr):
            self.selector.register(handle, selectors.EVENT_READ)
        try:
            result = self.request('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                'clientInfo': {'name': 'command-center', 'version': '1'}})
            if result.get('serverInfo', {}).get('name') != MCP_NAME or result.get('protocolVersion') != '2024-11-05':
                raise ValueError('Unexpected MCP server identity or protocol')
            self.server_info = result['serverInfo']
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            return self
        except BaseException:
            self.close(); raise

    def _send(self, value):
        data = (json.dumps(value, allow_nan=False) + '\n').encode()
        if len(data) > 20 * 1024:
            raise ValueError('MCP request exceeds its limit')
        self.process.stdin.write(data); self.process.stdin.flush()

    def request(self, method, params):
        if method not in ('initialize', 'tools/list', 'tools/call'):
            raise ValueError('MCP method is not permitted')
        self.sequence += 1
        identifier = self.sequence
        self._send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        messages = 0
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if not line.strip():
                    continue
                messages += 1
                if messages > 100:
                    raise ValueError('MCP message limit reached')
                reply = finite_json(line.decode('utf-8'))
                if not isinstance(reply, dict) or reply.get('jsonrpc') != '2.0':
                    raise ValueError('Malformed MCP response')
                if 'method' in reply and 'id' in reply:
                    raise ValueError('MCP client-side requests are not supported')
                if reply.get('id') == identifier:
                    if reply.get('error'):
                        raise ValueError('MCP server rejected the request')
                    if not isinstance(reply.get('result'), dict):
                        raise ValueError('Incomplete MCP response')
                    return reply['result']
            for key, _ in self.selector.select(min(.1, max(.01, deadline-time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.fileobj is self.process.stdout:
                        raise ValueError('MCP server exited before responding')
                    continue
                self.total += len(chunk)
                if self.total > MAX_OUTPUT:
                    raise ValueError('MCP output limit reached')
                if key.fileobj is self.process.stdout:
                    self.buffer += chunk
        raise TimeoutError('MCP request timed out')

    def tools(self):
        result, tools, cursors = self.request('tools/list', {}), [], set()
        for _ in range(5):
            page = result.get('tools')
            if not isinstance(page, list) or len(tools) + len(page) > 200:
                raise ValueError('Malformed or excessive MCP tool listing')
            for tool in page:
                if (not isinstance(tool, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', tool.get('name', ''))
                        or not isinstance(tool.get('inputSchema'), dict)):
                    raise ValueError('Malformed MCP tool definition')
            tools.extend(deepcopy(page))
            cursor = result.get('nextCursor')
            if cursor is None:
                if len({t['name'] for t in tools}) != len(tools):
                    raise ValueError('Duplicate MCP tool name')
                return tools
            if not isinstance(cursor, str) or cursor in cursors or len(cursor) > 1000:
                raise ValueError('Invalid MCP pagination')
            cursors.add(cursor); result = self.request('tools/list', {'cursor': cursor})
        raise ValueError('MCP pagination limit reached')

    def call(self, name, arguments):
        if name not in PERMITTED_MCP_TOOLS:
            raise PermissionError('MCP operation is not permitted by Command Center')
        tools = self.tools()
        tool = next((t for t in tools if t['name'] == name), None)
        if tool is None:
            raise ValueError('Permitted MCP tool is unavailable')
        validate_input(arguments, tool['inputSchema'])
        # These host restrictions are independent of server hints and schemas.
        if (arguments.get('limit', 10) > 20 or arguments.get('offset', 0) > 200
                or arguments.get('detail', 'identity') != 'identity'
                or arguments.get('include_details', False)):
            raise PermissionError('MCP metadata request exceeds the permitted policy')
        inputs = {**arguments, 'limit': arguments.get('limit', 10), 'metadata_only': True}
        result = self.request('tools/call', {'name': name, 'arguments': inputs})
        if result.get('isError') or not isinstance(result.get('content'), list):
            raise ValueError('MCP tool did not return a successful content result')
        return {'server': self.server_info, 'tool': name, 'input_schema': tool['inputSchema'],
                'effective_arguments': inputs, 'result': result}

    def close(self):
        self.selector.close()
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                process.stdin.close(); process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try: process.wait(timeout=1)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=1)
        for handle in (process.stdin, process.stdout, process.stderr):
            if handle and not handle.closed:
                handle.close()
        self.process = None

    def __exit__(self, *_):
        self.close()


def mcp_health(resources):
    """Explicit health command only: list schemas, never run paid or write tools."""
    value = {'id': MCP_ID, 'verified_at': now(), 'available': False, 'tools': [],
             'callable_tools': [], 'transport': 'MCP_STDIO'}
    try:
        with StdioMCP() as client:
            value.update(available=True, server=client.server_info, tools=client.tools())
        value['callable_tools'] = sorted(PERMITTED_MCP_TOOLS & {t['name'] for t in value['tools']})
        value['schema_sha256'] = hashlib.sha256(json.dumps(value['tools'], sort_keys=True).encode()).hexdigest()
        value['executable'] = str(MCP_BINARY.resolve())
        stat = MCP_BINARY.stat()
        value['executable_stamp'] = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    except (OSError, ValueError, TimeoutError) as exc:
        value['error'] = str(exc)
    path = Path(resources) / 'capability-mcp.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False)); temporary.chmod(0o600)
    os.replace(temporary, path)
    return value


def cached_mcp(resources):
    path = Path(resources) / 'capability-mcp.json'
    try:
        if path.stat().st_size > MAX_OUTPUT:
            return None
        value = finite_json(path.read_text())
        stat = MCP_BINARY.stat()
        if value.get('executable_stamp') != [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]:
            return None
        return value
    except (OSError, ValueError):
        return None


def native_record(identifier, name, description, operations, transport, source):
    record = {key: [] for key in FIELDS}
    record.update(id=identifier, name=name, kind='NATIVE_TOOL', description=description,
                  primary_use_cases=operations, secondary_use_cases=[], when_to_use=operations,
                  when_not_to_use=['Do not use for modifying, publishing, deleting or approving production records.'],
                  supported_operations=operations, execution_transport=transport, availability='CONFIGURED',
                  configuration_status='TRUSTED_READ_ONLY_ADAPTER', required_dependencies=[],
                  resource_requirements=['Bounded metadata inspection; no model loads'], side_effects=['Read-only'],
                  security_constraints=['Fixed adapter; no shell, arbitrary URLs or caller-supplied argv'],
                  provenance_requirements=['Record arguments, source identity, output, duration and timestamp'],
                  relevant_projects=['Command Center', 'Video Factory'], relevant_workflow_phases=['inspection', 'validation'],
                  documentation_path=str(Path(__file__)), version_or_source=source, last_verified=now(),
                  callable=True)
    return record


def augment_catalog(catalog, resources):
    records = deepcopy(catalog)
    agent_path = Path(resources) / 'capability-agent-mcp.json'
    agent_metadata = {}
    try:
        if agent_path.stat().st_size < 256 * 1024:
            agent_metadata = finite_json(agent_path.read_text())
    except (OSError, ValueError):
        pass
    for record in records:
        record['callable'] = False
        if record['kind'] == 'MCP_SERVER':
            runtime_name = record['name']
            aliases = {'cortex-memory': 'cortex', 'chrome-devtools': 'chrome_devtools', 'open-design': 'opendesign'}
            observed_tools = agent_metadata.get('servers', {}).get(aliases.get(runtime_name, runtime_name), [])
            if observed_tools:
                record['agent_observed_tools'] = observed_tools
                record['supported_operations'] = [t['name'] for t in observed_tools]
                record['agent_observed_at'] = agent_metadata.get('verified_at')
                descriptions = [t['description'] for t in observed_tools if t.get('description')]
                if descriptions:
                    record['description'] = 'Agent-host MCP operations observed: ' + ', '.join(t['name'] for t in observed_tools[:12]) + '. Inspect individual operation descriptions and permissions before use. Command Center execution is not granted.'
            if runtime_name.lower() == 'apify':
                record['description'] = ('Apify provides structured public-web research, page extraction, reference discovery and bounded Actor workflows. '
                    'The agent plugin is installed; Command Center has no credentialed Apify transport. Paid Actors, uncontrolled crawls and external writes are disabled. Scraped media is not licensed production footage.')
                record['primary_use_cases'] = ['Public reference and competitor research', 'Structured extraction of approved public sources']
                record['when_not_to_use'] = ['Do not run paid Actors without explicit approval and cost bounds', 'Do not treat scraped media as licensed footage']
                record['provenance_requirements'] = ['Preserve original source URLs, acquisition time, Actor identity and licensing evidence']
        if record['kind'] == 'SKILL':
            record['instruction_access'] = record.get('read_availability') == 'FULL'
        if record['id'] == 'cli:ffprobe' and record['availability'] != 'UNAVAILABLE' and shutil.which('ffprobe'):
            record.update(callable=True, execution_transport='SUBPROCESS',
                          invocation={'operation': 'inspect_media', 'arguments': {'source_id': 'operator-approved source ID'}},
                          security_constraints=['Only exact operator-approved regular media files; file protocol only',
                                                'Fixed metadata fields, 8-second timeout, 128 KiB output limit'],
                          configuration_status='TRUSTED_READ_ONLY_ADAPTER')
        if record['id'] == 'cli:ffmpeg':
            record['description'] = ('FFmpeg deterministically processes video and audio: extraction, trimming, transcoding, normalization, proxies, contact sheets and render QA. Use existing Video Factory render/QA wrappers for production. It does not make subjective creative decisions; direct model-driven rendering is disabled.')
            record['relevant_workflow_phases'] = ['reference inspection', 'asset preparation', 'editing', 'final QA']
        if record['id'] == 'cli:whisper-cli':
            record['required_dependencies'] = ['Operator-configured local whisper.cpp model weights; none verified by this inventory']
            record['description'] = 'Local whisper.cpp speech-to-text executable for transcription, timestamped dialogue analysis and caption preparation. The executable is installed; no transcription model or trusted transcription adapter is currently verified.'
        if record['id'] == 'service:comfyui':
            observation = record.get('observation', {})
            if not observation.get('loopback_listener') or not observation.get('model_file_count'):
                record.update(availability='UNAVAILABLE', execution_transport='UNAVAILABLE', availability_reason='Installed ComfyUI is offline and no compatible generation weights/workflow are verified. No generation operations are enabled.')
            record['resource_requirements'] = ['Any future heavy generation must acquire the existing exclusive lease after verified Qwen/Bonsai release, then verify generation cleanup before releasing it. No generation adapter is enabled.']
        if record['id'] == MCP_ID:
            health = cached_mcp(resources)
            if health:
                record['runtime_diagnostics'] = health
                record['last_verified'] = health['verified_at']
                record['version_or_source'] = health.get('server', {}).get('version', record['version_or_source'])
                record['resource_requirements'] = ['One bounded metadata-only MCP child; no repository indexing or model inference']
                record['required_dependencies'] = [str(MCP_BINARY)]
                record['supported_operations'] = [t['name'] for t in health.get('tools', [])]
                record['mcp_tools'] = health.get('tools', [])
                record['permitted_operations'] = health.get('callable_tools', [])
                record['callable'] = bool(health.get('available') and health.get('callable_tools'))
                record['execution_transport'] = 'MCP_STDIO' if record['callable'] else 'UNAVAILABLE'
                record['availability'] = 'READY' if record['callable'] else 'UNAVAILABLE'
                record['description'] = ('Local codebase graph and repository inspection server. It exposes project discovery, '
                    'graph queries, architecture and source inspection, plus index-management operations. Command Center '
                    'currently permits only bounded project-identity listing; indexing, deletion and arbitrary source reads are blocked.')
                record['invocation'] = {'arguments': {'tool': 'list_projects', 'input': {'format': 'json', 'limit': 10}}}
                record['security_constraints'] = ['Server schemas retained exactly; server annotations do not grant permissions',
                    'Only list_projects is permitted, limit <=20, identity metadata only', 'Owned protocol child always closed after use']
                record['when_to_use'] = ['Discover existing repository index identities before choosing an approved project workflow.']
                record['when_not_to_use'] = ['Do not index, delete projects or read arbitrary source through this adapter.']
    if shutil.which('git'):
        records.append(native_record('native:repo-status', 'Repository status',
            'Inspect the current revision and uncommitted file names in Command Center or Video Factory. '
            'Use before development or production integration work; it cannot edit files, commit or push.',
            ['inspect_revision_and_changes'], 'SUBPROCESS', 'Installed git; fixed read-only argv'))
        records[-1]['invocation'] = {'arguments': {'project': 'command-center or video-factory'}}
    records.append(native_record('native:center-status', 'Command Center queue and resource status',
        'Inspect the existing queue, worker availability, model registry, active lifecycle association and cleanup blocker through the authoritative version 1 bridge. Use before scheduling work; this operation cannot load a model or alter a job.',
        ['read_queue_and_resource_status'], 'NATIVE_FUNCTION', str(ROOT / 'tools/research/bridge.py')))
    records[-1]['invocation']={'arguments':{}}
    records.append(native_record('native:factory-status', 'Video Factory production status',
        'Read the production orchestrator’s current status through its existing local API. Use to check production '
        'availability before planning work. Scheduling, source approvals, rendering and human review remain owned by Video Factory.',
        ['read_production_status'], 'HTTP_API', str(FACTORY / 'src/server.ts')))
    records[-1].update(availability='UNVERIFIED', callable=False, endpoint='http://127.0.0.1:4317/api/production',
                       invocation={'arguments': {}}, required_dependencies=['Existing Video Factory service on its configured default port'])
    try:
        observed = factory_status()
        records[-1].update(availability='READY', callable=True, last_verified=now(),
                           runtime_diagnostics={'verified_read_only_status': True, 'observed_at': now()})
    except (OSError, ValueError, ModelError) as exc:
        records[-1].update(availability='UNAVAILABLE', availability_reason=str(exc),
                           runtime_diagnostics={'verified_read_only_status': False})
    for identifier, name, kind, description, source, operations, phases in (
        ('workflow:command-center-collaboration', 'Qwen/Bonsai collaboration', 'WORKFLOW',
         'Bounded draft, editorial critique, feedback evaluation and revision using the existing collaboration runner. Deterministic validation and immutable evidence determine accept/reject/abstain; model agreement is not factual proof.',
         ROOT/'tools/research/collaboration.py', ['submit existing collaboration contract', 'compare immutable revisions', 'inspect validation and provenance', 'request cooperative cancellation'],
         ['planning','drafting','editorial review','revision','validation']),
        ('workflow:video-factory-production', 'Video Factory production workflow', 'WORKFLOW',
         'The existing production orchestrator owns project knowledge, approved sources, proposals, asset selection, Remotion rendering, FFmpeg QA and human review. Command Center supplies local inference and shared resource leasing. Production actions require the existing approval controls.',
         FACTORY/'src/server.ts', ['existing project/proposal workflow', 'approved asset acquisition', 'Remotion rendering through shared lease', 'FFmpeg/ffprobe QA', 'human approval'],
         ['creative direction','reference research','asset preparation','editing','visual review','final QA','human approval']),
        ('validator:collaboration-constraints', 'Artifact constraint validation', 'VALIDATOR',
         'Existing deterministic checks enforce output schema, required sections/content, protected claims, formatting and length constraints. Use to validate declared requirements; it cannot independently prove semantic or factual correctness.',
         ROOT/'tools/research/validation.py', ['validate declared collaboration constraints'], ['validation','comparison'])):
        r=native_record(identifier,name,description,operations,'AGENT_ONLY',str(source))
        r.update(kind=kind,callable=False,relevant_workflow_phases=phases,
                 availability='CONFIGURED' if source.is_file() else 'UNAVAILABLE',
                 security_constraints=['Use the existing application service and human-approval contracts; model text grants no new permissions'])
        records.append(r)
    return sorted(records, key=lambda r: (r['kind'], r['name'].lower(), r['id']))


def factory_status():
    status, raw = LocalHTTP('http://127.0.0.1:4317', 3)._request('GET', '/api/production')
    if status != 200 or len(raw) > MAX_OUTPUT:
        raise ValueError('Configured Video Factory status endpoint is unavailable')
    output = finite_json(raw.decode())
    if not isinstance(output, dict) or not all(key in output for key in ('canStartJob', 'gateReason', 'jobConcurrency')):
        raise ValueError('Endpoint did not return the verified Video Factory status contract')
    return output


class ToolExecutor:
    def __init__(self, catalog, approved_sources=None, mcp_factory=StdioMCP, db=None, config=None, resources=None):
        self.records = {r['id']: r for r in catalog}
        self.approved_sources = dict(approved_sources or {})
        self.mcp_factory = mcp_factory
        self.db=Path(db or ROOT/'.research/queue.sqlite3')
        self.config=Path(config or ROOT/'tools/research/models.json')
        self.resources=Path(resources or ROOT/'.research')

    def invoke(self, identifier, arguments):
        started = time.monotonic()
        record = self.records.get(identifier)
        if not record or not record.get('callable'):
            raise PermissionError('Capability has no permitted executable adapter')
        if not isinstance(arguments, dict):
            raise ValueError('Tool arguments must be an object')
        source = None
        if identifier == 'cli:ffprobe':
            validate_input(arguments, {'type': 'object', 'properties': {'source_id': {'type': 'string', 'maxLength': 80}}, 'required': ['source_id']})
            path = self.approved_sources.get(arguments['source_id'])
            if path is None:
                raise PermissionError('Source was not explicitly approved by the operator')
            path = Path(path)
            if path.is_symlink() or path.resolve(strict=True) != path.absolute() or not path.is_file():
                raise PermissionError('Approved media path changed or is not a regular file')
            if path.suffix.lower() not in ('.mp4', '.mov', '.webm', '.mkv', '.wav', '.mp3', '.flac', '.m4a', '.ogg'):
                raise ValueError('Only ordinary approved media containers are supported')
            argv = [shutil.which('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file', '-show_entries',
                    'format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,duration,r_frame_rate,sample_rate,channels',
                    '-of', 'json', str(path)]
            output = finite_json(bounded_command(argv))
            if not isinstance(output, dict) or 'streams' not in output:
                raise ValueError('Incomplete media inspection result')
            source = {'id': arguments['source_id'], 'path': str(path), 'size': path.stat().st_size}
        elif identifier == 'native:repo-status':
            validate_input(arguments, {'type': 'object', 'properties': {'project': {'type': 'string', 'enum': ['command-center', 'video-factory']}}, 'required': ['project']})
            root = ROOT if arguments['project'] == 'command-center' else FACTORY
            if not (root / '.git').exists():
                raise ValueError('Configured repository is unavailable')
            executable = shutil.which('git')
            output = {'revision': bounded_command([executable, 'rev-parse', 'HEAD'], cwd=root).strip(),
                      'changes': bounded_command([executable, '-c', 'core.fsmonitor=false', '--no-optional-locks', 'status', '--short'], cwd=root).splitlines()}
            source = str(root)
        elif identifier == MCP_ID:
            validate_input(arguments, {'type': 'object', 'properties': {'tool': {'type': 'string', 'enum': ['list_projects']},
                'input': {'type': 'object', 'properties': {k: v for k, v in next(t for t in record['mcp_tools'] if t['name']=='list_projects')['inputSchema']['properties'].items()}}},
                'required': ['tool', 'input']})
            with self.mcp_factory() as client:
                output = client.call(arguments['tool'], arguments['input'])
            source = str(MCP_BINARY)
        elif identifier == 'native:center-status':
            validate_input(arguments, {'type':'object','properties':{}})
            from bridge import handle
            output=handle({'version':1,'action':'status'},self.db,self.config,self.resources)
            source=str(ROOT/'tools/research/bridge.py')
        elif identifier == 'native:factory-status':
            validate_input(arguments, {'type': 'object', 'properties': {}})
            output = factory_status()
            source = record['endpoint']
        else:
            raise PermissionError('No trusted adapter exists for this capability')
        encoded = json.dumps(output, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_OUTPUT:
            raise ValueError('Tool result exceeds the output limit')
        return {'capability_id': identifier, 'transport': record['execution_transport'], 'arguments': arguments,
                'status': 'completed', 'output': output, 'source': source, 'at': now(),
                'elapsed_ms': round((time.monotonic()-started)*1000), 'output_bytes': len(encoded.encode())}
