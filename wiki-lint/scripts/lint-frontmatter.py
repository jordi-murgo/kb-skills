#!/usr/bin/env python3
"""Check for frontmatter gaps in wiki pages.

Verifies that every wiki page has the required frontmatter fields:
type, title, created, updated, tags, status.

Excludes meta/navigation pages (hot.md) which are allowed to have minimal frontmatter.
"""

import sys
import os
import glob
from pathlib import Path


def find_wiki_root(start: str = ".") -> str:
    """Walk up for the vault root: wiki/ AND .git.

    Both markers are required. There is a skill directory named "wiki" under
    .agents/skills/, so wiki/ alone stops the walk at the skills directory
    instead of the vault root. cwd first, then this script's own location.
    """
    for base in (Path(start).resolve(), Path(__file__).resolve()):
        for parent in (base, *base.parents):
            if (
                (parent / "wiki").is_dir()
                and (parent / ".git").exists()
                and not (parent / "wiki" / "SKILL.md").exists()
            ):
                return str(parent)
    raise SystemExit(
        "error: vault root not found — run inside a project with wiki/ and .git"
    )


REQUIRED_FIELDS = ["type", "title", "created", "updated", "tags", "status"]

# Navigation and state pages allowed to have minimal frontmatter
EXEMPT_PAGES = {"hot", "index", "log", "_index", "overview", "dashboard", "goals"}


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
    checked = 0

    for md_file in sorted(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True)):
        stem = Path(md_file).stem
        if stem in EXEMPT_PAGES:
            continue
        rel_path = os.path.relpath(md_file, wiki_root)
        content = Path(md_file).read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(content)
        checked += 1

        missing = [f for f in REQUIRED_FIELDS if f not in fm]
        if missing:
            gaps.append((rel_path, missing))

    if gaps:
        print(f"FRONTMATTER GAPS: {len(gaps)}")
        for rel_path, missing in sorted(gaps):
            print(f"  {rel_path} — missing: {', '.join(missing)}")
        sys.exit(1)
    else:
        print(f"OK: all {checked} pages have required frontmatter")
        sys.exit(0)


if __name__ == "__main__":
    main()