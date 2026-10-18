#!/usr/bin/env python3
"""Tests for lint-citations.py — the source-citation gate inside wiki-lint.

Runs against a throwaway vault fixture. Per repo rules: a gate must be proven
to fail on a fault before it is trusted.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint-citations.py"


class VaultCase(unittest.TestCase):
    """A throwaway vault with wiki/, .git/ and a 10-line source in .raw/."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="citations-test-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        (self.vault / ".raw").mkdir()
        (self.vault / ".raw" / "src.md").write_text(
            "".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def page(self, body: str, name: str = "P"):
        (self.vault / "wiki" / f"{name}.md").write_text(
            "---\ntitle: " + name + "\n---\n" + body + "\n", encoding="utf-8"
        )

    def run_lint(self):
        result = subprocess.run(
            ["python3", str(SCRIPT), "--json"],
            cwd=self.vault, capture_output=True, text=True, timeout=30,
        )
        return result.returncode, json.loads(result.stdout)

    def kinds(self, payload):
        return {f["kind"] for f in payload["findings"]}

    # -- valid citations ----------------------------------------------------

    def test_valid_whole_and_range_citations_pass(self):
        self.page("A claim. ^[.raw/src.md]\nAnother. ^[.raw/src.md:2-4]\n"
                  "Third. ^[.raw/src.md#L5-L7]")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["markers"], 3)

    def test_single_line_citation_passes(self):
        self.page("Claim. ^[.raw/src.md:10]")
        code, _ = self.run_lint()
        self.assertEqual(code, 0)

    # -- faults that must fail the gate --------------------------------------

    def test_missing_source_fails(self):
        self.page("Claim. ^[.raw/absent.md:1-2]")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertIn("missing_source", self.kinds(payload))

    def test_reversed_range_fails(self):
        self.page("Claim. ^[.raw/src.md:5-2]")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertIn("bad_range", self.kinds(payload))

    def test_zero_start_fails(self):
        self.page("Claim. ^[.raw/src.md:0-3]")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertIn("bad_range", self.kinds(payload))

    def test_empty_marker_is_malformed(self):
        self.page("Claim. ^[]")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertIn("malformed", self.kinds(payload))

    # -- warnings do not fail the gate ---------------------------------------

    def test_out_of_bounds_warns_but_exits_zero(self):
        self.page("Claim. ^[.raw/src.md:8-99]")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(payload["errors"], 0)
        self.assertIn("out_of_bounds", self.kinds(payload))

    # -- scope ---------------------------------------------------------------

    def test_markers_inside_code_fences_are_ignored(self):
        self.page("```\nexample ^[.raw/absent.md]\n```\nreal ^[.raw/src.md:1]")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(payload["markers"], 1)

    def test_findings_report_page_and_line(self):
        self.page("ok ^[.raw/src.md:1]\nbad ^[.raw/nope.md]", name="Target")
        _, payload = self.run_lint()
        (finding,) = payload["findings"]
        self.assertEqual(finding["page"], "wiki/Target.md")
        self.assertEqual(finding["line"], 5)


if __name__ == "__main__":
    unittest.main()
