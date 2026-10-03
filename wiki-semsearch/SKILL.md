---
name: wiki-semsearch
description: "Semantic and hybrid search over the wiki vault: local BM25 + embeddings (RRF fusion) when keyword search misses — paraphrases, cross-language, concept questions. Builds an incremental index; read-only for the vault. Triggers on: search the wiki semantically, similar pages to, what relates to, hybrid search, wiki query deep, find pages about."
argument-hint: "query [TEXT] | build | status"
---

# wiki-semsearch: Hybrid Vault Search

Keyword search finds exact words; this finds meaning. Fully local: embeddings via an Ollama-compatible `/api/embed` endpoint, BM25 via SQLite FTS5 (own inverted index as fallback), fused with RRF.

## Commands

```bash
python3 scripts/wiki-semsearch.py build                  # incremental: new/changed pages only
python3 scripts/wiki-semsearch.py status                 # counts, staleness, backend
python3 scripts/wiki-semsearch.py query "TEXT" [--top 8] [--mode hybrid|bm25|vector] [--json]
```

Model via `--model` or `WIKISEM_MODEL` (default `bge-m3`); endpoint via `WIKISEM_ENDPOINT` (default `http://127.0.0.1:11434`). Index at `.vault-meta/sem/index.db` (`WIKISEM_DB` override) — a derived cache: delete and rebuild anytime (~6 s for a 35-page vault; a full rebuild also happens automatically when the model changes). `vector` mode needs the embedding endpoint up; `bm25` works offline.

## Workflow

1. `status` → stale pages? `build` first.
2. Query in the language of the question; multilingual models match across languages.
3. Read the returned page/heading with the normal tools; cite as `[[Page]]`. Persistence still routes through `save`.
4. After batch ingests, one `build` keeps the index current — `wiki-ingest` does not trigger it.

## Notes

- Default mode `hybrid` falls back to whichever list is non-empty (multi-term AND misses happen in bm25).
- Chunking: sections split on `#`-`##` headings, ≥200 chars, hard cap ~4000 — claim-level recall, not just page-level.
