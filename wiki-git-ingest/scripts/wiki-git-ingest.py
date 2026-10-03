#!/usr/bin/env python3
"""Watch related project repos and draft wiki claims from their git history.

The watch list lives in the `repos` section of kb-config.yaml at the vault root
(map keyed by name; values support ${VAR} macros like the rest of the config).
Each value is a git remote URL — the same string git understands in .git/config,
any forge (ssh 'git@host:group/proj.git', https://..., Azure DevOps, GitLab,
Bitbucket, ...) — tracked with git itself and your own credentials. A blobless
bare mirror is cloned once into .vault-meta/remotes/<name>.git and fetched on
every scan; new tags (name, date, annotated-tag subject) become claims shaped
for scripts/wiki_vet.py: {"claim", "quote", "target_page"}.

For a repo already cloned on this machine use {path: <clone>} instead: no
mirror, plus CHANGELOG.md version-diff and conventional-commit mining.

Optional mapping form adds target_page (default wiki/x-radar/<name>.md):
  repos:
    sacra-backend: git@git.gft.com:cybersecurity-practice/sacra/sacra-backend.git
    ollama:
      remote: https://github.com/ollama/ollama.git
      target_page: wiki/x-radar/ollama.md
    my-spike:
      path: ../my-spike

`scan` prints what is new since the last run; state advances with --commit,
without it the scan is dry. Scan state is derived (.vault-meta/repos.json and
.vault-meta/remotes/: deletable — forces a full rescan/re-clone).

Exit codes: 0 ok, 1 repo/git error, 2 usage error.
"""

from __future__ import annotations

import os
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path


