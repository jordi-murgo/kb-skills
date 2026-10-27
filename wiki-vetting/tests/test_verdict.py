#!/usr/bin/env python3
"""Offline tests for the wiki-vetting decision logic.

These never touch the network: they exercise `verdict_for` and `resolve_thresholds`
directly, which is where the calibration bug lived. The live-model check lives in
`scripts/calibration.py`.
"""

import importlib.util
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wiki-vet.py"


def load_module():
    spec = importlib.util.spec_from_file_location("wiki_vet_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ThresholdConfigTest(unittest.TestCase):
    def setUp(self):
        # Import-time bootstrap resolves a vault root; run it against a fixture.
        import os
        import tempfile

        self.vault = Path(tempfile.mkdtemp(prefix="vet-thresholds-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        (self.vault / "kb-config.yaml").write_text("decisions:\n  model: test\n", encoding="utf-8")
        self._cwd = Path.cwd()
        os.chdir(self.vault)

    def tearDown(self):
        import os
        import shutil

        os.chdir(self._cwd)
        shutil.rmtree(self.vault, ignore_errors=True)

    def test_defaults_are_asymmetric(self):
        mod = load_module()
        thresholds, band = mod.resolve_thresholds({})
        self.assertGreater(thresholds["grounded"], thresholds["sensitive"])
        self.assertEqual(band, mod.DEFAULT_BAND)

    def test_config_overrides_one_axis(self):
        mod = load_module()
        thresholds, _ = mod.resolve_thresholds({"thresholds": {"duplicate": 0.9}})
        self.assertEqual(thresholds["duplicate"], 0.9)
        self.assertEqual(thresholds["grounded"], mod.DEFAULT_THRESHOLDS["grounded"])

    def test_unknown_axis_fails_loudly(self):
        mod = load_module()
        with self.assertRaises(SystemExit):
            mod.resolve_thresholds({"thresholds": {"groundd": 0.9}})

    def test_out_of_range_fails_loudly(self):
        mod = load_module()
        with self.assertRaises(SystemExit):
            mod.resolve_thresholds({"thresholds": {"grounded": 1.5}})
        with self.assertRaises(SystemExit):
            mod.resolve_thresholds({"thresholds": {"band": 0.9}})


class VerdictTest(unittest.TestCase):
    """The verdict must refuse to accept when the quote is weakly supported."""

    @classmethod
    def setUpClass(cls):
        import os
        import tempfile

        cls.vault = Path(tempfile.mkdtemp(prefix="vet-verdict-"))
        (cls.vault / "wiki").mkdir()
        (cls.vault / ".git").mkdir()
        (cls.vault / "kb-config.yaml").write_text("decisions:\n  model: test\n", encoding="utf-8")
        cls._cwd = Path.cwd()
        os.chdir(cls.vault)
        cls.mod = load_module()

    @classmethod
    def tearDownClass(cls):
        import os
        import shutil

        os.chdir(cls._cwd)
        shutil.rmtree(cls.vault, ignore_errors=True)

    def test_grounded_below_threshold_rejects_even_if_durable(self):
        probs = {"grounded": 0.85, "durable": 0.95, "duplicate": 0.0, "sensitive": 0.0}
        verdict, bits, signal = self.mod.verdict_for(probs)
        self.assertFalse(bits["grounded"])
        self.assertEqual(verdict, "reject")

    def test_old_0_5_cut_would_have_accepted(self):
        """Regression guard: 0.85 cleared the old 0.5 bar and became `accept`."""
        probs = {"grounded": 0.85, "durable": 0.95, "duplicate": 0.0, "sensitive": 0.0}
        verdict, _, _ = self.mod.verdict_for(probs)
        self.assertNotEqual(verdict, "accept")

    def test_band_forces_review_near_the_threshold(self):
        limit = self.mod.DEFAULT_THRESHOLDS["grounded"]
        probs = {"grounded": limit - 0.01, "durable": 0.95, "duplicate": 0.05, "sensitive": 0.0}
        verdict, _, signal = self.mod.verdict_for(probs)
        self.assertEqual(verdict, "review")
        self.assertIn("grounded", signal["undecided_axes"])

    def test_low_sensitive_threshold_rejects_early(self):
        probs = {"grounded": 0.99, "durable": 0.99, "duplicate": 0.0, "sensitive": 0.35}
        verdict, bits, _ = self.mod.verdict_for(probs)
        self.assertTrue(bits["sensitive"])
        self.assertEqual(verdict, "reject")

    def test_clean_high_confidence_accepts(self):
        probs = {"grounded": 0.99, "durable": 0.95, "duplicate": 0.05, "sensitive": 0.01}
        verdict, _, signal = self.mod.verdict_for(probs)
        self.assertEqual(verdict, "accept")
        self.assertGreater(signal["confidence"], 0)

    def test_skip_grounded_still_requires_durable(self):
        probs = {"durable": 0.95, "duplicate": 0.0, "sensitive": 0.0}
        verdict, bits, _ = self.mod.verdict_for(probs, skip_grounded=True)
        self.assertTrue(bits["grounded"])
        self.assertEqual(verdict, "accept")

    def test_missing_axis_is_undecided_not_false_accept(self):
        probs = {"durable": 0.95, "duplicate": 0.0, "sensitive": 0.0}
        verdict, _, signal = self.mod.verdict_for(probs)
        self.assertEqual(verdict, "review")
        self.assertIn("grounded", signal["undecided_axes"])


class CalibrationSetTest(unittest.TestCase):
    def test_set_is_balanced_and_labelled(self):
        rows = json.loads(
            (Path(__file__).resolve().parent / "calibration-set.json").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(rows), 20)
        negatives = [r for r in rows if r["grounded"] is False]
        positives = [r for r in rows if r["grounded"] is True]
        self.assertEqual(len(negatives), 10)
        self.assertGreaterEqual(len(positives), 10)
        self.assertGreaterEqual(len([r for r in rows if r.get("sensitive")]), 3)
        for row in rows:
            self.assertIn("claim", row)
            self.assertIn("quote", row)


class ApiKeyHeaderTest(unittest.TestCase):
    """`call_systemone` must send a bearer token only when one is configured.

    A hosted System One endpoint (decision.vllm-sr.ai) rejects the request with
    401 and no useful message without it, while a local Ollama ignores the header.
    The bug this guards: the function had no `api_key` parameter at all, so
    `wiki-vetting` could not reach any remote decision service — found by trying
    to measure d3-turbo through it.
    """

    def setUp(self):
        import os
        import tempfile

        self.vault = Path(tempfile.mkdtemp(prefix="vet-apikey-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        self._cwd = Path.cwd()
        os.chdir(self.vault)

    def tearDown(self):
        import os
        import shutil

        os.chdir(self._cwd)
        shutil.rmtree(self.vault, ignore_errors=True)

    def _capture_headers(self, api_key):
        import builtins
        import urllib.request

        captured = {}
        original = urllib.request.urlopen

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"answers": {"q1": {"noul": 0.9}}}
                ).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.headers)
            return FakeResponse()

        urllib.request.urlopen = fake_urlopen
        try:
            module = load_module()
            module.call_systemone(
                "http://example.test", {"questions": {"q1": {}}}, ["q1"], api_key
            )
        finally:
            urllib.request.urlopen = original
        return captured["headers"]

    def test_no_header_when_no_key(self):
        headers = self._capture_headers("")
        self.assertNotIn("Authorization", headers)

    def test_bearer_header_when_key_present(self):
        headers = self._capture_headers("sk-test-123")
        self.assertEqual(headers.get("Authorization"), "Bearer sk-test-123")


if __name__ == "__main__":
    unittest.main()
