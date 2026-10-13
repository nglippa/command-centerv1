"""Resolve operator links from registered loopback endpoints, never user URLs."""
import ipaddress
import hashlib
from urllib.parse import quote, urlsplit


def local_endpoint(endpoint):
    if not isinstance(endpoint, str) or endpoint != endpoint.strip() or any(ord(c) < 32 for c in endpoint):
        return None
    try:
        value = urlsplit(endpoint)
        port = value.port
        if (value.scheme != 'http' or not ipaddress.ip_address(value.hostname or '').is_loopback
                or value.username or value.password or value.query or value.fragment
                or value.path not in ('', '/v1') or port is not None and not 1 <= port <= 65535):
            return None
    except (ValueError, TypeError):
        return None
    return endpoint


def model_access(entry):
    endpoint = local_endpoint(entry.endpoint)
    reason = ('Ollama exposes an inference API and a plain service-status page, not a model dashboard.'
              if entry.provider == 'ollama' else
              'The configured Prism runtime disables its browser UI (--no-webui).'
              if '--no-webui' in entry.startup_command else
              'No browser interface has been verified for the configured local runtime.')
    inspection = (endpoint + '/models' if urlsplit(endpoint).path == '/v1' else endpoint.rstrip('/') + '/v1/models') if endpoint else None
    return {'kind': 'command_center_details', 'browser_url': None,
            'inspection_url': inspection,
            'details_url': '#/models/registered/' + quote(entry.identifier, safe=''),
            'workspace_url': '#/models/' + hashlib.sha256(entry.endpoint.encode()).hexdigest()[:20] + '/' + quote(entry.identifier, safe=''),
            'endpoint_workspace_url': '#/endpoints/' + hashlib.sha256(entry.endpoint.encode()).hexdigest()[:20],
            'endpoint_url': endpoint, 'reason': reason}
