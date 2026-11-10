#!/usr/bin/env python3
"""Recent observations are searchable immediately without entering canonical pages."""

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

SCRIPT = Path(__file__).parents[1] / "scripts" / "wiki-search.py"


def load_search(root: Path):
    module_name = f"wiki_search_recent_{uuid.uuid4().hex}"
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


class RecentScopeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wiki-search-recent-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki").mkdir()
        self.flow = self.root / ".cache" / "knowledge-flow"
        self.flow.mkdir(parents=True)
        self.search = load_search(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_jsonl(self, name: str, rows: list[dict]):
        (self.flow / name).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    def observation(self, stable_id: str, claim: str, **overrides):
        row = {
            "observation_id": stable_id,
            "source_path": ".raw/jira/issue.md",
            "source_sha256": "a" * 64,
            "source_lines": "10-12",
            "quote": claim,
            "claim": claim,
            "recorded_at": "2026-10-31T10:00:00Z",
            "observed_at": "2026-10-31",
            "observed_at_precision": "day",
            "valid_from": None,
            "valid_to": None,
            "epistemic_status": "observed",
        }
        row.update(overrides)
        return row

    def run_query(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.search.main(["query", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_recent_scope_works_without_canonical_index(self):
        self.write_jsonl(
            "observations.jsonl",
            [self.observation("obs-oauth", "OAuth rollout reaches production")],
        )
        code, stdout, stderr = self.run_query(
            "OAuth rollout", "--scope", "recent", "--json"
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["scope"], "recent")
        self.assertEqual(payload["n_results"], 1)
        self.assertTrue(payload["results"][0]["provisional"])
        self.assertEqual(payload["results"][0]["action"], "unclassified")

    def test_all_scope_separates_recent_when_index_is_missing(self):
        self.write_jsonl(
            "observations.jsonl",
            [self.observation("obs-oauth", "OAuth rollout reaches production")],
        )
        code, stdout, stderr = self.run_query(
            "OAuth rollout", "--scope", "all", "--json"
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["canonical"]["available"])
        self.assertEqual(payload["canonical"]["results"], [])
        self.assertEqual(payload["recent"]["n_results"], 1)
        self.assertIn("canonical index unavailable", stderr)

    def test_terminal_candidates_leave_recent_results(self):
        observations = [
            self.observation("obs-rejected", "OAuth rejected secret"),
            self.observation("obs-merged", "OAuth existing fact"),
            self.observation("obs-dispute", "OAuth rollout date conflicts"),
            self.observation("obs-expired", "OAuth temporary outage", valid_to="2020-01-01"),
        ]
        candidates = [
            {
                "candidate_id": "cand-rejected",
                "observation_ids": ["obs-rejected"],
                "action": "reject",
                "status": "rejected",
                "decided_at": "2026-10-31T10:01:00Z",
            },
            {
                "candidate_id": "cand-merged",
                "observation_ids": ["obs-merged"],
                "action": "no_op",
                "status": "applied",
                "decided_at": "2026-10-31T10:02:00Z",
            },
            {
                "candidate_id": "cand-dispute",
                "observation_ids": ["obs-dispute"],
                "relation": "contradicts",
                "action": "hold_dispute",
                "status": "applied",
                "decided_at": "2026-10-31T10:03:00Z",
            },
        ]
        self.write_jsonl("observations.jsonl", observations)
        self.write_jsonl("candidates.jsonl", candidates)
        code, stdout, stderr = self.run_query("OAuth", "--scope", "recent", "--json")
        self.assertEqual(code, 0, stderr)
        results = json.loads(stdout)["results"]
        self.assertEqual([item["observation_id"] for item in results], ["obs-dispute"])
        self.assertEqual(results[0]["relation"], "contradicts")

    def test_malformed_ledger_fails_loudly(self):
        (self.flow / "observations.jsonl").write_text("not-json\n", encoding="utf-8")
        code, _, stderr = self.run_query("anything", "--scope", "recent", "--json")
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", stderr)


if __name__ == "__main__":
    unittest.main()
