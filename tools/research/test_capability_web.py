from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote
from unittest.mock import patch

from web import DEFAULT_CONFIG, LocalServer, WebApplication


class CapabilityWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        skill_dir = self.root / "skills" / "bounded"
        skill_dir.mkdir(parents=True)
        self.skill_path = skill_dir / "SKILL.md"
        self.skill_path.write_text("---\nname: bounded\ndescription: Inspect bounded fixtures.\n---\n\n# Guidance\nRead evidence before deciding.\n", encoding="utf-8")
        common = {
            "primary_use_cases": [], "secondary_use_cases": [], "when_to_use": [],
            "when_not_to_use": [], "supported_operations": [], "configuration_status": "DISCOVERED",
            "required_dependencies": [], "resource_requirements": [], "side_effects": [],
            "security_constraints": [], "provenance_requirements": [], "relevant_projects": [],
            "relevant_workflow_phases": [], "version_or_source": "fixture", "last_verified": "2026-10-13T12:00:00+00:00",
            "source_paths": [], "trusted_roots": [], "documentation_path": "", "callable": False,
        }
        self.records = [
            {**common, "id": "skill:bounded#guide", "name": "Bounded guide", "kind": "SKILL",
             "description": "Inspect bounded fixtures.", "availability": "CONFIGURED",
             "execution_transport": "SKILL_INSTRUCTIONS", "instruction_access": True,
             "when_to_use": ["When evidence needs inspection"], "when_not_to_use": ["Never as permission"],
             "documentation_path": str(self.skill_path), "source_paths": [str(self.skill_path)],
             "trusted_roots": [str(self.root / "skills")]},
            {**common, "id": "mcp:fixture", "name": "Fixture MCP", "kind": "MCP_SERVER",
             "description": "Configured schema fixture.", "availability": "READY", "callable": True,
             "execution_transport": "MCP_STDIO", "permitted_operations": ["list_projects"],
             "mcp_tools": [{"name": "list_projects", "description": "List identities",
                            "inputSchema": {"type": "object", "properties": {}}},
                           {"name": "delete_project", "description": "Not permitted",
                            "inputSchema": {"type": "object", "properties": {}}}]},
            {**common, "id": "cli:missing", "name": "Missing CLI", "kind": "CLI_TOOL",
             "description": "Unavailable executable.", "availability": "UNAVAILABLE",
             "configuration_status": "UNAVAILABLE", "execution_transport": "UNAVAILABLE"},
        ]
        self.app = WebApplication(self.root / "queue.sqlite3", DEFAULT_CONFIG, self.root / "resources",
                                  history_db=None, probe_enabled=False, capability_records=self.records)
        self.server = LocalServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.app.close(); self.temp.cleanup()

    def request(self, path, method="GET", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse(); raw = response.read(); headers = dict(response.getheaders())
        connection.close()
        content_type = headers.get("Content-Type", "")
        value = json.loads(raw) if raw and "application/json" in content_type else raw
        return response.status, headers, value

    def test_catalog_is_lazy_and_page_is_bounded(self):
        self.assertIsNone(self.app._capability_catalog)
        self.assertEqual(self.request("/api/state")[0], 200)
        self.assertIsNone(self.app._capability_catalog)
        status, _, value = self.request("/api/capabilities?limit=1&offset=0")
        self.assertEqual((status, value["total"], len(value["items"])), (200, 3, 1))
        self.assertIsNotNone(self.app._capability_catalog)

    def test_server_filters_kind_status_and_query(self):
        status, _, value = self.request("/api/capabilities?kind=SKILL&availability=CONFIGURED&query=evidence")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in value["items"]], ["skill:bounded#guide"])
        status, _, value = self.request("/api/capabilities?availability=UNAVAILABLE")
        self.assertEqual((status, value["items"][0]["id"]), (200, "cli:missing"))

    def test_query_validation_rejects_unknown_duplicate_and_bad_paging(self):
        paths = ("/api/capabilities?other=x", "/api/capabilities?kind=SKILL&kind=CLI_TOOL",
                 "/api/capabilities?kind=wrong", "/api/capabilities?availability=wrong",
                 "/api/capabilities?offset=-1", "/api/capabilities?limit=101",
                 "/api/capabilities?offset=nan")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)

    def test_detail_preserves_mcp_schemas_and_permission_subset(self):
        status, _, value = self.request("/api/capabilities/mcp%3Afixture")
        self.assertEqual(status, 200)
        record = value["capability"]
        self.assertEqual(record["permitted_operations"], ["list_projects"])
        self.assertEqual([tool["name"] for tool in record["mcp_tools"]], ["list_projects", "delete_project"])

    def test_skill_id_encoding_and_trusted_instruction_read(self):
        identifier = quote("skill:bounded#guide", safe="")
        self.assertEqual(self.request(f"/api/capabilities/{identifier}")[0], 200)
        status, _, value = self.request(f"/api/capabilities/{identifier}/instructions")
        self.assertEqual(status, 200)
        self.assertIn("Read evidence before deciding", value["content"])
        self.assertEqual(value["capability"]["id"], "skill:bounded#guide")

    def test_non_skill_unavailable_and_path_escape_have_no_instruction_access(self):
        for identifier in ("cli:missing", "mcp:fixture"):
            self.assertEqual(self.request(f"/api/capabilities/{quote(identifier, safe='')}/instructions")[0], 404)
        self.assertEqual(self.request("/api/capabilities/skill%3A..%2Fsecret/instructions")[0], 404)
        self.assertEqual(self.request("/api/capabilities/not-found")[0], 404)

    def test_capability_routes_never_dispatch_or_reconcile_models(self):
        with patch("web.Manager.execute", side_effect=AssertionError("must not execute")), \
             patch("web.Manager.reconcile", side_effect=AssertionError("must not reconcile")):
            status, _, value = self.request("/api/capabilities")
            self.assertEqual(status, 200, value)
            self.assertEqual(self.request("/api/capabilities/mcp%3Afixture")[0], 200)
        # Mutation guard rejects the request before routing; there is no POST capability endpoint.
        self.assertEqual(self.request("/api/capabilities/mcp%3Afixture", method="POST", body=b"{}")[0], 403)

    def test_static_application_exposes_safe_capability_route(self):
        status, headers, _ = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", "/static/app.js", headers={"Host": f"127.0.0.1:{self.server.server_port}"})
        response = connection.getresponse(); source = response.read().decode(); connection.close()
        self.assertIn("Skills & Tools", source)
        self.assertIn("encodeURIComponent(item.id)", source)
        self.assertNotIn("innerHTML", source)


if __name__ == "__main__":
    unittest.main()
