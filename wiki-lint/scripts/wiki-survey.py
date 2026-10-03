#!/usr/bin/env python3
"""Survey wiki pages in a directory or glob.

Extracts frontmatter (type, title, status, tags, created, updated) and the
first heading + first callout/insight from each .md file. Output is compact
and tabular, designed for quick comparison when evaluating merges, dedup,
or index improvements.

Usage:
    python3 wiki-survey.py [path_or_glob ...]

If no arguments, surveys all wiki/**/*.md.
Paths can be directories (scanned recursively) or glob patterns.
Excludes .reciclaje/ and meta pages (index, log, hot) by default.

Flags:
    --all          include meta pages and .reciclaje
    --fields f1,f2 limit fields shown (default: type,status,tags,updated)
    --no-callout   suppress first callout/insight line
    --json         machine-readable output
"""

import sys
import os
import re
import json
import glob
import argparse
from pathlib import Path


def find_wiki_root(start: str = ".") -> str:
    p = Path(start).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return str(parent)
    return str(p)


META_PAGES = {"index", "log", "hot"}
ALL_FIELDS = ["type", "title", "status", "tags", "created", "updated"]


def parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter into a dict. Values are strings or lists."""
    fm = {}
    if not content.startswith("---"):
        return fm
    lines = content.split("\n")
    i = 1
    current_key = None
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            break
        # List item under a key (  - value)
        if line.strip().startswith("- ") and current_key:
            val = line.strip()[2:].strip()
            # strip quotes
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            if isinstance(fm.get(current_key), list):
                fm[current_key].append(val)
            elif current_key in fm:
                fm[current_key] = [fm[current_key], val]
            else:
                fm[current_key] = [val]
        elif ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            if val == "":
                fm[key] = []
                current_key = key
            else:
                fm[key] = val
                current_key = key
        i += 1
    return fm


def extract_first_heading(content: str) -> str:
    """Extract the first markdown heading (# Title)."""
    for line in content.split("\n"):
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return ""


def extract_first_insight(content: str) -> str:
    """Extract the first callout/insight block content."""
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("> [!"):
            # Collect the callout body
            callout_lines = []
            i += 1
            while i < len(lines) and lines[i].strip().startswith(">"):
                text = lines[i].strip().lstrip(">").strip()
                if text:
                    callout_lines.append(text)
                i += 1
            return " ".join(callout_lines)[:200]
        i += 1
    return ""


def collect_files(paths: list[str], wiki_root: str, include_all: bool) -> list[str]:
    """Collect .md files from paths (dirs or globs), relative to wiki_root."""
    if not paths:
        pattern = f"{wiki_root}/wiki/**/*.md"
        files = sorted(glob.glob(pattern, recursive=True))
    else:
        files = []
        for p in paths:
            # Resolve relative to wiki_root if not absolute
            if not os.path.isabs(p):
                p = os.path.join(wiki_root, p)
            if os.path.isdir(p):
                files.extend(sorted(glob.glob(f"{p}/**/*.md", recursive=True)))
            else:
                files.extend(sorted(glob.glob(p, recursive=True)))
                # Also try as glob relative to wiki/
                if not files:
                    rel = os.path.relpath(p, wiki_root)
                    files.extend(sorted(glob.glob(f"{wiki_root}/{rel}", recursive=True)))

    # Deduplicate
    seen = set()
    result = []
    for f in files:
        rp = os.path.realpath(f)
        if rp not in seen:
            seen.add(rp)
            result.append(f)
    return sorted(result)


def filter_files(files: list[str], wiki_root: str, include_all: bool) -> list[str]:
    """Filter out .reciclaje and meta pages unless --all."""
    if include_all:
        return files
    result = []
    for f in files:
        rel = os.path.relpath(f, wiki_root)
        if ".reciclaje" in rel:
            continue
        stem = Path(f).stem
        if stem in META_PAGES:
            continue
        result.append(f)
    return result


def format_tags(tags) -> str:
    if isinstance(tags, list):
        return ",".join(tags)
    return str(tags) if tags else ""


def survey_file(filepath: str, wiki_root: str) -> dict:
    """Extract survey data from a single file."""
    content = Path(filepath).read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(content)
    rel = os.path.relpath(filepath, wiki_root)
    return {
        "path": rel,
        "slug": Path(filepath).stem,
        "type": fm.get("type", ""),
        "title": fm.get("title", ""),
        "status": fm.get("status", ""),
        "tags": format_tags(fm.get("tags", "")),
        "created": fm.get("created", ""),
        "updated": fm.get("updated", ""),
        "heading": extract_first_heading(content),
        "insight": extract_first_insight(content),
    }


def print_table(records: list[dict], fields: list[str], show_callout: bool):
    """Print a compact table."""
    # Header
    col_path = "path"
    cols = [col_path] + fields
    if show_callout:
        cols.append("insight")

    widths = {}
    for col in cols:
        widths[col] = len(col)
    for r in records:
        for col in cols:
            val = str(r.get(col, ""))
            # Truncate long values for display
            display = val[:60] if col != "path" else val
            widths[col] = max(widths[col], len(display))

    # Print header
    header = "  ".join(col.ljust(widths[col]) for col in cols)
    print(header)
    print("  ".join("-" * widths[col] for col in cols))

    for r in records:
        parts = []
        for col in cols:
            val = str(r.get(col, ""))
            if col != "path":
                val = val[:60]
            parts.append(val.ljust(widths[col]))
        print("  ".join(parts))


def main():
    parser = argparse.ArgumentParser(description="Survey wiki pages for dedup/merge analysis")
    parser.add_argument("paths", nargs="*", help="directories or glob patterns (default: all wiki)")
    parser.add_argument("--all", action="store_true", help="include meta pages and .reciclaje")
    parser.add_argument("--fields", default="type,status,tags,updated",
                        help="comma-separated fields to show (default: type,status,tags,updated)")
    parser.add_argument("--no-callout", action="store_true", help="suppress first callout/insight")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args()

    wiki_root = find_wiki_root()
    fields = [f.strip() for f in args.fields.split(",")]

    files = collect_files(args.paths, wiki_root, args.all)
    files = filter_files(files, wiki_root, args.all)

    if not files:
        print("No .md files found.", file=sys.stderr)
        sys.exit(1)

    records = [survey_file(f, wiki_root) for f in files]

    if args.json:
        json.dump(records, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        print(f"Wiki survey — {len(records)} pages\n")
        print_table(records, fields, not args.no_callout)


if __name__ == "__main__":
    main()