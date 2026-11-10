---
name: wiki-consolidate
description: "Consolidate provisional knowledge observations and candidates into canonical wiki pages: merge evidence, update existing knowledge, promote durable novel claims, hold disputes, expire recent-only facts, and measure knowledge yield. Triggers on: consolidate knowledge, promote candidates, process candidate queue, merge observations, expire recent knowledge, resolve knowledge flow."
argument-hint: "queue | candidate-id | expire before YYYY-MM-DD"
allowed-tools: Read Write Edit Bash
license: Apache-2.0
metadata:
  author: kb-skills
  version: "1.0"
---

# wiki-consolidate

## Activation Contract

Use this skill when observation/candidate ledgers exist under `.cache/knowledge-flow/` and the user wants to review, merge, promote, dispute, reject, or expire them. Use `wiki-ingest` to create observations and `wiki-search --scope all` to answer questions without changing canonical pages.

## Hard Rules

- Read the candidate's observations, exact raw source lines, and matched canonical pages before deciding. Ledger labels and snippets are not evidence.
- Default to no new page. Prefer `no_op`, evidence merge, or updating the page that already owns the subject.
- Never merge incompatible claims. Resolve date, validity interval, and scope first; unresolved conflicts stay `hold_dispute`.
- `recent_only` never enters `wiki/**`. It stays searchable as provisional information until promotion, rejection, or expiry.
- Append candidate transitions; never rewrite old JSONL rows. `.cache/knowledge-flow/` is derived and reconstructible from raw evidence.
- Canonical pages use independent `maturity` and `verification`; decisions additionally use `decision_status`.
- A source page is optional and only for a reusable artifact. A mentioned entity/concept does not earn a page.
- Batch canonical navigation updates once: affected index/subindexes, one top log entry, hot cache, overview if changed, and dashboard evidence. No canonical writes means no coordinator churn.

## Commands

Run from the vault root:

```bash
python3 .agents/skills/wiki-consolidate/scripts/consolidate.py queue --json
python3 .agents/skills/wiki-consolidate/scripts/consolidate.py queue --include-terminal --json
python3 .agents/skills/wiki-consolidate/scripts/consolidate.py decide cand-ID \
  --relation extends --durability durable --action update_existing \
  --status pending --matched-page wiki/concepts/example.md --reason "Adds a dated constraint."
python3 .agents/skills/wiki-consolidate/scripts/consolidate.py decide cand-ID \
  --status applied --reason "Updated wiki/concepts/example.md with cited evidence."
python3 .agents/skills/wiki-consolidate/scripts/consolidate.py expire --before YYYY-MM-DD --json
python3 .agents/skills/wiki-consolidate/scripts/consolidate.py expire --before YYYY-MM-DD --apply --json
```

`queue` groups candidates by first matched canonical page and reports `knowledge_yield`, duplicate rate, action/relation counts, and oldest open age. Applied disputes remain visible until explicitly rerouted/resolved. `expire` is dry-run unless `--apply` is present.

## Decision Gates

| Evidence comparison | Action | Canonical effect |
|---|---|---|
| Existing page already says it; provenance adds nothing | `no_op` | None; mark applied |
| Existing claim gains independent useful support | `add_evidence` | Add citation/evidence only |
| Compatible detail changes future answers | `update_existing` | Edit the existing owner page |
| Claims conflict after date/scope checks | `hold_dispute` | Add precise contradiction marker; keep recent result visible |
| Durable claim has no canonical owner | `promote` | Prefer extending a related page; create only if necessary |
| Temporary/current fact | `recent_only` | No wiki write; leave pending |
| Sensitive, unsupported, or unusable provenance | `reject` | No wiki write; mark rejected |

Promotion needs all of:

1. Verbatim source support and exact provenance.
2. `verification: supported` is justified by reviewed evidence; otherwise do not promote.
3. Durable value beyond the retention horizon.
4. No stronger existing owner page.
5. A stable page subject, not merely one isolated claim or entity mention.

## Execution Steps

1. Run `queue --json`. Pick disputes first, then durable updates/promotions, then evidence/no-op, then recent-only expiry. Work one canonical subject group at a time.
2. For each candidate, read every listed raw citation and matched page. Query `wiki-search --scope all` for the claim to catch newer canonical or provisional material.
3. Choose relation, durability, action, target page, and a concrete reason. Append a `pending` transition with `decide` before editing canonical content.
4. Apply:
   - `no_op`: append `status: applied`; no wiki edit.
   - `add_evidence`: add the citation to the existing claim without duplicating prose.
   - `update_existing`: revise the owning section; preserve history and contradictory evidence.
   - `hold_dispute`: add a `[!contradiction]` callout naming candidate ID, both claims, dates/scope, and evidence.
   - `promote`: update an existing page or create one with `maturity`, `verification`, citations, links, and a stable subject.
   - `recent_only`: leave `pending`; set/retain `valid_to` on observations when known.
   - `reject`: append `status: rejected`; no wiki edit.
5. After a successful canonical action, append `status: applied` with the exact changed path in `reason`. If the write fails, leave the pending transition visible; do not claim application.
6. After the subject batch, update canonical navigation/state once. Skip this step when every action was `no_op`, `recent_only`, `reject`, or expiry.
7. Run the relevant page/frontmatter lint and query the changed claim through canonical search after an explicit index build. Confirm recent scope no longer returns candidates applied as `no_op`, evidence, update, or promotion; applied disputes remain.
8. Periodically run `expire` dry-run, inspect candidates and dates, then rerun with `--apply` only when the cutoff is intended.

## Output Contract

Report:

```text
Candidates reviewed: N
Applied: no_op N | evidence N | update N | dispute N | promote N
Retained recent-only: N | expired N | rejected N
Canonical pages created: [paths or none]
Canonical pages updated: [paths or none]
Open disputes: [candidate IDs or none]
Metrics: knowledge_yield X | duplicate_rate Y | oldest_open_days Z
Verification: [commands and observed results]
```

Never describe pending or recent-only information as canonical knowledge.
