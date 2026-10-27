# Changing retrieval ranking: measure, then prove the gate fails

Read before changing anything that reorders `wiki-search` results: `FUSION_DEPTH`,
`RRF_K`, `rrf_weights`, the BM25 matching mode, chunk prefixes, the embedding
model, or `query_prefix`.

The failure mode this document exists to prevent: a ranking change that looks
right on the obvious query, is justified by a paper, and is never measured on
this corpus.

## 1. Measure on a real gold set before touching the constant

A committed gold file is the only thing that distinguishes a change from a
preference. `evals/wiki-search.json` in a vault (35 queries: exact-id,
paraphrase, cross-language) is the reference shape:

```bash
python3 .agents/skills/wiki-search/scripts/wiki-search.py eval evals/wiki-search.json \
  --mode bm25 --mode vector --mode hybrid --k 5
```

Run all three modes. `hybrid` alone hides the case where fusion is worse than
one channel; on a code corpus equal-weight fusion scored 0.497 MRR against the
vector channel's 0.771.

An A/B on a different embedding model needs a **full rebuild** (~155 s for 322
pages with bge-m3 over Ollama) because the index embeds chunks. `eval --json`
refuses to score while a build is mid-flight — that is intentional, not a bug:
scoring a half-built index compares vectors from two models.

## 2. Compare per query, never aggregates only

Two configurations can share identical hit@5 and MRR and still reorder results.
Diff `per_query[].first_relevant_rank` and apply a paired sign test:

```python
d = [new - old for each query]              # paired, never aggregate-only
wins, losses = count(d > 0), count(d < 0)
p = 2 * sum(comb(n, i) for i in range(max(wins, losses), n + 1)) / 2**n
```

A 3-win/3-loss split over 35 queries is noise. At n=35 the naive standard error
is 0.043 MRR, so the **minimum detectable effect is ≈0.121 RR** — any claim
below that is not measurable on this gold set, no matter how good the story is.
Report the MDE with the number.

## 3. Write the test first and watch it fail

For a cutoff-shaped change, build a fixture larger than the constant and assert
the shipped behaviour equals the unconstrained reference. Then reintroduce the
bad value and confirm RED.

This is not ceremony. The first fusion tests were vacuous: injecting the
weights back to 1:1 passed all 54 tests, because the tests exercised the CLI and
the weights were unreachable. Extracting `fuse_rankings()` made five behavioural
tests possible, and the injection then failed three of them. **A gate that has
never failed has never been tested** (AGENTS.md hard rule 3).

## 4. Re-run the real eval, then the suite

Eval on the vault, then `python3 -m pytest . -q` in this repo. A correct-looking
MRR with a broken top-1 on the obvious query means the index and the query path
disagree — usually a model mismatch, which `status` will report as healthy.

## Settled findings — do not re-litigate

| Change | Result on the vulcano gold set | Verdict |
|---|---|---|
| Fixed fusion cutoff 200 | 1/35 queries reordered vs complete-list fusion; at 500, zero. k=5 metrics and latency identical | **Removed** — `FUSION_DEPTH = None` (3201fce) |
| Title-chain chunk prefixes (`Page > Section`) | MRR 0.8786 → 0.8643, nDCG 0.876 → 0.866, 3 wins / 3 losses | **Rejected** — redundant with H1-matching filenames |
| BM25 recall via OR instead of AND | Cross-language BM25 noise outranks correct vector hits under equal-weight RRF | Needs a fusion change measured together |
| Reranking | A perfect reranker's ceiling is +0.1229 RR at n=35, against an MDE of 0.121 | **Unmeasurable at this n** — ship the measurement first, or grow the gold set to n≈206 |
| `query_prefix` (instruction-aware model) | MRR 0.862 → 0.914, hit@5 0.943 → 0.971, 4 wins / 1 loss / 30 ties, p=0.375 | **Direction only** — below the MDE; set it because it is free, do not quote it as a win |
| Splitting SKILL.md for token budget | Largest skill is 4,152 of 5,000 tokens | **Rejected** — published effect of size on adherence is null |

## Sharp edges in the code

- `LIMIT NULL` is a **datatype error** in SQLite (`IntegrityError`), not "no
  limit". Build the clause conditionally.
- `vector_rank` scans and scores every chunk before slicing, so a channel cap
  saves no work — it can only reorder.
- `dedupe_pages` collapses chunks to pages at their best chunk rank; metrics are
  computed on pages, so chunk-level churn may not surface at all.
- Hybrid falls back to a single list when BM25 returns nothing (25/35 queries on
  natural-language input). Report the fallback count with any metric; a
  "hybrid" number that is 70% vector-only is not a hybrid number.
- `query_prefix` applies to the **query embedding only**. Putting it on
  documents degrades every result quietly, since documents are meant to be
  embedded unchanged.

## Gold-set hygiene

- The eval format rejects `relevant: []`, so abstention and unanswerable queries
  are currently inexpressible. Any experiment on out-of-corpus questions —
  a raw-source channel, say — is unscoreable until that changes.
- Check for leakage: 1 of the 35 gold queries names its own target page
  (`id-bs-accordion-prop` → `bs-accordion`). A query that contains the answer's
  filename measures string matching, not retrieval.
