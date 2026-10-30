#!/usr/bin/env python3
"""Tests for the body-wikilink graph in wiki-search.

Before this existed, `wiki-lint/scripts/lint-relations.py graph` returned `{}` on a
vault with 2,263 wikilinks, because it reads only `relations:` frontmatter and no
page used any. The edges readers actually follow were never indexed.

The failure mode is silent: a graph query returns a well-formed answer that is
empty or wrong, and nothing raises. Every test here pins a property that a wrong
implementation would violate while still running.
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "wiki-search.py"

PAGE = """---
title: "{title}"
type: concept
---

# {title}

{body}
"""


def load_search(root: Path):
    name = f"wiki_search_graph_{uuid.uuid4().hex}"
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


class GraphCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wiki-graph-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki").mkdir()
        self.search = load_search(self.root)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relpath, title, body):
        path = self.root / "wiki" / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PAGE.format(title=title, body=body), encoding="utf-8")

    def build(self):
        class Args:
            model = "test"
            json = False

        self.search.cmd_build(Args())
        return sqlite3.connect(self.search.DB_PATH)


class LinkExtractionTests(GraphCase):
    def test_body_wikilinks_become_edges(self):
        self.write("a.md", "Alpha", "See [[Beta]] and [[Gamma]].")
        self.write("b.md", "Beta", "Nothing here.")
        self.write("c.md", "Gamma", "Nothing here.")
        conn = self.build()
        rows = set(conn.execute("SELECT src, dst FROM links").fetchall())
        self.assertEqual(rows, {("a", "b"), ("a", "c")})

    def test_frontmatter_links_are_not_body_links(self):
        """`sources: ["[[X]]"]` in frontmatter is metadata, not a graph edge.

        Counting it would double every relationship the lint gate already owns.
        """
        path = self.root / "wiki" / "a.md"
        path.write_text(
            '---\ntitle: "Alpha"\nsources:\n  - "[[Beta]]"\n---\n\n# Alpha\n\nNo body links.\n',
            encoding="utf-8",
        )
        self.write("b.md", "Beta", "x")
        conn = self.build()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 0)

    def test_aliases_and_anchors_resolve_to_the_page(self):
        """`[[Beta|the beta page]]` and `[[Beta#Section]]` both point at Beta."""
        self.write("a.md", "Alpha", "See [[Beta|the beta page]] and [[Beta#Intro]].")
        self.write("b.md", "Beta", "x")
        conn = self.build()
        rows = set(conn.execute("SELECT src, dst FROM links").fetchall())
        self.assertEqual(rows, {("a", "b")})

    def test_path_links_normalise_to_the_same_node_as_bare_links(self):
        """`[[wiki/x/beta.md]]` and `[[beta]]` are the same page.

        Without normalisation every edge dangles: measured on vulcano-kb, all
        1,272 edges were reported dangling before this existed.
        """
        self.write("a.md", "Alpha", "See [[wiki/b/beta.md]].")
        self.write("b/beta.md", "Beta", "x")
        conn = self.build()
        rows = set(conn.execute("SELECT src, dst FROM links").fetchall())
        self.assertEqual(rows, {("a", "beta")})

    def test_editing_a_page_replaces_its_edges(self):
        """Removing a wikilink must remove the edge, not leave it behind.

        Edges are keyed by source page, so an edit has to clear the old set
        first. Without that the graph only ever grows, and a deleted reference
        keeps inflating every centrality number.
        """
        self.write("a.md", "Alpha", "See [[Beta]].")
        self.write("b.md", "Beta", "x")
        self.build()
        self.write("a.md", "Alpha", "See [[Gamma]].")
        self.write("c.md", "Gamma", "x")
        conn = self.build()
        rows = set(conn.execute("SELECT src, dst FROM links").fetchall())
        self.assertEqual(rows, {("a", "c")})

    def test_deleting_a_page_removes_its_outgoing_edges(self):
        self.write("a.md", "Alpha", "See [[Beta]].")
        self.write("b.md", "Beta", "x")
        self.build()
        (self.root / "wiki" / "a.md").unlink()
        conn = self.build()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 0)

    def test_rebuild_does_not_duplicate_edges(self):
        self.write("a.md", "Alpha", "See [[Beta]].")
        self.write("b.md", "Beta", "x")
        self.build()
        conn = self.build()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 1)


class BackfillTests(GraphCase):
    def test_unchanged_pages_get_edges_when_the_links_table_is_new(self):
        """An index built before the `links` table must refill on the next build.

        The sha short-circuit skips unchanged pages before any extraction, so
        without an explicit backfill the graph stays empty on every existing
        vault — which is exactly what happened on the first run.

        Two pages are required: with one page a permanently-on backfill and a
        correctly-versioned one produce the same count, so the test would pass
        against a broken implementation. It did, on the first attempt.
        """
        self.write("a.md", "Alpha", "See [[Beta]]. See [[Gamma]].")
        self.write("b.md", "Beta", "See [[Gamma]].")
        self.write("c.md", "Gamma", "x")
        conn = self.build()
        conn.execute("DELETE FROM links")
        conn.execute("DELETE FROM meta WHERE key='links_version'")
        conn.commit()
        conn.close()
        conn = self.build()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 3)

    def test_second_build_does_not_backfill_again(self):
        """Once the version is recorded, unchanged pages must not be re-read.

        Otherwise every build silently pays the full-corpus read cost forever.
        """
        self.write("a.md", "Alpha", "See [[Beta]].")
        self.write("b.md", "Beta", "x")
        self.build()
        conn = self.build()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT value FROM meta WHERE key='links_version'").fetchone()[0],
            self.search.LINKS_VERSION,
        )

    def test_backfill_does_not_rebuild_chunks_or_embeddings(self):
        self.write("a.md", "Alpha", "See [[Beta]].")
        self.write("b.md", "Beta", "x")
        conn = self.build()
        before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        conn.execute("DELETE FROM links")
        conn.execute("DELETE FROM meta WHERE key='links_version'")
        conn.commit()
        conn.close()
        conn = self.build()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], before)


class GraphCommandTests(GraphCase):
    def test_hub_list_reports_edges_and_dangling_targets(self):
        self.write("a.md", "Alpha", "See [[Beta]] and [[Nowhere]].")
        self.write("b.md", "Beta", "x")
        conn = self.build()
        known = {self.search.page_key(r[0]) for r in conn.execute("SELECT path FROM pages")}
        dangling = [
            d for (d,) in conn.execute("SELECT DISTINCT dst FROM links") if d not in known
        ]
        self.assertEqual(dangling, ["nowhere"])

    def test_neighbours_include_both_directions(self):
        self.write("a.md", "Alpha", "See [[Beta]].")
        self.write("b.md", "Beta", "See [[Gamma]].")
        self.write("c.md", "Gamma", "x")
        self.build()
        rc = self.search.main(["graph", "beta"])
        self.assertEqual(rc, 0)

    def test_graph_without_an_index_fails_instead_of_reporting_zero(self):
        rc = self.search.main(["graph"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
