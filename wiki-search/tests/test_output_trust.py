#!/usr/bin/env python3
"""Tests for output verification quarantine in wiki-search.

A self-authored answer or summary stays out of canonical retrieval until its
`verification` qualifies. `maturity` is independent and never opens the gate.
The gate remains opt-in: an unconfigured vault indexes every page.
"""

import contextlib
import importlib.util
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

ANSWER = """---
type: {page_type}
title: "Answer"
maturity: {maturity}
verification: {verification}
created: 2026-10-20
updated: 2026-10-20
tags: [question]
---

# Answer

Body long enough to become a chunk. {filler}
"""

FILLER = "Supported by evidence. " * 30


def page(title: str, body: str) -> str:
    return f"# {title}\n\n{body} " + ("filler context sentence " * 12) + "\n"


def load_search(root: Path):
    module_name = f"wiki_search_trust_{uuid.uuid4().hex}"
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


class TrustCase(unittest.TestCase):
    trust_config: dict | None = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wiki-search-trust-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki" / "questions").mkdir(parents=True)
        (self.root / "wiki" / "concepts").mkdir(parents=True)
        if self.trust_config is not None:
            (self.root / "kb-config.yaml").write_text(
                self.trust_config_yaml(), encoding="utf-8"
            )
        self.search = load_search(self.root)
        self.search.embed_batch = lambda texts, _model: [[0.5, 0.5] for _ in texts]

    def trust_config_yaml(self) -> str:
        lines = ["trust:", "  output_types:"]
        for value in self.trust_config.get("output_types", []):
            lines.append(f"    - {value}")
        if "indexed_verification" in self.trust_config:
            lines.append("  indexed_verification:")
            for value in self.trust_config["indexed_verification"]:
                lines.append(f"    - {value}")
        if "require_evidence" in self.trust_config:
            lines.append(f"  require_evidence: {str(self.trust_config['require_evidence']).lower()}")
        lines.append("embeddings:")
        lines.append("  model: test")
        return "\n".join(lines) + "\n"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_answer(self, name: str, verification: str, page_type: str = "question",
                     evidence: str | None = None, maturity: str = "developing"):
        path = self.root / "wiki" / "questions" / f"{name}.md"
        body = ANSWER.format(
            maturity=maturity,
            verification=verification,
            page_type=page_type,
            filler=FILLER,
        )
        if evidence is not None:
            body = body.replace("tags: [question]\n", f"tags: [question]\n{evidence}\n", 1)
        path.write_text(body, encoding="utf-8")
        return path.relative_to(self.root).as_posix()

    def write_concept(self, name: str, maturity: str = "mature"):
        path = self.root / "wiki" / "concepts" / f"{name}.md"
        path.write_text(
            ANSWER.format(
                maturity=maturity, verification="supported", page_type="concept", filler=FILLER
            ),
            encoding="utf-8",
        )
        return path.relative_to(self.root).as_posix()

    def build(self):
        import contextlib
        import io

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.search.cmd_build(SimpleNamespace(model="test", json=True))
        return code, json.loads(stdout.getvalue())

    def indexed_paths(self):
        conn = self.search.connect_readonly()
        try:
            return {row[0] for row in conn.execute("SELECT path FROM pages")}
        finally:
            conn.close()


class UnconfiguredVaultTest(TrustCase):
    """The default must not change existing behavior."""

    trust_config = None

    def test_everything_is_indexed_without_trust_config(self):
        answer = self.write_answer("draft-answer", "unreviewed")
        code, _ = self.build()
        self.assertEqual(code, 0)
        self.assertIn(answer, self.indexed_paths())


class QuarantineTest(TrustCase):
    trust_config = {"output_types": ["question"]}

    def test_unreviewed_answer_is_not_indexed(self):
        answer = self.write_answer("draft-answer", "unreviewed")
        code, report = self.build()
        self.assertEqual(code, 0)
        self.assertNotIn(answer, self.indexed_paths())
        self.assertEqual(report["quarantined_count"], 1)
        self.assertEqual(report["quarantined"][0]["verification"], "unreviewed")

    def test_supported_answer_is_indexed(self):
        answer = self.write_answer("filed-answer", "supported")
        self.build()
        self.assertIn(answer, self.indexed_paths())

    def test_missing_verification_is_quarantined_and_reported(self):
        path = self.root / "wiki" / "questions" / "unverified.md"
        path.write_text(
            "---\ntype: question\ntitle: A\nmaturity: developing\ncreated: 2026-10-20\n"
            "updated: 2026-10-20\ntags: [question]\n---\n\n# A\n\n" + FILLER,
            encoding="utf-8",
        )
        _, report = self.build()
        self.assertEqual(report["quarantined"][0]["verification"], "(no verification)")

    def test_other_page_types_are_never_gated(self):
        concept = self.write_concept("concept", maturity="seed")
        self.build()
        self.assertIn(concept, self.indexed_paths(),
                      "only configured output types may be quarantined")

    def test_verification_matching_is_case_insensitive(self):
        upper = self.write_answer("upper", "SUPPORTED")
        lower = self.write_answer("lower", "unreviewed")
        self.build()
        indexed = self.indexed_paths()
        self.assertIn(upper, indexed)
        self.assertNotIn(lower, indexed)


