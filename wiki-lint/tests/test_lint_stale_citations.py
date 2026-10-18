#!/usr/bin/env python3
"""Tests for lint-stale-citations.py — the stale-citation gate inside wiki-lint.

Runs against a throwaway vault fixture. Per repo rules: a gate must be proven
to fail on a fault before it is trusted.
"""

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint-stale-citations.py"

SRC_TEXT = "".join(f"line {i}\n" for i in range(1, 11))


class VaultCase(unittest.TestCase):
    """A throwaway vault with wiki/, .git/ and a 10-line source in .raw/."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="stale-citations-test-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        (self.vault / ".raw").mkdir()
        self.source(".raw/src.md", SRC_TEXT)

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    # -- fixture helpers ------------------------------------------------------

    def source(self, rel: str, content: str):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def page(self, body: str, name: str = "P"):
        (self.vault / "wiki" / f"{name}.md").write_text(
            "---\ntitle: " + name + "\n---\n" + body + "\n", encoding="utf-8"
        )

    def entry(self, rel: str = ".raw/src.md", content: str = SRC_TEXT,
              algo: str = "sha256", upper: bool = False) -> dict:
        digest = hashlib.new(algo, content.encode("utf-8")).hexdigest()
        return {"hash": digest.upper() if upper else digest,
                "ingested_at": "2026-01-01T00:00:00Z"}

    def write_manifest(self, sources: dict):
        (self.vault / ".raw" / ".manifest.json").write_text(
            json.dumps({"sources": sources}), encoding="utf-8"
        )

    def run_lint(self):
        result = subprocess.run(
            ["python3", str(SCRIPT), "--json"],
            cwd=self.vault, capture_output=True, text=True, timeout=30,
        )
        return result.returncode, json.loads(result.stdout)

    def kinds(self, payload):
        return {f["kind"] for f in payload["findings"]}

    def findings_of_kind(self, payload, kind):
        return [f for f in payload["findings"] if f["kind"] == kind]

    # -- happy path -----------------------------------------------------------

    def test_current_source_sha256_passes(self):
        self.write_manifest({".raw/src.md": self.entry()})
        self.page("A claim. ^[.raw/src.md]")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(payload["errors"], 0)
        self.assertEqual(payload["cited_sources"], 1)

    def test_current_source_md5_passes(self):
        self.write_manifest({".raw/src.md": self.entry(algo="md5", upper=True)})
        self.page("A claim. ^[.raw/src.md:2-4]")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(payload["findings"], [])

    def test_no_citations_exits_zero(self):
        self.page("Just prose, no markers here.")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(payload["cited_sources"], 0)
        self.assertEqual(payload["findings"], [])

    def test_source_missing_on_disk_is_skipped(self):
        # lint-citations.py reports missing_source; this gate must not duplicate it.
        self.write_manifest({".raw/absent.md": self.entry()})
        self.page("A claim. ^[.raw/absent.md]")
        code, payload = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(payload["findings"], [])

    # -- faults that must fail the gate ---------------------------------------

    def test_gate_fault_stale_source_fails(self):
        self.write_manifest({".raw/src.md": self.entry(content="old content\n")})
        self.page("A claim. ^[.raw/src.md]")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), {"stale_source"})
        finding = self.findings_of_kind(payload, "stale_source")[0]
        self.assertIn(".raw/src.md", finding["message"])
        self.assertIn("current", finding["message"])

    def test_untracked_source_fails(self):
        self.write_manifest({})  # manifest exists, tracks nothing
        self.page("A claim. ^[.raw/src.md]")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), {"untracked_source"})

    def test_missing_manifest_makes_all_citations_untracked(self):
        self.page("A claim. ^[.raw/src.md]")  # no .raw/.manifest.json at all
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), {"untracked_source"})

    # -- manifest edge cases ---------------------------------------------------

    def test_malformed_manifest_exits_two(self):
        (self.vault / ".raw" / ".manifest.json").write_text(
            "{not json", encoding="utf-8"
        )
        self.page("A claim. ^[.raw/src.md]")
        result = subprocess.run(
            ["python3", str(SCRIPT)],
            cwd=self.vault, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(".manifest.json", result.stderr)

    def test_manifest_without_sources_key_is_untracked(self):
        (self.vault / ".raw" / ".manifest.json").write_text(
            json.dumps({"ingest": "session"}), encoding="utf-8"
        )
        self.page("A claim. ^[.raw/src.md]")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(payload), {"untracked_source"})

    # -- reporting behaviour ---------------------------------------------------

    def test_duplicate_marker_reported_once_per_page(self):
        self.write_manifest({".raw/src.md": self.entry(content="old content\n")})
        self.page("A. ^[.raw/src.md]\nB. ^[.raw/src.md]")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        stale = self.findings_of_kind(payload, "stale_source")
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["line"], 4)  # first marker's line

    def test_same_source_two_pages_two_findings(self):
        self.write_manifest({".raw/src.md": self.entry(content="old content\n")})
        self.page("A. ^[.raw/src.md]", name="P1")
        self.page("B. ^[.raw/src.md]", name="P2")
        code, payload = self.run_lint()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.findings_of_kind(payload, "stale_source")), 2)
        self.assertEqual(payload["cited_sources"], 1)

    def test_findings_carry_schema_fields(self):
        self.write_manifest({".raw/src.md": self.entry(content="old content\n")})
        self.page("A claim. ^[.raw/src.md]", name="Target")
        code, payload = self.run_lint()
        finding = payload["findings"][0]
        self.assertEqual(
            sorted(finding.keys()),
            ["kind", "line", "message", "page", "severity"],
        )
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["page"], "wiki/Target.md")
        self.assertEqual(finding["line"], 4)
        self.assertEqual(
            sorted(payload.keys()),
            ["cited_sources", "errors", "findings", "pages", "warnings"],
        )
        self.assertEqual(payload["pages"], 1)
        self.assertEqual(payload["errors"], 1)
        self.assertEqual(payload["warnings"], 0)


if __name__ == "__main__":
    unittest.main()