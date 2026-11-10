#!/usr/bin/env python3
"""Lifecycle audit keeps maturity, verification, and decision state independent."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "wiki-lifecycle.py"


def load_lifecycle(root: Path):
    module_name = f"wiki_lifecycle_{uuid.uuid4().hex}"
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


class LifecycleAuditTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wiki-lifecycle-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki" / "decisions").mkdir(parents=True)
        (self.root / "wiki" / "concepts").mkdir(parents=True)
        self.lifecycle = load_lifecycle(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_page(self, relative: str, frontmatter: str, body: str = "# Page\n"):
        path = self.root / "wiki" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")

    def test_reports_each_independent_missing_dimension(self):
        self.write_page(
            "decisions/incomplete.md",
            "type: decision\ntitle: Incomplete\ncreated: 2026-10-31\nupdated: 2026-10-31\n",
        )
        report = self.lifecycle.audit()
        self.assertEqual(report["missing_maturity"], [{"page": "wiki/decisions/incomplete.md"}])
        self.assertEqual(report["missing_verification"], [{"page": "wiki/decisions/incomplete.md"}])
        self.assertEqual(report["missing_decision_status"], [{"page": "wiki/decisions/incomplete.md"}])

    def test_supported_list_evidence_satisfies_audit(self):
        self.write_page(
            "concepts/supported.md",
            "type: concept\ntitle: Supported\nmaturity: mature\nverification: supported\n"
            "evidence:\n  - \"[[source]]\"\n",
        )
        report = self.lifecycle.audit()
        self.assertEqual(report["supported_missing_evidence"], [])

    def test_dispute_queue_reads_verification_and_callouts(self):
        self.write_page(
            "concepts/disputed.md",
            "type: concept\ntitle: Disputed\nmaturity: developing\nverification: disputed\n",
            "# Disputed\n\n> [!contradiction] Conflicting source\n",
        )
        report = self.lifecycle.audit()
        reasons = {item["reason"] for item in report["disputes"]}
        self.assertIn("verification: disputed", reasons)
        self.assertIn("> [!contradiction] Conflicting source", reasons)


if __name__ == "__main__":
    unittest.main()
