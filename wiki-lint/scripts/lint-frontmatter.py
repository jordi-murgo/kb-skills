#!/usr/bin/env python3
"""Check for frontmatter gaps in wiki pages.

Verifies that every wiki page has the required frontmatter fields:
type, title, created, updated, tags, status.

Excludes meta/navigation pages (hot.md) which are allowed to have minimal frontmatter.
"""

import sys
import os
import re
import glob
from pathlib import Path


def find_wiki_root(start: str = ".") -> str:
    p = Path(start).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return str(parent)
    return str(p)


REQUIRED_FIELDS = ["type", "title", "created", "updated", "tags", "status"]

# Pages allowed to have minimal frontmatter
EXEMPT_PAGES = {"hot", "index", "log"}


def parse_frontmatter(content: str) -> dict[str, bool]:
    """Parse YAML frontmatter and return {field: present}."""
    fm = {}
    if not content.startswith("---"):
        return fm
    # Find closing ---
    lines = content.split("\n")
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            break
        if ":" in line:
            key = line.split(":")[0].strip()
            fm[key] = True
    return fm


def main():
    wiki_root = find_wiki_root()
    gaps = []

    for md_file in sorted(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True)):
        stem = Path(md_file).stem
        if stem in EXEMPT_PAGES:
            continue
        rel_path = os.path.relpath(md_file, wiki_root)
        content = Path(md_file).read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(content)

        missing = [f for f in REQUIRED_FIELDS if f not in fm]
        if missing:
            gaps.append((rel_path, missing))

    if gaps:
        print(f"FRONTMATTER GAPS: {len(gaps)}")
        for rel_path, missing in sorted(gaps):
            print(f"  {rel_path} — missing: {', '.join(missing)}")
        sys.exit(1)
    else:
        files = len(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True))
        checked = files - len(EXEMPT_PAGES)
        print(f"OK: all {checked} pages have required frontmatter")
        sys.exit(0)


if __name__ == "__main__":
    main()