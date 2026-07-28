#!/usr/bin/env python3
"""Check for contradictions in the wiki.

Detects pages that contain [!contradiction] callouts and reports them.
Also checks for entities described as deployed but not mentioned in any
.raw/ source file (potential planned-vs-deployed contradictions).

This is a heuristic check, not a semantic analyzer. It catches:
1. Explicit [!contradiction] callouts already flagged in pages
2. Entity pages whose title is not mentioned in any .raw/ source file
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


def find_contradiction_callouts(wiki_root: str) -> list[tuple[str, str]]:
    """Find all [!contradiction] callouts in wiki pages."""
    results = []
    for md_file in sorted(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True)):
        rel_path = os.path.relpath(md_file, wiki_root)
        # Skip meta/lint reports — they document contradictions, not create them
        if "/meta/" in rel_path:
            continue
        content = Path(md_file).read_text(encoding="utf-8", errors="replace")
        # Match > [!contradiction] Title
        for match in re.finditer(r'>\s*\[!contradiction\]\s*(.+)', content):
            title = match.group(1).strip()
            results.append((rel_path, title))
    return results


def find_unsourced_entities(wiki_root: str) -> list[tuple[str, str]]:
    """Find entity pages whose title is not mentioned in any .raw/ source file."""
    entity_dir = f"{wiki_root}/wiki/entities"
    if not os.path.isdir(entity_dir):
        return []

    # Collect all raw source content
    raw_content = ""
    for raw_file in glob.glob(f"{wiki_root}/.raw/**/*", recursive=True):
        if os.path.isfile(raw_file):
            try:
                raw_content += Path(raw_file).read_text(
                    encoding="utf-8", errors="replace"
                ).lower() + "\n"
            except Exception:
                pass

    results = []
    for md_file in sorted(glob.glob(f"{entity_dir}/*.md")):
        stem = Path(md_file).stem
        rel_path = os.path.relpath(md_file, wiki_root)
        content = Path(md_file).read_text(encoding="utf-8", errors="replace")

        # Skip pages that already have a contradiction callout
        if "[!contradiction]" in content:
            continue

        # Check if the entity name appears in any raw source
        # Try multiple forms: filename stem, title from frontmatter
        title_match = re.search(r'^title:\s*"?([^"\n]+)"?', content, re.MULTILINE)
        title = title_match.group(1) if title_match else stem

        # Check various forms in raw content
        found = False
        # Build search terms: stem, title, title without spaces, key words
        terms = [stem.lower(), title.lower(), title.lower().replace(" ", "")]
        # Add individual significant words from the title (len > 3)
        for word in title.lower().split():
            if len(word) > 3:
                terms.append(word)
        # Add stem parts (e.g. "cicd-pipeline" -> "cicd", "pipeline")
        for part in stem.lower().split("-"):
            if len(part) > 3:
                terms.append(part)

        for term in terms:
            if term in raw_content:
                found = True
                break

        if not found:
            results.append((rel_path, title))

    return results


def main():
    wiki_root = find_wiki_root()

    callouts = find_contradiction_callouts(wiki_root)
    unsourced = find_unsourced_entities(wiki_root)

    issues = 0

    if callouts:
        print(f"CONTRADICTION CALLOUTS: {len(callouts)}")
        for rel_path, title in sorted(callouts):
            print(f"  {rel_path} — {title}")
        issues += len(callouts)
        print()

    if unsourced:
        print(f"UNSOURCED ENTITIES (not in any .raw/ file): {len(unsourced)}")
        for rel_path, title in sorted(unsourced):
            print(f"  {rel_path} — \"{title}\" not found in .raw/ sources")
        issues += len(unsourced)

    if issues == 0:
        print("OK: no contradictions or unsourced entities found")
        sys.exit(0)
    else:
        print(f"\nTotal issues: {issues}")
        sys.exit(1)


if __name__ == "__main__":
    main()