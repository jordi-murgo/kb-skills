#!/usr/bin/env python3
"""Lifecycle audit for the wiki vault: status coverage, disputes, evidence.

Read-only. Scans wiki/**/*.md frontmatter and callouts, reports:
  (a) content pages missing `status`
  (b) dispute queue: pages with `status: disputed` or `> [!contradiction]` lines
  (c) `status: verified` pages missing `evidence`

Exit codes: 0 ok, 1 scan error, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
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
SKIP_BASENAMES = {"log.md", "hot.md"}
SKIP_NAMES = {"_index.md"}


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


def audit() -> dict:
    missing_status: list[dict] = []
    disputes: list[dict] = []
    missing_evidence: list[dict] = []
    pages = 0
    for path in iter_pages():
        pages += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        rel = path.relative_to(VAULT).as_posix()
        if meta.get("type", "").lower() == "meta":
            continue
        status = meta.get("status", "")
        if not status:
            missing_status.append({"page": rel})
        if status.lower() == "disputed":
            disputes.append({"page": rel, "line": 0, "reason": "status: disputed"})
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "[!contradiction]" in line:
                disputes.append({"page": rel, "line": lineno, "reason": line.strip()[:120]})
        if status.lower() == "verified" and not meta.get("evidence", "").strip():
            missing_evidence.append({"page": rel})
    return {
        "pages_scanned": pages,
        "missing_status": missing_status,
        "disputes": disputes,
        "verified_missing_evidence": missing_evidence,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if not WIKI.is_dir():
        print(f"no wiki directory at {WIKI}", file=sys.stderr)
        return 1
    try:
        report = audit()
    except OSError as error:
        print(f"scan error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    print(f"pages scanned: {report['pages_scanned']}")
    print(f"content pages missing status: {len(report['missing_status'])}")
    for item in report["missing_status"]:
        print(f"  {item['page']}")
    print(f"dispute queue: {len(report['disputes'])}")
    for item in report["disputes"]:
        where = f"{item['page']}:{item['line']}" if item["line"] else item["page"]
        print(f"  {where}  {item['reason']}")
    print(f"verified pages missing evidence: {len(report['verified_missing_evidence'])}")
    for item in report["verified_missing_evidence"]:
        print(f"  {item['page']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
