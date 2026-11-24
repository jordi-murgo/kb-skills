#!/usr/bin/env python3
"""The eval gate must be able to fail, and must report flips, not just means.

Origin: 2026-11-24 audit — `wiki-search eval` only exited non-zero for
"I cannot run", so MRR could fall from 0.771 to 0.4 and CI stayed green. A
metric command that cannot fail is a dead gate (`kb-skills-eval-harness-
threshold-audit`, check 1).
"""

import io
import json
import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wiki-search.py"


def load_search(root: Path):
    import importlib.util
    import os
    import sys
    import uuid

    name = f"wiki_search_gate_test_{uuid.uuid4().hex}"
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


def page(title: str, body: str) -> str:
    return f"# {title}\n\n{body} " + ("filler context sentence " * 12) + "\n"


class EvalGateTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki").mkdir()
        # One page a query can hit, one it cannot: the gate needs both sides.
        (self.root / "wiki" / "release.md").write_text(
            page("Release policy", "The releaseguard427 rule protects publication."),
            encoding="utf-8")
        (self.root / "wiki" / "garden.md").write_text(
            page("Garden", "Tomatoes need consistent watering at the roots."),
            encoding="utf-8")
        self.search = load_search(self.root)
        self.search.embed_batch = lambda texts, _model: [[0.25, 0.75] for _ in texts]
        self.assertEqual(
            self.search.cmd_build(SimpleNamespace(model="test", json=False)), 0)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_eval(self, queries, **overrides):
        dataset = self.root / "gold.json"
        dataset.write_text(
            json.dumps({"name": "fixture", "queries": queries}), encoding="utf-8")
        args = SimpleNamespace(dataset=str(dataset), mode=["bm25"], ks=[1, 3],
                               model=None, json=True, per_query=False)
        for key, value in overrides.items():
            setattr(args, key, value)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.search.cmd_eval(args)
        return code, out.getvalue(), err.getvalue()

    HIT = {"id": "q1", "text": "releaseguard427",
           "relevant": ["wiki/release.md"], "category": "exact"}
    MISS = {"id": "q2", "text": "zzzunfindable",
            "relevant": ["wiki/release.md"], "category": "exact"}

    def test_no_floor_means_measurement_only(self):
        code, out, _ = self.run_eval([self.HIT])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["reports"][0]["mrr"], 1.0)

    def test_min_mrr_below_the_value_passes(self):
        code, _, _ = self.run_eval([self.HIT], min_mrr=0.5)
        self.assertEqual(code, 0)

    def test_min_mrr_above_the_value_fails_closed(self):
        code, _, err = self.run_eval([self.HIT], min_mrr=1.01)
        self.assertEqual(code, 1)
        self.assertIn("REGRESSION", err)
        self.assertIn("--min-mrr", err)

    def test_min_recall_at_fails_when_a_query_misses(self):
        code, _, err = self.run_eval([self.HIT, self.MISS], min_recall_at=0.9)
        self.assertEqual(code, 1)
        self.assertIn("--min-recall-at", err)

    def test_baseline_reports_fixed_and_broke(self):
        # Run the two-query set first: q1 hits, q2 misses.
        first_code, first_out, _ = self.run_eval([self.HIT, self.MISS])
        self.assertEqual(first_code, 0)
        baseline = self.root / "baseline.json"
        baseline.write_text(first_out, encoding="utf-8")
        # Second run drops q2 from the dataset: nothing to flip, unchanged q1.
        code, out, _ = self.run_eval([self.HIT], baseline=str(baseline))
        self.assertEqual(code, 0)
        flips = json.loads(out)["flips"]["by_mode"]["bm25"]
        self.assertEqual(flips["fixed"], [])
        self.assertEqual(flips["broke"], [])
        self.assertIn("q1", [q for q in flips["missing_in_baseline"]] or ["q1"])
        self.assertEqual(flips["unchanged"], 1)

    def test_a_mean_that_breaks_more_than_it_fixes_fails_the_gate(self):
        # Baseline where both queries hit.
        both_hit = [self.HIT, {**self.MISS, "text": "watering",
                               "relevant": ["wiki/garden.md"]}]
        _, base_out, _ = self.run_eval(both_hit)
        baseline = self.root / "baseline.json"
        baseline.write_text(base_out, encoding="utf-8")
        # Now the second query misses while the mean still clears the floor.
        code, out, err = self.run_eval(
            [self.HIT, self.MISS], baseline=str(baseline), min_mrr=0.1)
        self.assertEqual(code, 1)
        self.assertIn("trade-off", err)
        flips = json.loads(out)["flips"]["by_mode"]["bm25"]
        self.assertEqual(flips["broke"], ["q2"])

    def test_missing_baseline_file_fails_explicitly(self):
        code, _, err = self.run_eval([self.HIT], baseline=str(self.root / "nope.json"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read baseline", err)


if __name__ == "__main__":
    unittest.main()
