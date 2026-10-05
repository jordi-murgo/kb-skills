# Index commands and configuration

Fully local: BM25 via SQLite FTS5 (own inverted index as fallback), embeddings via an Ollama-compatible `/api/embed` endpoint, fused with RRF. Chunking splits on `#`–`##` headings (≥200 chars, hard cap ~4000) for claim-level recall. Fully derived: read-only for the vault, deleting the index must always be safe.

## Commands

```bash
python3 .agents/skills/wiki-search/scripts/wiki-search.py build                  # incremental: configured source dirs
python3 .agents/skills/wiki-search/scripts/wiki-search.py status                 # counts, staleness, backend
python3 .agents/skills/wiki-search/scripts/wiki-search.py query "TEXT" [--top 8] [--mode hybrid|bm25|vector] [--json]
```

Add `--json` to `build` or `status` for machine-readable reports. `query` fails with "no index … run 'build' first" rather than building automatically; `status` is the actual staleness check and reports pages/chunks/embedded/model/dims/backend/source_dirs/last_build/stale/new.

## Index maintenance

1. For explicitly requested index maintenance, use `status` → stale pages? `build` first (incremental sub-second on a 35-page vault). Ordinary answering does not silently rebuild the index.
2. After batch ingests, one `build` keeps the index current — `wiki-ingest` does not trigger it.
3. `backend` reports the BM25 engine (`fts5` or `own`); it does not say whether vector embeddings are present — check `embedded`/`dims`. A `--mode vector` query is the actual endpoint check.

## Configuration

Config lives in kb-config.yaml `embeddings` section: `model`, `endpoint` (server base URL with `/api/embed` appended), `db` index path, and `source_dirs`. Values support `${VAR}`/`${VAR:-default}` macros expanded from the environment — process env, then vault-root `.env`/`.env.local` (`.env.example`); CLI flags beat config values. `source_dirs` is a non-empty list of vault-relative directories, defaults to `[wiki]`; include `.` only to explicitly index the vault root. Every candidate passes through Git ignore rules, including nested `.gitignore` files; the build fails if a configured source directory is missing, and absolute paths, `~` and `..` traversal are rejected.

Changing the configured `db` path requires an explicit edit to kb-config.yaml; the tool never moves or deletes an existing index. Existing indices are derived and can be rebuilt at any location, so leave any old index in place until you decide to delete it.

Without config: `bge-m3` at `http://127.0.0.1:11434`, index at `.vault-meta/wiki-search/index.db` — disposable derived cache, delete and rebuild anytime (~6 s for a 35-page vault; a full rebuild also happens automatically when the model changes). `WIKI_SEM_NO_FTS5=1` forces the fallback inverted index. `vector` mode needs the embedding endpoint up; `bm25` works offline.

## Query semantics

Default mode `hybrid` fuses BM25 and vector lists with RRF (fusion depth 200) and falls back to whichever list is non-empty (multi-term AND misses happen in bm25). A query `--model` override differing from the index model warns that cosine scores may be meaningless; a model change forces full re-embedding on the next build. Empty query text and `--top < 1` are rejected.