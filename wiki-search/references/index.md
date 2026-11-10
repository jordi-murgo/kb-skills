# Index commands and configuration

Fully local: BM25 via SQLite FTS5 (own inverted index as fallback), embeddings via an Ollama-compatible `/api/embed` endpoint, fused with RRF. Chunking splits on `#`–`##` headings (≥200 chars, hard cap ~4000) for claim-level recall. Fully derived: read-only for the vault, deleting the index must always be safe.

## Commands

```bash
python3 .agents/skills/wiki-search/scripts/wiki-search.py build                  # incremental: configured source dirs
python3 .agents/skills/wiki-search/scripts/wiki-search.py status                 # counts, staleness, backend
python3 .agents/skills/wiki-search/scripts/wiki-search.py query "TEXT" [--scope canonical|recent|all] [--top 8] [--mode hybrid|bm25|vector] [--json]
python3 .agents/skills/wiki-search/scripts/wiki-search.py eval GOLD.json [--mode M]... [--k 1,3,5,10] [--json] [--per-query]
python3 .agents/skills/wiki-search/scripts/wiki-search.py graph [PAGE] [--top 10] [--json]
python3 .agents/skills/wiki-search/scripts/wiki-search.py graph --html [PATH] [--max-nodes 1500]
```

`graph` reports the **body-wikilink graph**: `[[...]]` links in page text, collected
during `build` into a `links(src, dst)` table. With no argument it lists total
edges, page count, dangling targets and the highest-degree pages; with a page stem
it lists that page's neighbours in both directions.

`graph --html` writes one self-contained file (no CDN, no build step, opens offline)
and prints the path. It defaults to `.cache/wiki-graph.html`; a relative `PATH` is
resolved against the vault root and an absolute one is taken as given. Node ids are
page stems, so `wiki/x/bi2614-1.md` and `.raw/jira/BI2614-1.md` are one node and the
header reports `N indexed files → M nodes` when they differ. Isolated pages and
dangling link targets are nodes too — the first version built its node list from
the edge list and silently rendered 229 of 421 files. `--max-nodes` truncates by
degree only when the graph is too large for a browser, and says so on the page.

The view is `scripts/graph-view.html`, read at generation time and inlined into the
output. It is deliberately not a Python string literal: 900 lines of CSS and JS in
a literal is unreadable and one stray quote breaks the module.

In the page: drag the canvas to pan, drag a node to pin it (double-click it or the
canvas to unpin and re-fit), wheel to zoom, click a node to open the inspector with
its outgoing/incoming links and a 1- or 2-hop focus, `/` to search with keyboard
navigation, the type legend to show or hide page types, and the toolbar to fit,
re-layout, pause the simulation or export a PNG. A selected node is addressable as
`#<node-id>`. Every node links to its Markdown file relative to the output path.

Two edge sets exist and are deliberately separate. `relations:` in frontmatter
carries **typed** edges (`uses`, `extends`, `contradicts`…) validated by
`wiki-lint/scripts/lint-relations.py`; body wikilinks are the **untyped** links a
reader clicks. Merging them would count one relationship twice and inflate every
centrality number. Measured on vulcano-kb after the change: **1,188 edges over 330
pages, 16 dangling targets** — before it existed the same vault reported `{}`,
because only frontmatter was read and no page used `relations:`.

`query --scope canonical` fails with "no index … run 'build' first" rather than building automatically. `--scope recent` needs no index; `--scope all` reports canonical unavailability and still returns provisional observations. `status` is the canonical staleness check.

## Index maintenance

1. For explicitly requested index maintenance, use `status` → stale pages? `build` first (incremental sub-second on a 35-page vault). Ordinary answering does not silently rebuild the index.
2. After batch ingests, one `build` keeps the index current — `wiki-ingest` does not trigger it.
3. `backend` reports the BM25 engine (`fts5` or `own`); it does not say whether vector embeddings are present — check `embedded`/`dims`. A `--mode vector` query is the actual endpoint check.

## Configuration

Config lives in kb-config.yaml. This skill reads its own `wiki_search` section first, then falls back to the shared `embeddings` section: `model`, `endpoint` (server base URL with `/api/embed` appended), `db` index path, and `source_dirs`. Precedence is per key, so a section can set only `model` and inherit the rest; blank strings count as unset. `code_search` is the symmetric section for the other search and is never read here. Values support `${VAR}`/`${VAR:-default}` macros expanded from the environment — process env, then vault-root `.env`/`.env.local` (`.env.example`); CLI flags beat config values. `source_dirs` is a non-empty list of vault-relative directories, defaults to `[wiki]`; include `.` only to explicitly index the vault root. Every candidate passes through Git ignore rules, including nested `.gitignore` files, and wiki navigation/archive files (`wiki/{index,hot,log,goals,dashboard,overview}.md`, `_index.md`, `wiki/meta/`, `wiki/.reciclaje/`) are excluded so summaries and retired pages do not outrank the pages they summarize. The build fails if a configured source directory is missing, and absolute paths, `~` and `..` traversal are rejected.

Changing the configured `db` path requires an explicit edit to kb-config.yaml; the tool never moves or deletes an existing index. Existing indices are derived and can be rebuilt at any location, so leave any old index in place until you decide to delete it.

Without config: `bge-m3` at `http://127.0.0.1:11434`, index at `.cache/wiki-search/index.db` — disposable derived cache, delete and rebuild anytime (~6 s for a 35-page vault; a full rebuild also happens automatically when the model changes). `WIKI_SEM_NO_FTS5=1` forces the fallback inverted index. `vector` mode needs the embedding endpoint up; `bm25` works offline.


## Query scopes

| Scope | Source | Ranking | Contract |
|---|---|---|---|
| `canonical` (default) | indexed configured Markdown | BM25/vector/hybrid | Consolidated knowledge only |
| `recent` | `.cache/knowledge-flow/observations.jsonl` plus candidate state | in-memory BM25 | Provisional, immediate, no build |
| `all` | both | separate rankings | Two blocks; never fuse epistemic layers |

Recent search keeps observations whose latest epistemic state is `observed`, validity has not expired, and latest candidate is not terminally rejected/expired or already applied to canonical knowledge. Applied `hold_dispute` candidates remain visible because the conflict is unresolved. Missing candidate state appears as `unclassified`. JSONL corruption fails loudly with the exact file and line.

The default remains `canonical` for CLI compatibility. Agents answering freshness-sensitive questions and `wiki-ingest` comparing new claims use `--scope all`. A recent hit is a pointer to source evidence, not proof: read `source_path` + `source_lines` before answering.

## Output trust

Self-authored output is not supported knowledge until checked. Gate listed page types by `verification`, independently from `maturity`:

```yaml
trust:
  output_types: [question, summary]
  indexed_verification: [supported]
  require_evidence: false
```

Only listed types are gated. Matching is case-insensitive; missing `verification:` is reported as `(no verification)`. Without `trust`, nothing is gated. With `require_evidence: true`, a qualifying page without a populated `evidence:` scalar/list is held as `supported without evidence`.

`build` and `status` report quarantine as `{page, verification}`. A page removed from the index by this gate is `quarantined`, not `deleted`.

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