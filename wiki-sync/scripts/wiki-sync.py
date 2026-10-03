#!/usr/bin/env python3
"""Staleness check for file-linked wiki claims against git history.

Scans wiki/**/*.md for references to repo files, compares each referenced
file's last commit date with the page's `updated:` frontmatter, and reports
claims that may be stale. Read-only unless --commit is passed, which records
the sync point in .vault-meta/sync.json.

Exit codes: 0 ok, 1 git/scan error, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
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
WIKI = VAULT / "wiki"
STATE = VAULT / ".vault-meta" / "sync.json"
SKIP_BASENAMES = {"log.md", "hot.md"}
SKIP_NAMES = {"_index.md"}

FILE_REF = re.compile(r"(?:[\w.-]+/){1,4}[\w.-]+\.(?:py|ts|js|json|md|gguf|toml|yaml)")
TRAILING_PUNCT = ".,;:)'\"»«]"


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line[:1] in (" ", "\t", "-"):
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        if key.strip() and key.strip() not in fields:
            fields[key.strip()] = value
    return fields


def iter_pages():
    for path in sorted(WIKI.rglob("*.md")):
        if path.name in SKIP_BASENAMES or path.name in SKIP_NAMES:
            continue
        yield path


def referenced_files(text: str) -> set[str]:
    refs = set()
    for match in FILE_REF.finditer(text):
        ref = match.group(0).rstrip(TRAILING_PUNCT)
        if (VAULT / ref).is_file():
            refs.add(ref)
    return refs


def last_commit_date(path: str) -> str | None:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cI", "--", path],
        cwd=VAULT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Page frontmatter carries bare dates; git gives offset-aware stamps. Naive means UTC here.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def collect(since: str | None) -> dict:
    if since:
        cutoff = parse_date(since)
    else:
        cutoff = None
    file_to_pages: dict[str, set[str]] = {}
    page_updated: dict[str, str] = {}
    for page in iter_pages():
        rel = page.relative_to(VAULT).as_posix()
        text = page.read_text(encoding="utf-8", errors="replace")
        page_updated[rel] = parse_frontmatter(text).get("updated", "")
        for ref in referenced_files(text):
            if ref == rel:
                continue
            file_to_pages.setdefault(ref, set()).add(rel)

    stale: list[dict] = []
    checked = 0
    for ref, pages in sorted(file_to_pages.items()):
        committed = last_commit_date(ref)
        if committed is None:
            continue
        commit_dt = parse_date(committed)
        if commit_dt is None:
            continue
        if cutoff is not None and commit_dt <= cutoff:
            continue
        checked += 1
        for rel in sorted(pages):
            updated_dt = parse_date(page_updated.get(rel, ""))
            if updated_dt is None or updated_dt < commit_dt:
                stale.append(
                    {
                        "page": rel,
                        "file": ref,
                        "file_committed": committed,
                        "page_updated": page_updated.get(rel, ""),
                        "reason": "missing updated" if updated_dt is None else "file newer than page",
                    }
                )
    return {
        "referenced_files": len(file_to_pages),
        "checked_files": checked,
        "since": since,
        "stale": stale,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--since", action="store_true", help="only consider commits after the last sync point")
    parser.add_argument("--commit", action="store_true", help="record this run as the new sync point")
    args = parser.parse_args(argv)
    if not WIKI.is_dir():
        print(f"no wiki directory at {WIKI}", file=sys.stderr)
        return 1

    since = None
    if args.since and STATE.is_file():
        try:
            since = json.loads(STATE.read_text()).get("last_sync")
        except (OSError, ValueError):
            since = None

    report = collect(since)
    if args.commit:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"last_sync": datetime.now(timezone.utc).isoformat()}, indent=1) + "\n")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    window = f" (since {since})" if since else ""
    print(f"referenced files: {report['referenced_files']}, checked: {report['checked_files']}{window}")
    print(f"stale claims: {len(report['stale'])}")
    for item in report["stale"]:
        print(f"  {item['page']}  ->  {item['file']}")
        print(f"      file committed {item['file_committed']}, page updated {item['page_updated'] or '(none)'} [{item['reason']}]")
    if args.commit:
        print(f"sync point written to {STATE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
