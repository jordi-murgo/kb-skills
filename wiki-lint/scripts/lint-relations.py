#!/usr/bin/env python3
"""Typed directed relations between wiki pages: lint gate and graph queries.

Lint contract (run-lint.py): exit 0 clean, exit 1 findings, stdout report,
--json for machine-readable output.

Convention: a page's YAML frontmatter may carry
    relations:
      - {type: replaces, target: "[[Other Page]]"}
Canonical types and their inverses:
    replaces <-> replaced_by
    validates <-> validated_by
    uses <-> used_by
    extends <-> extended_by
    depends_on <-> needed_by
    contradicts <-> contradicted_by

Pages are identified by [[wikilink title]] (the page's `title` frontmatter,
falling back to filename stem). Graph queries (beyond lint):
    graph [PAGE]       adjacency list (all, or one page's neighborhood)
    reverse PAGE       what points at PAGE (typed backlinks)

Exit codes: 0 ok, 1 lint findings or error, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RELATION_TYPES = {
    "replaces", "replaced_by",
    "validates", "validated_by",
    "uses", "used_by",
    "extends", "extended_by",
    "depends_on", "needed_by",
    "contradicts", "contradicted_by",
}

INVERSES = {
    "replaces": "replaced_by", "replaced_by": "replaces",
    "validates": "validated_by", "validated_by": "validates",
    "uses": "used_by", "used_by": "uses",
    "extends": "extended_by", "extended_by": "extends",
    "depends_on": "needed_by", "needed_by": "depends_on",
    "contradicts": "contradicted_by", "contradicted_by": "contradicts",
}

LINK = re.compile(r"\[\[([^\]|#]+)")


def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git (a skills dir also has wiki/)."""
    import os
    cwd = Path(os.getcwd()).resolve()
    for start in (cwd, Path(__file__).resolve()):
        for parent in [start] + list(start.parents):
            if (parent / "wiki").is_dir() and (parent / ".git").exists():
                return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/")


VAULT = find_vault_root()
WIKI = VAULT / "wiki"
SKIP_BASENAMES = {"log.md", "hot.md"}


def parse_frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.startswith((" ", "\t")):
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
    return fields


def page_title(meta: dict, path: Path) -> str:
    title = meta.get("title", "").strip()
    return title or path.stem


def load_pages() -> dict[str, dict]:
    pages: dict[str, dict] = {}
    for md in sorted(WIKI.rglob("*.md")):
        if md.name in SKIP_BASENAMES or md.parent.name == "meta":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        relations = []
        for entry in re.findall(
            r"^\s*-\s*\{([^}]+)\}", text, re.MULTILINE
        ):
            type_m = re.search(r"type:\s*(\w+)", entry)
            target_m = re.search(r"target:\s*\"?\[\[([^\]]+)\]\]", entry)
            if type_m and target_m:
                relations.append((type_m.group(1), target_m.group(1).strip()))
        pages[page_title(meta, md)] = {
            "path": md,
            "relations": relations,
        }
    return pages


def lint(pages: dict[str, dict]) -> list[dict]:
    findings: list[dict] = []
    for title, page in pages.items():
        for rel_type, target in page["relations"]:
            if rel_type not in RELATION_TYPES:
                findings.append({
                    "page": title,
                    "type": rel_type,
                    "target": target,
                    "problem": f"unknown relation type: {rel_type}",
                })
            elif target == title:
                findings.append({
                    "page": title,
                    "type": rel_type,
                    "target": target,
                    "problem": "self-relation",
                })
            elif target not in pages:
                findings.append({
                    "page": title,
                    "type": rel_type,
                    "target": target,
                    "problem": f"dangling target: no page titled '{target}'",
                })
    return findings


def reverse_edges(pages: dict[str, dict]) -> dict[str, list[tuple[str, str]]]:
    edges: dict[str, list[tuple[str, str]]] = {}
    for title, page in pages.items():
        for rel_type, target in page["relations"]:
            edges.setdefault(target, []).append((title, rel_type))
    return edges


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", nargs="?", default="lint", choices=["lint", "graph", "reverse"])
    parser.add_argument("page", nargs="?", help="page title for graph/reverse")
    parser.add_argument("--depth", type=int, default=1, help="graph traversal depth")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    pages = load_pages()

    if args.mode == "lint":
        findings = lint(pages)
        if args.json:
            print(json.dumps({"findings": findings}, ensure_ascii=False))
            return 1 if findings else 0
        if not findings:
            print(f"relations: ok ({len(pages)} pages)")
            return 0
        print(f"relations: {len(findings)} finding(s)")
        for f in findings:
            print(f"  {f['page']}: [{f['type']}] -> {f['target']}: {f['problem']}")
        return 1

    if args.mode == "graph":
        # adjacency list: all, or one page's neighborhood to --depth
        if args.page:
            edges = reverse_edges(pages)
            seen = {args.page}
            frontier = [args.page]
            for _ in range(args.depth):
                nxt = []
                for node in frontier:
                    for rel_type, target in pages.get(node, {}).get("relations", []):
                        if target not in seen:
                            seen.add(target)
                            nxt.append(target)
                    for src, rel_type in edges.get(node, []):
                        if src not in seen:
                            seen.add(src)
                            nxt.append(src)
                frontier = nxt
            print(json.dumps({args.page: sorted(seen)}, ensure_ascii=False, indent=2))
        else:
            out = {title: [
                {"type": t, "target": tg} for t, tg in page["relations"]
            ] for title, page in pages.items() if page["relations"]}
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.mode == "reverse":
        if not args.page:
            print("usage: reverse PAGE", file=sys.stderr)
            return 2
        edges = reverse_edges(pages)
        incoming = [
            {"from": src, "type": t} for src, t in edges.get(args.page, [])
        ]
        print(json.dumps({args.page: incoming}, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())