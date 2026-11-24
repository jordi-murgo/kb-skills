#!/usr/bin/env python3
"""Offline tests for the wiki-vetting decision logic.

These never touch the network: they exercise `verdict_for` and `resolve_thresholds`
directly, which is where the calibration bug lived. The live-model check lives in
`scripts/calibration.py`.
"""

import importlib.util
import json
import unittest
import urllib.request
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


class RoutingActionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        import tempfile

        cls.vault = Path(tempfile.mkdtemp(prefix="vet-routing-"))
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

    def route(self, probs, *, skip_grounded=False):
        verdict, bits, signal = self.mod.verdict_for(probs, skip_grounded=skip_grounded)
        return self.mod.routing_action(verdict, bits, signal)

    def test_sensitive_claim_is_rejected_explicitly(self):
        probs = {"grounded": 0.99, "durable": 0.95, "duplicate": 0.0, "sensitive": 0.35}
        self.assertEqual(self.route(probs), "reject_sensitive")

    def test_ungrounded_claim_is_rejected_explicitly(self):
        probs = {"grounded": 0.2, "durable": 0.95, "duplicate": 0.0, "sensitive": 0.0}
        self.assertEqual(self.route(probs), "reject_ungrounded")

    def test_ephemeral_claim_stays_recent_only(self):
        probs = {"grounded": 0.99, "durable": 0.2, "duplicate": 0.0, "sensitive": 0.0}
        self.assertEqual(self.route(probs), "recent_only")

    def test_duplicate_durable_claim_routes_to_merge_or_noop(self):
        probs = {"grounded": 0.99, "durable": 0.95, "duplicate": 0.95, "sensitive": 0.0}
        self.assertEqual(self.route(probs), "merge_or_noop")

    def test_novel_durable_claim_routes_to_promotion(self):
        probs = {"grounded": 0.99, "durable": 0.95, "duplicate": 0.0, "sensitive": 0.0}
        self.assertEqual(self.route(probs), "promote_candidate")

    def test_uncertain_axis_requires_human_review(self):
        probs = {
            "grounded": self.mod.DEFAULT_THRESHOLDS["grounded"] - 0.01,
            "durable": 0.95,
            "duplicate": 0.0,
            "sensitive": 0.0,
        }
        self.assertEqual(self.route(probs), "human_review")


class CalibrationSplitTest(unittest.TestCase):
    """The tuning set must not be the measuring set.

    2026-11-24 audit: calibration-set.json is what DEFAULT_THRESHOLDS came
    from, so scoring all of it reports recall of the tuning data. The split is
    content-addressed so it is stable, reproducible and needs no hand-kept
    manifest.
    """

    @staticmethod
    def split_fn():
        from pathlib import Path as _Path
        script = _Path(__file__).resolve().parent.parent / "scripts" / "calibration.py"
        src = script.read_text(encoding="utf-8")
        start = src.index("def split_holdout")
        end = src.index("def check_floors")
        ns = {}
        exec(compile(src[start:end], "calibration-split", "exec"), ns)
        return ns["split_holdout"]

    def test_split_is_deterministic_and_partitions(self):
        split_holdout = self.split_fn()
        rows = [{"claim": f"c{i}", "quote": f"q{i}"} for i in range(100)]
        tuning, holdout = split_holdout(rows)
        self.assertEqual(len(tuning) + len(holdout), len(rows))
        self.assertEqual(set(map(id, tuning)) & set(map(id, holdout)), set())
        tuning2, holdout2 = split_holdout(rows)
        self.assertEqual([r["claim"] for r in holdout], [r["claim"] for r in holdout2])

    def test_editing_one_case_does_not_reshuffle_the_rest(self):
        split_holdout = self.split_fn()
        rows = [{"claim": f"c{i}", "quote": f"q{i}"} for i in range(60)]
        _t1, before = split_holdout(rows)
        changed = [dict(r) for r in rows]
        changed[0]["quote"] = "edited"
        _t2, after = split_holdout(changed)
        untouched_before = {r["claim"] for r in before} - {"c0"}
        untouched_after = {r["claim"] for r in after} - {"c0"}
        self.assertEqual(untouched_before, untouched_after)

    def test_real_set_has_a_holdout_big_enough_to_measure(self):
        split_holdout = self.split_fn()
        rows = json.loads(
            (Path(__file__).resolve().parent / "calibration-set.json").read_text(encoding="utf-8"))
        _tuning, holdout = split_holdout(rows)
        share = len(holdout) / len(rows)
        self.assertGreaterEqual(share, 0.2, "holdout under 20% is not a check")
        self.assertLessEqual(share, 0.4, "holdout over 40% starves the tuning side")
        self.assertGreaterEqual(
            sum(1 for r in holdout if r["grounded"] is False), 10,
            "the holdout needs >=10 negatives to carry the FPR floor")
        self.assertGreaterEqual(
            sum(1 for r in holdout if r.get("sensitive")), 5,
            "the holdout needs several secrets to carry the recall floor")


