#!/usr/bin/env python3
"""Tests for tests/scan_skill_security.py — the measurement gate itself.

Proves the snapshot contract (tracked bytes retained, ignored caches omitted
without deletion, tracked cache/credential content refused), CLI validation,
profile generation, live sandbox isolation probes, and — when the pinned
scanner is installed — that an inert executable-payload fixture produces a
real executable-code finding without the payload ever running.
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "scan_skill_security.py"
REPO = Path(__file__).resolve().parents[1]

GIT_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": "/nonexistent"}


def load_module():
    spec = importlib.util.spec_from_file_location("scan_skill_security", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                    "-c", "user.name=t", *args],
                   check=True, capture_output=True, env=GIT_ENV)


def make_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="scan-sec-repo-"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True, env=GIT_ENV)
    return repo

class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.repo = make_repo()
        (self.repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        skill = self.repo / "skill-a"
        (skill / "scripts").mkdir(parents=True)
        (skill / "tests").mkdir()
        (skill / "references").mkdir()
        (skill / "SKILL.md").write_text("---\nname: skill-a\n---\nbody\n")
        script = skill / "scripts" / "run.sh"
        script.write_text("#!/bin/sh\ntrue\n")
        script.chmod(0o755)
        helper = skill / "tests" / "helper.py"
        helper.write_text("x = 1\n")
        helper.chmod(0o755)
        (skill / ".dotfile").write_text("dot\n")
        (skill / "references" / "ref.md").write_text("ref\n")
        (skill / ".env.example").write_text("COPILOT_DEBUG=0\n")
        (skill / "notes-untracked.md").write_text("new\n")
        (skill / "__pycache__").mkdir()
        (skill / "__pycache__" / "junk.pyc").write_bytes(b"\x00pyc")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)

    def snapshot(self):
        files, context = self.module.inventory(self.repo)
        snap = Path(tempfile.mkdtemp(prefix="scan-sec-snap-")) / "snapshot"
        self.module.materialize(self.repo, files, snap)
        return files, context, snap

    def test_tracked_bytes_modes_and_hashes_retained(self):
        files, _context, snap = self.snapshot()
        by_rel = {f["rel"]: f for f in files}
        for rel in ("skill-a/SKILL.md", "skill-a/scripts/run.sh",
                    "skill-a/tests/helper.py", "skill-a/.dotfile",
                    "skill-a/references/ref.md", "skill-a/.env.example",
                    "skill-a/notes-untracked.md"):
            self.assertIn(rel, by_rel, f"{rel} missing from inventory")
            self.assertEqual((snap / rel).read_bytes(),
                             (self.repo / rel).read_bytes())
        self.assertEqual(by_rel["skill-a/scripts/run.sh"]["mode"], "0o755")
        self.assertEqual(stat.S_IMODE((snap / "skill-a/scripts/run.sh").stat().st_mode), 0o755)
        self.assertEqual(by_rel["skill-a/SKILL.md"]["sha256"],
                         __import__("hashlib").sha256(
                             (self.repo / "skill-a/SKILL.md").read_bytes()).hexdigest())

    def test_ignored_bytecode_absent_but_source_untouched(self):
        files, context, snap = self.snapshot()
        rels = {f["rel"] for f in files}
        self.assertNotIn("skill-a/__pycache__/junk.pyc", rels)
        self.assertFalse((snap / "skill-a/__pycache__").exists())
        # The local cache is packaging context, not a deletion target.
        self.assertTrue((self.repo / "skill-a/__pycache__/junk.pyc").exists())
        self.assertGreaterEqual(context["ignored_local_files"], 1)

    def test_deleted_from_worktree_is_recorded_not_crashed(self):
        (self.repo / "skill-a" / "notes-untracked.md").unlink()  # untracked: not listed anyway
        victim = self.repo / "skill-a" / "references" / "ref.md"
        victim.unlink()
        files, context, snap = self.snapshot()
        rels = {f["rel"] for f in files}
        self.assertNotIn("skill-a/references/ref.md", rels)
        self.assertIn("skill-a/references/ref.md", context["deleted_from_worktree"])

    def test_tracked_pyc_refused(self):
        bad = self.repo / "skill-a" / "legacy.pyc"
        bad.write_bytes(b"\x00pyc")
        git(self.repo, "add", "-f", "skill-a/legacy.pyc")
        git(self.repo, "commit", "-qm", "bad")
        with self.assertRaises(self.module.SnapshotError):
            self.module.inventory(self.repo)

    def test_tracked_env_refused(self):
        (self.repo / "skill-a" / ".env").write_text("SECRET=1\n")
        git(self.repo, "add", "-f", "skill-a/.env")
        git(self.repo, "commit", "-qm", "bad")
        with self.assertRaises(self.module.SnapshotError):
            self.module.inventory(self.repo)

    def test_source_symlink_refused(self):
        link = self.repo / "skill-a" / "link.md"
        link.symlink_to(self.repo / "skill-a" / "SKILL.md")
        git(self.repo, "add", "skill-a/link.md")
        git(self.repo, "commit", "-qm", "bad")
        with self.assertRaises(self.module.SnapshotError):
            self.module.inventory(self.repo)

    def test_hardlink_refused(self):
        target = self.repo / "skill-a" / "hard.md"
        os.link(self.repo / "skill-a" / "SKILL.md", target)
        git(self.repo, "add", "skill-a/hard.md")
        git(self.repo, "commit", "-qm", "bad")
        with self.assertRaises(self.module.SnapshotError):
            self.module.inventory(self.repo)


class CliValidationTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = Path(tempfile.mkdtemp(prefix="scan-sec-cli-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bad_run_name_rejected(self):
        for name in ("../escape", "sub/dir", "with space", "semi;colon", ""):
            rc = self.module.main(["--run", name])
            self.assertEqual(rc, 2, name)

    def test_existing_run_directory_rejected(self):
        existing = self.tmp / "before"
        existing.mkdir()
        with unittest.mock.patch.object(self.module, "SECURITY_ROOT", self.tmp):
            rc = self.module.main(["--run", "before"])
        self.assertEqual(rc, 2)

    def test_exec_tests_and_compare_are_exclusive(self):
        import argparse
        with self.assertRaises(SystemExit) as ctx:
            self.module.main(["--run", "x", "--exec-tests", "--compare", "y"])
        self.assertEqual(ctx.exception.code, 2)

    def test_run_is_required(self):
        with self.assertRaises(SystemExit) as ctx:
            self.module.main([])
        self.assertEqual(ctx.exception.code, 2)


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_profile_shape(self):
        import re as _re
        scratch = Path("/tmp/never-used-scratch")
        tricky = Path("/tmp/snap.dir(1)")
        profile = self.module.build_profile(scratch, [tricky])
        self.assertIn("(deny default)", profile)
        self.assertIn("(allow file-write*", profile)
        # canonical /private alias, not the raw /tmp spelling
        self.assertIn('(subpath "/private/tmp/never-used-scratch")', profile)
        self.assertNotIn("(subpath \"/tmp/", profile)
        # regex metacharacters in read trees are escaped
        self.assertIn(_re.escape("/private/tmp/snap.dir(1)"), profile)
        self.assertNotIn("/private/tmp/snap.dir(1)(/|$)", profile)


class SandboxPreflightTest(unittest.TestCase):
    """Live isolation proof; skips (loudly) where sandbox-exec is absent."""

    def setUp(self):
        self.module = load_module()
        if not self.module.SANDBOX_EXEC.exists():
            self.skipTest("/usr/bin/sandbox-exec not available on this host")
        # Seatbelt profiles cannot be layered: inside the runner's own
        # checks-mode sandbox, a nested preflight fails with EPERM. Detect
        # that and skip loudly rather than fail.
        probe = subprocess.run(
            [str(self.module.SANDBOX_EXEC), "-p", "(version 1)(allow default)",
             "/usr/bin/true"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest(
                "sandbox-exec cannot apply a profile in this process "
                "(already sandboxed or unsupported); live probes skipped")

    def test_probes_pass_and_bounds_applied(self):
        with tempfile.TemporaryDirectory(prefix="scan-sec-sbx-") as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            box = self.module.Sandbox(run_dir, [REPO])
            failures = box.preflight()
            self.assertEqual(failures, [])
            readback = json.loads(
                (run_dir / "scratch" / "policy" / "readback.json").read_text())
            self.assertEqual(readback["failures"], [])
            applied = readback["applied"]
            self.assertEqual(applied["cpu_s"], [300, 300])
            self.assertEqual(applied["nofile"], [256, 256])
            self.assertGreaterEqual(applied["nproc_ceiling"][0], 64)
            self.assertTrue(
                applied.get("address_bytes") == [4 * 1024**3] * 2
                or readback.get("platform_unenforced"),
                "address-space bound must be applied or recorded unenforced")


class ScannerProofTest(unittest.TestCase):
    """The pinned scanner must flag an inert os.system payload, never run it.

    This is the only test that executes the real scanner, and only against a
    fixture snapshot under the OS sandbox. Skipped when the pinned build is
    not installed — the runner CLI reports the same gap with exit 2.
    """

    def setUp(self):
        self.module = load_module()
        if not self.module.SANDBOX_EXEC.exists():
            self.skipTest("/usr/bin/sandbox-exec not available on this host")
        # Seatbelt cannot layer profiles (see SandboxPreflightTest.setUp):
        # the scanner proof runs in scan mode and standalone, but inside the
        # runner's own checks-mode sandbox it must skip loudly.
        probe = subprocess.run(
            [str(self.module.SANDBOX_EXEC), "-p", "(version 1)(allow default)",
             "/usr/bin/true"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("sandbox nesting unavailable in this process; "
                          "the scan mode exercises the same proof")
        try:
            self.identity = self.module.scanner_identity()
        except self.module.RunnerError as exc:
            self.skipTest(f"pinned scanner unavailable: {exc}")

    def test_executable_payload_flagged_not_run(self):
        module = self.module
        with tempfile.TemporaryDirectory(prefix="scan-sec-proof-") as tmp:
            snap = Path(tmp) / "snapshot" / "skillx"
            snap.mkdir(parents=True)
            (snap / "SKILL.md").write_text(
                "---\nname: skillx\ndescription: inert fixture\n---\n# x\n")
            (snap / "evil.py").write_text("import os\nos.system('echo SENTINEL')\n")
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            box = module.Sandbox(run_dir, [Path(tmp) / "snapshot",
                                           self.identity["tool_root"],
                                           self.identity["python_root"]])
            self.assertEqual(box.preflight(), [])
            report = box.scratch / "reports" / "skillx.json"
            result = box.run([self.identity["exec"], "scan", str(snap),
                              "--no-llm", "--format", "json", "--output",
                              str(report), "--fail-on-incomplete"], "proof")
            stdout = Path(result["stdout"]).read_text("utf-8", "replace")
            stderr = Path(result["stderr"]).read_text("utf-8", "replace")
            # The payload never executed: only scanner output is allowed.
            self.assertNotIn("SENTINEL\n", stdout)
            self.assertNotIn("SENTINEL", stderr)
            self.assertFalse(result["timed_out"])
            parsed = module.parse_report(report)
            self.assertIn("AST5", parsed["distinct_finding_ids"])
            self.assertTrue(any("os.system" in (i.get("pattern") or "")
                                or "os.system" in (i.get("finding") or "")
                                for i in json.loads(
                                    report.read_text("utf-8"))["issues"]))


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_counts_by_rule_counts_distinct_ids_per_skill(self):
        rows = [{"name": "a", "distinct_finding_ids": ["AST5", "LP1"]},
                {"name": "b", "distinct_finding_ids": ["AST5"]}]
        self.assertEqual(self.module.counts_by_rule(rows),
                         {"AST5": 2, "LP1": 1})

    def test_parse_report_rejects_foreign_scanner(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps({
                "risk_assessment": {"score": 1}, "metadata": {
                    "skillspector_version": "9.9.9"}}))
            with self.assertRaises(ValueError):
                self.module.parse_report(p)


import unittest.mock  # noqa: E402  (used by CliValidationTest)


if __name__ == "__main__":
    unittest.main()
