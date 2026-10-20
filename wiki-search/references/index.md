# Index commands and configuration

Fully local: BM25 via SQLite FTS5 (own inverted index as fallback), embeddings via an Ollama-compatible `/api/embed` endpoint, fused with RRF. Chunking splits on `#`–`##` headings (≥200 chars, hard cap ~4000) for claim-level recall. Fully derived: read-only for the vault, deleting the index must always be safe.

## Commands

```bash
python3 .agents/skills/wiki-search/scripts/wiki-search.py build                  # incremental: configured source dirs
python3 .agents/skills/wiki-search/scripts/wiki-search.py status                 # counts, staleness, backend
python3 .agents/skills/wiki-search/scripts/wiki-search.py query "TEXT" [--top 8] [--mode hybrid|bm25|vector] [--json]
python3 .agents/skills/wiki-search/scripts/wiki-search.py eval GOLD.json [--mode M]... [--k 1,3,5,10] [--json] [--per-query]
```

Add `--json` to `build` or `status` for machine-readable reports. `query` fails with "no index … run 'build' first" rather than building automatically; `status` is the actual staleness check and reports pages/chunks/embedded/model/dims/backend/source_dirs/last_build/stale/new.

## Index maintenance

1. For explicitly requested index maintenance, use `status` → stale pages? `build` first (incremental sub-second on a 35-page vault). Ordinary answering does not silently rebuild the index.
2. After batch ingests, one `build` keeps the index current — `wiki-ingest` does not trigger it.
3. `backend` reports the BM25 engine (`fts5` or `own`); it does not say whether vector embeddings are present — check `embedded`/`dims`. A `--mode vector` query is the actual endpoint check.

## Configuration

Config lives in kb-config.yaml `embeddings` section: `model`, `endpoint` (server base URL with `/api/embed` appended), `db` index path, and `source_dirs`. Values support `${VAR}`/`${VAR:-default}` macros expanded from the environment — process env, then vault-root `.env`/`.env.local` (`.env.example`); CLI flags beat config values. `source_dirs` is a non-empty list of vault-relative directories, defaults to `[wiki]`; include `.` only to explicitly index the vault root. Every candidate passes through Git ignore rules, including nested `.gitignore` files, and wiki navigation/archive files (`wiki/{index,hot,log,goals,dashboard,overview}.md`, `_index.md`, `wiki/meta/`, `wiki/.reciclaje/`) are excluded so summaries and retired pages do not outrank the pages they summarize. The build fails if a configured source directory is missing, and absolute paths, `~` and `..` traversal are rejected.

Changing the configured `db` path requires an explicit edit to kb-config.yaml; the tool never moves or deletes an existing index. Existing indices are derived and can be rebuilt at any location, so leave any old index in place until you decide to delete it.

Without config: `bge-m3` at `http://127.0.0.1:11434`, index at `.cache/wiki-search/index.db` — disposable derived cache, delete and rebuild anytime (~6 s for a 35-page vault; a full rebuild also happens automatically when the model changes). `WIKI_SEM_NO_FTS5=1` forces the fallback inverted index. `vector` mode needs the embedding endpoint up; `bm25` works offline.

## Output trust

An answer or summary an agent files back into the wiki is a claim, not knowledge. With a `trust` section in kb-config.yaml, pages of the listed types stay out of the index until their `status:` says they were checked:

```yaml
trust:
  output_types: [question, summary]   # types whose pages are gated
  indexed_status: [verified]          # statuses that earn indexing (default)
  require_evidence: false             # `verified` also needs `evidence:` (default false)
```

Only the listed types are gated — a `concept` in `draft` is still indexed, because the gate is about self-authored output re-entering the retrieval corpus, not about every page's maturity. Matching is case-insensitive; a page with no `status:` is quarantined and reported as `(no status)`. Without the section nothing is gated, which is the pre-trust behavior every existing vault depends on.

With `require_evidence: true`, a page whose status is in `indexed_status` but has no `evidence:` is held and reported as `verified without evidence`. Both frontmatter shapes count — a scalar and a populated list — while a bare `evidence:` with nothing under it does not. Turn it on only after the vault adopted the field: `wiki-vetting`'s lifecycle audit reports `verified` pages missing evidence, so run that first.

`build` and `status` both report quarantine (`quarantined_count` plus one `{page, status}` per held page). In `status`, a page that left the index because of quarantine is reported as `reason: quarantined`, not `deleted` — the distinction matters: one is a promotion away from returning, the other needs re-ingesting.

## Query semantics

Default mode `hybrid` fuses the complete BM25 and vector lists with RRF (no top-L cutoff: truncated fusion is a different ranking, not a cheaper one — see arXiv:2608.07152) and falls back to whichever list is non-empty. The FTS5 backend matches all query terms (AND), so natural-language questions often have no BM25 hits and `hybrid` then ranks by vector alone — measured on vulcano-kb in 5 of 7 smoke queries. `query --json` reports `bm25_rank`/`vector_rank` per hit, and `eval` counts these fallbacks. A query `--model` override differing from the index model warns that cosine scores may be meaningless; a model change forces full re-embedding on the next build. Empty query text and `--top < 1` are rejected.

## Retrieval evaluation

`eval` scores the existing index against a human-authored gold set, read-only (it never builds). Measure before changing ranking, fusion or chunking; a change ships only if the metrics improve on a real gold set.

```json
{"name": "vault-gold", "queries": [
  {"id": "m2m-en", "category": "cross-language", "text": "machine-to-machine OAuth naming",
   "relevant": ["wiki/arquitectura/m2m-service-credentials.md"]}
]}
```

- `relevant` holds vault-relative page paths exactly as `query` reports `page`; any path not in the index fails the run instead of silently scoring zero (fix the set or `build`).
- Chunks collapse to pages at their best-ranked chunk; metrics are binary-relevance hit/recall/precision/nDCG at each `--k`, MRR, and latency p50/p95. `--mode` repeats to compare modes; categories break MRR/recall down per query type.
- Write questions the way users ask them, not page titles, and mix exact identifiers, paraphrases and cross-language queries. Keep the set outside `.cache/` (it is not derived) and outside `wiki/` (it is not knowledge), e.g. a committed `evals/wiki-search.json` at the vault root. A handful of queries is a smoke test; decide ranking changes on dozens.