class CalibrationIntervalTest(unittest.TestCase):
    """A floor on 3 items is a coin toss; the interval must say so.

    2026-11-24 audit: the sensitive axis has 3 cases, so one flip moves recall
    by 33 points against a 0.90 floor. Reporting the Wilson interval turns an
    invisible coin toss into a visible width.
    """

    @staticmethod
    def intervals():
        import importlib.util
        from pathlib import Path as _Path
        script = _Path(__file__).resolve().parent.parent / "scripts" / "calibration.py"
        src = script.read_text(encoding="utf-8")
        start = src.index("def wilson_interval")
        end = src.index("def check_floors")
        ns = {}
        exec(compile(src[start:end], "calibration-fns", "exec"), ns)
        return ns["axis_intervals"], ns["wilson_interval"]

    def metrics(self, tp_secret=3, secrets=3, fp_grounded=2, negatives=10):
        return {
            "grounded": {"tp": negatives - fp_grounded, "tn": negatives - fp_grounded,
                         "fp": fp_grounded, "fn": 0,
                         "precision": 0.8, "recall": 1.0,
                         "false_positive_rate": fp_grounded / negatives},
            "sensitive": {"tp": tp_secret, "tn": 10, "fp": 0, "fn": secrets - tp_secret,
                          "precision": 1.0, "recall": tp_secret / secrets},
        }

    def test_three_item_axis_is_flagged_too_narrow(self):
        axis_intervals, _ = self.intervals()
        out = axis_intervals(self.metrics())
        self.assertIn("sensitive_recall", out["too_narrow"])
        self.assertNotIn("grounded_false_positive_rate", out["too_narrow"])

    def test_perfect_recall_on_three_items_has_a_wide_interval(self):
        axis_intervals, _ = self.intervals()
        info = axis_intervals(self.metrics())["sensitive_recall"]
        self.assertEqual(info["n"], 3)
        low, high = info["interval"]
        self.assertLess(low, 0.6, "3/3 must not look like a tight 1.0")
        self.assertGreater(high - low, 0.4)

    def test_losing_one_secret_widens_below_the_floor(self):
        axis_intervals, _ = self.intervals()
        info = axis_intervals(self.metrics(tp_secret=2))["sensitive_recall"]
        self.assertAlmostEqual(2 / 3, 0.667, places=3)
        self.assertLess(info["interval"][0], 0.5)

    def test_thirty_items_make_the_floor_measurable(self):
        axis_intervals, _ = self.intervals()
        out = axis_intervals(self.metrics(tp_secret=30, secrets=30))
        self.assertEqual(out["too_narrow"], [])
        low, _high = out["sensitive_recall"]["interval"]
        self.assertGreater(low, 0.85, "30/30 should have a tight lower bound")

    def test_empty_axis_returns_no_interval(self):
        _, wilson = self.intervals()
        self.assertIsNone(wilson(0, 0))


