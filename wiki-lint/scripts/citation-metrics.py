#!/usr/bin/env python3
"""Citation metrics: reports how well the wiki is grounded in sources.

Walks every wiki page and aggregates the ^[source] citation markers
(same syntax as lint-citations.py):

    ^[.raw/paper.md]              whole-source citation
    ^[.raw/paper.md:42-58]        line-range citation
    ^[.raw/paper.md#L42-L58]      same range, GitHub-style anchor

Metrics (per vault):
    markers                  total citation markers outside code fences
    whole / ranged           whole-source vs line-range split and ratio
    grounded_pages           pages with >=1 marker; ratio over all pages
    distinct_sources         distinct cited source paths
    top_sources              10 most-cited sources
    ungrounded_pages         zero-marker content pages, by path
                             (meta pages are exempt: wiki/meta/** and
                             wiki/{index,log,hot,_index}.md)

This is a REPORT, not a lint gate: it never fails on low coverage.
Exit codes: 0 ok, 1 when the vault root cannot be found or a page cannot
be read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

MARKER = re.compile(r"\^\[([^\]\n]*)\]")
SPEC = re.compile(r"^(?P<path>.+?)(?:(?::|#L)(?P<a>\d+)(?:-L?(?P<b>\d+))?)?$")

TOP_SOURCES_LIMIT = 10
META_DIR = "meta"
META_BASENAMES = {"index.md", "log.md", "hot.md", "_index.md"}


def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git (a skills dir also has wiki/)."""
    cwd = Path(os.getcwd()).resolve()
    for start in (cwd, Path(__file__).resolve()):
        for parent in [start] + list(start.parents):
            if (parent / "wiki").is_dir() and (parent / ".git").exists():
                return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/")


def iter_markers(text: str):
    """Yield (line_number, inner_text) for markers outside fenced code blocks."""
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in MARKER.finditer(line):
            yield lineno, m.group(1)


def is_meta_page(rel: str) -> bool:
    """True for wiki/meta/** and root meta files; exempt from ungrounded list."""
    parts = Path(rel).parts
    if not parts:
        return True
    return parts[0] == META_DIR or (len(parts) == 1 and parts[0] in META_BASENAMES)


def measure(vault: Path) -> dict:
    pages = 0
    markers = 0
    whole = 0
    ranged = 0
    grounded_pages = 0
    ungrounded = []
    source_counts: Counter[str] = Counter()

    for page in sorted((vault / "wiki").rglob("*.md")):
        pages += 1
        rel_page = page.relative_to(vault / "wiki").as_posix()
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"error: cannot read {rel_page}: {exc}", file=sys.stderr)
            sys.exit(1)

        page_markers = 0
        for _, inner in iter_markers(text):
            page_markers += 1
            spec = inner.strip()
            m = SPEC.match(spec)
            if not m or not m.group("path").strip():
                continue  # unparseable marker: counted, but cites no source
            source_counts[m.group("path").strip()] += 1
            if m.group("a"):
                ranged += 1
            else:
                whole += 1

        markers += page_markers
        if page_markers:
            grounded_pages += 1
        elif not is_meta_page(rel_page):
            ungrounded.append(rel_page)

    top = sorted(source_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_SOURCES_LIMIT]
    return {
        "vault": str(vault),
        "pages": pages,
        "markers": markers,
        "whole": whole,
        "ranged": ranged,
        "ranged_ratio": round(ranged / markers, 4) if markers else 0.0,
        "grounded_pages": grounded_pages,
        "grounded_ratio": round(grounded_pages / pages, 4) if pages else 0.0,
        "distinct_sources": len(source_counts),
        "top_sources": [[path, count] for path, count in top],
        "ungrounded_pages": sorted(ungrounded),
    }


def print_report(r: dict) -> None:
    print(f"Citation metrics for {r['vault']}")
    print(f"  pages: {r['pages']}  grounded: {r['grounded_pages']} "
          f"({r['grounded_ratio']:.1%})")
    print(f"  markers: {r['markers']}  whole: {r['whole']}  ranged: {r['ranged']} "
          f"(ranged {r['ranged_ratio']:.1%})")
    print(f"  distinct sources: {r['distinct_sources']}")
    if r["top_sources"]:
        print("  top sources:")
        for path, count in r["top_sources"]:
            print(f"    {count:4d}  {path}")
    else:
        print("  top sources: none")
    if r["ungrounded_pages"]:
        print(f"  ungrounded pages ({len(r['ungrounded_pages'])}):")
        for page in r["ungrounded_pages"]:
            print(f"    {page}")
    else:
        print("  ungrounded pages: none")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    vault = find_vault_root()
    result = measure(vault)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_report(result)

    sys.exit(0)


if __name__ == "__main__":
    main()
