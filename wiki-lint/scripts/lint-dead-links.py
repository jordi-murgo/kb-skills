#!/usr/bin/env python3
"""Check for dead wikilinks in the wiki.

Scans all .md files under wiki/ for [[slug]] references and verifies
that each slug resolves to an existing .md file in the wiki directory tree.
Excludes .raw/ path references (those are source files, not wiki pages).
"""

import sys
import os
import re
import glob
from pathlib import Path


def find_wiki_root(start: str = ".") -> str:
    """Find the wiki root by looking for wiki/ directory."""
    p = Path(start).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return str(parent)
    return str(p)


def get_all_wiki_pages(wiki_root: str) -> set[str]:
    """Get all .md filenames (without extension) in the wiki tree.
    Also includes path-qualified stems like 'infrastructure/_index'."""
    pages = set()
    for md_file in glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True):
        stem = Path(md_file).stem
        pages.add(stem)
        # Also add path-relative stem (e.g. "infrastructure/_index")
        rel = os.path.relpath(md_file, f"{wiki_root}/wiki")
        rel_stem = str(Path(rel).with_suffix(""))
        pages.add(rel_stem)
    return pages


def extract_wikilinks(content: str) -> list[str]:
    """Extract all [[slug]] references from content, excluding .raw/ paths."""
    links = []
    # Match [[slug]] or [[slug|alias]] but not [[.raw/...]]
    for match in re.finditer(r'\[\[([^]|]+)(?:\|[^\]]*)?\]\]', content):
        slug = match.group(1)
        if not slug.startswith(".raw/"):
            links.append(slug)
    return links


def main():
    wiki_root = find_wiki_root()
    wiki_pages = get_all_wiki_pages(wiki_root)
    
    dead_links = []
    files_scanned = 0
    
    for md_file in sorted(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True)):
        files_scanned += 1
        rel_path = os.path.relpath(md_file, wiki_root)
        content = Path(md_file).read_text(encoding="utf-8", errors="replace")
        links = extract_wikilinks(content)
        
        for slug in links:
            # Skip special cases
            if slug in ("_index", "index", "log", "hot", "overview"):
                continue
            if slug not in wiki_pages:
                dead_links.append((slug, rel_path))
    
    # Output
    if dead_links:
        print(f"DEAD LINKS: {len(dead_links)}")
        for slug, source in sorted(dead_links):
            print(f"  [[{slug}]] referenced in {source}")
        sys.exit(1)
    else:
        print(f"OK: no dead links found ({files_scanned} pages scanned)")
        sys.exit(0)


if __name__ == "__main__":
    main()