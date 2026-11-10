#!/usr/bin/env python3
"""End-to-end CLI tests for candidate queue transitions and expiry."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "consolidate.py"


class ConsolidateCliTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wiki-consolidate-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki").mkdir()
        self.flow = self.root / ".cache" / "knowledge-flow"
        self.flow.mkdir(parents=True)
        self.write_jsonl(
            "observations.jsonl",
            [
                self.observation("obs-old", "Old temporary rollout", "2025-01-01"),
                self.observation("obs-update", "Durable OAuth constraint", "2026-10-20"),
                self.observation("obs-dispute", "Conflicting OAuth deadline", "2026-10-21"),
            ],
        )
        self.write_jsonl(
            "candidates.jsonl",
            [
                self.candidate("cand-old", "obs-old", "novel", "ephemeral", "recent_only"),
                self.candidate(
                    "cand-update", "obs-update", "extends", "durable", "update_existing",
                    matched_pages=["wiki/concepts/oauth.md"],
                ),
                self.candidate(
                    "cand-dispute", "obs-dispute", "contradicts", "durable", "hold_dispute",
                    status="applied", matched_pages=["wiki/concepts/oauth.md"],
                ),
            ],
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def observation(self, stable_id: str, claim: str, observed_at: str):
        return {
            "observation_id": stable_id,
            "source_path": ".raw/source.md",
            "source_sha256": "a" * 64,
            "source_lines": "1-2",
            "quote": claim,
            "claim": claim,
            "recorded_at": "2026-10-31T10:00:00Z",
            "observed_at": observed_at,
            "observed_at_precision": "day",
            "valid_from": None,
            "valid_to": None,
            "epistemic_status": "observed",
        }

    def candidate(
        self, stable_id: str, observation_id: str, relation: str, durability: str,
        action: str, *, status: str = "pending", matched_pages=None,
    ):
        return {
            "candidate_id": stable_id,
            "observation_ids": [observation_id],
            "matched_pages": matched_pages or [],
            "relation": relation,
            "durability": durability,
            "action": action,
            "reason": "test",
            "decided_at": "2026-10-31T10:01:00Z",
            "status": status,
        }

    def write_jsonl(self, name: str, rows: list[dict]):
        (self.flow / name).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    def run_cli(self, *arguments):
        return subprocess.run(
            ["python3", str(SCRIPT), *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def latest_candidates(self):
        latest = {}
        for line in (self.flow / "candidates.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            latest[row["candidate_id"]] = row
        return latest

    def test_queue_groups_by_owner_and_keeps_applied_dispute_open(self):
        result = self.run_cli("queue", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        ids = {row["candidate_id"] for row in payload["candidates"]}
        self.assertEqual(ids, {"cand-old", "cand-update", "cand-dispute"})
        self.assertEqual(
            payload["groups"]["wiki/concepts/oauth.md"],
            ["cand-dispute", "cand-update"],
        )
        self.assertEqual(payload["metrics"]["open_candidates"], 3)

    def test_decide_appends_transition_without_rewriting_history(self):
        before = len((self.flow / "candidates.jsonl").read_text(encoding="utf-8").splitlines())
        result = self.run_cli(
            "decide", "cand-update", "--status", "applied",
            "--reason", "Updated wiki/concepts/oauth.md with evidence.", "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        after = len((self.flow / "candidates.jsonl").read_text(encoding="utf-8").splitlines())
        self.assertEqual(after, before + 1)
        latest = self.latest_candidates()["cand-update"]
        self.assertEqual(latest["status"], "applied")
        self.assertEqual(latest["action"], "update_existing")

    def test_recent_only_cannot_be_marked_applied(self):
        result = self.run_cli("decide", "cand-old", "--status", "applied")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recent_only stays pending", result.stderr)

    def test_expire_is_dry_run_then_append_only_apply(self):
        dry = self.run_cli("expire", "--before", "2026-01-01", "--json")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["candidate_ids"], ["cand-old"])
        self.assertEqual(self.latest_candidates()["cand-old"]["status"], "pending")

        applied = self.run_cli(
            "expire", "--before", "2026-01-01", "--apply", "--json"
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(self.latest_candidates()["cand-old"]["status"], "expired")

        queue = self.run_cli("queue", "--json")
        ids = {row["candidate_id"] for row in json.loads(queue.stdout)["candidates"]}
        self.assertNotIn("cand-old", ids)


if __name__ == "__main__":
    unittest.main()
