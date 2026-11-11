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
falling back to filename stem).

This gate reads ONLY the typed `relations:` block above. The untyped graph a
reader follows — `[[...]]` in page bodies — is indexed by `wiki-search build` and
queried with `wiki-search graph [PAGE]`. They are separate on purpose: one
relationship counted in both places would double every centrality number.

Graph queries here (typed edges only):
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

WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def link_text_key(text: str) -> str:
    """Canonical node id for a wikilink target as written (stem, lowercased)."""
    raw = text.strip()
    if raw.endswith(".md"):
        raw = raw[:-3]
    return Path(raw).name.lower()


def page_key(relpath: Path | str) -> str:
    return Path(relpath).stem.lower()

def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git, and wiki/ is not a skill.

    A skill directory named "wiki" lives under .agents/skills/ and this
    repository itself contains wiki/SKILL.md; either would otherwise satisfy
    the wiki/ + .git test and silently resolve to the wrong tree.
    """
    import os
    cwd = Path(os.getcwd()).resolve()
    for start in (cwd, Path(__file__).resolve()):
        for parent in [start] + list(start.parents):
            if (
                (parent / "wiki").is_dir()
                and (parent / ".git").exists()
                and not (parent / "wiki" / "SKILL.md").exists()
            ):
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
                fields[key.strip()] = value.strip().strip("'\"")
    return fields


def page_title(meta: dict, path: Path) -> str:
    title = meta.get("title", "").strip()
    return title or path.stem


def resolve_page(args_page: str, pages: dict[str, dict]) -> str | None:
    """Accept any spelling the user might write: title, filename stem, path.

    Body links spell targets by stem far more often than by frontmatter title
    (measured in vulcano-kb: `[[backend-lenguaje-vulcano]]`, never the title),
    so `reverse backend-lenguaje-vulcano` must land on the page titled
    "Arquitectura de implementación — ...". Without this alias the query
    answered [] on every real vault.
    """
    if args_page in pages:
        return args_page
    for title, page in pages.items():
        if page["key"] == args_page.lower():
            return title
        if page_key(page["path"]) == link_text_key(args_page):
            return title
    # case-insensitive title match as a last resort
    for title in pages:
        if title.lower() == args_page.lower():
            return title
    return None


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
        # Body wikilinks: the untyped edges readers actually follow. Resolved
        # like wiki-search resolves them (stem + frontmatter title onto one
        # node id), so `[[Real Title]]` and filename-different.md are the same
        # node. 0 pages in any production vault declare typed relations:, so
        # without these the `graph`/`reverse` queries answer {} everywhere
        # (measured: 2,263 body links in vulcano-kb produced an empty graph).
        body = text
        if body.startswith("---"):
            end = body.find("\n---", 3)
            if end != -1:
                body = body[end + 4 :]
        body_links = sorted({link_text_key(t) for t in WIKILINK.findall(body)})
        pages[page_title(meta, md)] = {
            "path": md,
            "relations": relations,
            "body_links": body_links,
            "key": page_key(md),
        }
    return pages



def body_edges(pages: dict[str, dict]) -> list[tuple[str, str]]:
    """Resolved body-wikilink edges as (source_title, target_title)."""
    by_key = {page["key"]: title for title, page in pages.items()}
    # A link may spell the target as its stem, its path, or its frontmatter
    # title; resolve onto node keys, then back to titles for reporting.
    resolve: dict[str, str] = {}
    for title, page in pages.items():
        resolve[page_key(page["path"])] = page["key"]
        if title:
            resolve[link_text_key(title)] = page["key"]
    edges = []
    for title, page in pages.items():
        for target_key in page["body_links"]:
            resolved = resolve.get(target_key, target_key)
            target_title = by_key.get(resolved, target_key)
            if target_title != title:
                edges.append((title, target_title))
    return edges


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
        typed_out = {title: [
            {"type": t, "target": tg} for t, tg in page["relations"]
        ] for title, page in pages.items() if page["relations"]}
        body = body_edges(pages)
        body_adj: dict[str, list[str]] = {}
        for src, dst in body:
            body_adj.setdefault(src, []).append(dst)
            body_adj.setdefault(dst, []).append(src)
        if args.page:
            # one page's neighbourhood to --depth, over BOTH edge sets
            resolved = resolve_page(args.page, pages)
            if resolved is None:
                print(f"error: no page titled '{args.page}'", file=sys.stderr)
                return 2
            edges = reverse_edges(pages)
            seen = {resolved}
            frontier = [resolved]
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
                    for neighbour in body_adj.get(node, []):
                        if neighbour not in seen:
                            seen.add(neighbour)
                            nxt.append(neighbour)
                frontier = nxt
            print(json.dumps({resolved: sorted(seen - {resolved})}, ensure_ascii=False, indent=2))
        else:
            out = {
                "typed": typed_out,
                "body": {title: sorted(neighbours) for title, neighbours in body_adj.items()},
                "body_edge_count": len(body),
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.mode == "reverse":
        if not args.page:
            print("usage: reverse PAGE", file=sys.stderr)
            return 2
        resolved = resolve_page(args.page, pages)
        if resolved is None:
            print(f"error: no page titled '{args.page}'", file=sys.stderr)
            return 2
        edges = reverse_edges(pages)
        incoming_typed = [
            {"from": src, "type": t} for src, t in edges.get(resolved, [])
        ]
        incoming_body = sorted({
            src for src, dst in body_edges(pages) if dst == resolved
        })
        print(json.dumps({
            resolved: {
                "typed": incoming_typed,
                "body": incoming_body,
            }
        }, ensure_ascii=False, indent=2))
        return 0

if __name__ == "__main__":
    sys.exit(main())