---
name: wiki-semsearch
description: "Semantic and hybrid search over the wiki vault: local BM25 + embeddings (RRF fusion) when keyword search misses — paraphrases, cross-language, concept questions. Builds an incremental index; read-only for the vault. Triggers on: search the wiki semantically, similar pages to, what relates to, hybrid search, wiki query deep, find pages about."
argument-hint: "query [TEXT] | build | status"
---

# wiki-semsearch: Hybrid Vault Search

Keyword search finds exact words; this finds meaning. Fully local: embeddings via an Ollama-compatible `/api/embed` endpoint, BM25 via SQLite FTS5 (own inverted index as fallback), fused with RRF.

## Commands

```bash
python3 scripts/wiki-semsearch.py build                  # incremental: configured source dirs
python3 scripts/wiki-semsearch.py status                 # counts, staleness, backend
python3 scripts/wiki-semsearch.py query "TEXT" [--top 8] [--mode hybrid|bm25|vector] [--json]
```

Config in kb-config.yaml `embeddings` section (model, server base URL with `/api/embed` appended, index path, `source_dirs`); values support `${VAR}`/`${VAR:-default}` macros from the environment incl. vault-root `.env`/`.env.local` (`.env.example`). `source_dirs` is a non-empty list of vault-relative directories and defaults to `[wiki]`; include `.` only to explicitly index the vault root. Every candidate is passed through Git ignore rules, including nested `.gitignore` files. The build fails if a configured source directory is missing. Without config: `bge-m3` at `127.0.0.1:11434`, index at `.vault-meta/sem/index.db` — a derived cache: delete and rebuild anytime (~6 s for a 35-page vault; a full rebuild also happens automatically when the model changes). `vector` mode needs the embedding endpoint up; `bm25` works offline.

## Workflow

1. `status` → stale pages? `build` first.
2. For genuinely semantic queries — paraphrases, related concepts, or cross-language questions — prefer `--mode vector` (e.g. `python3 scripts/wiki-semsearch.py query "..." --mode vector`). Use `--mode hybrid` when both semantic similarity and exact terms matter; reserve `--mode bm25` for identifiers, filenames, symbols, or when the embedding endpoint is unavailable.
3. Read the returned page/heading with the normal tools; cite as `[[Page]]`. Persistence still routes through `save`.
4. After batch ingests, one `build` keeps the index current — `wiki-ingest` does not trigger it.

## Notes

- Default mode `hybrid` falls back to whichever list is non-empty (multi-term AND misses happen in bm25).
- In `status`, `backend` reports the BM25 engine (`fts5` or `own`); it does **not** say whether vector embeddings are present. Check `embedded` / `dims` for vector-index state. A `--mode vector` query is the actual endpoint check.
- Chunking: sections split on `#`-`##` headings, ≥200 chars, hard cap ~4000 — claim-level recall, not just page-level.
