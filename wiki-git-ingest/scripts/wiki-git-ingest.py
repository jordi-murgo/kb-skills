#!/usr/bin/env python3
"""Watch associated project repos and draft wiki claims from their changelogs.

Repos live in .vault-meta/repos.json. Two kinds are supported:
  - {"name": ..., "path": <local clone>}   -> git log + CHANGELOG.md diff since the last seen commit
  - {"name": ..., "github": "<owner>/<repo>"} -> GitHub releases.atom (no API key, no clone)

`scan` prints what is new since the last run and emits claims shaped for
scripts/wiki_vet.py: {"claim", "quote", "target_page"}. State advances with
--commit; without it the scan is dry.

Exit codes: 0 ok, 1 repo/git error, 2 usage error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
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
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir() and (parent / ".git").exists():
            return parent
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/")


VAULT = find_vault_root()
CONFIG = VAULT / ".vault-meta" / "repos.json"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
CONVENTIONAL = re.compile(r"^(\w+)(?:\([^)]*\))?(!)?:\s+(.+)$")
VERSION_HEADER = re.compile(r"^##\s+\[?([^\]\s]+)\]?")

# Feeds are fetched once per scan; be polite and fail soft per repo.
HTTP_TIMEOUT = 30


def load_config() -> dict:
    if not CONFIG.is_file():
        return {"repos": [], "state": {}}
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError) as error:
        print(f"error: cannot parse {CONFIG}: {error}", file=sys.stderr)
        sys.exit(1)
    data.setdefault("repos", [])
    data.setdefault("state", {})
    return data


def save_config(data: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")


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
        elif current is not None and line.strip().startswith(("-", "*")):
            bullet = line.strip().lstrip("-* ").strip()
            if bullet:
                versions[current].append(bullet)
    return versions


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


def scan_github(repo: dict, state: dict) -> tuple[list[dict], dict]:
    owner_repo = repo["github"]
    url = f"https://github.com/{owner_repo}/releases.atom"
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
        feed = ET.fromstring(response.read())
    last_seen = state.get("last_seen")
    last_seen_dt = dt.datetime.fromisoformat(last_seen) if last_seen else None

    claims: list[dict] = []
    newest = last_seen_dt
    for entry in feed.findall("a:entry", ATOM_NS):
        title = (entry.findtext("a:title", "", ATOM_NS) or "").strip()
        updated = (entry.findtext("a:updated", "", ATOM_NS) or "").strip()
        content = (entry.findtext("a:content", "", ATOM_NS) or "").strip()
        if not title or not updated:
            continue
        try:
            updated_dt = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            continue
        if last_seen_dt is not None and updated_dt <= last_seen_dt:
            continue
        if newest is None or updated_dt > newest:
            newest = updated_dt
        text_lines = [re.sub(r"<[^>]+>", "", ln).strip() for ln in content.splitlines()]
        first_lines = [ln for ln in text_lines if ln][:5]
        headline = first_lines[0] if first_lines else title
        claim = f"{owner_repo} released {title}" if headline == title else f"{owner_repo} released {title}: {headline}"
        link_el = entry.find("a:link", ATOM_NS)
        link = link_el.get("href", "").strip() if link_el is not None else ""
        claims.append(
            {
                "claim": claim,
                "quote": f"{title}\n{headline}"[:800],
                "target_page": repo.get("target_page", f"wiki/x-radar/{owner_repo.replace('/', '-')}.md"),
                "source": link,
                "skip_grounded": True,
            }
        )

    if newest is not None:
        state["last_seen"] = newest.isoformat()
    return claims, state


def scan(repos: list[dict], states: dict, selected: list[str] | None) -> tuple[dict, dict]:
    new_states: dict = dict(states)
    per_repo: dict = {}
    for repo in repos:
        name = repo.get("name") or repo.get("github") or repo.get("path")
        if selected and name not in selected:
            continue
        state = new_states.get(name, {})
        try:
            if "path" in repo:
                claims, state = scan_local(repo, state)
            elif "github" in repo:
                claims, state = scan_github(repo, state)
            else:
                print(f"skip {name}: needs 'path' or 'github'", file=sys.stderr)
                continue
        except (RuntimeError, OSError, ET.ParseError, ValueError) as error:
            print(f"error scanning {name}: {error}", file=sys.stderr)
            continue
        new_states[name] = state
        per_repo[name] = claims
    return per_repo, new_states


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("repos", help="list watched repos")
    p_add = sub.add_parser("add", help="add a repo to the watch list")
    p_add.add_argument("name")
    group = p_add.add_mutually_exclusive_group(required=True)
    group.add_argument("--path", help="local clone path")
    group.add_argument("--github", help="owner/repo for the releases feed")
    p_add.add_argument("--target-page", help="wiki page claims point at")
    p_rm = sub.add_parser("remove", help="stop watching a repo")
    p_rm.add_argument("name")

    p_scan = sub.add_parser("scan", help="draft claims for changes since the last scan")
    p_scan.add_argument("--json", action="store_true")
    p_scan.add_argument("--commit", action="store_true", help="advance the state after reporting")
    p_scan.add_argument("repos", nargs="*", help="restrict to these repo names")

    args = parser.parse_args(argv)
    data = load_config()

    if args.cmd == "repos":
        for repo in data["repos"]:
            name = repo.get("name") or repo.get("github") or repo.get("path")
            kind = "local" if "path" in repo else "github"
            where = repo.get("path") or repo.get("github")
            print(f"{name:32} {kind:6} {where}  last={data['state'].get(name, {}).get('last_commit', data['state'].get(name, {}).get('last_seen', 'never'))[:12]}")
        return 0

    if args.cmd == "add":
        entry = {"name": args.name}
        if args.path:
            entry["path"] = str(Path(args.path).resolve())
        else:
            entry["github"] = args.github
        if args.target_page:
            entry["target_page"] = args.target_page
        if any((r.get("name") or r.get("github") or r.get("path")) == args.name for r in data["repos"]):
            print(f"error: {args.name} already watched", file=sys.stderr)
            return 2
        data["repos"].append(entry)
        save_config(data)
        print(f"added {args.name}")
        return 0

    if args.cmd == "remove":
        before = len(data["repos"])
        data["repos"] = [
            r for r in data["repos"] if (r.get("name") or r.get("github") or r.get("path")) != args.name
        ]
        data["state"].pop(args.name, None)
        if len(data["repos"]) == before:
            print(f"error: {args.name} not watched", file=sys.stderr)
            return 2
        save_config(data)
        print(f"removed {args.name}")
        return 0

    if args.cmd == "scan":
        per_repo, new_states = scan(data["repos"], data["state"], args.repos or None)
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
            data["state"] = new_states
            save_config(data)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
