#!/usr/bin/env python3
"""The frontmatter gate rejects legacy overloaded status and accepts split fields."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "lint-frontmatter.py"


class FrontmatterGateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lint-frontmatter-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki" / "concepts").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_page(self, fields: str):
        (self.root / "wiki" / "concepts" / "page.md").write_text(
            f"---\n{fields}---\n\n# Page\n", encoding="utf-8"
        )

    def run_lint(self):
        return subprocess.run(
            ["python3", str(SCRIPT)],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def base_fields(self):
        return (
            "type: concept\ntitle: Page\ncreated: 2026-10-31\nupdated: 2026-10-31\n"
            "tags:\n  - test\n"
        )

    def test_fault_missing_verification_fails_gate(self):
        self.write_page(self.base_fields() + "maturity: developing\n")
        result = self.run_lint()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing: verification", result.stdout)

    def test_legacy_status_does_not_satisfy_split_fields(self):
        self.write_page(self.base_fields() + "status: verified\n")
        result = self.run_lint()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("maturity", result.stdout)
        self.assertIn("verification", result.stdout)

    def test_maturity_and_verification_pass(self):
        self.write_page(
            self.base_fields() + "maturity: developing\nverification: supported\n"
        )
        result = self.run_lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_empty_scalar_field_fails_gate(self):
        self.write_page(
            "type:\ntitle: Page\ncreated: 2026-10-31\nupdated: 2026-10-31\n"
            "tags:\n  - test\nmaturity: developing\nverification: supported\n"
        )
        result = self.run_lint()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing: type", result.stdout)

    def test_empty_tags_list_fails_gate(self):
        self.write_page(
            "type: concept\ntitle: Page\ncreated: 2026-10-31\nupdated: 2026-10-31\n"
            "tags: []\nmaturity: developing\nverification: supported\n"
        )
        result = self.run_lint()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing: tags", result.stdout)

    def test_invalid_maturity_enum_fails_gate(self):
        self.write_page(
            self.base_fields() + "maturity: draft\nverification: supported\n"
        )
        result = self.run_lint()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid maturity 'draft'", result.stdout)

    def test_invalid_verification_enum_fails_gate(self):
        self.write_page(
            self.base_fields() + "maturity: developing\nverification: verified\n"
        )
        result = self.run_lint()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid verification 'verified'", result.stdout)

    def test_decision_requires_valid_decision_status(self):
        self.write_page(
            "type: decision\ntitle: ADR 1\ncreated: 2026-10-31\nupdated: 2026-10-31\n"
            "tags:\n  - decision\nmaturity: developing\nverification: supported\n"
        )
        missing = self.run_lint()
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("missing: decision_status", missing.stdout)

        self.write_page(
            "type: decision\ntitle: ADR 1\ncreated: 2026-10-31\nupdated: 2026-10-31\n"
            "tags:\n  - decision\nmaturity: developing\nverification: supported\n"
            "decision_status: pending\n"
        )
        invalid = self.run_lint()
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("invalid decision_status 'pending'", invalid.stdout)

        self.write_page(
            "type: decision\ntitle: ADR 1\ncreated: 2026-10-31\nupdated: 2026-10-31\n"
            "tags:\n  - decision\nmaturity: developing\nverification: supported\n"
            "decision_status: active\ndecision_date: 2026-10-31\n"
        )
        valid = self.run_lint()
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)

    def test_date_format_rejects_invalid_and_allows_updated_iso_timestamp(self):
        self.write_page(
            "type: concept\ntitle: Page\ncreated: 2026-02-30\nupdated: 2026-10-31\n"
            "tags:\n  - test\nmaturity: developing\nverification: supported\n"
        )
        bad_cal = self.run_lint()
        self.assertNotEqual(bad_cal.returncode, 0)
        self.assertIn("invalid created '2026-02-30'", bad_cal.stdout)

        self.write_page(
            "type: concept\ntitle: Page\ncreated: 2026-10-31T10:00:00\nupdated: 2026-10-31\n"
            "tags:\n  - test\nmaturity: developing\nverification: supported\n"
        )
        ts_on_created = self.run_lint()
        self.assertNotEqual(ts_on_created.returncode, 0)
        self.assertIn("invalid created '2026-10-31T10:00:00'", ts_on_created.stdout)

        self.write_page(
            "type: concept\ntitle: Page\ncreated: 2026-10-31\nupdated: 2026-10-31T14:30:00Z\n"
            "tags:\n  - test\nmaturity: developing\nverification: supported\n"
        )
        ts_on_updated = self.run_lint()
        self.assertEqual(ts_on_updated.returncode, 0, ts_on_updated.stdout + ts_on_updated.stderr)

    def test_templates_directory_is_skipped(self):
        tpl_dir = self.root / "wiki" / "_templates"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "concept.md").write_text(
            "---\ntype: concept\ntitle: \"{{title}}\"\ncreated: {{date}}\nupdated: {{date}}\n"
            "tags:\n  - concept\nmaturity: seed\nverification: unreviewed\n---\n\n# {{title}}\n",
            encoding="utf-8",
        )
        self.write_page(
            self.base_fields() + "maturity: developing\nverification: supported\n"
        )
        result = self.run_lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
if __name__ == "__main__":
    unittest.main()