class CustomVerificationTest(TrustCase):
    trust_config = {"output_types": ["question", "summary"],
                    "indexed_verification": ["supported", "corroborated"]}

    def test_every_listed_verification_passes(self):
        self.write_answer("a", "supported")
        self.write_answer("b", "corroborated")
        self.write_answer("c", "unreviewed")
        self.build()
        indexed = self.indexed_paths()
        self.assertEqual(
            sorted(Path(p).stem for p in indexed), ["a", "b"]
        )

    def test_multiple_output_types_are_gated_together(self):
        answer = self.write_answer("answer", "unreviewed")
        summary = self.write_answer("summary", "unreviewed", page_type="summary")
        self.build()
        indexed = self.indexed_paths()
        self.assertNotIn(answer, indexed)
        self.assertNotIn(summary, indexed)


class InvalidConfigTest(TrustCase):
    trust_config = {"output_types": "question"}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wiki-search-trust-bad-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki").mkdir()
        (self.root / "kb-config.yaml").write_text(
            "trust:\n  output_types: question\n", encoding="utf-8"
        )

    def test_a_scalar_output_types_value_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            load_search(self.root)
        self.assertIn("trust.output_types", str(ctx.exception))


class QuarantinedPageIsNotSearchableTest(TrustCase):
    """The point of the gate: a pending answer must not answer questions."""

    trust_config = {"output_types": ["question"]}

    def test_pending_answer_never_appears_in_results(self):
        import contextlib
        import io

        self.write_concept("concept")
        self.write_answer("draft-answer", "unreviewed")
        self.build()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.search.cmd_query(SimpleNamespace(
                text="Answer", top=10, scope="canonical", mode="bm25", model=None, json=True,
            ))
        payload = json.loads(stdout.getvalue())
        pages = {hit["page"] for hit in payload["results"]}
        self.assertTrue(pages, "the concept should still be searchable")
        self.assertNotIn("wiki/questions/draft-answer.md", pages)


class EvidenceTest(TrustCase):
    """`supported` needs an evidence pointer when the stricter gate is enabled."""

    trust_config = {"output_types": ["question"], "require_evidence": True}

    def test_supported_without_evidence_is_held(self):
        answer = self.write_answer("no-evidence", "supported")
        _, report = self.build()
        self.assertNotIn(answer, self.indexed_paths())
        self.assertEqual(report["quarantined"][0]["verification"], "supported without evidence")

    def test_supported_with_scalar_evidence_is_indexed(self):
        answer = self.write_answer(
            "scalar", "supported", evidence='evidence: "https://example.test/commit/abc"'
        )
        self.build()
        self.assertIn(answer, self.indexed_paths())

    def test_supported_with_list_evidence_is_indexed(self):
        answer = self.write_answer(
            "listed", "supported",
            evidence='evidence:\n  - "https://example.test/commit/one"\n'
                     '  - "https://example.test/commit/two"',
        )
        self.build()
        self.assertIn(answer, self.indexed_paths())

    def test_empty_evidence_list_is_not_evidence(self):
        answer = self.write_answer("empty-list", "supported", evidence="evidence:")
        _, report = self.build()
        self.assertNotIn(answer, self.indexed_paths())
        self.assertEqual(report["quarantined"][0]["verification"], "supported without evidence")

    def test_requirement_is_off_by_default(self):
        self.search.REQUIRE_EVIDENCE = False
        answer = self.write_answer("no-evidence", "supported")
        self.build()
        self.assertIn(answer, self.indexed_paths())


class StatusCommandTest(TrustCase):
    trust_config = {"output_types": ["question"]}

    def test_quarantined_page_is_reported_as_quarantined_not_deleted(self):
        answer = self.write_answer("draft-answer", "unreviewed")
        self.build()
        # Once indexed then quarantined: the row disappears but must be labelled.
        self.write_answer("draft-answer", "supported")
        self.build()
        self.assertIn(answer, self.indexed_paths())

        self.write_answer("draft-answer", "unreviewed")
        import io

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.search.cmd_status(SimpleNamespace(json=True))
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["quarantined_count"], 1)
        reasons = {s["page"]: s["reason"] for s in payload["stale"]}
        self.assertEqual(reasons.get(answer), "quarantined")


if __name__ == "__main__":
    unittest.main()
