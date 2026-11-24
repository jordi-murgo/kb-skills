#!/usr/bin/env python3
"""Reproducible, sandboxed SkillSpector measurement for this repo's skills.

Usage:
    python3 -B tests/scan_skill_security.py --run before
    python3 -B tests/scan_skill_security.py --run after --compare .cache/security/before
    python3 -B tests/scan_skill_security.py --run checks --exec-tests

What this is
------------
A maintainer measurement tool, not a safety certificate. It snapshots the
tracked source of every root skill into .cache/security/<run>/snapshot/, then
scans one skill at a time with the pinned SkillSpector build under an OS
sandbox (no network, reads limited to the snapshot + toolchain, writes limited
to per-run scratch). `--exec-tests` runs the fixed unittest command list under
the same sandbox against the read-only repo. Static-only scanning proves
nothing about runtime behaviour, dependencies beyond pinned analyzers, or
prose quality.

macOS RLIMIT_NPROC is accounted per real UID across the whole system, so a
literal 64-process ceiling denies every fork on a normal user account (the
planning host had ~600 processes). The launcher therefore applies
`ambient_uid_procs + 64` as the UID ceiling: the sandboxed child can still
spawn at most ~64 concurrent processes above the measurement-time baseline.
Requested vs applied values are recorded in the manifest and summary; a
readback mismatch fails preflight.

Exit codes
    0  every skill report executed with complete declared static scope
       (or, with --exec-tests, every command passed)
    1  partial/incomplete declared coverage, or a failed test command
    2  infrastructure failure: scanner identity mismatch, sandbox/preflight
       failure, timeout, absent/invalid report JSON, output-contract breach

Raw risk scores are reported verbatim; exit 0 never means "safe".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECURITY_ROOT = (REPO_ROOT / ".cache" / "security").resolve()
NON_SKILL_DIRS = {"tests"}
RUN_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")

# Pinned scanner identity. On mismatch: exit 2, never install or update.
SCANNER_EXEC_CANDIDATES = (
    Path("/Users/jpmo/.local/bin/skillspector"),
    Path(shutil.which("skillspector") or "/nonexistent"),
)
SCANNER_VERSION = "2.12.0"
SCANNER_COMMIT = "a09091e0ebf6c96ff327457529d7b0d372125884"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")

WALL_S = 600
LIMITS = {  # requested hard bounds, applied by the in-sandbox launcher
    "cpu_s": 300,
    "file_bytes": 64 * 1024 * 1024,
    "nofile": 256,
    "nproc_children": 64,
    "address_bytes": 4 * 1024**3,
}
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_RUN_BYTES = 256 * 1024 * 1024

SYSTEM_READ_TREES = ("/System", "/usr", "/bin", "/sbin", "/Library",
                     "/private/etc", "/opt/homebrew")
SYSTEM_READ_FILES = ("/dev/null", "/dev/urandom")

CHECKS_COMMANDS = (
    ("tests", "test_skill_permissions.py"),
    ("tests", "test_scan_skill_security.py"),
    ("kb-jira-sync/tests", "test_import_jira.py"),
    ("wiki-vetting/tests", "test_verdict.py"),
    ("kb-m365-fetch/tests", "test_m365_copilot.py"),
    ("kb-publish/tests", "test_export_okf.py"),
    ("tests", "test_skill_budget.py"),
)


class RunnerError(Exception):
    """Fatal infrastructure error (exit 2)."""


class SnapshotError(Exception):
    """Source inventory violates the snapshot contract (exit 2)."""


# --------------------------------------------------------------------------- #
# Source inventory
# --------------------------------------------------------------------------- #

def discover_skill_dirs(repo: Path) -> list[Path]:
    """Root-skill discovery, same convention as tests/test_skill_budget.py."""
    return sorted(
        path for path in repo.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and path.name not in NON_SKILL_DIRS
    )


def _git(repo: Path, *args: str) -> bytes:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": "/dev/null"}
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, env=env)
    if proc.returncode != 0:
        raise RunnerError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def inventory(repo: Path) -> tuple[list[dict], dict]:
    """Tracked + non-ignored untracked files under the root skills.

    Returns (files, context). files: [{skill, rel, mode, sha256}]; context
    carries deleted-from-worktree and ignored-cache counts (packaging notes,
    never silently dropped problems — contract violations raise).
    """
    skill_dirs = discover_skill_dirs(repo)
    raw = _git(repo, "ls-files", "-z", "--cached", "--others",
               "--exclude-standard", "--", *(d.name for d in skill_dirs))
    files, deleted, ignored_bytes, ignored_count = [], [], 0, 0
    for entry in raw.decode("utf-8").split("\0"):
        if not entry:
            continue
        src = repo / entry
        if not src.exists() and not src.is_symlink():
            deleted.append(entry)
            continue
        st = src.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise SnapshotError(f"source path is a symlink: {entry}")
        if not stat.S_ISREG(st.st_mode):
            raise SnapshotError(f"source path is not a regular file: {entry}")
        if st.st_nlink > 1:
            raise SnapshotError(f"source path is hard-linked: {entry}")
        parts = Path(entry).parts
        name = Path(entry).name
        if name in (".env", ".env.local") or "__pycache__" in parts \
                or name.endswith((".pyc", ".pyo")):
            raise SnapshotError(
                f"tracked cache/credential content refused in snapshot: {entry}")
        data = src.read_bytes()
        files.append({
            "skill": parts[0], "rel": entry,
            "mode": oct(stat.S_IMODE(st.st_mode)),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    for entry in _git(repo, "ls-files", "-z", "--others", "--ignored",
                      "--exclude-standard", "--",
                      *(d.name for d in skill_dirs)).decode("utf-8").split("\0"):
        if entry:
            ignored_count += 1
            try:
                ignored_bytes += (repo / entry).stat().st_size
            except OSError:
                pass
    context = {
        "deleted_from_worktree": sorted(deleted),
        "ignored_local_files": ignored_count,
        "ignored_local_bytes": ignored_bytes,
        "note": "ignored local caches are omitted from the snapshot as packaging "
                "context; they are not deleted and not counted as findings",
    }
    return files, context


def materialize(repo: Path, files: list[dict], snapshot: Path) -> None:
    snapshot.mkdir(parents=True)
    for record in files:
        src = repo / record["rel"]
        dst = snapshot / record["rel"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        with open(dst, "xb") as fh:
            fh.write(data)
        os.chmod(dst, int(record["mode"], 8))


# --------------------------------------------------------------------------- #
# Scanner identity
# --------------------------------------------------------------------------- #

def scanner_identity() -> dict:
    """Locate the pinned scanner or raise RunnerError with the exact gap."""
    for cand in dict.fromkeys(SCANNER_EXEC_CANDIDATES):
        try:
            resolved = cand.resolve(strict=True)
        except OSError:
            continue
        if not (resolved.is_file() and os.access(resolved, os.X_OK)):
            continue
        tool_root = resolved.parents[1]  # <tool>/bin/skillspector
        dist_infos = sorted((tool_root / "lib").glob(
            "python3.*/site-packages/skillspector-*.dist-info"))
        if not dist_infos:
            raise RunnerError(f"scanner dist-info not found under {tool_root}")
        direct = json.loads((dist_infos[-1] / "direct_url.json").read_text("utf-8"))
        commit = (direct.get("vcs_info") or {}).get("commit_id", "")
        version = dist_infos[-1].name.removesuffix(".dist-info").split("-", 1)[1]
        if version != SCANNER_VERSION or commit != SCANNER_COMMIT:
            raise RunnerError(
                f"scanner identity mismatch: found skillspector {version} "
                f"commit {commit or '<unknown>'}, pinned {SCANNER_VERSION} "
                f"commit {SCANNER_COMMIT}; refusing to run a different build")
        py = tool_root / "bin" / "python"
        base_prefix = subprocess.run(
            [str(py), "-I", "-c", "import sys; print(sys.base_prefix)"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
        ).stdout.strip()
        if not base_prefix.startswith("/"):
            raise RunnerError("could not resolve the scanner interpreter root")
        return {"exec": str(resolved), "tool_root": str(tool_root),
                "python_root": base_prefix, "version": version, "commit": commit}
    raise RunnerError(
        "skillspector executable not found; the pinned build "
        f"({SCANNER_VERSION} @ {SCANNER_COMMIT[:8]}) is required — "
        "install it separately, this tool never installs or updates it")


# --------------------------------------------------------------------------- #
# Sandbox
# --------------------------------------------------------------------------- #

def _rx(paths) -> str:
    return "^(" + "|".join(re.escape(str(p)) for p in paths) + ")(/|$)"

def _rx_rules(paths, per_rule: int = 16, *, exact: bool = False) -> list[str]:
    """Split a path set into several seatbelt regex alternations.

    The profile compiler rejects some longer alternation lists with a
    misleading "Error reading string" pointing at an unrelated line, so every
    generated regex stays small. exact=True anchors to the full path only
    (directory opens), otherwise the rule also matches everything below it.
    """
    ordered = sorted((str(p) for p in paths), key=len)
    tail = "$" if exact else "(/|$)"
    return ["^(" + "|".join(re.escape(p) for p in ordered[i:i + per_rule])
            + ")" + tail
            for i in range(0, len(ordered), per_rule)]


def ancestors_of(path: Path) -> list[Path]:
    out, cur = [], path.resolve()
    for parent in (cur, *cur.parents):
        out.append(parent)
    return out


def build_profile(scratch: Path, read_trees: list[Path]) -> str:
    """Seatbelt profile: deny default, explicit reads, scratch-only writes.

    `file-read-metadata` is allowed globally so realpath/stat of ancestors
    works; content reads stay restricted to the listed trees. Exact-match
    `file-read-data` on every ancestor directory of every read tree is
    required because the pinned scanner opens files with a secure
    descriptor walk: openat(O_DIRECTORY|O_NOFOLLOW|O_RDONLY) from `/` through
    each component, both for the skill snapshot and for its own rule files.
    Directory opens expose entry names only, never file content.
    """
    scratch = scratch.resolve()
    trees = [Path(p).resolve() for p in (*SYSTEM_READ_TREES, *read_trees)]
    anc: set = set()
    for tree in (scratch, *trees):
        anc.update(ancestors_of(tree))
    anc.discard(scratch)
    anc = sorted(anc, key=str)
    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow signal (target self))",
        "(allow ipc-posix-shm-read-data)",
        "(allow ipc-posix-shm-write-data)",
        "(allow file-read-metadata)",
    ]
    for rule in _rx_rules(trees):
        lines.append(f'(allow file-read* (regex "{rule}"))')
    lines.append('(allow file-read* (regex "^('
                 + "|".join(re.escape(str(p)) for p in SYSTEM_READ_FILES)
                 + ')$"))')
    for rule in _rx_rules(anc, per_rule=12, exact=True):
        lines.append(f'(allow file-read-data (regex "{rule}"))')
    lines += [
        f'(allow file-read* (subpath "{scratch}"))',
    ]
    for rule in _rx_rules(trees):
        lines.append(f'(allow file-map-executable (regex "{rule}"))')
    lines.append(f'(allow file-write* (subpath "{scratch}"))')
    return "\n".join(lines) + "\n"


LAUNCHER = r'''#!/usr/bin/env python3
"""In-sandbox launcher: apply rlimits, record readback, exec the command."""
import json, os, resource, sys

limits = json.load(open(sys.argv[1]))
env = json.load(open(sys.argv[2]))
readback_path = sys.argv[3]
cmd = sys.argv[4:]

RL = {
    "cpu_s": (resource.RLIMIT_CPU, limits["cpu_s"]),
    "file_bytes": (resource.RLIMIT_FSIZE, limits["file_bytes"]),
    "nofile": (resource.RLIMIT_NOFILE, limits["nofile"]),
    "nproc_ceiling": (resource.RLIMIT_NPROC, limits["nproc_ceiling"]),
    "address_bytes": (resource.RLIMIT_AS, limits["address_bytes"]),
}
readback, bad, unenforced = {}, [], []
for key, (res, value) in RL.items():
    soft, hard = resource.getrlimit(res)
    if soft > value:  # lower soft first: a new hard below current soft raises
        try:
            resource.setrlimit(res, (value, hard))
        except (ValueError, OSError):
            pass  # diagnosed by the readback below
    try:
        resource.setrlimit(res, (value, value))
    except (ValueError, OSError) as exc:
        if res == resource.RLIMIT_AS:
            unenforced.append(
                f"address_space: kernel refused finite RLIMIT_AS ({exc}); "
                f"requested {value} bytes — not enforceable on this platform, "
                "compute is bounded by CPU/wall limits instead")
            readback[key] = list(resource.getrlimit(res))
            continue
        bad.append(f"{key}: setrlimit failed: {exc}")
        continue
    soft, hard = resource.getrlimit(res)
    readback[key] = [soft, hard]
    if soft != value or hard != value:
        bad.append(f"{key}: requested {value}, readback ({soft}, {hard})")
json.dump({"applied": readback, "failures": bad,
           "platform_unenforced": unenforced}, open(readback_path, "w"))
if bad:
    sys.stderr.write("launcher: rlimits not applied: " + "; ".join(bad) + "\n")
    sys.exit(97)
os.execve(cmd[0], cmd, env)
'''

PROBE = r'''#!/usr/bin/env python3
"""Trusted sandbox preflight probe; prints a JSON verdict to stdout."""
import json, os, socket, subprocess, sys

scratch_write, canary_read, canary_write, canary = sys.argv[1:5]
out = {}
try:
    with open(scratch_write, "w") as fh:
        fh.write("PROBE-TOKEN")
    out["scratch_write"] = "ok"
except OSError as exc:
    out["scratch_write"] = f"fail:{exc.errno}"
try:
    open(canary_read, "rb").read()
    out["canary_read"] = "BAD-allowed"
except PermissionError:
    out["canary_read"] = "denied"
except OSError as exc:
    out["canary_read"] = f"denied:{exc.errno}"
try:
    open(canary_write, "xb").close()
    out["canary_write"] = "BAD-allowed"
except PermissionError:
    out["canary_write"] = "denied"
except OSError as exc:
    out["canary_write"] = f"denied:{exc.errno}"
try:
    socket.create_connection(("127.0.0.1", 9), timeout=3).close()
    out["network"] = "BAD-allowed"
except PermissionError:
    out["network"] = "denied"
except OSError as exc:
    out["network"] = f"BAD-other:{type(exc).__name__}"
try:
    proc = subprocess.run(["/usr/bin/true"], capture_output=True)
    out["fork"] = f"rc={proc.returncode}"
except OSError as exc:
    out["fork"] = f"fail:{exc.errno}"
print(json.dumps(out))
'''


def ambient_uid_procs() -> int:
    """Count this UID's processes without execing ps.

    /bin/ps is setuid-root and refuses to exec under Seatbelt, so the count
    comes from libproc's proc_listallpids directly. It counts all PIDs on the
    system, which on a single-user workstation is the same population the
    kernel charges against RLIMIT_NPROC."""
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        buf = (ctypes.c_int * 8192)()
        count = libc.proc_listallpids(buf, ctypes.sizeof(buf))
        if count > 0:
            return count
    except (OSError, AttributeError):
        pass
    raise RunnerError("cannot count ambient processes for the NPROC ceiling")


def child_env(scratch: Path) -> dict:
    return {
        "PATH": ":".join([str(Path(sys.executable).parent),
                          "/opt/homebrew/bin", "/opt/homebrew/sbin",
                          "/usr/local/bin", "/usr/bin", "/bin",
                          "/usr/sbin", "/sbin"]),
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "XDG_CACHE_HOME": str(scratch / "xdg-cache"),
        "LANG": "en_US.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "OTEL_SDK_DISABLED": "true",
        "LANGCHAIN_TRACING_V2": "false",
        "SKILLSPECTOR_OSV_TIMEOUT": "1",
    }


class Sandbox:
    """Owns one run's profile, launcher, scratch and preflight verdict."""

    def __init__(self, run_dir: Path, read_trees: list[Path]):
        self.run_dir = run_dir.resolve()
        self.scratch = self.run_dir / "scratch"
        self.canary = self.run_dir / "canary"
        # The launcher and probe always execute this runner's interpreter, so
        # its own installation trees must be readable inside the sandbox.
        # sys.prefix, not realpath(executable): a venv's python is a symlink
        # into the base install, and resolving it would drop the venv root
        # (and pyvenv.cfg) from the allow set.
        interp_trees = [Path(sys.prefix), Path(sys.base_prefix)]
        self.read_trees = [Path(p).resolve() for p in (*read_trees, *interp_trees)]
        for sub in ("reports", "logs", "home", "tmp", "policy"):
            (self.scratch / sub).mkdir(parents=True, exist_ok=True)
        self.profile_path = self.scratch / "policy" / "profile.sb"
        # The canary must sit OUTSIDE every readable tree, otherwise the
        # sibling-isolation probe reads it legitimately and fails preflight.
        # In checks mode the repo itself is readable, so the canary lives in a
        # fresh system temp directory outside all read trees; it is removed
        # after preflight.
        if any(tree == self.run_dir or tree in self.run_dir.parents
               for tree in self.read_trees):
            import tempfile as _tempfile
            self.canary = Path(_tempfile.mkdtemp(prefix="scan-sec-canary-"))
            self._canary_temp = self.canary
        else:
            self.canary = self.run_dir / "canary"
            self._canary_temp = None
        self.profile = build_profile(self.scratch, self.read_trees)
        self.profile_path.write_text(self.profile, encoding="utf-8")
        (self.scratch / "policy" / "launcher.py").write_text(LAUNCHER, encoding="utf-8")
        self.nproc_ceiling = ambient_uid_procs() + LIMITS["nproc_children"]
        self.limits = {**LIMITS, "nproc_ceiling": self.nproc_ceiling}
        self.probe_failures: list[str] = []

    def _limits_files(self) -> tuple[Path, Path, Path]:
        return (self.scratch / "policy" / "limits.json",
                self.scratch / "policy" / "env.json",
                self.scratch / "policy" / "readback.json")

    def _launch(self, cmd: list[str], env: dict, log_stem: str,
                extra_env: dict | None = None, cwd: Path | None = None) -> dict:
        limits_f, env_f, readback_f = self._limits_files()
        full_env = {**env, **(extra_env or {})}
        limits_f.write_text(json.dumps(self.limits), encoding="utf-8")
        env_f.write_text(json.dumps(full_env), encoding="utf-8")
        argv = [str(SANDBOX_EXEC), "-f", str(self.profile_path),
                sys.executable, "-I", "-B",
                str(self.scratch / "policy" / "launcher.py"),
                str(limits_f), str(env_f), str(readback_f), *cmd]
        out_log = self.scratch / "logs" / f"{log_stem}.stdout"
        err_log = self.scratch / "logs" / f"{log_stem}.stderr"
        with open(out_log, "wb") as out_fh, open(err_log, "wb") as err_fh:
            proc = subprocess.Popen(argv, stdout=out_fh, stderr=err_fh,
                                    stdin=subprocess.DEVNULL,
                                    cwd=str(cwd or self.scratch),
                                    start_new_session=True)
            timed_out = False
            try:
                rc = proc.wait(timeout=WALL_S)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                rc = proc.wait()
        try:
            readback = json.loads(readback_f.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            readback = {"applied": {}, "failures": ["launcher readback missing"]}
        return {"rc": rc, "timed_out": timed_out,
                "stdout": out_log, "stderr": err_log, "readback": readback}

    def preflight(self) -> list[str]:
        """Run the trusted probes; return human-readable failures."""
        canary_bytes = os.urandom(32)
        secret = self.canary / "secret.bin"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_bytes(canary_bytes)
        probe = self.scratch / "policy" / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        result = self._launch(
            [sys.executable, "-I", "-B", str(probe),
             str(self.scratch / "probe-ok.txt"),
             str(secret), str(self.canary / "evil.bin"), str(secret)],
            child_env(self.scratch), "preflight")
        failures: list[str] = []
        if result["timed_out"] or result["rc"] != 0:
            failures.append(f"preflight probe did not run cleanly (rc={result['rc']}, "
                            f"timed_out={result['timed_out']})")
            return failures
        try:
            verdict = json.loads(result["stdout"].read_text("utf-8").strip().splitlines()[-1])
        except (OSError, IndexError, json.JSONDecodeError):
            return ["preflight probe produced no JSON verdict"]
        if verdict.get("scratch_write") != "ok":
            failures.append(f"scratch write denied: {verdict.get('scratch_write')}")
        for key in ("canary_read", "canary_write"):
            if str(verdict.get(key, "")).startswith("BAD"):
                failures.append(f"{key} was permitted by the sandbox")
            elif verdict.get(key) in (None, "fail"):
                failures.append(f"{key} errored unexpectedly: {verdict.get(key)}")
        if str(verdict.get("network", "")).startswith("BAD"):
            failures.append(f"network egress was permitted: {verdict.get('network')}")
        if verdict.get("fork") != "rc=0":
            failures.append(f"process creation under limits failed: {verdict.get('fork')} "
                            "(NPROC ceiling may sit below the ambient UID process count)")
        if secret.read_bytes() != canary_bytes:
            failures.append("canary bytes changed during preflight")
        try:
            if (self.scratch / "probe-ok.txt").read_text("utf-8") != "PROBE-TOKEN":
                failures.append("scratch probe token mismatch")
        except OSError:
            failures.append("scratch probe token missing")
        if result["readback"].get("failures"):
            failures.append("rlimit readback mismatch: "
                            + "; ".join(result["readback"]["failures"]))
        if self._canary_temp is not None:
            shutil.rmtree(self._canary_temp, ignore_errors=True)
        self.probe_failures = failures
        return failures

    def run(self, cmd: list[str], log_stem: str, extra_env: dict | None = None,
            cwd: Path | None = None) -> dict:
        return self._launch(cmd, child_env(self.scratch), log_stem, extra_env, cwd)


# --------------------------------------------------------------------------- #
# Report parsing / summary
# --------------------------------------------------------------------------- #

def parse_report(path: Path) -> dict:
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or "risk_assessment" not in data:
        raise ValueError("report JSON lacks risk_assessment")
    version = ((data.get("metadata") or {}).get("skillspector_version") or "")
    if version != SCANNER_VERSION:
        raise ValueError(f"report scanner identity changed: {version!r}")
    risk = data["risk_assessment"]
    issues = data.get("issues") or []
    completeness = data.get("analysis_completeness") or {}
    return {
        "score": risk.get("score"),
        "severity": risk.get("severity"),
        "recommendation": risk.get("recommendation"),
        "max_issue_severity": risk.get("max_issue_severity"),
        "finding_count_reported": data.get("finding_count", len(issues)),
        "issue_records": len(issues),
        "distinct_finding_ids": sorted({i.get("id", "?") for i in issues}),
        "execution_successful": bool(data.get("execution_successful")),
        "coverage_percent": completeness.get("coverage_percent"),
        "completeness_status": completeness.get("status"),
        "is_complete": bool(completeness.get("is_complete")),
        "limitations": completeness.get("limitations") or [],
    }


def counts_by_rule(rows: list[dict]) -> dict:
    out: dict = {}
    for row in rows:
        for fid in row["distinct_finding_ids"]:
            out[fid] = out.get(fid, 0) + 1
    return dict(sorted(out.items()))


def promote(src: Path, dst: Path) -> int:
    st = src.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise RunnerError(f"refusing non-regular promoted output: {src}")
    if st.st_size > MAX_OUTPUT_BYTES:
        raise RunnerError(f"promoted output exceeds 64 MiB: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return st.st_size


def write_summary(run_dir: Path, payload: dict) -> None:
    (run_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = ["# Skill security measurement", "",
             f"- run: `{payload['run']}` ({payload['created']})",
             f"- mode: {payload['mode']} — static-only, no network, "
             "snapshot-scoped; not a safety certificate"]
    if payload["mode"] == "scan":
        lines.append(f"- scanner: SkillSpector {payload['scanner']['version']} "
                     f"@ {payload['scanner']['commit'][:12]}")
    lines.append(f"- exit status: {payload['exit_status']}")
    lines.append("")
    if payload["mode"] == "scan":
        lines += ["| skill | score | severity | recommendation | findings | "
                  "distinct ids | coverage | status |", "|---|---|---|---|---|---|---|---|"]
        for row in payload["skills"]:
            lines.append(
                f"| {row['name']} | {row['score']} | {row['severity']} | "
                f"{row['recommendation']} | {row['finding_count_reported']} "
                f"(records {row['issue_records']}) | "
                f"{','.join(row['distinct_finding_ids']) or '—'} | "
                f"{row['coverage_percent']}% | {row['completeness_status']} |")
        lines += ["", f"- analyzer limitations recorded: "
                  f"{sum(1 for r in payload['skills'] if r['limitations'])} skill(s)"
                  " (dependency/OSV lookups unavailable offline unless stated otherwise)",
                  "- findings are raw scanner output; a lower score alone is not a fix"]
        if payload.get("compare"):
            cmp_ = payload["compare"]
            lines += ["", "## Before/after", "",
                      "| skill | score before | score after | delta |", "|---|---|---|---|"]
            for name, (before, after) in sorted(cmp_["score_deltas"].items()):
                mark = "" if before == after else (f"{after - before:+d}" if isinstance(before, int) and isinstance(after, int) else "changed")
                lines.append(f"| {name} | {before} | {after} | {mark} |")
            lines += ["", "rule-count deltas (before → after):"]
            for rule, (b, a) in sorted(cmp_["rule_deltas"].items()):
                lines.append(f"- {rule}: {b} → {a}")
    else:
        lines += ["| command | exit |", "|---|---|"]
        for row in payload["commands"]:
            lines.append(f"| `{row['command']}` | {row['exit_code']} |")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def run_scan(run_dir: Path, compare_dir: Path | None) -> int:
    identity = scanner_identity()
    files, context = inventory(REPO_ROOT)
    skills = sorted({f["skill"] for f in files})
    expected = [p.name for p in discover_skill_dirs(REPO_ROOT)]
    missing = sorted(set(expected) - set(skills))
    if missing:
        print(f"error: skills present in the repo but absent from the source "
              f"inventory: {', '.join(missing)}", file=sys.stderr)
        return 2
    snapshot = run_dir / "snapshot"
    materialize(REPO_ROOT, files, snapshot)

    head = _git(REPO_ROOT, "rev-parse", "HEAD").decode().strip()
    argv_template = ["<skillspector>", "scan", "<snapshot>/<skill>", "--no-llm",
                     "--format", "json", "--output", "<scratch>/reports/<skill>.json",
                     "--fail-on-incomplete"]
    read_trees = [snapshot, identity["tool_root"], identity["python_root"]]
    box = Sandbox(run_dir, read_trees)
    if (failures := box.preflight()):
        for failure in failures:
            print(f"error: sandbox preflight: {failure}", file=sys.stderr)
        return 2

    manifest = {
        "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "scanner": identity,
        "scanner_argv_template": argv_template,
        "head_commit_informational": head,
        "skills": skills,
        "files": files,
        "source_context": context,
        "static_only": True,
        "scope": "per-skill static scan; no --exclude/--baseline/--transitive; "
                 "no dependency-content inspection; network denied (OSV lookups "
                 "unavailable by design in this mode)",
        "sandbox": {
            "engine": f"sandbox-exec profile sha256 "
                      f"{hashlib.sha256(box.profile.encode()).hexdigest()}",
            "probes_failed": box.probe_failures,
            "limits": box.limits,
            "nproc_note": "RLIMIT_NPROC is per-UID on macOS; applied ceiling = "
                          "ambient+64 keeps the child budget at 64 without "
                          "denying all forks (see module docstring)",
        },
        "child_environment_keys": sorted(child_env(box.scratch)),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    rows, promoted_bytes, failures = [], 0, 0
    for skill in skills:
        report_scratch = box.scratch / "reports" / f"{skill}.json"
        result = box.run([identity["exec"], "scan", str(snapshot / skill),
                          "--no-llm", "--format", "json",
                          "--output", str(report_scratch),
                          "--fail-on-incomplete"], f"scan-{skill}")
        row = {"name": skill, "exit_code": result["rc"],
               "timed_out": result["timed_out"]}
        try:
            row.update(parse_report(report_scratch))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            row.update({"score": None, "severity": None, "recommendation": None,
                        "max_issue_severity": None, "finding_count_reported": None,
                        "issue_records": 0, "distinct_finding_ids": [],
                        "execution_successful": False, "coverage_percent": None,
                        "completeness_status": "unparsed", "is_complete": False,
                        "limitations": [], "parse_error": str(exc)[:200]})
            failures += 1
        if report_scratch.exists():
            # Retain the raw report even when parsing failed, for diagnosis.
            row["report"] = f"reports/{skill}.json"
            promoted_bytes += promote(report_scratch,
                                      run_dir / "reports" / f"{skill}.json")
            if promoted_bytes > MAX_RUN_BYTES:
                print("error: promoted outputs exceed the 256 MiB run budget",
                      file=sys.stderr)
                return 2
        for stream_path in (Path(result["stdout"]), Path(result["stderr"])):
            if stream_path.exists() and stream_path.stat().st_size:
                promote(stream_path, run_dir / "logs" / stream_path.name)
        rows.append(row)
        status = ("ok" if row["execution_successful"] and row["is_complete"]
                  else "incomplete" if row["report"] else "FAILED")
        print(f"  {skill}: {status} score={row['score']} "
              f"coverage={row['coverage_percent']}%")

    shutil.rmtree(box.scratch, ignore_errors=True)
    payload = {
        "run": run_dir.name, "created": manifest["created"], "mode": "scan",
        "scanner": identity, "static_only": True,
        "skills": rows, "counts_by_rule": counts_by_rule(rows),
        "source_context": context, "manifest": "manifest.json",
        "exit_status": None,
    }
    if compare_dir:
        try:
            base = json.loads((compare_dir / "summary.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot load baseline summary: {exc}", file=sys.stderr)
            return 2
        base_rows = {r["name"]: r for r in base.get("skills", [])}
        payload["compare"] = {
            "baseline": str(compare_dir),
            "score_deltas": {r["name"]: (base_rows.get(r["name"], {}).get("score"),
                                         r["score"]) for r in rows},
            "rule_deltas": {rule: (base.get("counts_by_rule", {}).get(rule, 0), count)
                            for rule, count in payload["counts_by_rule"].items()},
            "baseline_rules_extra": {rule: n for rule, n in base.get(
                "counts_by_rule", {}).items()
                if rule not in payload["counts_by_rule"]},
        }
    infra = failures or any(r["timed_out"] for r in rows)
    incomplete = any(not r["is_complete"] for r in rows)
    payload["exit_status"] = 2 if infra else 1 if incomplete else 0
    write_summary(run_dir, payload)
    print(f"summary: {run_dir / 'summary.md'}")
    return payload["exit_status"]


def run_checks(run_dir: Path) -> int:
    box = Sandbox(run_dir, [REPO_ROOT])
    if (failures := box.preflight()):
        for failure in failures:
            print(f"error: sandbox preflight: {failure}", file=sys.stderr)
        return 2
    commands, failed, infra = [], 0, False
    extra = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null"}
    for start_dir, pattern in CHECKS_COMMANDS:
        cmd = [sys.executable, "-B", "-m", "unittest", "discover",
               "-s", start_dir, "-p", pattern, "-v"]
        label = f"checks-{start_dir.replace('/', '-')}-{pattern}"
        result = box.run(cmd, label, extra_env=extra, cwd=REPO_ROOT)
        stdout = result["stdout"].read_text("utf-8", "replace")
        stderr = result["stderr"].read_text("utf-8", "replace")
        if result["timed_out"] or result["readback"].get("failures"):
            infra = True
        if result["rc"] != 0:
            failed += 1
            print(f"FAILED {start_dir}/{pattern} (exit {result['rc']}):\n"
                  f"{stderr[-2000:]}", file=sys.stderr)
        commands.append({
            "command": f"python3.13 -B -m unittest discover -s {start_dir} "
                       f"-p '{pattern}' -v",
            "exit_code": result["rc"], "timed_out": result["timed_out"],
            "stdout_log": f"logs/{label}.stdout", "stderr_log": f"logs/{label}.stderr",
            "stdout_tail": stdout.strip().splitlines()[-3:],
            "stderr_tail": [ln for ln in stderr.strip().splitlines()[-8:]],
        })
        for stream in (result["stdout"], result["stderr"]):
            src = Path(stream)
            if src.exists():
                promote(src, run_dir / "logs" / f"{Path(stream).name}")
    shutil.rmtree(box.scratch, ignore_errors=True)
    payload = {
        "run": run_dir.name,
        "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "checks", "commands": commands,
        "sandbox": {"limits": box.limits, "probes_failed": box.probe_failures},
        "exit_status": 2 if infra else 1 if failed else 0,
    }
    write_summary(run_dir, payload)
    return payload["exit_status"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--run", required=True,
                        help="run name matching [A-Za-z0-9_-]+; must be new")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--compare", metavar="RUN_DIR",
                      help="baseline .cache/security/<run> to compare against")
    mode.add_argument("--exec-tests", action="store_true",
                      help="run the fixed unittest command list under the sandbox")
    args = parser.parse_args(argv)

    if not RUN_NAME_RE.match(args.run):
        print(f"error: --run must match [A-Za-z0-9_-]+: {args.run!r}", file=sys.stderr)
        return 2
    run_dir = SECURITY_ROOT / args.run
    if run_dir.exists() or run_dir.is_symlink():
        print(f"error: run directory already exists: {run_dir}", file=sys.stderr)
        return 2
    if run_dir.resolve().parent != SECURITY_ROOT.resolve():
        print("error: run path escapes .cache/security/", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True)

    try:
        if args.exec_tests:
            return run_checks(run_dir)
        compare_dir = None
        if args.compare:
            candidate = Path(args.compare)
            if not candidate.is_absolute():
                candidate = REPO_ROOT / candidate
            candidate = candidate.resolve()
            if candidate.parent != SECURITY_ROOT.resolve() or not candidate.is_dir():
                print(f"error: --compare must be a run directory under "
                      f".cache/security/: {args.compare}", file=sys.stderr)
                return 2
            compare_dir = candidate
        return run_scan(run_dir, compare_dir)
    except (RunnerError, SnapshotError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
