#!/usr/bin/env python3
"""Tests for citation-metrics.py — the grounding report (not a gate).

Runs against a throwaway vault fixture. This is a report tool: it must exit
0 even on zero coverage, and exit non-zero only when the vault root cannot
be found or a page cannot be read.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "citation-metrics.py"


class VaultCase(unittest.TestCase):
    """A throwaway vault with wiki/, .git/ and a source dir."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="citation-metrics-test-"))
        (self.vault / "wiki").mkdir()
        (self.vault / "wiki" / "meta").mkdir()
        (self.vault / ".git").mkdir()
        (self.vault / "sources").mkdir()

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def page(self, body: str, name: str = "P", sub: str = ""):
        target = self.vault / "wiki" / sub if sub else self.vault / "wiki"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{name}.md"
        path.write_text("---\ntitle: " + name + "\n---\n" + body + "\n",
                        encoding="utf-8")
        return path.relative_to(self.vault / "wiki").as_posix()

    def run_metrics(self, cwd=None):
        result = subprocess.run(
            ["python3", str(SCRIPT), "--json"],
            cwd=cwd or self.vault, capture_output=True, text=True, timeout=30,
        )
        return result

    def payload(self):
        result = self.run_metrics()
        return result.returncode, json.loads(result.stdout)

    # -- counting ---------------------------------------------------------------

    def test_markers_counted_whole_vs_ranged(self):
        self.page("A ^[sources/a.md]\nB ^[sources/a.md:2-4]\n"
                  "C ^[sources/b.md#L7]\n```\nignored ^[sources/a.md]\n```\n")
        self.page("D ^[sources/a.md]", name="Q")
        code, p = self.payload()
        self.assertEqual(code, 0)
        self.assertEqual(p["markers"], 4)
        self.assertEqual(p["whole"], 2)
        self.assertEqual(p["ranged"], 2)
        self.assertEqual(p["grounded_pages"], 2)
        self.assertEqual(p["distinct_sources"], 2)

    def test_top_sources_ranked(self):
        self.page("a ^[sources/top.md]", name="T0")
        self.page("b ^[sources/top.md]", name="T1")
        self.page("c ^[sources/top.md]", name="T2")
        self.page("d ^[sources/a.md]", name="RA")
        self.page("e ^[sources/a.md]", name="RB")
        self.page("f ^[sources/one.md]", name="R1")
        code, p = self.payload()
        self.assertEqual(code, 0)
        self.assertEqual(p["top_sources"][0], ["sources/top.md", 3])
        self.assertEqual(p["top_sources"][1], ["sources/a.md", 2])
        self.assertEqual(p["top_sources"][2], ["sources/one.md", 1])

    # -- grounded / ungrounded --------------------------------------------------

    def test_ungrounded_page_is_listed(self):
        self.page("Grounded. ^[sources/a.md]", name="Grounded")
        self.page("No citations here.", name="Ungrounded")
        code, p = self.payload()
        self.assertEqual(code, 0)
        self.assertIn("Ungrounded.md", p["ungrounded_pages"])
        self.assertNotIn("Grounded.md", p["ungrounded_pages"])

    def test_meta_pages_exempt_from_ungrounded(self):
        self.page("no markers", name="Notes", sub="meta")
        self.page("no markers", name="index")
        self.page("no markers", name="log")
        self.page("no markers", name="hot")
        self.page("no markers", name="_index")
        code, p = self.payload()
        self.assertEqual(code, 0)
        self.assertEqual(p["ungrounded_pages"], [])

    def test_deep_index_is_not_root_meta(self):
        self.page("no markers", name="index", sub="guides")
        code, p = self.payload()
        self.assertEqual(code, 0)
        self.assertIn("guides/index.md", p["ungrounded_pages"])

    # -- report shape and zero-marker vault -------------------------------------

    def test_json_output_shape(self):
        self.page("A ^[sources/a.md:1-2]", name="Shape")
        code, p = self.payload()
        self.assertEqual(code, 0)
        for key in ("vault", "pages", "markers", "whole", "ranged",
                    "ranged_ratio", "grounded_pages", "grounded_ratio",
                    "distinct_sources", "top_sources", "ungrounded_pages"):
            self.assertIn(key, p)
        self.assertIsInstance(p["pages"], int)
        self.assertIsInstance(p["markers"], int)
        self.assertIsInstance(p["ranged_ratio"], float)
        self.assertIsInstance(p["grounded_ratio"], float)
        self.assertIsInstance(p["top_sources"], list)
        self.assertIsInstance(p["ungrounded_pages"], list)
        self.assertEqual(p["pages"], 1)
        self.assertEqual(p["grounded_pages"], 1)

    def test_zero_markers_reports_zero_grounding_without_error(self):
        self.page("Nothing cited.", name="Solo")
        code, p = self.payload()
        self.assertEqual(code, 0)
        self.assertEqual(p["markers"], 0)
        self.assertEqual(p["grounded_ratio"], 0.0)
        self.assertEqual(p["grounded_pages"], 0)
        self.assertEqual(p["top_sources"], [])
        self.assertIn("Solo.md", p["ungrounded_pages"])

    def test_empty_vault_reports_zero_pages(self):
        code, p = self.payload()
        self.assertEqual(code, 0)
        self.assertEqual(p["pages"], 0)
        self.assertEqual(p["grounded_ratio"], 0.0)

    # -- failure paths (report still fails honestly on real faults) --------------

    def test_unreadable_page_fails(self):
        self.page("Nothing cited.", name="Sealed")
        target = self.vault / "wiki" / "Sealed.md"
        target.chmod(0o000)
        try:
            result = self.run_metrics()
            self.assertEqual(result.returncode, 1)
            self.assertIn("cannot read", result.stderr)
        finally:
            target.chmod(0o644)

    # find_vault_root raises SystemExit when no vault is found, but the
    # script's __file__ fallback means the "not found" branch is only
    # reachable outside this repo; the honest reachable fault is the one
    # proven above.


if __name__ == "__main__":
    unittest.main()