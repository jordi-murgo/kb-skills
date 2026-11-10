# Frontmatter Schema

Every wiki page starts with flat YAML frontmatter. No nested objects. The wiki requires flat structure.

---

## Universal Fields

Every page, no exceptions:

```yaml
---
type: <source|entity|concept|domain|comparison|question|decision|overview|meta>
title: "Human-Readable Title"
created: 2026-04-07
updated: 2026-04-07
tags:
  - <domain-tag>
  - <type-tag>
maturity: <seed|developing|mature|evergreen>
verification: <unreviewed|supported|disputed>
related:
  - "[[Other Page]]"
sources:
  - "[[source-page]]"          # a wiki page; raw files are cited inline with ^[.raw/...]
---
```

`maturity` describes completeness only: `seed` (barely populated), `developing`
(useful but incomplete), `mature` (comprehensive and linked), `evergreen`
(stable enough for infrequent revision).

`verification` describes evidential support only: `unreviewed` (not checked),
`supported` (checked against cited evidence), `disputed` (unresolved conflict).
Never encode verification or workflow state in `maturity`.

Decision pages additionally use `decision_status`; they do not overload either
universal field.

---

## Bilingual Terminology Fields (opt-in)

When the vault opts into the Bilingual Terminology Policy (see `docs/bilingual-terminology-policy.md`), pages that preserve a native-language term carry two additional fields:

```yaml
dnt_class: term-of-art   # term-of-art | eigenname | coined | hybrid
lang: de                 # ISO 639-1 code for the native language; default "en" (and field can be omitted)
```

**dnt_class values:**
- `term-of-art`: defined legal, regulatory, or institutional meaning (`AGB`, `Geschäftsführer`, `Gewerbesteuer`).
- `eigenname`: institution, statute, or scheme without equivalent (`IHK`, `BGB`, `Mittelstand-Digital-Zentrum`).
- `coined`: the wiki's internal vocabulary (project names, conventions).
- `hybrid`: bilingual compound where the compound itself is the unit of meaning (`KI-Berater`, `AI-Engineer-Rolle-DE`).

When `dnt_class` is set, the `aliases:` field **must** contain both the native form and the English gloss. Lint enforces this deterministically only where the vault ships the bilingual check (`lint-terminology.py`, not included in this repo); otherwise the rule is on you to apply.

Translatable pages omit both fields. Default = translatable.

---

## Type-Specific Additions

### source

Add these fields after the universal fields:

```yaml
source_type: article    # article | video | podcast | paper | book | transcript | data
author: ""
date_published: YYYY-MM-DD
url: ""
confidence: high        # high | medium | low
key_claims:
  - "First key claim from this source"
  - "Second key claim"
```

### entity

Keep `type: entity` across all entity subtypes (`person`, `org`, `system`, `service`, `product`, `repository`, `place`) and attach the composable flat facet keys that apply (see `wiki/references/templates.md` for canonical templates and `.obsidian/types.json`):

```yaml
entity_kind: system     # person | org | system | service | product | repository | place
role: ""                # person facet
org: "[[Organization]]" # person facet
owner: ""               # system / service facet
repo: ""                # system / repository facet
endpoint: ""            # system / service facet
first_mentioned: "[[Source Title]]"
aliases:                # required when dnt_class is set; otherwise optional
  - "alternative name"
  - "english gloss"
```

### concept

```yaml
complexity: intermediate  # basic | intermediate | advanced
domain: ""
aliases:                # required when dnt_class is set; otherwise optional
  - "alternative name"
  - "abbreviation"
```

### comparison

```yaml
subjects:
  - "[[Thing A]]"
  - "[[Thing B]]"
dimensions:
  - "performance"
  - "cost"
  - "ease of use"
verdict: "One-line conclusion."
```

### question

```yaml
question: "The original query as asked."
answer_quality: solid   # draft | solid | definitive
```

### decision

```yaml
decision_date: YYYY-MM-DD
decision_status: active  # active | superseded | rejected
```

### domain

```yaml
subdomain_of: ""        # leave empty for top-level domains
page_count: 0
```

---

## Rules

1. Use flat YAML only. Never nest objects.
2. Dates as `YYYY-MM-DD` strings, not ISO datetime on canonical pages. Reserve ISO-8601 timestamps (`YYYY-MM-DDTHH:MM:SSZ`) for machine ledgers (`.cache/knowledge-flow/`, `.raw/.manifest.json`) or optional volatile operational state; `updated` accepts an optional ISO timestamp when a script writes one, and `wiki-sync` treats bare `YYYY-MM-DD` on `updated` as end-of-day UTC so same-day commits do not trigger false staleness.
3. Lists always use the `- item` format, not inline `[a, b, c]` (empty lists `[]` are allowed on optional fields like `related: []`).
4. Wikilinks in YAML fields must be quoted: `"[[Page Name]]"`.
5. Every page has non-empty `type`, `title`, `created`, `updated`, `tags`, `maturity` (`seed|developing|mature|evergreen`), and `verification` (`unreviewed|supported|disputed`); decisions (`type: decision`) also require `decision_status` (`active|superseded|rejected`).
6. Keep `related` and `sources` as wikilinks to wiki pages, not raw paths or URLs. Cite a raw file inline with the `^[.raw/path:L1-L2]` marker that `wiki-lint` validates.
7. Update `updated` every time you edit page content.
