---
name: wiki-vetting
description: "Score atomic claims before canonical knowledge, emit explicit routing actions, and audit independent maturity, verification, decision lifecycle, disputes, and evidence. Triggers on: vet these claims, should this go in the wiki, route candidate, audit verification, lifecycle audit, dispute queue."
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

Input: JSON array of `{"claim", "quote", "target_page", "source", "skip_grounded"}`. Every result retains the compatibility verdict (`accept|review|reject`) and adds an operational `action`: `reject_sensitive`, `reject_ungrounded`, `recent_only`, `merge_or_noop`, `promote_candidate`, or `human_review`. Config in kb-config.yaml `decisions`: `model`, `endpoint`, per-axis `thresholds`, and uncertainty `band`. `${VAR}` macros expand from the environment; CLI flags beat config.

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

1. Extract atomic claims, each with a verbatim supporting quote. Event claims may use `skip_grounded` plus `source`.
2. Vet; read probabilities, `undecided_axes`, and `action`, not just the coarse verdict.
3. Route: sensitive/ungrounded → reject; ephemeral → recent ledger only; duplicate → compare for corroboration versus no-op; novel durable → candidate promotion; undecided → human review.
4. The action is not proof. Read source and retrieved canonical evidence before `wiki-ingest` chooses `add_evidence`, `update_existing`, `hold_dispute`, `promote`, or `no_op`.

## Hard-won contract details (do not "fix" these)

- **Questions name their claim**: instructions are prefixed `Consider ONLY claim [cN]` — question keys never reach the model, so without the prefix every answer maps to any claim in the batch.
- **`skip_grounded` for events**: a release/event claim is evidenced by its source (feed/URL in `source`), not by a quote; the grounded question is omitted and the verdict treats it as satisfied. Content claims keep the full four axes.
- Batches: ≤16 claims (64-question cap), quotes truncate at 800 chars, body <60 KiB.
- `questions` is a RECORD keyed by id — an array fails with a misleading JSON parse error.

## Lifecycle audit

Reports pages missing `maturity` or `verification`, decisions missing `decision_status`, the dispute queue (`verification: disputed` or contradiction callouts), and `verification: supported` pages missing `evidence`. Fix with actual review; never bulk-stamp.

## Answer filing is a separate gate

Inbound source claims use this skill. Self-authored answers did not pass claim extraction, so `wiki-search` gates configured output types by `verification` at retrieval time. `maturity` never bypasses evidence review.