class CalibrationSetTest(unittest.TestCase):
    """The set must be big enough on each gated axis for the floor to mean
    something: a floor needs >=10 items on its axis (2026-11-24 audit), and
    one flip must not be able to swing it by more than a few points.
    """

    def setUp(self):
        self.rows = json.loads(
            (Path(__file__).resolve().parent / "calibration-set.json").read_text(encoding="utf-8"))

    def test_every_axis_is_large_enough_to_gate_on(self):
        negatives = [r for r in self.rows if r["grounded"] is False]
        positives = [r for r in self.rows if r["grounded"] is True]
        secrets = [r for r in self.rows if r.get("sensitive")]
        self.assertGreaterEqual(len(negatives), 30, "grounded FPR axis too small")
        self.assertGreaterEqual(len(positives), 30, "grounded recall axis too small")
        self.assertGreaterEqual(len(secrets), 15, "sensitive recall axis too small")

    def test_one_flip_cannot_swing_a_floor(self):
        negatives = sum(1 for r in self.rows if r["grounded"] is False)
        secrets = sum(1 for r in self.rows if r.get("sensitive"))
        self.assertLessEqual(100 / negatives, 5, "one false positive must cost <=5 pts")
        self.assertLessEqual(100 / secrets, 10, "one lost secret must cost <=10 pts")

    def test_rows_are_labelled_and_unique(self):
        seen = set()
        for row in self.rows:
            self.assertIn("claim", row)
            self.assertIn("quote", row)
            self.assertIsInstance(row["grounded"], bool)
            self.assertIsInstance(row.get("sensitive"), bool)
            key = (row["claim"], row["quote"])
            self.assertNotIn(key, seen, f"duplicate case: {row['claim'][:60]}")
            seen.add(key)

    def test_set_has_both_sides_of_every_axis(self):
        for grounded in (True, False):
            self.assertTrue(any(r["grounded"] is grounded for r in self.rows))
        self.assertTrue(any(r.get("sensitive") for r in self.rows))
        self.assertTrue(any(not r.get("sensitive") for r in self.rows))


class FakeResponse:
    """Minimal urllib response object driving the real handler chain."""

    def __init__(self, code, body=b"", headers=None, url="https://source.test/v1/systemone"):
        import email
        import io

        self.code = code
        self.msg = "OK"
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self._buf = io.BytesIO(body)
        self.url = url

    def read(self, *args):
        return self._buf.read(*args)

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass


