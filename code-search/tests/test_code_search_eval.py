#!/usr/bin/env python3
"""code-search eval: the gold set finally has a consumer, and a gate that fails.

Origin: 2026-11-24 audit — `code-search/tests/gold-code.json` (20 queries) was
referenced only by prose in `references/embedding-models.md`; the metrics were
"scored by whatever script the experiment writes", so the script decided the
result. This is the frozen scorer plus optional floors.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "code-search.py"


def load_module(root: Path):
    import os
    import sys
    import uuid

    name = f"code_search_eval_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    previous = Path.cwd()
    try:
        os.chdir(root)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous)
        sys.modules.pop(name, None)
    return module


class EvalIndexTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        # config paths resolve against the process cwd, so the suite must run
        # from inside the vault (same seam as test_code_search.py).
        self._previous = Path.cwd()
        os.chdir(self.root)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "projects" / "demo").mkdir(parents=True)
        (self.root / "wiki").mkdir()
        (self.root / "kb-config.json").write_text(json.dumps({
            "code_search": {"source_dirs": ["projects"], "extensions": [".py"],
                            "endpoint": "http://unused.invalid", "model": "test"}}),
            encoding="utf-8")
        (self.root / "projects" / "demo" / "auth.py").write_text(
            "def cifra_credencial(valor):\n    \"\"\"Cifra la credencial con RSA.\"\"\"\n"
            "    return valor\n", encoding="utf-8")
        (self.root / "projects" / "demo" / "http.py").write_text(
            "def fetch_retry(url):\n    \"\"\"Reintenta amb backoff.\"\"\"\n"
            "    return url\n", encoding="utf-8")
        self.module = load_module(self.root)
        # Vector search needs an embedding service; the metrics under test are
        # ranking arithmetic, so embed with a constant fake and keep bm25 real.
        from unittest import mock
        config = self.module.load_config(self.module.find_root())
        with mock.patch.object(self.module, "embed_texts",
                               side_effect=lambda _c, texts, expected_dimension=0: [[1.0, 0.0] for _ in texts]):
            self.assertEqual(self.module.build_index(config)["ok"], True)

    def tearDown(self):
        os.chdir(self._previous)
        self.tempdir.cleanup()

    def config(self):
        return self.module.load_config(self.module.find_root())

    def run_eval(self, queries, **kwargs):
        dataset = self.root / "gold.json"
        dataset.write_text(json.dumps({"name": "fixture", "queries": queries}),
                           encoding="utf-8")
        kwargs.setdefault("mode", "bm25")
        return self.module.eval_index(self.config(), str(dataset), **kwargs)

    HIT = {"id": "q1", "text": "cifra la credencial con RSA",
           "relevant": ["projects/demo/auth.py"], "category": "paraphrase"}
    MISS = {"id": "q2", "text": "backoff exponencial reintenta",
            "relevant": ["projects/demo/auth.py"], "category": "paraphrase"}

    def test_measures_without_a_gate(self):
        out = self.run_eval([self.HIT])
        self.assertTrue(out["ok"])
        self.assertEqual(out["mrr"], 1.0)
        self.assertNotIn("failures", out)

    def test_floor_above_the_value_fails_closed(self):
        out = self.run_eval([self.HIT], min_mrr=1.01)
        self.assertFalse(out["ok"])
        self.assertIn("--min-mrr", out["failures"][0])

    def test_floor_below_the_value_passes(self):
        out = self.run_eval([self.HIT], min_mrr=0.5)
        self.assertTrue(out["ok"])

    def test_recall_floor_fails_when_a_query_misses(self):
        out = self.run_eval([self.HIT, self.MISS], min_recall=0.9)
        self.assertFalse(out["ok"])
        self.assertIn("--min-recall-at", out["failures"][0])

    def test_baseline_reports_broke_queries(self):
        first = self.run_eval([self.HIT, {"id": "q3", "text": "fetch_retry backoff",
                                          "relevant": ["projects/demo/http.py"]}])
        baseline = self.root / "baseline.json"
        baseline.write_text(json.dumps(first), encoding="utf-8")
        # q3 now points at a page that will never be retrieved for its text.
        second = self.run_eval([self.HIT, {"id": "q3", "text": "fetch_retry backoff",
                                           "relevant": ["projects/demo/absent.py"]}],
                               baseline=str(baseline), min_mrr=0.01)
        self.assertFalse(second["ok"])
        self.assertEqual(second["flips"]["broke"], ["q3"])
        self.assertIn("trade-off", second["failures"][0])

    def test_missing_gold_set_raises_instead_of_scoring_zero(self):
        with self.assertRaises(self.module.CodeSearchError):
            self.module.eval_index(self.config(), str(self.root / "nope.json"))

    def test_gold_set_without_queries_raises(self):
        dataset = self.root / "empty.json"
        dataset.write_text(json.dumps({"name": "x"}), encoding="utf-8")
        with self.assertRaises(self.module.CodeSearchError):
            self.module.eval_index(self.config(), str(dataset))

    def test_real_repo_gold_set_loads(self):
        """The shipped gold set must stay consumable by this scorer."""
        shipped = SCRIPT.parent.parent / "tests" / "gold-code.json"
        data = json.loads(shipped.read_text(encoding="utf-8"))
        self.assertIn("queries", data)
        self.assertGreaterEqual(len(data["queries"]), 20)
        for query in data["queries"]:
            self.assertIn("text", query)
            self.assertIn("relevant", query)


if __name__ == "__main__":
    unittest.main()
