---
name: wiki-vetting
description: "Score atomic claims before they enter the vault (grounded/durable/duplicate/sensitive) with a System One decision model, and audit vault lifecycle: missing status, disputes, verified pages without evidence. Triggers on: vet these claims, should this go in the wiki, audit statuses, lifecycle audit, dispute queue."
argument-hint: "--claims claims.json | audit"
---

# wiki-vetting: Claim Vetting and Lifecycle Audit

A gate between "we read it" and "the wiki says it". Claims are scored by a local `/v1/systemone` decision endpoint (noul questions, bit rule p>=0.5); the audit pass is pure static analysis, offline.

## Commands

```bash
python3 scripts/wiki-vet.py --claims claims.json [--json] [--model M] [--endpoint URL]
python3 scripts/wiki-lifecycle.py [--json]
```

Input: JSON array of `{"claim", "quote", "target_page", "source", "skip_grounded"}`. Verdicts: `accept` (write it), `reject` (ungrounded or sensitive — drop or sanitize and re-vet), `review` (human decides). Defaults: model `clef-flash:9b` (env `WIKI_VET_MODEL`), server `http://127.0.0.1:11434` with `/v1/systemone` appended (env `WIKI_VET_ENDPOINT`, else shared `WIKI_OLLAMA_URL`).

## Workflow

1. Extract atomic claims, each with a verbatim quote that proves it.
2. Vet; read the probabilities, not just the verdicts.
3. `accept` → write with `status: verified` and `evidence:` in frontmatter. `review` → `status: developing` or hold.
4. `reject` (sensitive) → never enters the vault; strip the secret, re-vet.

## Hard-won contract details (do not "fix" these)

- **Questions name their claim**: instructions are prefixed `Consider ONLY claim [cN]` — question keys never reach the model, so without the prefix every answer maps to any claim in the batch.
- **`skip_grounded` for events**: a release/event claim is evidenced by its source (feed/URL in `source`), not by a quote; the grounded question is omitted and the verdict treats it as satisfied. Content claims keep the full four axes.
- Batches: ≤16 claims (64-question cap), quotes truncate at 800 chars, body <60 KiB.
- `questions` is a RECORD keyed by id — an array fails with a misleading JSON parse error.

## Lifecycle audit

Reports content pages missing `status`, the dispute queue (`status: disputed` or `> [!contradiction]` callouts with file:line), and `status: verified` pages missing `evidence`. Run after batch ingests; fix the top of the list with real statuses, not bulk-stamping.
