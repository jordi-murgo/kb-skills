#!/usr/bin/env python3
"""Fusion depth must not truncate below the corpus: truncated RRF is a
different ranking than complete-list RRF (arXiv:2608.07152).

The test builds an index larger than the historical 200-chunk cutoff and
asserts that the shipped FUSION_DEPTH fuses the complete lists, i.e. the
ordered hybrid ranking is identical to one computed with no cutoff at all.
Lowering FUSION_DEPTH below the corpus size makes this fail.
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "wiki-search.py"

PAGES = 320  # > the historical FUSION_DEPTH of 200


def load_search(root: Path):
    """Load the script after making the temporary vault its cwd."""
    module_name = f"wiki_search_fusion_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    previous_cwd = Path.cwd()
    try:
        os.chdir(root)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous_cwd)
        sys.modules.pop(module_name, None)
    return module


def page(title: str, marker: str) -> str:
    body = f"{marker} sharedtoken " + ("filler context sentence " * 12)
    return f"# {title}\n\n{body}\n"


def embedding_for(text: str) -> list[float]:
    """Deterministic, distinct-per-chunk 2-dim vector (no Ollama needed)."""
    digest = uuid.uuid5(uuid.NAMESPACE_OID, text).int
    return [(digest % 1000) / 1000.0, 1.0]


class FusionDepthTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        wiki = self.root / "wiki"
        wiki.mkdir()
        for i in range(PAGES):
            (wiki / f"page-{i:03d}.md").write_text(
                page(f"Page {i}", f"marker{i:03d}"), encoding="utf-8"
            )
        self.search = load_search(self.root)
        self.search.embed_batch = lambda texts, _model: [embedding_for(t) for t in texts]
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.search.cmd_build(type("A", (), {"model": "test", "json": False})())
        self.assertEqual(code, 0)
        self.conn = self.search.connect_readonly()
        self.backend = self.search.get_meta(self.conn, "bm25_backend") or "fts5"
        self.dims = self.search.get_meta(self.conn, "dims")
        if isinstance(self.dims, str):
            self.dims = int(self.dims)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def ranked_ids(self, depth: int) -> list[int]:
        previous = self.search.FUSION_DEPTH
        self.search.FUSION_DEPTH = depth
        try:
            ranked, _bm25, _vec, _effective, _notes = self.search.rank_chunks(
                self.conn, self.backend, "sharedtoken", "hybrid", "test", self.dims
            )
        finally:
            self.search.FUSION_DEPTH = previous
        return [chunk_id for chunk_id, _score in ranked]

    def test_shipped_depth_reproduces_complete_list_fusion(self):
        shipped = self.ranked_ids(self.search.FUSION_DEPTH)
        complete = self.ranked_ids(10**9)
        self.assertEqual(len(complete), PAGES)
        self.assertEqual(
            shipped,
            complete,
            "FUSION_DEPTH truncates the channels below the corpus size: "
            f"{len(shipped)} fused chunks vs {len(complete)} with complete lists",
        )

    def test_truncated_fusion_is_a_different_ranking(self):
        """Guards the premise: a low cutoff really does change the result."""
        truncated = self.ranked_ids(10)
        complete = self.ranked_ids(10**9)
        self.assertNotEqual(truncated, complete)


if __name__ == "__main__":
    unittest.main()
