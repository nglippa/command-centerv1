"""Loopback operator interface. All dispatch and lifecycle management remain in the worker."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import fcntl
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import unquote, urlsplit

from collaboration_store import CollaborationStore
from lifecycle import Manager
from model_access import model_access
from progress import research_progress, collaboration_progress
from queue_store import QueueStore, normalize_time, utc_now
from registry import Registry
from validation import normalize_spec

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_DB = ROOT / '.research/queue.sqlite3'
DEFAULT_HISTORY = ROOT / '.research/collaboration-verification.sqlite3'
DEFAULT_RESOURCES = ROOT / '.research'
DEFAULT_CONFIG = HERE / 'models.json'
MAX_BODY = 128 * 1024
MAX_RECORDS = 500
MODEL_FIELDS = ('family', 'identifier', 'provider', 'endpoint', 'owner_mode',
                'context_window', 'max_tokens', 'timeout_seconds')
SESSION_FIELDS = ('id', 'task_id', 'model', 'provider', 'endpoint', 'owner_mode', 'state',
                  'pid', 'pid_birth', 'started_at', 'updated_at', 'load_ms', 'infer_ms',
                  'unload_ms', 'last_successful_inference', 'last_verified_unload',
                  'blocker', 'observations_json', 'diagnostics_json')


class APIError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def checked_object(value, allowed, required=()):
    if not isinstance(value, dict):
        raise APIError('The request must be a JSON object.')
    if set(value) - set(allowed):
        raise APIError('Unsupported request fields: ' + ', '.join(sorted(set(value) - set(allowed))))
    if set(required) - set(value):
        raise APIError('Required fields: ' + ', '.join(required))
    return value


def decode(row):
    if row is None:
        return None
    value = dict(row)
    for key in tuple(value):
        if key.endswith('_json'):
            value[key[:-5]] = json.loads(value[key]) if value[key] else None
    return value


def duration(row):
    if not row.get('started_at') or not row.get('completed_at'):
        return None
    return round((datetime.fromisoformat(row['completed_at']) -
                  datetime.fromisoformat(row['started_at'])).total_seconds() * 1000)


def lock_held(path):
    """Observe existing advisory locks without creating files or rewriting owner records."""
    try:
        with Path(path).open('rb') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False


@contextmanager
def read_database(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def table_exists(db, name):
    return bool(db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', name)).fetchone())


def session_view(row):
    if not row:
        return None
    result = {key: row[key] for key in SESSION_FIELDS}
    for key in ('observations_json', 'diagnostics_json'):
        if isinstance(result[key], str):
            result[key] = json.loads(result[key])
    return result


class WebApplication:
    def __init__(self, db=DEFAULT_DB, config=DEFAULT_CONFIG, resources=DEFAULT_RESOURCES,
                 history_db=DEFAULT_HISTORY, probe_enabled=True):
        self.config = Path(config).resolve()
        self.resources = Path(resources).resolve()
        self.registry = Registry(self.config)
        self.databases = {'research': Path(db).resolve()}
        if history_db is not None and Path(history_db).exists() and Path(history_db).resolve() != Path(db).resolve():
            self.databases['verification'] = Path(history_db).resolve()
        # Existing services initialize a fresh queue; they never dispatch work here.
        with QueueStore(self.databases['research']):
            pass
        with CollaborationStore(self.databases['research']):
            pass
        self.token = secrets.token_urlsafe(32)
        self._stop = threading.Event()
        self._observation_lock = threading.Lock()
        self._observation = {'observed_at': None, 'models': []}
        self._thread = None
        if probe_enabled:
            self._thread = threading.Thread(target=self._observe, name='local-resource-observer', daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _observe(self):
        # One connection, owned by this thread. No execute/reconcile/load/unload calls.
        manager = None
        try:
            manager = Manager(self.resources, self.config)
            while not self._stop.is_set():
                live = manager.probe_live()
                for entry, model in zip(manager.registry.entries, live['models']):
                    model['availability'] = 'unknown'
                    try:
                        if entry.provider == 'ollama':
                            tags = manager.d.native(entry, 'GET', '/api/tags', None, entry.health_timeout)
                            if not isinstance(tags, dict) or not isinstance(tags.get('models'), list):
                                raise ValueError('Invalid installed-model listing.')
                            if any(not isinstance(item, dict) or not isinstance(item.get('name'), str) for item in tags['models']):
                                raise ValueError('Incomplete installed-model listing.')
                            present = any(item['name'] == entry.identifier for item in tags['models'])
                            reason = 'Exact identifier present in Ollama installed-model listing.' if present else 'Configured model is missing from Ollama.'
                        else:
                            present = manager.d.path_file(entry.model_path or '') and manager.d.path_file(entry.runtime_path or '', executable=True)
                            reason = ('Configured weights and runtime are present; worker verifies compatibility before use.' if present
                                      else 'Configured Bonsai weights or executable runtime are missing.')
                        model.update(availability='available' if present else 'unavailable', availability_reason=reason)
                    except Exception as exc:
                        model['availability_reason'] = str(exc)
                with self._observation_lock:
                    self._observation = live
                self._stop.wait(15)
        except Exception as exc:
            with self._observation_lock:
                self._observation = {'observed_at': utc_now(), 'models': [
                    {'model': e.identifier, 'ok': False, 'error': str(exc), 'availability': 'unknown'}
                    for e in self.registry.entries]}
        finally:
            if manager:
                manager.store.close()

    def path(self, key, write=False):
        if key not in self.databases:
            raise APIError('Unknown application database.', 404)
        if write and key != 'research':
            raise APIError('Verified experiment history is read-only.', 403)
        return self.databases[key]

    def bootstrap(self):
        examples = []
        for key in ('marketing', 'explanation', 'structured'):
            path = ROOT / 'examples/collaboration' / (key + '.json')
            if path.is_file():
                spec = json.loads(path.read_text())
                examples.append({'id': key, 'name': spec['name'], 'spec': spec})
        return {'csrf_token': self.token,
                'databases': [{'key': k, 'label': 'Research queue' if k == 'research' else 'Verified experiments',
                               'writable': k == 'research'} for k in self.databases],
                'models': [{k: getattr(e, k) for k in MODEL_FIELDS} for e in self.registry.entries],
                'examples': examples}

    def _lifecycle(self):
        path = self.resources / 'lifecycle.sqlite3'
        if not path.exists():
            return {'current': None, 'blocker': None}, [], []
        with read_database(path) as db:
            current = db.execute('SELECT * FROM current WHERE singleton=1').fetchone()
            sessions = [session_view(db.execute('SELECT * FROM sessions WHERE model=? ORDER BY started_at DESC LIMIT 1', (entry.identifier,)).fetchone()) for entry in self.registry.entries]
            sessions = [s for s in sessions if s]
            selected = None
            if current and current['session_id']:
                selected = session_view(db.execute('SELECT * FROM sessions WHERE id=?', (current['session_id'],)).fetchone())
            events = [decode(r) for r in db.execute('SELECT events.*,sessions.model,sessions.task_id FROM events '
                       'JOIN sessions ON sessions.id=events.session_id ORDER BY events.seq DESC LIMIT 200')]
            return {'current': selected, 'blocker': current['blocker'] if current else None}, sessions, events

    @staticmethod
    def lifecycle_job(task_id, jobs):
        if isinstance(task_id, str) and task_id.startswith('collab:'):
            base, separator, call = task_id.rpartition(':')
            if separator and call.isdigit():
                identifier = base[len('collab:'):]
                return next((j for j in jobs if j['database']=='research' and j['kind']=='collaboration'
                             and j['id']==identifier), None)
        matched = [j for j in jobs if j['id']==task_id]
        return matched[0] if len(matched)==1 else None

    def state(self):
        jobs, activity, counts, workers = [], [], Counter(), []
        truncated = False
        for key, path in self.databases.items():
            with read_database(path) as db:
                workers.append({'database': key, 'active': lock_held(str(path) + '.worker.lock'), 'observed_at': utc_now()})
                for kind, table in (('research', 'tasks'), ('collaboration', 'collaboration_runs')):
                    if not table_exists(db, table):
                        continue
                    for row in db.execute(f'SELECT state,COUNT(*) AS n FROM {table} GROUP BY state'):
                        counts[row['state']] += row['n']
                    total = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    truncated |= total > MAX_RECORDS
                    # Read summaries, not full model responses, on the polling path.
                    fields = ('id,name,model_identifier,model_family,state,created_at,scheduled_at,started_at,completed_at,'
                              '(result IS NOT NULL) AS has_result' if kind == 'research' else
                              'id,state,created_at,scheduled_at,started_at,completed_at,cancel_requested,stop_reason,'
                              'accepted_revision_id,json_extract(spec_json,"$.name") AS name,'
                              '(SELECT MAX(seq) FROM collaboration_events WHERE run_id=collaboration_runs.id) AS event_seq')
                    rows = [dict(r) for r in db.execute(f'SELECT {fields} FROM {table} ORDER BY created_at DESC LIMIT ?', (MAX_RECORDS,))]
                    for row in rows:
                        row.update(database=key, kind=kind, duration_ms=duration(row),
                                   can_cancel=key == 'research' and (row['state'] == 'pending' or
                                        (kind == 'collaboration' and row['state'] == 'running' and not row.get('cancel_requested'))))
                        if kind == 'collaboration':
                            row.update(model_identifier='Qwen + Bonsai', model_family='collaboration',
                                       has_result=bool(row.get('accepted_revision_id')))
                        jobs.append(row)
                        if kind == 'research':
                            for field, title in (('created_at', 'Job queued'), ('started_at', 'Worker claimed job'),
                                                 ('completed_at', 'Job ' + row['state'])):
                                if row.get(field):
                                    activity.append({'id': f'job:{key}:{row["id"]}:{field}', 'at': row[field],
                                        'title': title, 'detail': row.get('name') or row['id'], 'source': 'job',
                                        'database': key, 'record_id': row['id'], 'kind': kind, 'state': row['state']})
                if table_exists(db, 'collaboration_events'):
                    titles = {'input': 'Collaboration queued', 'claimed': 'Worker claimed collaboration',
                              'call_started': 'Model call started', 'call_received': 'Model response recorded',
                              'call_finished': 'Model call finished', 'review': 'Editorial review recorded',
                              'evaluation': 'Feedback evaluation recorded', 'validation': 'Validation recorded',
                              'decision': 'Candidate decision recorded', 'finished': 'Collaboration finished',
                              'cancel_requested': 'Collaboration cancellation requested', 'recovery': 'Interrupted collaboration recovered'}
                    for r in db.execute('SELECT * FROM collaboration_events ORDER BY seq DESC LIMIT 100'):
                        data = json.loads(r['data_json'])
                        detail = ' · '.join(str(data[k]) for k in ('purpose', 'role', 'round', 'reason', 'stop_reason', 'state') if k in data)
                        activity.append({'id': f'collab:{key}:{r["seq"]}', 'at': r['at'],
                            'title': titles.get(r['kind'], r['kind'].replace('_', ' ').capitalize()),
                            'detail': detail[:1000], 'source': 'collaboration', 'database': key,
                            'record_id': r['run_id'], 'kind': 'collaboration', 'state': data.get('state')})
        lifecycle, sessions, events = self._lifecycle()
        titles = {'STARTING': 'Model server starting', 'LOADING': 'Model loading', 'READY': 'Model ready',
                  'BUSY': 'Inference started', 'UNLOADING': 'Model unloading', 'STOPPING': 'Owned server stopping',
                  'ERROR': 'Lifecycle error recorded', 'UNAVAILABLE': 'Model unavailable'}
        for event in events:
            linked = self.lifecycle_job(event['task_id'], jobs)
            # An initial STOPPED session is not evidence of unloading.
            title = ('Model release verified' if event['from_state'] in ('UNLOADING', 'STOPPING', 'ERROR', 'UNAVAILABLE')
                     else 'Lifecycle session opened') if event['to_state'] == 'STOPPED' else titles.get(event['to_state'], event['to_state'])
            activity.append({'id': 'lifecycle:' + str(event['seq']), 'at': event['at'], 'title': title,
                'detail': event['model'] + ' · ' + str(event['from_state'] or 'New session') + ' → ' + event['to_state'],
                'source': 'lifecycle', 'database': linked['database'] if linked else None,
                'record_id': linked['id'] if linked else None, 'kind': linked['kind'] if linked else None,
                'state': event['to_state']})
        with self._observation_lock:
            observed = self._observation
        models = []
        for entry in self.registry.entries:
            model = {k: getattr(entry, k) for k in MODEL_FIELDS}
            latest = next((s for s in sessions if s['model'] == entry.identifier), None)
            observation = next((o for o in observed['models'] if o['model'] == entry.identifier), {})
            active = None
            if observation.get('ok'):
                value = observation.get('value', {})
                if entry.provider == 'ollama' and isinstance(value.get('loaded'), list):
                    active = entry.identifier in value['loaded']
                elif entry.provider == 'bonsai' and 'listener_pid' in value:
                    active = bool(value.get('serving_model')) if value.get('listener_pid') else False
                    if value.get('listener_pid') and not value.get('serving_model'):
                        active = None
            model.update(access=model_access(entry), runtime='Ollama' if entry.provider == 'ollama' else 'Prism (configured compatible runtime)',
                         availability=observation.get('availability', 'unknown'),
                         availability_reason=observation.get('availability_reason', 'Observation pending.'),
                         observation={**observation, 'observed_at': observed['observed_at']},
                         observed_active=active, last_session=latest)
            models.append(model)
        current_job = self.lifecycle_job((lifecycle['current'] or {}).get('task_id'), jobs)
        for job in jobs:
            job['lifecycle_active'] = bool(job['database']=='research' and lifecycle['current']
                and current_job is job
                and lifecycle['current']['state'] in ('STARTING','LOADING','READY','BUSY','UNLOADING','STOPPING','ERROR'))
            if job['state'] in ('pending', 'running') or job['lifecycle_active']:
                job['progress'] = self.progress_for(job['database'], job['id'], job['kind'])
        jobs.sort(key=lambda j: j['created_at'], reverse=True)
        activity.sort(key=lambda a: a['at'], reverse=True)
        return {'observed_at': utc_now(), 'workers': workers,
                'lease': {'held': lock_held(self.resources / 'heavy-model.lease'), 'blocker': lifecycle['blocker']},
                'counts': dict(counts), 'jobs': jobs, 'models': models, 'lifecycle': lifecycle,
                'activity': activity[:200], 'truncated': bool(truncated)}

    def session_evidence(self, key, identifier, session_id=None, model=None, collaboration=False):
        path = self.resources / 'lifecycle.sqlite3'
        if key != 'research' or not path.exists():
            return {'history': [], 'events': []}
        with read_database(path) as db:
            if collaboration:
                # Prefix matching without SQL LIKE wildcards or stale PID assumptions.
                prefix = 'collab:' + identifier + ':'
                rows = db.execute('SELECT * FROM sessions WHERE substr(task_id,1,?)=? ORDER BY started_at',
                                  (len(prefix), prefix)).fetchall()
            elif session_id:
                rows = db.execute('SELECT * FROM sessions WHERE id=? AND task_id=?', (session_id, identifier)).fetchall()
            else:
                rows = db.execute('SELECT * FROM sessions WHERE task_id=? AND model=? ORDER BY started_at DESC LIMIT 1',
                                  (identifier, model)).fetchall()
            history = [session_view(row) for row in rows]
            events = []
            for row in rows:
                events.extend(dict(event) for event in db.execute(
                    'SELECT seq,session_id,at,from_state,to_state FROM events WHERE session_id=? ORDER BY seq', (row['id'],)))
        return {'history': history, 'events': events}

    @staticmethod
    def usage_projection(raw):
        # Projection in SQLite avoids fetching generated text into summary polling.
        return ("CASE WHEN json_valid(" + raw + ") THEN json_object("
                "'usage',json_extract(" + raw + ",'$.usage'),"
                "'eval_count',json_extract(" + raw + ",'$.eval_count'),"
                "'prompt_eval_count',json_extract(" + raw + ",'$.prompt_eval_count'),"
                "'eval_duration',json_extract(" + raw + ",'$.eval_duration')) ELSE NULL END")

    def progress_for(self, key, identifier, kind):
        if kind == 'research':
            with read_database(self.path(key)) as db:
                fields = 'id,state,model_identifier,started_at,completed_at,max_tokens,lifecycle_session_id,'
                # Remove absent-provider keys: NULL Ollama keys must not hide Prism usage.
                row = db.execute('SELECT ' + fields + self.usage_projection('raw_response_json') +
                                 ' AS usage_json FROM tasks WHERE id=?', (identifier,)).fetchone()
                job = dict(row)
                usage = json.loads(job.pop('usage_json') or '{}')
                job['raw_response_json'] = {k: v for k, v in usage.items() if v is not None}
            evidence = self.session_evidence(key, identifier, job.get('lifecycle_session_id'), job['model_identifier'])
            return research_progress(job, next(iter(evidence['history']), None), evidence['events'])
        with read_database(self.path(key)) as db:
            run = dict(db.execute('SELECT id,state,started_at,completed_at,cancel_requested,'
                                 'json_extract(spec_json,"$.limits") AS limits_json FROM collaboration_runs WHERE id=?',
                                 (identifier,)).fetchone())
            run['spec'] = {'limits': json.loads(run.pop('limits_json'))}
            raw = "json_extract(data_json,'$.raw_transport_json')"
            projection = self.usage_projection(raw)
            columns = "json_object('call',json_extract(data_json,'$.call'),'attempt',json_extract(data_json,'$.attempt'),"                       "'purpose',json_extract(data_json,'$.purpose'),'role',json_extract(data_json,'$.role'),"                       "'model',json_extract(data_json,'$.model.identifier'),'label',json_extract(data_json,'$.label'),"                       "'session_id',json_extract(data_json,'$.session_id'),'error',json_extract(data_json,'$.error'),"                       "'cleanup_error',json_extract(data_json,'$.cleanup_error'),'lifecycle',json_extract(data_json,'$.lifecycle'),"                       "'raw_transport_json',json(" + projection + "))"
            events = [dict(r) for r in db.execute('SELECT seq,at,kind,' + columns +
                         ' AS data_json FROM collaboration_events WHERE run_id=? ORDER BY seq', (identifier,))]
            for event in events:
                data = json.loads(event.pop('data_json'))
                usage = data.get('raw_transport_json')
                if isinstance(usage, dict):
                    data['raw_transport_json'] = {k: v for k, v in usage.items() if v is not None}
                event['data'] = data
        return collaboration_progress(run, events, self.session_evidence(key, identifier, collaboration=True))

    def research_detail(self, key, identifier):
        with read_database(self.path(key)) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone() if table_exists(db, 'tasks') else None
            if not row:
                raise APIError('Job not found.', 404)
            job = dict(row)
            # Full responses stay available, while executable configuration is not an API capability.
            job.pop('registry_json', None)
        session = None
        path = self.resources / 'lifecycle.sqlite3'
        if key == 'research' and path.exists():
            with read_database(path) as db:
                if job.get('lifecycle_session_id'):
                    row = db.execute('SELECT * FROM sessions WHERE id=? AND task_id=?', (job['lifecycle_session_id'], identifier)).fetchone()
                else:
                    row = db.execute('SELECT * FROM sessions WHERE task_id=? AND model=? ORDER BY started_at DESC LIMIT 1', (identifier, job['model_identifier'])).fetchone()
                session = session_view(row)
        evidence = self.session_evidence(key, identifier, job.get('lifecycle_session_id'), job['model_identifier'])
        return {'database': key, 'job': job, 'lifecycle': session, 'stage_history': evidence['events'],
                'progress': research_progress(job, session, evidence['events'])}

    def collaboration_detail(self, key, identifier):
        # Query-only projection of the existing immutable evidence tables.
        with read_database(self.path(key)) as db:
            run = decode(db.execute('SELECT * FROM collaboration_runs WHERE id=?', (identifier,)).fetchone()) if table_exists(db, 'collaboration_runs') else None
            if not run:
                raise APIError('Collaboration not found.', 404)
            events = [decode(r) for r in db.execute('SELECT * FROM collaboration_events WHERE run_id=? ORDER BY seq', (identifier,))]
            revisions = [decode(r) for r in db.execute('SELECT * FROM collaboration_revisions WHERE run_id=? ORDER BY rowid', (identifier,))]
        # Avoid duplicating large raw JSON alongside its decoded representation.
        for row in [run] + events + revisions:
            for name in tuple(row):
                if name.endswith('_json'):
                    del row[name]
        evidence = self.session_evidence(key, identifier, collaboration=True)
        return {'database': key, 'run': run, 'events': events, 'revisions': revisions,
                'stage_history': evidence['events'], 'progress': collaboration_progress(run, events, evidence)}

    def submit_job(self, payload):
        checked_object(payload, ('model_identifier', 'prompt', 'name', 'scheduled_at', 'max_tokens', 'timeout_seconds'), ('model_identifier', 'prompt'))
        entries = [e for e in self.registry.entries if e.identifier == payload['model_identifier']]
        if len(entries) != 1:
            raise APIError('Select an exact configured model identifier.')
        entry = entries[0]
        if not isinstance(payload['prompt'], str) or (payload.get('name') is not None and not isinstance(payload['name'], str)):
            raise APIError('Prompt and optional name must be text.')
        tokens = payload.get('max_tokens', entry.max_tokens)
        timeout = payload.get('timeout_seconds', entry.timeout_seconds)
        if type(tokens) is not int or not 1 <= tokens <= entry.max_tokens:
            raise APIError(f'max_tokens must be an integer from 1 to {entry.max_tokens}.')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= entry.timeout_seconds:
            raise APIError(f'timeout_seconds must be positive and at most {entry.timeout_seconds}.')
        scheduled = self._scheduled(payload.get('scheduled_at'))
        with QueueStore(self.path('research', write=True)) as store:
            identifier = store.enqueue(model_family=entry.family, model_identifier=entry.identifier,
                endpoint=entry.endpoint, prompt=payload['prompt'], name=payload.get('name'), scheduled_at=scheduled,
                max_tokens=tokens, timeout_seconds=timeout, registry_json=entry.snapshot_json())
        return {'database': 'research', 'id': identifier, 'kind': 'research'}

    @staticmethod
    def _scheduled(value):
        if value is None or value == '':
            return utc_now()
        if not isinstance(value, str) or len(value) > 64:
            raise APIError('scheduled_at must be a timezone-aware ISO timestamp.')
        return normalize_time(value)

    def submit_collaboration(self, payload):
        checked_object(payload, ('spec', 'scheduled_at'), ('spec',))
        spec = normalize_spec(payload['spec'])
        models = {}
        for family in ('qwen', 'bonsai'):
            entries = [e for e in self.registry.entries if e.family == family]
            if len(entries) != 1:
                raise APIError('Collaboration requires one configured Qwen and one configured Bonsai.')
            models[family] = entries[0].export()
        with CollaborationStore(self.path('research', write=True)) as store:
            identifier = store.enqueue(spec, models, self._scheduled(payload.get('scheduled_at')))
        return {'database': 'research', 'id': identifier, 'kind': 'collaboration'}

    def cancel(self, kind, key, identifier, payload):
        checked_object(payload, ())
        path = self.path(key, write=True)
        klass = QueueStore if kind == 'jobs' else CollaborationStore
        with klass(path) as store:
            row = store.get(identifier)
            if row is None:
                raise APIError('Record not found.', 404)
            if row['state'] != 'cancelled' and not row.get('cancel_requested') and not store.cancel(identifier):
                raise APIError('This job is no longer eligible for cancellation.', 409)
            row = store.get(identifier)
        return {'id': identifier, 'state': row['state'], 'cancel_requested': bool(row.get('cancel_requested'))}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, application):
        if address[0] != '127.0.0.1':
            raise ValueError('The web application must bind to IPv4 loopback.')
        self.application = application
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = 'CommandCenter'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass  # Do not log request bodies, prompts, cookies or arbitrary request targets.

    def send_payload(self, value, status=200, content_type='application/json; charset=utf-8', extra=None):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if content_type.startswith('application/json') else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _guard(self, mutation=False):
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        host = self.headers.get('Host', '')
        if len(self.headers.get_all('Host', [])) != 1 or host not in hosts:
            raise APIError('Untrusted Host header.', 403)
        origin = self.headers.get('Origin')
        if origin is not None and origin != 'http://' + host:
            raise APIError('Requests must originate from this application.', 403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise APIError('Cross-site requests are not allowed.', 403)
        if mutation:
            if origin != 'http://' + host:
                raise APIError('A same-origin request is required.', 403)
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except Exception:
                raise APIError('Invalid application session.', 403)
            saved = cookie.get('cc_session')
            token = self.headers.get('X-CSRF-Token', '')
            expected = self.server.application.token
            if not saved or not secrets.compare_digest(saved.value, expected) or not secrets.compare_digest(token, expected):
                raise APIError('Application session expired or CSRF token missing.', 403)

    def _body(self):
        if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
            raise APIError('Use a bounded application/json request.')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise APIError('A valid Content-Length is required.')
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            raise APIError('Request exceeds the 128 KiB limit.', 413)
        body = self.rfile.read(length)
        if len(body) != length:
            raise APIError('Incomplete request body.')
        try:
            def constant(_):
                raise ValueError('Non-finite JSON number.')
            def pairs(items):
                value = {}
                for key, val in items:
                    if key in value:
                        raise ValueError('Duplicate JSON field.')
                    value[key] = val
                return value
            return json.loads(body.decode('utf-8'), parse_constant=constant, object_pairs_hook=pairs)
        except (ValueError, UnicodeError, RecursionError):
            raise APIError('Malformed JSON request.')

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def do_PUT(self):
        self.send_payload({'error': 'Method not supported.'}, 405)

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    def _dispatch(self, mutation):
        try:
            self._guard(mutation)
            app = self.server.application
            path = unquote(urlsplit(self.path).path)
            if any(part in ('.', '..') for part in path.split('/')) or '\\' in path:
                raise APIError('Route not found.', 404)
            if not mutation:
                static = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/static/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/static/styles.css': ('styles.css', 'text/css; charset=utf-8')}
                if path in static:
                    filename, mime = static[path]
                    return self.send_payload((HERE / 'web_static' / filename).read_bytes(), content_type=mime)
                if path == '/favicon.ico':
                    return self.send_payload(b'', 204, 'image/x-icon')
                if path == '/api/bootstrap':
                    return self.send_payload(app.bootstrap(), extra={'Set-Cookie':
                        'cc_session=' + app.token + '; Path=/; HttpOnly; SameSite=Strict'})
                if path == '/api/state':
                    return self.send_payload(app.state())
                match = re.fullmatch(r'/api/(jobs|collaborations)/([a-z]+)/([^/]{1,160})', path)
                export = re.fullmatch(r'/api/export/(research|collaboration)/([a-z]+)/([^/]{1,160})', path)
                if match or export:
                    kind, key, identifier = (match or export).groups()
                    data = app.research_detail(key, identifier) if kind in ('jobs', 'research') else app.collaboration_detail(key, identifier)
                    filename = re.sub(r'[^A-Za-z0-9_-]', '_', identifier)[:100] + '.json'
                    return self.send_payload(data, extra={'Content-Disposition': f'attachment; filename="{filename}"'} if export else None)
            else:
                payload = self._body()
                if path == '/api/jobs':
                    return self.send_payload(app.submit_job(payload), 201)
                if path == '/api/collaborations':
                    return self.send_payload(app.submit_collaboration(payload), 201)
                match = re.fullmatch(r'/api/(jobs|collaborations)/([a-z]+)/([^/]{1,160})/cancel', path)
                if match:
                    return self.send_payload(app.cancel(*match.groups(), payload))
            raise APIError('Route not found.', 404)
        except APIError as exc:
            self.send_payload({'error': str(exc)}, exc.status)
        except ValueError as exc:
            self.send_payload({'error': str(exc)}, 400)
        except (sqlite3.Error, OSError):
            self.send_payload({'error': 'Local application data is temporarily unavailable. Retry after checking the application.'}, 503)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send_payload({'error': 'Unable to process this application request.'}, 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--resources-dir', type=Path, default=DEFAULT_RESOURCES)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('port must be in 0..65535')
    app = WebApplication(args.db, args.config, args.resources_dir)
    try:
        server = LocalServer(('127.0.0.1', args.port), app)
    except OSError:
        app.close()
        parser.exit(1, 'Local port unavailable. Choose another port with --port; existing services were left running.\n')
    print(f'Command Center: http://127.0.0.1:{server.server_port}\nWeb interface only; the existing independent worker executes queued jobs.', flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == '__main__':
    main()
