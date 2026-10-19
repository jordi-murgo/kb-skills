#!/usr/bin/env python3
"""Tests for lint-empty-sections.py — the empty-section gate.

The check is heading-level aware, so most of these tests are about what does
NOT count as an empty section: a heading followed by a child heading, a fenced
code block, a horizontal rule, or content that belongs to the previous
same-or-higher heading.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint-empty-sections.py"


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="lint-empty-sections-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def page(self, body: str, name: str = "page", sub: str = "") -> str:
        target = self.vault / "wiki" / sub if sub else self.vault / "wiki"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{name}.md"
        path.write_text(body, encoding="utf-8")
        return path.relative_to(self.vault).as_posix()

    def run_lint(self, *args):
        result = subprocess.run(
            ["python3", str(SCRIPT), "--json", *args],
            cwd=self.vault, capture_output=True, text=True, timeout=30,
        )
        return result

    def payload(self, *args):
        result = self.run_lint(*args)
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode, json.loads(result.stdout)


class EmptySectionTest(VaultCase):
    def test_heading_with_nothing_under_it_is_an_error(self):
        self.page("---\ntitle: P\n---\n# P\n\n## Filled\n\nText.\n\n## Hollow\n")
        code, data = self.payload()
        self.assertEqual(code, 1)
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["findings"][0]["kind"], "empty_section")
        self.assertEqual(data["findings"][0]["heading"], "## Hollow")

    def test_heading_followed_by_child_heading_is_not_empty(self):
        self.page("---\ntitle: P\n---\n# P\n\n## Parent\n\n### Child\n\nText.\n")
        code, data = self.payload()
        self.assertEqual(code, 0)
        self.assertEqual(data["errors"], 0)

    def test_fenced_code_block_counts_as_content(self):
        self.page("---\ntitle: P\n---\n# P\n\n## Example\n\n```bash\nls -la\n```\n")
        code, data = self.payload()
        self.assertEqual(code, 0)

    def test_heading_inside_a_fence_is_not_a_heading(self):
        self.page(
            "---\ntitle: P\n---\n# P\n\n## Example\n\n```md\n# not a heading\n```\n"
            "\n## Hollow\n"
        )
        code, data = self.payload()
        self.assertEqual(code, 1)
        self.assertEqual([f["heading"] for f in data["findings"]], ["## Hollow"])

    def test_horizontal_rule_alone_is_not_content(self):
        self.page("---\ntitle: P\n---\n# P\n\n## Hollow\n\n---\n\n## Filled\n\nText.\n")
        code, data = self.payload()
        self.assertEqual(code, 1)
        self.assertEqual([f["heading"] for f in data["findings"]], ["## Hollow"])

    def test_content_after_a_higher_level_heading_belongs_to_that_one(self):
        self.page(
            "---\ntitle: P\n---\n# P\n\n## A\n\n### B\n\nText under B.\n\n## C\n"
        )
        code, data = self.payload()
        self.assertEqual(code, 1)
        self.assertEqual([f["heading"] for f in data["findings"]], ["## C"])

    def test_frontmatter_is_not_scanned(self):
        self.page("---\ntitle: P\n# not a heading\n---\n# P\n\n## Filled\n\nText.\n")
        code, data = self.payload()
        self.assertEqual(code, 0)

    def test_navigation_and_meta_pages_are_skipped(self):
        self.page("# Index\n\n## Hollow\n", name="index")
        self.page("# Log\n\n## Hollow\n", name="log")
        self.page("# Meta\n\n## Hollow\n", name="meta-page", sub="meta")
        code, data = self.payload()
        self.assertEqual(code, 0)
        self.assertEqual(data["pages"], 0)

    def test_thin_check_is_opt_in_and_only_warns(self):
        self.page("---\ntitle: P\n---\n# P\n\nIntro paragraph long enough.\n\n## Brief\n\nShort.\n")
        code, data = self.payload()
        self.assertEqual(code, 0)
        self.assertEqual(data["warnings"], 0)

        code, data = self.payload("--thin", "50")
        self.assertEqual(code, 0, "thin findings must not fail the gate")
        self.assertEqual(data["warnings"], 1)
        self.assertEqual(data["findings"][0]["kind"], "thin_section")

    def test_thin_does_not_mask_a_real_empty_section(self):
        self.page("---\ntitle: P\n---\n# P\n\n## Brief\n\nShort.\n\n## Hollow\n")
        code, data = self.payload("--thin", "50")
        self.assertEqual(code, 1)
        kinds = sorted(f["kind"] for f in data["findings"])
        self.assertEqual(kinds, ["empty_section", "thin_section"])

    def test_rejects_a_non_positive_threshold(self):
        result = self.run_lint("--thin", "0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive", result.stderr)

    def test_document_title_is_not_thin_checked(self):
        """The H1 is a title, not a section: an intro-less page is normal."""
        long_text = "This sentence is comfortably longer than the fifty character threshold."
        self.page(f"---\ntitle: P\n---\n# P\n\n## Filled\n\n{long_text}\n")
        code, data = self.payload("--thin", "50")
        self.assertEqual(code, 0)
        self.assertEqual(data["warnings"], 0)

    def test_reports_line_number_of_the_heading(self):
        self.page("---\ntitle: P\n---\n# P\n\n## Filled\n\nText.\n\n## Hollow\n")
        _, data = self.payload()
        self.assertEqual(data["findings"][0]["line"], 10)


if __name__ == "__main__":
    unittest.main()
