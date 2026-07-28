#!/usr/bin/env python3
"""Check for orphan wiki pages (no inbound wikilinks).

A page is an orphan if no other wiki page contains a [[slug]] reference to it.
Excludes meta/navigation pages (index, log, hot, overview, _index).
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


META_PAGES = {"index", "log", "hot", "overview", "_index", "dashboard"}


def is_meta_page(stem: str) -> bool:
    """Check if a page stem is a meta/navigation page."""
    if stem in META_PAGES:
        return True
    # Lint reports: lint-report-YYYY-MM-DD
    if stem.startswith("lint-report-"):
        return True
    return False


def get_all_wiki_pages(wiki_root: str) -> dict[str, str]:
    """Return {stem: relative_path} for all .md files in wiki/."""
    pages = {}
    for md_file in glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True):
        stem = Path(md_file).stem
        pages[stem] = os.path.relpath(md_file, wiki_root)
    return pages


def build_inbound_map(wiki_root: str, pages: dict[str, str]) -> dict[str, set[str]]:
    """Build {target_slug: set(source_files)} for all wikilinks."""
    inbound = {slug: set() for slug in pages}
    for md_file in sorted(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True)):
        rel_path = os.path.relpath(md_file, wiki_root)
        content = Path(md_file).read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'\[\[([^]|]+)(?:\|[^\]]*)?\]\]', content):
            slug = match.group(1)
            if slug.startswith(".raw/"):
                continue
            if slug in inbound and rel_path not in inbound[slug]:
                # Don't count self-references
                source_stem = Path(md_file).stem
                if slug != source_stem:
                    inbound[slug].add(rel_path)
    return inbound


def main():
    wiki_root = find_wiki_root()
    pages = get_all_wiki_pages(wiki_root)
    inbound = build_inbound_map(wiki_root, pages)

    orphans = []
    for slug, rel_path in sorted(pages.items()):
        if is_meta_page(slug):
            continue
        if len(inbound.get(slug, set())) == 0:
            orphans.append((slug, rel_path))

    if orphans:
        print(f"ORPHAN PAGES: {len(orphans)}")
        for slug, rel_path in sorted(orphans):
            print(f"  [[{slug}]] ({rel_path}) — no inbound links")
        sys.exit(1)
    else:
        total = len(pages) - len(META_PAGES)
        print(f"OK: no orphan pages ({total} content pages checked)")
        sys.exit(0)


if __name__ == "__main__":
    main()