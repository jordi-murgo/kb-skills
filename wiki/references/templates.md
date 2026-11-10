# Canonical Note Templates & Property Types

When scaffolding a vault (`wiki/SKILL.md` step 6) or creating new pages manually, materialize canonical note templates under `wiki/_templates/`. All deterministic `wiki-lint` checks exempt `wiki/_templates/`, so placeholder skeletons never pollute orphan, dead-link, empty-section, or frontmatter gates.

## Design principles (adapted from Kepano's Obsidian pattern)

1. **Flat YAML only** — never nest objects. Every property is a top-level key so standard regex/line parsers, SQLite indexers, and Obsidian Bases read it without a full YAML AST.
2. **Orthogonal dimensions** — never overload a single `status` field:
   - `maturity`: completeness (`seed` | `developing` | `mature` | `evergreen`)
   - `verification`: evidence backing (`unreviewed` | `supported` | `disputed`)
   - `decision_status`: decision lifecycle (`active` | `superseded` | `rejected`, required when `type: decision`)
3. **List-by-default relational fields** — properties that hold tags, aliases, wikilink relations, or citations (`tags`, `aliases`, `related`, `sources`, `evidence`, `stakeholders`) always use block lists (`- item`), even for a single value. That prevents scalar-to-list migration churn as the graph grows.
4. **Dates as `YYYY-MM-DD`** — `created`, `updated`, `decision_date`, and `date_published` use calendar dates (`YYYY-MM-DD`), never ISO timestamps on canonical pages. Reserve ISO-8601 timestamps (`YYYY-MM-DDTHH:MM:SSZ`) for machine ledgers (`.cache/knowledge-flow/`, `.raw/.manifest.json`) and optional volatile operational caches (`wiki/hot.md`).
5. **Composable entity facets** — keep `type: entity` for all entity pages (`person`, `org`, `system`, `product`, `service`, `place`) so folder routing and lint gates stay uniform, and add only the small facet block (`role`/`org`, `owner`/`repo`/`endpoint`) that applies to that subtype.

---

## 1. Concept Template (`wiki/_templates/concept.md`)

```markdown
---
type: concept
title: "{{title}}"
created: {{date}}
updated: {{date}}
tags:
  - concept
aliases: []
maturity: seed
verification: unreviewed
related: []
sources: []
---

# {{title}}

One-paragraph definition in declarative present tense.

## Key Properties

- 

## Relationships & Tradeoffs

- 

## Sources

- 
```

## 2. Entity Template (`wiki/_templates/entity.md`)

Keep `type: entity` across all entity subtypes and include the relevant facet keys (`entity_kind: person | org | system | service | product`):

```markdown
---
type: entity
entity_kind: system
title: "{{title}}"
created: {{date}}
updated: {{date}}
tags:
  - entity
aliases: []
maturity: seed
verification: unreviewed
related: []
sources: []
---

# {{title}}

Role and responsibility of this entity within the domain.

## Key Facts

- 

## Interfaces & Dependencies

- 

## Sources

- 
```

### Composable entity facets (optional flat keys)

Add only the facet block matching `entity_kind`:

```yaml
# Facet: person
entity_kind: person
role: ""
org: "[[Organization]]"

# Facet: system / service
entity_kind: system
owner: ""
repo: ""
endpoint: ""

# Facet: org / vendor
entity_kind: org
domain: ""
```

## 3. Source Template (`wiki/_templates/source.md`)

```markdown
---
type: source
title: "{{title}}"
created: {{date}}
updated: {{date}}
tags:
  - source
maturity: developing
verification: supported
source_type: article
author: ""
date_published: {{date}}
url: ""
confidence: high
related: []
---

# {{title}}

## Summary

Concise synthesis of what this source establishes and why it matters.

## Key Claims

- Claim supported by verbatim evidence ^[.raw/path-to-source.md:1-10]

## Implications

- Pages updated or challenged:
```

## 4. Decision Template (`wiki/_templates/decision.md`)

`decision_status` is mandatory on every `type: decision` page and validated by `lint-frontmatter.py`.

```markdown
---
type: decision
title: "{{title}}"
created: {{date}}
updated: {{date}}
tags:
  - decision
maturity: developing
verification: supported
decision_status: active
decision_date: {{date}}
evidence:
  - "[[Source Page]]"
related: []
sources: []
---

# {{title}}

## Context

Problem statement, constraints, and forces at play.

## Decision

Chosen option and rationale.

## Alternatives Considered

- **Option A** — why rejected or deferred.

## Consequences

- Positive outcomes, risks, and follow-up obligations.
```

## 5. Comparison Template (`wiki/_templates/comparison.md`)

```markdown
---
type: comparison
title: "{{title}}"
created: {{date}}
updated: {{date}}
tags:
  - comparison
maturity: developing
verification: unreviewed
items_compared:
  - "[[Item A]]"
  - "[[Item B]]"
verdict: ""
related: []
sources: []
---

# {{title}}

## Evaluation Criteria

| Criterion | [[Item A]] | [[Item B]] |
|---|---|---|
| | | |

## Tradeoffs & Verdict

When to choose each option based on constraints.
```

## 6. Question Template (`wiki/_templates/question.md`)

```markdown
---
type: question
title: "{{title}}"
created: {{date}}
updated: {{date}}
tags:
  - question
maturity: developing
verification: unreviewed
question: "{{title}}"
answer_quality: partial
related: []
sources: []
---

# {{title}}

## Answer

Direct answer based on current vault evidence.

> [!gap] Open uncertainty
> What remains unverified or requires primary source confirmation.

## Supporting Pages

- 
```

---

## Optional Obsidian Property Types (`.obsidian/types.json`)

When a vault is opened in Obsidian (especially with Obsidian Bases `.base` views), shipping `.obsidian/types.json` binds each frontmatter key to a single widget type (`text`, `multitext`, `date`, `number`, `checkbox`).

Notice that `created`, `updated`, `decision_date`, and `date_published` are all bound to `"date"` (not `"datetime"`): mixing `YYYY-MM-DD` and `YYYY-MM-DDTHH:MM:SS` in the same property breaks Obsidian's date picker and day-level grouping.

```json
{
  "types": {
    "type": "text",
    "entity_kind": "text",
    "title": "text",
    "maturity": "text",
    "verification": "text",
    "decision_status": "text",
    "source_type": "text",
    "author": "text",
    "url": "text",
    "confidence": "text",
    "verdict": "text",
    "question": "text",
    "answer_quality": "text",
    "address": "text",
    "created": "date",
    "updated": "date",
    "decision_date": "date",
    "date_published": "date",
    "tags": "multitext",
    "aliases": "multitext",
    "related": "multitext",
    "sources": "multitext",
    "evidence": "multitext",
    "items_compared": "multitext"
  }
}
```
