#!/usr/bin/env python3
"""Tests for lint-relations.py — the relation-lint gate inside wiki-lint.

Runs against a throwaway vault fixture. Per repo rules: a gate must be proven
to fail on a fault before it is trusted.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint-relations.py"


class VaultCase(unittest.TestCase):
    """A throwaway vault with wiki/, .git/, and pages to relate."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="relations-test-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        for f in ("log.md", "hot.md", "index.md"):
            (self.vault / "wiki" / f).write_text("---\n---\n", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.vault, ignore_errors=True)

    def page(self, name: str, relations: str = ""):
        body = "---\ntitle: " + name + "\ntype: entity\n---\n"
        if relations:
            body += "relations:\n" + relations
        (self.vault / "wiki" / f"{name}.md").write_text(body, encoding="utf-8")

    def run_lint(self, *args, as_json=True):
        cmd = ["python3", str(SCRIPT)]
        if as_json:
            cmd.append("--json")
        cmd.extend(args)
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=self.vault
        )
        payload = {}
        if as_json and result.stdout.strip():
            payload = json.loads(result.stdout)
        return result.returncode, payload

    # -- lint gate ---------------------------------------------------------

    def test_clean_vault_passes_with_exit_zero(self):
        self.page("A", "  - {type: replaces, target: \"[[B]]\"}\n")
        self.page("B")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(payload["findings"], [])

    def test_dangling_target_fails_with_exit_one(self):
        self.page("A", "  - {type: replaces, target: \"[[Missing]]\"}\n")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        problems = [f["problem"] for f in payload["findings"]]
        self.assertTrue(any("dangling" in p for p in problems))

    def test_unknown_type_fails(self):
        self.page("A", "  - {type: invalidates, target: \"[[B]]\"}\n")
        self.page("B")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertTrue(any("unknown relation type" in f["problem"]
                            for f in payload["findings"]))

    def test_self_relation_fails(self):
        self.page("A", "  - {type: uses, target: \"[[A]]\"}\n")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertTrue(any("self-relation" in f["problem"]
                            for f in payload["findings"]))

    def test_human_report_mode_exits_one_on_findings(self):
        self.page("A", "  - {type: replaces, target: \"[[Missing]]\"}\n")
        code, payload = self.run_lint(as_json=False)
        self.assertEqual(code, 1)

    def test_meta_and_navigation_pages_are_skipped(self):
        # a relation declared on a meta page must not produce findings
        # against pages it references or itself
        (self.vault / "wiki" / "meta").mkdir()
        (self.vault / "wiki" / "meta" / "dashboard.md").write_text(
            "---\ntitle: dashboard\n---\nrelations:\n"
            "  - {type: uses, target: \"[[A]]\"}\n",
            encoding="utf-8",
        )
        self.page("A")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)

    def test_title_frontmatter_resolves_target_not_filename(self):
        self.page("Alpha Page", "  - {type: uses, target: \"[[Real Title]]\"}\n")
        body = "---\ntitle: Real Title\ntype: entity\n---\n"
        (self.vault / "wiki" / "filename-different.md").write_text(
            body, encoding="utf-8"
        )
        code, payload = self.run_lint()
        self.assertEqual(code, 0)

    # -- graph queries (consulting, not the gate) ---------------------------

    def test_reverse_reports_typed_backlinks(self):
        self.page("A", "  - {type: replaces, target: \"[[B]]\"}\n")
        self.page("B")
        code, _ = self.run_lint("reverse", "B", as_json=False)
        self.assertEqual(code, 0)

    def test_graph_without_page_lists_adjacency(self):
        self.page("A", "  - {type: uses, target: \"[[B]]\"}\n")
        self.page("B")
        code, _ = self.run_lint("graph", as_json=False)
        self.assertEqual(code, 0)

    def test_usage_error_when_reverse_has_no_page(self):
        code, _ = self.run_lint("reverse", as_json=False)
        self.assertEqual(code, 2)

    # -- body-wikilink graph (the real graph readers follow) ----------------

    def page_with_body(self, name: str, body_links: str):
        body = f"---\ntitle: {name}\ntype: entity\n---\n\n# {name}\n\n{body_links}\n"
        (self.vault / "wiki" / f"{name}.md").write_text(body, encoding="utf-8")

    def test_graph_without_typed_relations_still_reports_body_edges(self):
        # The fault this guards against: 0 typed relations + a vault full of
        # [[body links]] answered "{}" — an invisible graph (radar round 6).
        self.page_with_body("Alpha", "See also [[Beta]] and [[Gamma]].")
        self.page("Beta")
        self.page("Gamma")
        code, _ = self.run_lint("graph", as_json=False)
        self.assertEqual(code, 0)
        # non-json prints JSON anyway for graph; re-run and parse
        result = subprocess.run(
            ["python3", str(SCRIPT), "graph"], capture_output=True, text=True,
            cwd=self.vault,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["typed"], {})
        self.assertGreaterEqual(payload["body_edge_count"], 2)
        self.assertIn("Beta", payload["body"]["Alpha"])

    def test_reverse_reports_body_backlinks_without_typed_relations(self):
        self.page_with_body("Alpha", "Discussed in [[Beta]].")
        self.page_with_body("Beta", "See [[Alpha]] for context.")
        result = subprocess.run(
            ["python3", str(SCRIPT), "reverse", "Alpha"], capture_output=True,
            text=True, cwd=self.vault,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["Alpha"]["typed"], [])
        self.assertIn("Beta", payload["Alpha"]["body"])

    def test_body_links_resolve_frontmatter_title_not_filename(self):
        # A link spelled as the frontmatter title must land on the page whose
        # filename differs, like wiki-search's resolver does.
        self.page_with_body("Alpha", "Compare with [[Real Title]].")
        (self.vault / "wiki" / "filename-different.md").write_text(
            "---\ntitle: Real Title\ntype: entity\n---\n\n# Real Title\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["python3", str(SCRIPT), "reverse", "Real Title"], capture_output=True,
            text=True, cwd=self.vault,
        )
        payload = json.loads(result.stdout)
        self.assertIn("Alpha", payload["Real Title"]["body"])

    def test_graph_page_neighbourhood_includes_body_links(self):
        self.page_with_body("Alpha", "See [[Beta]].")
        self.page("Beta")
        result = subprocess.run(
            ["python3", str(SCRIPT), "graph", "Alpha"], capture_output=True,
            text=True, cwd=self.vault,
        )
        payload = json.loads(result.stdout)
        self.assertIn("Beta", payload["Alpha"])

if __name__ == "__main__":
    unittest.main()