def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git (a skills dir also has wiki/)."""
    p = Path.cwd()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir() and (parent / ".git").exists():
            return parent
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/")


VAULT = find_vault_root()
STATE_FILE = VAULT / ".vault-meta" / "repos.json"
CONVENTIONAL = re.compile(r"^(\w+)(?:\([^)]*\))?(!)?:\s+(.+)$")
VERSION_HEADER = re.compile(r"^##\s+\[?([^\]\s]+)\]?")


def load_env_files(root: Path) -> None:
    """Fill os.environ from <root>/.env then <root>/.env.local (later file wins;
    keys set in the process environment always win over both). Plain KEY=VALUE
    lines, optional leading 'export ', '#' comments, blank lines ignored."""
    process_keys = set(os.environ)
    for name in (".env", ".env.local"):
        path = root / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in process_keys:
                os.environ[key] = value


MACRO_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_macros(value):
    """Expand ${VAR} / ${VAR:-default} in strings (unset or empty -> default,
    else ""). Non-strings pass through unchanged."""
    if isinstance(value, str):
        return MACRO_RE.sub(
            lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value
        )
    return value


def load_config_section(root: Path, section: str) -> dict:
    """Read one optional section of kb-config.{yaml,yml,json} at the vault root.
    Missing file or section -> {}. ${VAR} macros expand from the environment
    (process env, then .env/.env.local loaded above). Kept inline per the
    no-shared-import rule: a skill copied on its own must keep working."""
    candidates = [root / "kb-config.yaml", root / "kb-config.yml", root / "kb-config.json"]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path} is not valid JSON: {e}")
    else:
        try:
            import yaml
        except ImportError:
            raise SystemExit(
                f"{path} needs PyYAML, which is not installed.\n"
                "Run: uv add pyyaml   (or: python3 -m pip install pyyaml)"
            )
        try:
            cfg = yaml.safe_load(text)
        except Exception as e:
            raise SystemExit(f"{path} is not valid YAML: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path} must contain a mapping at the top level")
    sec = cfg.get(section) or {}
    if not isinstance(sec, dict):
        raise SystemExit(f"{path}: '{section}' must be a mapping")
    return {k: expand_macros(v) for k, v in sec.items()}


def watch_list(cfg: dict) -> list[dict]:
    """Normalize the `repos` config into the ordered list the scanners expect.
    Entry value: a git remote URL string, or a mapping with exactly one of
    'remote' (URL, any forge) / 'path' (existing local clone; vault-relative
    allowed, may point outside — read-only) plus optional 'target_page'."""
    repos: list[dict] = []
    for name, value in cfg.items():
        entry = {"name": name}
        if isinstance(value, str) and value.strip():
            entry["remote"] = value.strip()
        elif isinstance(value, dict):
            if ("remote" in value) == ("path" in value):
                raise SystemExit(
                    f"kb-config repos.{name}: mapping needs exactly one of 'remote' or 'path'"
                )
            entry.update(value)
            if "path" in entry and not os.path.isabs(str(entry["path"])):
                entry["path"] = str((VAULT / entry["path"]).resolve())
        else:
            raise SystemExit(
                f"kb-config repos.{name} must be a git remote URL string or a "
                "mapping with 'remote'/'path'"
            )
        repos.append(entry)
    return repos


def load_state() -> dict:
    """Scan state is derived: missing/corrupt file -> {} (forces full rescan).
    Legacy files that also carried a 'repos' watch list are tolerated."""
    if not STATE_FILE.is_file():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError) as error:
        print(f"warning: cannot parse {STATE_FILE} ({error}); starting fresh", file=sys.stderr)
        return {}
    if isinstance(data, dict) and isinstance(data.get("state"), dict):
        return data["state"]
    return {} if not isinstance(data, dict) else data.get("state", {})


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"state": state}, ensure_ascii=False, indent=1) + "\n")


load_env_files(VAULT)
REPOS = watch_list(load_config_section(VAULT, "repos"))


def git(path: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", path, *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed in {path}")
    return result.stdout


def changelog_versions(text: str) -> dict[str, list[str]]:
    """Keep-a-Changelog style: '## [x.y.z] - date' followed by bullets."""
    versions: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        header = VERSION_HEADER.match(line)
        if header:
            current = header.group(1)
            versions.setdefault(current, [])
        elif current and line.startswith(("-", "*")):
            versions[current].append(line.lstrip("-* ").strip())
    return versions


def web_source(url: str) -> str:
    """Best-effort browsable URL for provenance from a git remote URL."""
    if url.startswith("git@"):
        host, _, path = url[4:].partition(":")
        return f"https://{host}/{path.removesuffix('.git')}"
    if url.startswith(("http://", "https://")):
        return url.removesuffix(".git")
    return url


def scan_remote(repo: dict, state: dict) -> tuple[list[dict], dict]:
    """Track a remote by its git URL with the user's own credentials:
    blobless bare mirror in .vault-meta/remotes/, fetch + for-each-ref tags."""
    url = repo["remote"]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo["name"]).strip("_.") or "repo"
    mirror = VAULT / ".vault-meta" / "remotes" / f"{safe}.git"
    if (mirror / "HEAD").exists():
        git(str(mirror), "fetch", "--prune", "--tags")
    else:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--mirror", "--filter=blob:none", url, str(mirror)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"git clone {url} failed")
    out = git(
        str(mirror), "for-each-ref", "refs/tags", "--sort=-creatordate",
        "--format=%(refname:short)%09%(creatordate:iso8601-strict)%09%(contents:subject)",
    )

    last_seen = state.get("last_seen")
    last_seen_dt = dt.datetime.fromisoformat(last_seen) if last_seen else None
    claims: list[dict] = []
    newest = last_seen_dt
    for line in out.splitlines():
        fields = (line.split("\t", 2) + ["", ""])[:3]
        tag, when, subject = (f.strip() for f in fields)
        if not tag or not when:
            continue
        try:
            when_dt = dt.datetime.fromisoformat(when)
        except ValueError:
            continue
        if last_seen_dt is not None and when_dt <= last_seen_dt:
            continue
        if newest is None or when_dt > newest:
            newest = when_dt
        claims.append(
            {
                "claim": f"{repo['name']} tagged {tag}" + (f": {subject}" if subject else ""),
                "quote": f"{tag}\n{subject}"[:800],
                "target_page": repo.get("target_page", f"wiki/x-radar/{repo['name']}.md"),
                "source": web_source(url),
                "skip_grounded": True,
            }
        )
    if newest is not None:
        state["last_seen"] = newest.isoformat()
    return claims, state


def scan_local(repo: dict, state: dict) -> tuple[list[dict], dict]:
    path = repo["path"]
    head = git(path, "rev-parse", "HEAD").strip()
    last = state.get("last_commit")
    if last == head:
        return [], state

    # Prefer CHANGELOG.md diff: versions present at HEAD but absent (or with
    # fewer bullets) at the last seen commit.
    claims: list[dict] = []
    try:
        current_cl = git(path, "show", "HEAD:CHANGELOG.md")
    except RuntimeError:
        current_cl = ""
    old_cl = ""
    if last:
        old_cl = git(path, "show", f"{last}:CHANGELOG.md") if git(path, "cat-file", "-e", f"{last}:CHANGELOG.md") else ""
    if current_cl:
        new_versions = changelog_versions(current_cl)
        old_versions = changelog_versions(old_cl) if old_cl else {}
        for version, bullets in new_versions.items():
            old_bullets = old_versions.get(version)
            fresh = bullets if old_bullets is None else [b for b in bullets if b not in old_bullets]
            if not fresh:
                continue
            for bullet in fresh[:10]:
                claims.append(
                    {
                        "claim": f"{repo['name']} {version}: {bullet}",
                        "quote": bullet,
                        "target_page": repo.get("target_page", f"wiki/x-radar/{repo['name']}.md"),
                    }
                )
    else:
        # No changelog: conventional-commit subjects since the last seen commit.
        log = (
            git(path, "log", f"{last}..HEAD", "--format=%s", "--no-merges")
            if last
            else git(path, "log", "-n", "30", "--format=%s", "--no-merges")
        )
        for subject in log.splitlines():
            match = CONVENTIONAL.match(subject)
            if not match:
                continue
            kind, breaking, summary = match.groups()
            if kind in ("feat", "fix", "perf") or breaking:
                claims.append(
                    {
                        "claim": f"{repo['name']} (unreleased): {summary}",
                        "quote": subject,
                        "target_page": repo.get("target_page", f"wiki/x-radar/{repo['name']}.md"),
                    }
                )

    state["last_commit"] = head
    return claims, state


def scan(repos: list[dict], states: dict, selected: list[str] | None) -> tuple[dict, dict]:
    new_states: dict = dict(states)
    per_repo: dict = {}
    for repo in repos:
        name = repo.get("name")
        if selected and name not in selected:
            continue
        state = new_states.get(name, {})
        try:
            if "path" in repo:
                claims, state = scan_local(repo, state)
            else:
                claims, state = scan_remote(repo, state)
        except (RuntimeError, OSError, ValueError) as error:
            print(f"error scanning {name}: {error}", file=sys.stderr)
            continue
        new_states[name] = state
        per_repo[name] = claims
    return per_repo, new_states


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("repos", help="list watched repos (from kb-config.yaml `repos`)")
    p_scan = sub.add_parser("scan", help="draft claims for changes since the last scan")
    p_scan.add_argument("--json", action="store_true")
    p_scan.add_argument("--commit", action="store_true", help="advance the state after reporting")
    p_scan.add_argument("repos", nargs="*", help="restrict to these repo names")

    args = parser.parse_args(argv)
    states = load_state()

    if args.cmd == "repos":
        for repo in REPOS:
            name = repo.get("name")
            kind = "local" if "path" in repo else "remote"
            where = repo.get("path") or repo.get("remote")
            last = states.get(name, {}).get("last_commit", states.get(name, {}).get("last_seen", "never"))
            print(f"{name:32} {kind:7} {where}  last={str(last)[:12]}")
        return 0

    if args.cmd == "scan":
        per_repo, new_states = scan(REPOS, states, args.repos or None)
        claims = [c for cs in per_repo.values() for c in cs]
        if args.json:
            print(json.dumps({"repos": {k: len(v) for k, v in per_repo.items()}, "claims": claims}, ensure_ascii=False, indent=1))
        else:
            for name, repo_claims in per_repo.items():
                print(f"{name}: {len(repo_claims)} new")
                for claim in repo_claims:
                    print(f"  - {claim['claim']}")
            print(f"total: {len(claims)} draft claims")
        if args.commit:
            save_state(new_states)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
