#!/usr/bin/env python3
"""Tests for temporal/graph context in query results and table-safe chunking."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).parents[1] / "scripts" / "wiki-search.py"


def load_search(root: Path):
    name = f"wiki_search_sufficiency_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    previous = Path.cwd()
    try:
        os.chdir(root)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous)
        sys.modules.pop(name, None)
    module.embed_batch = lambda texts, _model: [[0.5, 0.5] for _ in texts]
    return module


class SufficiencyFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wiki-sufficiency-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki" / "sources").mkdir(parents=True)
        self.search = load_search(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relpath: str, content: str):
        path = self.root / "wiki" / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def build(self):
        self.assertEqual(
            self.search.cmd_build(SimpleNamespace(model="test", json=False)), 0
        )

    def run_query(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.search.main(["query", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_query_returns_page_dates_and_bidirectional_graph_links(self):
        """An older meeting note linked by a newer Jira ticket must surface the newer page in `linked_pages`."""
        self.write(
            "sources/meeting-20260723.md",
            """---
type: source
title: "Security Meeting"
created: 2026-07-29
updated: 2026-11-10
fecha: 2026-07-23
---

# Security Meeting

PPR-139463 is not yet approved according to the meeting notes. Context filler words for chunk size.
""",
        )
        self.write(
            "sources/jira-ppr-139463.md",
            """---
type: source
title: "PPR-139463 Jira"
created: 2026-08-12
updated: 2026-11-10
fetched: 2026-08-12
---

# PPR-139463 Jira

Resolution Listo on 2026-07-31. Supersedes [[meeting-20260723]]. Context filler words for chunk size.
""",
        )
        self.build()
        code, stdout, stderr = self.run_query(
            "approved meeting notes", "--mode", "bm25", "--json"
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertGreaterEqual(payload["n_results"], 1)
        hit = payload["results"][0]
        self.assertEqual(hit["page"], "wiki/sources/meeting-20260723.md")
        self.assertEqual(hit["page_date"], "2026-07-23")
        self.assertEqual(hit["updated"], "2026-11-10")
        self.assertIn("wiki/sources/jira-ppr-139463.md", hit["linked_pages"])

    def test_split_markdown_table_repeats_header_and_marks_chunk_part(self):
        """When a large Markdown table spans multiple chunks, continuation chunks keep the table header and part index."""
        header = "| # | Key | Amount |\n|---|---|---|"
        rows = [
            f"| {i} | PPR-{140000 + i} | {i * 1000} EUR — row description padding to exceed the chunk character limit cleanly |"
            for i in range(1, 65)
        ]
        footer = "\n> [!warning] Partial capture: 64 of 80 rows (Total items 80 | Items per page 64)."
        page_text = f"# Valuations\n\n## Valoraciones\n\n{header}\n" + "\n".join(rows) + "\n" + footer
        chunks = self.search.chunk_page("wiki/sources/ppr.md", page_text)
        self.assertGreaterEqual(len(chunks), 2)
        first_heading, first_text = chunks[0]
        second_heading, second_text = chunks[1]
        self.assertEqual(first_heading, "## Valoraciones")
        self.assertEqual(second_heading, "## Valoraciones")
        self.assertIn("[chunk 1/", first_text)
        self.assertIn("[chunk 2/", second_text)
        # Continuation chunk must repeat the Markdown table header so columns are never orphaned
        self.assertIn("| # | Key | Amount |\n|---|---|---|", second_text)


if __name__ == "__main__":
    unittest.main()
