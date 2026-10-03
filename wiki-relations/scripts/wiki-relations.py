#!/usr/bin/env python3
"""Typed directed relations between wiki pages: lint and graph queries.

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
falling back to filename stem). Commands:
    lint               dangling targets, unknown types, self-relations
    graph [PAGE]       adjacency list (all, or one page's neighborhood)
    reverse PAGE       what points at PAGE (typed backlinks)

Exit codes: 0 ok (lint clean), 1 lint findings or error, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
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

INVERSES = {
    "replaces": "replaced_by",
    "replaced_by": "replaces",
    "validates": "validated_by",
    "validated_by": "validates",
    "uses": "used_by",
    "used_by": "uses",
    "extends": "extended_by",
    "extended_by": "extends",
    "depends_on": "needed_by",
    "needed_by": "depends_on",
    "contradicts": "contradicted_by",
    "contradicted_by": "contradicts",
}

LINK = re.compile(r"\[\[([^\]|#]+)")


def parse_frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    in_relations = False
    fields: dict = {"relations": []}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if re.match(r"^relations:\s*$", line):
            in_relations = True
            continue
        if in_relations:
            item = re.match(r"^\s*-\s*\{?(.*?)\}?,?\s*$", line.rstrip())
            if not item:
                if re.match(r"^\S", line):
                    in_relations = False
                continue
            entry: dict = {}
            for part in re.finditer(r"(\w+):\s*([^,}]+)", item.group(1)):
                entry[part.group(1)] = part.group(2).strip().strip("'\"")
            if entry:
                fields["relations"].append(entry)
        elif ":" in line and line[:1] not in (" ", "\t", "-"):
            key, _, value = line.partition(":")
            fields.setdefault(key.strip(), value.strip().strip("'\""))
    return fields


def page_title(meta: dict, path: Path) -> str:
    title = meta.get("title", "").strip()
    return title or path.stem


def load_pages() -> dict[str, dict]:
    pages: dict[str, dict] = {}
    for path in sorted(WIKI.rglob("*.md")):
        if path.name in SKIP_BASENAMES or path.name == "_index.md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = parse_frontmatter(text)
        title = page_title(meta, path)
        entry = pages.setdefault(title, {"path": path.relative_to(VAULT).as_posix(), "relations": []})
        for rel in meta.get("relations", []):
            target_match = LINK.search(rel.get("target", ""))
            if target_match:
                entry["relations"].append({"type": rel.get("type", ""), "target": target_match.group(1).strip()})
            else:
                # Entry parsed but no [[target]]: block-style YAML or missing link.
                # Keep it so lint can flag it instead of silently dropping (rule: gates must not pass on corrupt input).
                entry["relations"].append({"type": rel.get("type", ""), "target": "", "malformed": True})
    return pages


def lint(pages: dict[str, dict]) -> list[dict]:
    findings: list[dict] = []
    for title, page in pages.items():
        for rel in page["relations"]:
            rtype, target = rel["type"], rel["target"]
            if rel.get("malformed"):
                findings.append({"page": title, "issue": "malformed entry (no [[target]] parsed; use flow style '- {type: X, target: \"[[page]]\"}')", "detail": rtype or "?"})
                continue
            if rtype not in INVERSES:
                findings.append({"page": title, "issue": f"unknown type '{rtype}'", "detail": target})
            if target == title:
                findings.append({"page": title, "issue": "self-relation", "detail": target})
            if target not in pages:
                findings.append({"page": title, "issue": "dangling target", "detail": target})
    return findings


def reverse_edges(pages: dict[str, dict]) -> dict[str, list[tuple[str, str]]]:
    edges: dict[str, list[tuple[str, str]]] = {}
    for title, page in pages.items():
        for rel in page["relations"]:
            if rel.get("malformed"):
                continue
            edges.setdefault(rel["target"], []).append((rel["type"], title))
    return edges


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lint", help="dangling targets, unknown types, self-relations").add_argument("--json", action="store_true")
    graph_p = sub.add_parser("graph", help="adjacency list; PAGE restricts to its neighborhood")
    graph_p.add_argument("page", nargs="?")
    graph_p.add_argument("--depth", type=int, default=1, help="neighborhood depth (default 1)")
    rev = sub.add_parser("reverse", help="typed backlinks pointing at PAGE")
    rev.add_argument("page")

    args = parser.parse_args(argv)
    if not WIKI.is_dir():
        print(f"no wiki directory at {WIKI}", file=sys.stderr)
        return 1
    pages = load_pages()

    if args.cmd == "lint":
        findings = lint(pages)
        if args.json:
            print(json.dumps({"pages": len(pages), "findings": findings}, ensure_ascii=False, indent=1))
        else:
            rel_count = sum(len(p["relations"]) for p in pages.values())
            print(f"pages: {len(pages)}, relations: {rel_count}, findings: {len(findings)}")
            for f in findings:
                print(f"  {f['page']}: {f['issue']} -> {f['detail']}")
        return 1 if findings else 0

    if args.cmd == "graph":
        if args.page:
            if args.page not in pages and args.page not in reverse_edges(pages):
                print(f"error: unknown page '{args.page}'", file=sys.stderr)
                return 2
            seen = {args.page}
            frontier = [args.page]
            for _ in range(max(1, args.depth)):
                nxt = []
                for node in frontier:
                    for rel in pages.get(node, {}).get("relations", []):
                        if rel.get("malformed"):
                            continue
                        print(f"{node}  --{rel['type']}-->  {rel['target']}")
                        if rel["target"] not in seen:
                            seen.add(rel["target"])
                            nxt.append(rel["target"])
                frontier = nxt
        else:
            for title, page in sorted(pages.items()):
                for rel in page["relations"]:
                    if rel.get("malformed"):
                        continue
                    print(f"{title}  --{rel['type']}-->  {rel['target']}")
        return 0

    if args.cmd == "reverse":
        edges = reverse_edges(pages)
        if args.page not in pages and args.page not in edges:
            print(f"error: unknown page '{args.page}'", file=sys.stderr)
            return 2
        for rtype, source in sorted(edges.get(args.page, [])):
            print(f"{source}  --{rtype}-->  {args.page}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
