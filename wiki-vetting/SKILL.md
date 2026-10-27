---
name: wiki-vetting
description: "Score atomic claims before they enter the vault (grounded/durable/duplicate/sensitive) with a System One decision model, and audit vault lifecycle: missing status, disputes, verified pages without evidence. Triggers on: vet these claims, should this go in the wiki, audit statuses, lifecycle audit, dispute queue."
argument-hint: "--claims claims.json | audit"
---

# wiki-vetting: Claim Vetting and Lifecycle Audit

A gate between "we read it" and "the wiki says it". Claims are scored by a local `/v1/systemone` decision endpoint (noul questions, per-axis thresholds); the audit pass is pure static analysis, offline.

## Commands

```bash
python3 .agents/skills/wiki-vetting/scripts/wiki-vet.py --claims claims.json [--json] [--model M] [--endpoint URL]
python3 .agents/skills/wiki-vetting/scripts/calibration.py --set .agents/skills/wiki-vetting/tests/calibration-set.json   # measure before trusting
python3 .agents/skills/wiki-vetting/scripts/wiki-lifecycle.py [--json]
```

Input: JSON array of `{"claim", "quote", "target_page", "source", "skip_grounded"}`. Verdicts: `accept` (write it), `reject` (ungrounded or sensitive — drop or sanitize and re-vet), `review` (human decides, or an axis landed inside its uncertainty band). Config in kb-config.yaml `decisions`: `model` (default `clef-flash:9b`), `endpoint` (server base URL; `/v1/systemone` appended) and `thresholds` (per-axis cuts plus `band`). `${VAR}` macros expand from the environment incl. vault-root `.env`/`.env.local` (`.env.example`). CLI flags beat config.

## Thresholds are a dial, not a fix

Each axis has its own threshold because they differ in cost: `sensitive` rejects at 0.30 (over-flagging a secret is cheap, missing one is not), `grounded` demands 0.90. A probability inside `band` of its threshold marks the axis *undecided* and forces `review` — the verdict carries `confidence` and `undecided_axes` so the caller sees how close the call was.

**Measured (2026-10-27, 23 labelled cases, `scripts/calibration.py`):** seven
decision models tried, and `pplx-decider:27b-q4_K_M` remains the only one that is
both safe and usable batched.

| model | grounded P / R (batched) | worst unsupported | accepted | 23 cases |
|---|---|---|---|---|
| **pplx-decider 27B** | 1.00 / **0.615** | 0.305 | 0/10 | 87 s |
| clef (cloud only) | 1.00 / 0.154 | 0.381 | 0/10 | **2.5 s** |
| clef-flash 9B | 0.80 / 0.308 | **0.912** | **2/10** | 42 s |
| d3 base Q4 | 1.00 / 0.46 (single-claim only) | 0.174 | 0/10 | cannot batch |
| kev 9B | 1.00 / 0.31 | 0.599 | 0/10 | 26 s |
| laya 421M | 0.00 / 0.00 | 0.380 | 0/10 | — |

The separating property is not size but **batching**: 16 claims × 4 axes in one
request degrades every model except the 27B. Small models look fast per claim and
lose it under the load the skill actually applies. Do not switch to `clef-flash`:
it accepts 2/10 unsupported claims, one at 0.912 against a 0.90 threshold.

**Measured limit (clef-flash:9b, 23 labelled cases, 2026-10-19):** the `grounded` axis does **not** reliably separate a supporting quote from an adjacent-but-wrong one. Unsupported quotes scored 0.47–0.93; supported ones 0.59–0.96. At 0.90 the false-positive rate is 10% but only 60% of real claims pass. Do not treat `accept` as proof the quote supports the claim; read the probability and the source. Re-run `calibration.py` after any model or prompt change and compare the overlap, not just the aggregate.

## Workflow

1. Extract atomic claims, each with a verbatim quote that proves it.
2. Vet; read the probabilities and `undecided_axes`, not just the verdicts.
3. `accept` → write with `status: verified` and `evidence:` in frontmatter. `review` → `status: developing` or hold.
4. `reject` (sensitive) → never enters the vault; strip the secret, re-vet.

## Hard-won contract details (do not "fix" these)

- **Questions name their claim**: instructions are prefixed `Consider ONLY claim [cN]` — question keys never reach the model, so without the prefix every answer maps to any claim in the batch.
- **`skip_grounded` for events**: a release/event claim is evidenced by its source (feed/URL in `source`), not by a quote; the grounded question is omitted and the verdict treats it as satisfied. Content claims keep the full four axes.
- Batches: ≤16 claims (64-question cap), quotes truncate at 800 chars, body <60 KiB.
- `questions` is a RECORD keyed by id — an array fails with a misleading JSON parse error.

## Lifecycle audit

Reports content pages missing `status`, the dispute queue (`status: disputed` or `> [!contradiction]` callouts with file:line), and `status: verified` pages missing `evidence`. Run after batch ingests; fix the top of the list with real statuses, not bulk-stamping.

## Answer filing is a separate gate

This skill guards the *inbound* path: a source becomes claims, claims become pages. It does not guard an answer an agent files back into the wiki, which never passed through claim extraction. That path is gated at retrieval time instead: configure `trust.output_types` in kb-config.yaml and `wiki-search` keeps pages of those types out of the index until `status:` says they were checked (see `wiki-search/references/index.md` § Output trust). Use this skill's lifecycle audit to see which filed pages still lack evidence; use the trust gate to keep them out of search until they have it.
