from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import capabilities
from capabilities import build_catalog, read_skill, route_capabilities


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skills = self.root / "skills"
        self.skills.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_skill(self, folder, text):
        path = self.skills / folder / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        return path

    def catalog(self, **kwargs):
        return build_catalog(repo_root=self.root, extra_skill_roots=[self.skills],
                             include_defaults=False, include_cli=False,
                             include_services=False, **kwargs)

    def test_frontmatter_and_content_derive_structured_skill(self):
        path = self.write_skill("media", """---
name: media-inspection
description: Inspect supplied media before editing.
version: 2.1
---
# Media
Use this workflow to inspect real source files and retain evidence.
## When to use
- When source duration and streams must be verified
## When not to use
- Do not infer metadata from filenames
""")
        catalog = self.catalog()
        self.assertEqual(len(catalog), 1)
        record = catalog[0]
        self.assertEqual(record["id"], "skill:media-inspection")
        self.assertEqual(record["description"], "Inspect supplied media before editing.")
        self.assertEqual(record["kind"], "SKILL")
        self.assertEqual(record["execution_transport"], "SKILL_INSTRUCTIONS")
        self.assertEqual(record["availability"], "CONFIGURED")
        self.assertIn(str(path.resolve()), record["source_paths"])
        self.assertIn("source duration", " ".join(record["when_to_use"]))

    def test_missing_description_uses_substantive_markdown_not_name(self):
        self.write_skill("observe", "# Observe\n\nInspect real payloads and preserve bounded evidence before making decisions.\n")
        record = self.catalog()[0]
        self.assertIn("Inspect real payloads", record["description"])
        self.assertNotEqual(record["description"].lower(), record["name"].lower())

    def test_empty_name_only_document_is_catalogued_with_honest_description(self):
        self.write_skill("empty", "# Empty\n")
        record = self.catalog()[0]
        self.assertIn("no substantive description", record["description"])

    def test_symlink_escape_is_rejected(self):
        outside = self.root / "outside.md"
        outside.write_text("---\nname: escaped\ndescription: Must not be read.\n---\n", encoding="utf-8")
        link = self.skills / "escape" / "SKILL.md"
        link.parent.mkdir()
        link.symlink_to(outside)
        diagnostics = {}
        catalog = build_catalog(repo_root=self.root, extra_skill_roots=[self.skills], include_defaults=False,
                                include_cli=False, include_services=False, diagnostics=diagnostics)
        self.assertEqual(catalog, [])
        self.assertEqual(diagnostics["symlink_escape"], 1)

    def test_oversized_skill_is_inventory_only_and_full_read_refused(self):
        self.write_skill("large", "---\nname: large\ndescription: bounded\n---\n" + "x" * (70 * 1024))
        catalog = self.catalog()
        self.assertEqual(catalog[0]["read_availability"], "BOUNDED_PREVIEW_ONLY")
        with self.assertRaises(ValueError):
            read_skill("skill:large", catalog)

    def test_aliases_deduplicate_with_all_source_paths(self):
        second = self.root / "other"
        second.mkdir()
        content = "---\nname: shared\ndescription: Shared documented workflow.\n---\n"
        self.write_skill("first", content)
        path = second / "second" / "SKILL.md"; path.parent.mkdir(); path.write_text(
            content, encoding="utf-8")
        catalog = build_catalog(repo_root=self.root, extra_skill_roots=[self.skills, second],
                                include_defaults=False, include_cli=False, include_services=False)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(len(catalog[0]["source_paths"]), 2)

    def test_same_name_different_documentation_has_digest_ids(self):
        second = self.root / "other"; second.mkdir()
        self.write_skill("first", "---\nname: app-planner\ndescription: Plan an iOS app.\n---\n")
        path = second / "second" / "SKILL.md"; path.parent.mkdir(); path.write_text(
            "---\nname: app-planner\ndescription: Plan a macOS app.\n---\n", encoding="utf-8")
        records = build_catalog(repo_root=self.root, extra_skill_roots=[self.skills, second],
                                include_defaults=False, include_cli=False, include_services=False)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["id"].startswith("skill:app-planner:") for record in records))

    def test_read_skill_returns_bounded_documentation(self):
        self.write_skill("reader", "---\nname: reader\ndescription: Read documented input.\n---\n\nBody text.\n")
        catalog = self.catalog()
        value = read_skill("skill:reader", catalog)
        self.assertIn("Body text", value["content"])
        self.assertEqual(value["capability"]["id"], "skill:reader")
        with self.assertRaises(ValueError):
            read_skill("cli:reader", catalog)

    def test_route_is_bounded_and_prefers_name_match(self):
        self.write_skill("video", "---\nname: video-editing\ndescription: Edit supplied video and audio.\n---\n")
        self.write_skill("audio", "---\nname: audio-notes\ndescription: Write notes about audio.\n---\n")
        catalog = self.catalog()
        routed = route_capabilities("edit this video", "media", "qwen", catalog, limit=1)
        self.assertEqual(routed[0]["id"], "skill:video-editing")
        self.assertNotIn("source_paths", routed[0])

    def test_mcp_config_exposes_only_server_name_and_not_values(self):
        config = self.root / "config.toml"
        secret = "do-not-expose-token"
        config.write_text('[mcp_servers.example]\ncommand="server"\nenv={TOKEN="' + secret + '"}\n', encoding="utf-8")
        catalog = self.catalog(config_paths=[config])
        record = next(item for item in catalog if item["id"] == "mcp:example")
        self.assertEqual(record["kind"], "MCP_SERVER")
        self.assertEqual(record["execution_transport"], "AGENT_ONLY")
        self.assertNotIn(secret, json.dumps(record))
        self.assertEqual(record["configuration_status"], "CONFIGURED")

    def test_json_mcp_names_are_bounded_and_not_probed(self):
        config = self.root / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"files": {"command": "never-run"}}}), encoding="utf-8")
        record = next(item for item in self.catalog(config_paths=[config]) if item["kind"] == "MCP_SERVER")
        self.assertEqual(record["name"], "files")
        self.assertEqual(record["supported_operations"], [])

    def test_video_metadata_is_explicit_and_conservative(self):
        self.write_skill("video", "---\nname: video-editing\ndescription: Edit arbitrary supplied video into a polished deliverable.\n---\n")
        record = self.catalog()[0]
        self.assertIn("validate deliverable", record["supported_operations"])
        self.assertTrue(any("publish" in item for item in record["when_not_to_use"]))
        self.assertTrue(any("checksum" in item for item in record["provenance_requirements"]))

    def test_cli_inventory_includes_unavailable_and_rejects_broken_probe(self):
        with mock.patch.object(capabilities.shutil, "which", return_value=None):
            missing = capabilities._cli_records()
        self.assertEqual(set(capabilities.CLI_SPECS), {item["name"] for item in missing})
        self.assertTrue(all(item["kind"] == "CLI_TOOL" for item in missing))
        self.assertTrue(all(item["execution_transport"] == "UNAVAILABLE" for item in missing))
        with mock.patch.object(capabilities.shutil, "which", return_value="/bin/tool"), \
                mock.patch.object(capabilities, "_version", return_value=None):
            broken = capabilities._cli_records()
        ffprobe = next(item for item in broken if item["name"] == "ffprobe")
        self.assertEqual(ffprobe["availability"], "UNVERIFIED")

    def test_remotion_roles_are_distinct(self):
        self.write_skill("create", "---\nname: remotion-create\ndescription: Create a Remotion composition.\n---\n")
        self.write_skill("captions", "---\nname: remotion-captions\ndescription: Add captions to Remotion video.\n---\n")
        records = {item["name"]: item for item in self.catalog()}
        self.assertNotEqual(records["remotion-create"]["supported_operations"],
                            records["remotion-captions"]["supported_operations"])
        self.assertNotEqual(records["remotion-create"]["relevant_workflow_phases"],
                            records["remotion-captions"]["relevant_workflow_phases"])


if __name__ == "__main__":
    unittest.main()