class FakeTransport(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """In-memory HTTP(S) transport: records requests, serves a script.

    Registered before NoRedirect in a real opener, so redirect handling runs
    through the production handler chain instead of being faked."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.requests = []

    def http_open(self, req):
        return self._serve(req)

    def https_open(self, req):
        return self._serve(req)

    def _serve(self, req):
        self.requests.append((req.get_method(), req.full_url,
                              dict(req.header_items()), req.data))
        return self.script.pop(0)


class TransportCase(unittest.TestCase):
    """call_systemone against an in-memory transport; no live requests."""

    def setUp(self):
        import os
        import tempfile

        self.vault = Path(tempfile.mkdtemp(prefix="vet-transport-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        self._cwd = Path.cwd()
        os.chdir(self.vault)
        self.module = load_module()

    def tearDown(self):
        import os
        import shutil

        os.chdir(self._cwd)
        shutil.rmtree(self.vault, ignore_errors=True)

    def run_call(self, script, endpoint, api_key=""):
        transport = FakeTransport(script)
        original = self.module.make_opener

        def fake_make_opener():
            return urllib.request.build_opener(transport, self.module.NoRedirect())

        self.module.make_opener = fake_make_opener
        try:
            result = self.module.call_systemone(
                endpoint, {"questions": {"q1": {}}}, ["q1"], api_key)
            return transport, result, None
        except SystemExit as exc:
            return transport, None, str(exc)
        finally:
            self.module.make_opener = original

    @staticmethod
    def ok_body():
        return json.dumps({"answers": {"q1": {"noul": 0.9}}}).encode()

    def test_bearer_reaches_intended_https_origin_once(self):
        transport, result, error = self.run_call(
            [FakeResponse(200, body=self.ok_body())],
            "https://decision.example", api_key="sk-SENTINEL")
        self.assertIsNone(error)
        self.assertEqual(result, {"q1": 0.9})
        self.assertEqual(len(transport.requests), 1)
        method, url, headers, _ = transport.requests[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.startswith("https://decision.example/v1/systemone"), url)
        self.assertEqual(headers.get("Authorization"), "Bearer sk-SENTINEL")

    def test_no_bearer_when_unauthenticated(self):
        transport, result, _ = self.run_call(
            [FakeResponse(200, body=self.ok_body())], "https://decision.example")
        self.assertEqual(result, {"q1": 0.9})
        self.assertNotIn("Authorization", transport.requests[0][2])

    def test_redirects_refused_for_every_code(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                transport, result, error = self.run_call(
                    [FakeResponse(code, headers={
                        "Location": "https://evil.test/steal?sk-SENTINEL"})],
                    "https://decision.example", api_key="sk-SENTINEL")
                self.assertIsNotNone(error, "redirect must fail closed")
                self.assertIn(f"HTTP {code}", error)
                self.assertNotIn("evil.test", error)
                self.assertNotIn("SENTINEL", error)
                self.assertNotIn("Location", error)
                # exactly one request; the redirect target was never contacted
                self.assertEqual(len(transport.requests), 1)
                self.assertTrue(
                    transport.requests[0][1].startswith("https://decision.example"))

    def test_authenticated_remote_http_refused_without_request(self):
        transport, result, error = self.run_call(
            [FakeResponse(200, body=self.ok_body())],
            "http://decision.example", api_key="sk-SENTINEL")
        self.assertIsNotNone(error)
        self.assertIn("HTTPS", error)
        self.assertEqual(transport.requests, [])

    def test_unauthenticated_evo_shaped_http_allowed(self):
        transport, result, error = self.run_call(
            [FakeResponse(200, body=self.ok_body())],
            "http://evo.example.ts.net:11434")
        self.assertIsNone(error)
        self.assertEqual(result, {"q1": 0.9})
        self.assertEqual(len(transport.requests), 1)

    def test_authenticated_loopback_http_allowed(self):
        transport, result, error = self.run_call(
            [FakeResponse(200, body=self.ok_body())],
            "http://127.0.0.1:11434", api_key="sk-SENTINEL")
        self.assertIsNone(error)
        self.assertEqual(transport.requests[0][2].get("Authorization"),
                         "Bearer sk-SENTINEL")

    def test_401_fails_on_first_attempt(self):
        transport, _, error = self.run_call(
            [FakeResponse(401)], "https://decision.example", api_key="sk-SENTINEL")
        self.assertIn("HTTP 401", error)
        self.assertNotIn("SENTINEL", error)
        self.assertEqual(len(transport.requests), 1)

    def test_503_uses_two_attempts_then_fails(self):
        transport, _, error = self.run_call(
            [FakeResponse(503), FakeResponse(503)],
            "https://decision.example", api_key="sk-SENTINEL")
        self.assertIn("after retry", error)
        self.assertIn("HTTP 503", error)
        self.assertEqual(len(transport.requests), 2)

    def test_503_then_success_parses_normally(self):
        transport, result, error = self.run_call(
            [FakeResponse(503), FakeResponse(200, body=self.ok_body())],
            "https://decision.example", api_key="sk-SENTINEL")
        self.assertIsNone(error)
        self.assertEqual(result, {"q1": 0.9})
        self.assertEqual(len(transport.requests), 2)

    def test_malformed_endpoints_rejected_without_echo(self):
        for endpoint in (
            "https://user:pw@decision.example",
            "https://decision.example/v1/systemone?x=1",
            "https://decision.example/v1/systemone#frag",
            "ftp://decision.example",
            "https://",
            "not a url",
        ):
            with self.subTest(endpoint=endpoint[:28]):
                transport, _, error = self.run_call(
                    [FakeResponse(200, body=self.ok_body())], endpoint,
                    api_key="sk-SENTINEL")
                self.assertIsNotNone(error)
                secret_bits = [part for part in endpoint.split("/")[2:3]]
                for bit in secret_bits:
                    if "@" in bit or "?" in endpoint or "#" in endpoint:
                        self.assertNotIn(bit, error)
                self.assertIn("error:", error)
                self.assertEqual(transport.requests, [])

    def test_error_bodies_never_leak(self):
        transport, _, error = self.run_call(
            [FakeResponse(500, body=b"sk-SENTINEL leaked body"),
             FakeResponse(500, body=b"sk-SENTINEL leaked body")],
            "https://decision.example", api_key="sk-SENTINEL")
        self.assertIn("HTTP 500", error)
        self.assertNotIn("SENTINEL", error)
        self.assertNotIn("leaked", error)


class ApiKeySourceTest(unittest.TestCase):
    """decisions.api_key must be an env reference; CLI selects by name."""

    def setUp(self):
        import os
        import tempfile

        self.vault = Path(tempfile.mkdtemp(prefix="vet-keysrc-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        self._cwd = Path.cwd()
        self._env = dict(os.environ)
        os.chdir(self.vault)

    def tearDown(self):
        import os
        import shutil

        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.vault, ignore_errors=True)

    def load_with_config(self, api_key_yaml, env=None):
        import os

        (self.vault / "kb-config.json").unlink(missing_ok=True)
        (self.vault / "kb-config.yaml").write_text(
            f"decisions:\n  api_key: {api_key_yaml}\n", encoding="utf-8")
        for key in ("VET_TEST_KEY", "OTHER_KEY"):
            os.environ.pop(key, None)
        os.environ.update(env or {})
        return load_module()

    def test_absent_key_is_unauthenticated(self):
        module = self.load_with_config("''")
        self.assertIsNone(module._CONFIGURED_API_KEY_ENV)
        self.assertEqual(module.resolve_api_key(None), "")

    def test_placeholder_records_variable_name(self):
        module = self.load_with_config("${VET_TEST_KEY}",
                                       env={"VET_TEST_KEY": "sk-from-env"})
        self.assertEqual(module._CONFIGURED_API_KEY_ENV, "VET_TEST_KEY")
        self.assertEqual(module.resolve_api_key("VET_TEST_KEY"), "sk-from-env")
        self.assertEqual(module._API_KEY, "sk-from-env")

    def test_empty_default_placeholder_accepted(self):
        module = self.load_with_config("${VET_TEST_KEY:-}")
        self.assertEqual(module._CONFIGURED_API_KEY_ENV, "VET_TEST_KEY")

    def test_literal_secret_refused_at_load(self):
        with self.assertRaises(SystemExit) as ctx:
            self.load_with_config("sk-literal-DO-NOT-PRINT")
        self.assertIn("environment variable", str(ctx.exception))
        self.assertNotIn("sk-literal", str(ctx.exception))

    def test_nonempty_default_refused(self):
        with self.assertRaises(SystemExit):
            self.load_with_config("${VET_TEST_KEY:-fallback}")

    def test_missing_selected_variable_fails_by_name(self):
        module = self.load_with_config("${VET_TEST_KEY}")  # variable unset
        with self.assertRaises(SystemExit) as ctx:
            module.resolve_api_key("VET_TEST_KEY")
        self.assertIn("VET_TEST_KEY", str(ctx.exception))

    def test_cli_rejects_removed_option(self):
        module = self.load_with_config("''")
        for argv in (["--api-key", "sk-SENTINEL"], ["--api-key=sk-SENTINEL"],
                     ["--api-key"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    module.reject_literal_api_key_args(argv)
                self.assertIn("--api-key-env", str(ctx.exception))
                self.assertNotIn("SENTINEL", str(ctx.exception))

    def test_cli_selection_wins_over_unset_config_variable(self):
        module = self.load_with_config("${VET_TEST_KEY}",
                                       env={"OTHER_KEY": "sk-cli"})
        args = module.parse_args(["--api-key-env", "OTHER_KEY"])
        selected = args.api_key_env or module._CONFIGURED_API_KEY_ENV
        self.assertEqual(module.resolve_api_key(selected), "sk-cli")

    def test_bad_variable_name_rejected(self):
        module = self.load_with_config("''")
        with self.assertRaises(SystemExit):
            module.parse_args(["--api-key-env", "not a name"])

    def test_abbreviations_disabled(self):
        module = self.load_with_config("''")
        with self.assertRaises(SystemExit):
            module.parse_args(["--api-k", "X"])

if __name__ == "__main__":
    unittest.main()
