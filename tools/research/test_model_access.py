import unittest
from dataclasses import replace
from model_access import local_endpoint, model_access
from registry import Registry
from web import DEFAULT_CONFIG


class ModelAccessTests(unittest.TestCase):
    def test_exact_registered_local_endpoints(self):
        for entry in Registry(DEFAULT_CONFIG).entries:
            self.assertEqual(local_endpoint(entry.endpoint), entry.endpoint)
            access = model_access(entry)
            self.assertEqual(access['endpoint_url'], entry.endpoint)
            self.assertEqual(access['inspection_url'],entry.endpoint+'/models')
            self.assertTrue(access['details_url'].startswith('#/models/registered/'))
            self.assertIsNone(access['browser_url'])

    def test_no_dashboard_is_fabricated(self):
        qwen, bonsai = Registry(DEFAULT_CONFIG).entries
        self.assertIn('not a model dashboard', model_access(qwen)['reason'])
        self.assertIn('--no-webui', model_access(bonsai)['reason'])
        self.assertIn('No browser interface has been verified', model_access(replace(bonsai, startup_command=()))['reason'])

    def test_reject_external_credentials_redirect_style_or_invalid_urls(self):
        for value in ('https://127.0.0.1/v1', 'http://example.com/v1', 'http://localhost:8080/v1',
                      'http://127.0.0.1@evil.test/v1', 'http://user:secret@127.0.0.1/v1',
                      'http://127.0.0.1:8080/v1?url=evil', 'http://127.0.0.1:8080/v1#x',
                      '//127.0.0.1/v1', 'http://127.0.0.1:99999/v1', 'http://127.0.0.1/private',
                      ' http://127.0.0.1/v1', 'http://127.0.0.1:80/v1\n', None):
            with self.subTest(value=value): self.assertIsNone(local_endpoint(value))
        self.assertEqual(local_endpoint('http://[::1]:8080/v1'), 'http://[::1]:8080/v1')

    def test_identifier_is_encoded_and_invalid_endpoint_is_never_opened(self):
        entry = replace(Registry(DEFAULT_CONFIG).entries[0],identifier='model/name#fragment',endpoint='http://evil.test/v1')
        access = model_access(entry)
        self.assertEqual(access['details_url'], '#/models/registered/model%2Fname%23fragment')
        self.assertIsNone(access['endpoint_url'])
        self.assertIsNone(access['inspection_url'])
