# Knowledge-flow contract

Read this file before every single or batch ingest. The ingest path is observation-first:
source evidence becomes immediately searchable provisional state; only durable, non-duplicate
knowledge changes canonical wiki pages.

## Storage layers

| Layer | Location | Purpose | May answer queries? |
|---|---|---|---|
| Raw evidence | `.raw/**` | Immutable source material | Only through citations |
| Observation ledger | `.cache/knowledge-flow/observations.jsonl` | Atomic sourced claims | Yes, as provisional recent information |
| Candidate ledger | `.cache/knowledge-flow/candidates.jsonl` | Comparison and routing decisions | Yes, with its action/relation shown |
| Canonical knowledge | `wiki/**` | Consolidated durable knowledge | Yes, as canonical information |

Both JSONL ledgers are derived caches. Deleting `.cache/knowledge-flow/` must be safe: rerunning
ingest against the raw source reconstructs it. Never write observation or candidate ledgers under
`wiki/**` or `.raw/**`.

## Observation record

One JSON object per line:

```json
{
  "observation_id": "obs-<16 lowercase hex>",
  "source_path": ".raw/articles/example.md",
  "source_sha256": "<64 lowercase hex>",
  "source_lines": "42-47",
  "quote": "Verbatim source text.",
  "claim": "One atomic factual assertion supported by the quote.",
  "recorded_at": "YYYY-MM-DDTHH:MM:SSZ",
  "observed_at": "YYYY-MM-DD",
  "observed_at_precision": "day",
  "valid_from": null,
  "valid_to": null,
  "epistemic_status": "observed"
}
```

Required invariants:

- `observation_id` is deterministic: first 16 hex characters of SHA-256 over
  `source_sha256 + "\n" + source_lines + "\n" + claim`.
- `quote` is verbatim and `source_lines` names lines actually read. Never infer line ranges.
- `claim` is atomic. Split conjunctions that can become independently true or false.
- `observed_at` is when the underlying event/fact was observed, not when ingest ran. Use the source
  publication date when no more precise event date exists.
- `observed_at_precision` is `instant`, `day`, `month`, `year`, or `unknown`.
- `valid_from`/`valid_to` are optional validity bounds for facts whose truth changes over time.
- `epistemic_status` is `observed`, `retracted`, or `superseded`. Append a new record to change it;
  do not rewrite history in place.

Before appending, load existing records keyed by `observation_id`. An identical record is a no-op.
A changed record with the same ID is an error: the deterministic inputs and content disagree.

## Candidate record

One JSON object per line:

```json
{
  "candidate_id": "cand-<16 lowercase hex>",
  "observation_ids": ["obs-..."],
  "matched_pages": ["wiki/concepts/example.md"],
  "relation": "extends",
  "durability": "durable",
  "action": "update_existing",
  "reason": "Adds a dated constraint absent from the canonical page.",
  "decided_at": "YYYY-MM-DDTHH:MM:SSZ",
  "status": "pending"
}
```

`candidate_id` is deterministic: first 16 hex characters of SHA-256 over sorted observation IDs
joined by `\n`. Group observations only when one canonical edit must apply them together.

Allowed values:

- `relation`: `duplicate`, `corroborates`, `extends`, `contradicts`, `novel`
- `durability`: `durable`, `ephemeral`
- `action`: `no_op`, `add_evidence`, `update_existing`, `hold_dispute`, `promote`,
  `recent_only`, `reject`
- `status`: `pending`, `applied`, `expired`, `rejected`

A later decision appends a complete record with the same `candidate_id`; the last valid record wins.
Never edit old JSONL rows.

## Decision gates

Classify only after retrieving related canonical and recent material. Use exact citations, dates,
and validity intervals before semantic similarity.

| Condition | Relation | Action | Canonical write |
|---|---|---|---|
| Same claim and provenance adds nothing | `duplicate` | `no_op` | None |
| Same claim, independent useful evidence | `corroborates` | `add_evidence` | Add citation/evidence to existing page |
| Compatible new detail changes future answers | `extends` | `update_existing` | Update existing page |
| Incompatible claim, unresolved by dates/scope | `contradicts` | `hold_dispute` | Add a precise contradiction marker; do not choose a winner |
| New durable knowledge with a stable subject | `novel` | `promote` | Prefer an existing page; create one only if no page can own it |
| New but transient/current state | `novel` | `recent_only` | None; query from recent ledger until expiry |
| Sensitive, unsupported, or unusable provenance | any | `reject` | None |

Default to no new page. A source page is justified only when the source is itself a reusable
artifact: a standard, paper, ADR, contract, design, dataset, or reference whose whole structure
will be cited again. A mention does not justify an entity page. Create an entity/concept page only
when it owns durable knowledge that cannot be represented clearly on an existing page.

## Applying candidates

1. Record observations before any wiki write.
2. Search with `wiki-search.py query "<claim>" --scope all --top 8 --mode hybrid --json`.
   If the canonical index is unavailable, use `--scope recent` immediately and inspect relevant
   wiki pages through `wiki/index.md`; never treat a missing index as permission to create.
3. Run `wiki-vetting` for grounded/durable/duplicate/sensitive signals. Its routing action is an
   input, not proof; inspect undecided axes and retrieved evidence.
4. Write the candidate record before applying it.
5. Apply only `add_evidence`, `update_existing`, `hold_dispute`, or `promote`.
6. Append the candidate again with `status: applied` after a successful canonical write.
   `no_op` is also `applied`; `recent_only` remains `pending` until expiry or promotion;
   `reject` uses `rejected`.
7. Preserve raw citations on every changed factual sentence.

An ingest that yields only `no_op`, `recent_only`, or `reject` changes no wiki page, index, hot
cache, dashboard, or wiki log. It may update `.raw/.manifest.json` and the derived ledgers. Report
observations, actions, and zero canonical writes explicitly.
