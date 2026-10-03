---
name: wiki-relations
description: "Typed directed relations between wiki pages (replaces, validates, uses, extends, depends_on, contradicts): lint for dangling targets and unknown types, graph traversal, typed backlinks. Triggers on: what replaces X, what depends on, relate these pages, relation lint, successors of."
argument-hint: "lint | graph [PAGE] | reverse PAGE"
---

# wiki-relations: Typed Page Graph

Wikilinks say "related"; typed directed relations say "this **replaces** that", "this **validates** that", "this **depends on** that". For vaults about evolving systems (model successions, stack dependencies, refutations) the graph is the knowledge.

## Convention

```yaml
relations:
  - {type: replaces, target: "[[Old Model]]"}
  - {type: validates, target: "[[Reference Judge]]"}
```

Canonical types and inverses: `replaces`↔`replaced_by`, `validates`↔`validated_by`, `uses`↔`used_by`, `extends`↔`extended_by`, `depends_on`↔`needed_by`, `contradicts`↔`contradicted_by`. Targets are `[[titles]]` (the page's `title` frontmatter, or filename stem). Anything else fails lint.

## Commands

```bash
python3 scripts/wiki-relations.py lint [--json]     # dangling, unknown type, self-relation; exit 1 if findings
python3 scripts/wiki-relations.py graph [PAGE] [--depth N]
python3 scripts/wiki-relations.py reverse PAGE      # typed backlinks
```

## Workflow

- On ingest, declare relations for anything that supersedes, validates, uses or contradicts an existing page.
- `reverse <page>` before changing or superseding: what points here?
- `lint` alongside `wiki-lint` in maintenance passes; renaming a page means updating targets (dangling catches misses).
- Inverses are written manually when discovery matters; symmetry is not enforced.
