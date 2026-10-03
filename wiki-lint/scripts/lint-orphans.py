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
    """Return {stem: relative_path} for all .md files in wiki/, plus
    path-qualified keys (relative to wiki/ and to repo root) so wikilinks
    with a full path resolve too. Also includes auto-generated directory
    index slugs for directories without a .md file of the same name."""
    pages: dict[str, str] = {}
    wiki_dir = Path(f"{wiki_root}/wiki")
    for md_file in glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True):
        stem = Path(md_file).stem
        rel = os.path.relpath(md_file, f"{wiki_root}/wiki")
        rel_stem = str(Path(rel).with_suffix(""))
        full_rel = os.path.relpath(md_file, wiki_root)
        full_stem = str(Path(full_rel).with_suffix(""))
        if stem not in pages:
            pages[stem] = os.path.relpath(md_file, wiki_root)
        pages.setdefault(rel_stem, os.path.relpath(md_file, wiki_root))
        pages.setdefault(full_stem, os.path.relpath(md_file, wiki_root))

    # Add auto-generated directory index slugs
    for d in sorted(wiki_dir.rglob("*")):
        if not d.is_dir():
            continue
        rel_dir = str(d.relative_to(wiki_dir))
        if not rel_dir or rel_dir == ".":
            continue
        if not any(d.rglob("*.md")):
            continue
        parent = d.parent
        if (parent / f"{d.name}.md").exists():
            continue
        # Add as a virtual page
        pages.setdefault(rel_dir, f"{rel_dir}.md (auto-generated)")

    return pages


def build_inbound_map(wiki_root: str, pages: dict[str, str]) -> dict[str, set[str]]:
    """Build {target_slug: set(source_files)} for all wikilinks.
    Also simulates inbound links from auto-generated directory indexes."""
    inbound = {slug: set() for slug in pages}
    wiki_dir = Path(f"{wiki_root}/wiki")

    for md_file in sorted(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True)):
        rel_path = os.path.relpath(md_file, wiki_root)
        content = Path(md_file).read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'\[\[([^]|]+)(?:\|[^\]]*)?\]\]', content):
            slug = match.group(1)
            if slug.startswith(".raw/"):
                continue
            if slug in pages:
                target_rel = pages[slug]
                for k in [kk for kk in pages if pages[kk] == target_rel]:
                    if rel_path not in inbound[k]:
                        if Path(rel_path).stem != Path(target_rel).stem:
                            inbound[k].add(rel_path)

    # Simulate inbound links from auto-generated directory indexes.
    # Each directory index lists all .md files in that directory, so each
    # direct child .md gets an inbound link from the directory index slug.
    for d in sorted(wiki_dir.rglob("*")):
        if not d.is_dir():
            continue
        rel_dir = str(d.relative_to(wiki_dir))
        if not rel_dir or rel_dir == ".":
            continue
        if not any(d.rglob("*.md")):
            continue
        parent = d.parent
        if (parent / f"{d.name}.md").exists():
            continue
        # This directory will get an auto-generated index.
        index_slug = rel_dir
        if index_slug not in inbound:
            inbound[index_slug] = set()
        for md_file in sorted(d.iterdir()):
            if not md_file.is_file() or md_file.suffix != ".md":
                continue
            child_rel = os.path.relpath(md_file, f"{wiki_root}/wiki")
            child_slug = str(Path(child_rel).with_suffix(""))
            if child_slug in inbound:
                inbound[child_slug].add(f"{rel_dir}.md (auto-generated)")

    return inbound


def main():
    wiki_root = find_wiki_root()
    pages = get_all_wiki_pages(wiki_root)
    inbound = build_inbound_map(wiki_root, pages)

    # Deduplicate: report each unique file once, keyed by its canonical stem.
    seen: set[str] = set()
    orphans = []
    for slug, rel_path in sorted(pages.items()):
        if rel_path in seen:
            continue
        stem = Path(rel_path).stem
        # A page is meta if its canonical stem is a meta page.
        if is_meta_page(stem):
            continue
        # Check inbound on the canonical stem key and all path variants.
        rel_to_wiki = rel_path[len("wiki/"):] if rel_path.startswith("wiki/") else rel_path
        rel_stem = str(Path(rel_to_wiki).with_suffix(""))
        has_inbound = (
            len(inbound.get(stem, set())) > 0
            or len(inbound.get(rel_stem, set())) > 0
        )
        if not has_inbound:
            seen.add(rel_path)
            orphans.append((stem, rel_path))

    if orphans:
        print(f"ORPHAN PAGES: {len(orphans)}")
        for slug, rel_path in sorted(orphans):
            print(f"  [[{slug}]] ({rel_path}) — no inbound links")
        sys.exit(1)
    else:
        total = len(set(pages.values())) - len(META_PAGES)
        print(f"OK: no orphan pages ({total} content pages checked)")
        sys.exit(0)


if __name__ == "__main__":
    main()