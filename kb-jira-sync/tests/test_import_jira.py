#!/usr/bin/env python3
"""Tests for import-jira.py — pure rendering and credential resolution.

No credentials, no network, no vault: the script is loaded as a module with a
throwaway vault fixture so its import-time bootstrap (root walk + config read)
can be exercised, then its pure helpers are tested directly.
"""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import-jira.py"


def load_module(vault: Path):
    """Import import-jira.py with its bootstrap pointed at `vault`.

    The module resolves its root from the process cwd at import time, so the
    caller chdir()s into the fixture first. Importing under a unique name keeps
    the two fixture-based tests from sharing module state.
    """
    spec = importlib.util.spec_from_file_location(f"import_jira_{id(vault)}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VaultCase(unittest.TestCase):
    def setUp(self):
        import os

        self.vault = Path(tempfile.mkdtemp(prefix="import-jira-test-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        (self.vault / "kb-config.yaml").write_text(
            "jira:\n"
            "  enabled: true\n"
            "  base_url: https://example.atlassian.net\n"
            "  project_key: PROJ\n"
            "  output_dir: .raw/jira\n",
            encoding="utf-8",
        )
        self._cwd = Path.cwd()
        os.chdir(self.vault)

    def tearDown(self):
        import os

        os.chdir(self._cwd)
        shutil.rmtree(self.vault, ignore_errors=True)


class RenderTest(VaultCase):
    """The ADF renderer is a pure function of the API payload."""

    def test_adf_converts_blocks_to_markdown(self):
        mod = load_module(self.vault)
        adf = {
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 2}, "content": [
                    {"type": "text", "text": "Scope"}]},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "First line."}]},
                {"type": "bulletList", "content": [
                    {"type": "listItem", "content": [
                        {"type": "paragraph", "content": [
                            {"type": "text", "text": "bullet one"}]}]},
                ]},
            ],
        }
        out = mod.adf_to_markdown(adf)
        self.assertIn("## Scope", out)
        self.assertIn("First line.", out)
        self.assertIn("- bullet one", out)

    def test_plain_string_payload_passes_through(self):
        mod = load_module(self.vault)
        self.assertEqual(mod.adf_to_markdown("already markdown"), "already markdown")
        self.assertEqual(mod.adf_to_markdown(None), "")

    def test_issue_markdown_renders_fields_and_table(self):
        mod = load_module(self.vault)
        issue = {
            "key": "PROJ-7",
            "fields": {
                "summary": "Broken connector",
                "issuetype": {"name": "Bug"},
                "status": {"name": "Open"},
                "priority": {"name": "High"},
                "assignee": None,
                "reporter": {"displayName": "Ana"},
                "created": "2026-01-02T03:04:05.000+0000",
                "updated": "2026-01-03T03:04:05.000+0000",
                "duedate": None,
                "project": {"key": "PROJ", "name": "Project"},
                "parent": None,
                "description": {"type": "doc", "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "It fails."}]}]},
                "labels": ["ops"],
                "subtasks": [],
                "issuelinks": [],
            },
        }
        md = mod.format_issue_markdown(issue, [])
        self.assertTrue(md.startswith("# PROJ-7: Broken connector"))
        self.assertIn("| Assignee | Unassigned |", md)
        self.assertIn("https://example.atlassian.net/browse/PROJ-7", md)
        self.assertIn("It fails.", md)
        self.assertIn("No comments", md)


class CredentialTest(VaultCase):
    """ATLASSIAN_* is canonical; the one-S misspelling warns and still works."""

    def test_canonical_spelling_wins(self):
        import os

        os.environ["ATLASSIAN_EMAIL"] = "canonical@example.com"
        os.environ["ATLASIAN_EMAIL"] = "legacy@example.com"
        try:
            mod = load_module(self.vault)
            self.assertEqual(mod.credential("EMAIL"), "canonical@example.com")
        finally:
            os.environ.pop("ATLASSIAN_EMAIL", None)
            os.environ.pop("ATLASIAN_EMAIL", None)

    def test_legacy_spelling_still_resolves(self):
        import os

        os.environ["ATLASIAN_API_KEY"] = "legacy-token"
        try:
            mod = load_module(self.vault)
            self.assertEqual(mod.credential("API_KEY"), "legacy-token")
        finally:
            os.environ.pop("ATLASIAN_API_KEY", None)

    def test_missing_credential_is_empty(self):
        import os

        os.environ.pop("ATLASSIAN_EMAIL", None)
        os.environ.pop("ATLASIAN_EMAIL", None)
        mod = load_module(self.vault)
        self.assertEqual(mod.credential("EMAIL"), "")


class ConfigTest(VaultCase):
    def test_macro_expansion_reads_the_environment(self):
        self.vault.joinpath("kb-config.yaml").write_text(
            "jira:\n"
            "  enabled: true\n"
            "  base_url: ${JIRA_BASE:-https://fallback.atlassian.net}\n"
            "  project_key: PROJ\n",
            encoding="utf-8",
        )
        mod = load_module(self.vault)
        self.assertEqual(mod.JIRA_BASE, "https://fallback.atlassian.net")

    def test_path_escaping_is_rejected(self):
        mod = load_module(self.vault)
        with self.assertRaises(SystemExit):
            mod.vault_path(self.vault, "../outside", "jira.output_dir")

    def test_cli_flags_reject_unknown_and_accept_documented_flags(self):
        mod = load_module(self.vault)
        with self.assertRaises(SystemExit):
            mod.main(["--bogus"])
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["--render-only", "--full", "--debug"])
        self.assertIn("--render-only", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
