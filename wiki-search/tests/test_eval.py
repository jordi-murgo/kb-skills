#!/usr/bin/env python3
"""Behaviour coverage for the wiki-search retrieval evaluation harness."""

import contextlib
import importlib.util
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "wiki-search.py"


def load_search(root: Path):
    """Load the script after making the temporary vault its cwd."""
    module_name = f"wiki_search_eval_test_{uuid.uuid4().hex}"
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


def page(title: str, body: str) -> str:
    return f"# {title}\n\n{body} " + ("filler context sentence " * 12) + "\n"


class MetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        root = Path(cls.tempdir.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "wiki").mkdir()
        cls.search = load_search(root)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def test_metrics_reward_rank_of_relevant_pages(self):
        m = self.search.retrieval_metrics(["a", "b", "c", "d"], ["b", "d"], [1, 3])
        self.assertEqual(m["hit"], {1: 0.0, 3: 1.0})
        self.assertEqual(m["recall"], {1: 0.0, 3: 0.5})
        self.assertAlmostEqual(m["precision"][3], 1 / 3)
        expected_ndcg = (1 / math.log2(3)) / (1 + 1 / math.log2(3))
        self.assertAlmostEqual(m["ndcg"][3], expected_ndcg)
        self.assertEqual(m["first_relevant_rank"], 2)
        self.assertEqual(m["rr"], 0.5)

    def test_metrics_score_zero_when_nothing_relevant_retrieved(self):
        m = self.search.retrieval_metrics(["a", "c"], ["b"], [1, 5])
        self.assertEqual(m["hit"][5], 0.0)
        self.assertEqual(m["ndcg"][5], 0.0)
        self.assertIsNone(m["first_relevant_rank"])
        self.assertEqual(m["rr"], 0.0)

    def test_pages_rank_at_their_best_chunk(self):
        self.assertEqual(self.search.dedupe_pages(["a", "a", "b", "a", "c"], 2), ["a", "b"])


class EvalCommandTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki").mkdir()
        (self.root / "wiki" / "release.md").write_text(
            page("Release policy", "The releaseguard427 rule protects publication."), encoding="utf-8"
        )
        (self.root / "wiki" / "garden.md").write_text(
            page("Garden", "Tomatoes need consistent watering at the roots."), encoding="utf-8"
        )
        self.search = load_search(self.root)
        self.search.embed_batch = lambda texts, _model: [[0.25, 0.75] for _ in texts]
        self.assertEqual(self.search.cmd_build(SimpleNamespace(model="test", json=False)), 0)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_eval(self, queries, **overrides):
        dataset = self.root / "gold.json"
        dataset.write_text(json.dumps({"name": "fixture", "queries": queries}), encoding="utf-8")
        args = SimpleNamespace(dataset=str(dataset), mode=["bm25"], ks=[1, 3], model=None,
                               json=True, per_query=False)
        for key, value in overrides.items():
            setattr(args, key, value)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.search.cmd_eval(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_exact_term_query_ranks_its_page_first(self):
        code, out, _ = self.run_eval([
            {"id": "q1", "text": "releaseguard427", "relevant": ["wiki/release.md"], "category": "exact"},
        ])
        self.assertEqual(code, 0)
        report = json.loads(out)["reports"][0]
        self.assertEqual(report["mode"], "bm25")
        self.assertEqual(report["mrr"], 1.0)
        self.assertEqual(report["per_query"][0]["first_relevant_rank"], 1)
        self.assertEqual(report["per_query"][0]["retrieved"][0], "wiki/release.md")

    def test_unindexed_relevant_page_fails_instead_of_scoring_zero(self):
        code, _, err = self.run_eval([
            {"id": "q1", "text": "watering", "relevant": ["wiki/does-not-exist.md"]},
        ])
        self.assertEqual(code, 1)
        self.assertIn("wiki/does-not-exist.md", err)

    def test_duplicate_query_ids_are_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_eval([
                {"id": "q1", "text": "a", "relevant": ["wiki/release.md"]},
                {"id": "q1", "text": "b", "relevant": ["wiki/garden.md"]},
            ])
        self.assertIn("duplicate eval query id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
