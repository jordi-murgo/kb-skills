---
name: wiki-search
description: "Trigger: query:, what do you know about, what is, explain, summarize, based on the wiki, search the wiki, recent information, provisional observations, similar pages, find wiki knowledge. Searches canonical wiki pages (BM25 + vector) and immediate observation ledgers, keeps the two scopes explicit, and answers with citations. For source code, use code-search."
argument-hint: "query [TEXT] [--scope canonical|recent|all] | build | status; answers: quick | standard | deep"
---

# wiki-search

## Commands

Run from the project cwd with the whole skill copied into `.agents/skills/wiki-search/`:

```bash
python3 .agents/skills/wiki-search/scripts/wiki-search.py status
python3 .agents/skills/wiki-search/scripts/wiki-search.py query "QUESTION TERMS" --scope all --top 8 --mode hybrid
python3 .agents/skills/wiki-search/scripts/wiki-search.py query "QUESTION TERMS" --scope recent --top 8
python3 .agents/skills/wiki-search/scripts/wiki-search.py build
python3 .agents/skills/wiki-search/scripts/wiki-search.py eval evals/wiki-search.json --mode bm25 --mode vector --mode hybrid
python3 .agents/skills/wiki-search/scripts/wiki-search.py graph [PAGE]
python3 .agents/skills/wiki-search/scripts/wiki-search.py graph --html
```

`query TEXT` ranks candidates; it does not synthesize. `--scope canonical` is backward-compatible indexed search, `recent` searches `.cache/knowledge-flow/observations.jsonl` immediately, and `all` returns two separate result blocks. Recent hits are provisional and never silently fused with canonical rankings. Quick/standard/deep are answer depths, not CLI switches.

Read [Index commands and configuration](references/index.md) before using the engine.

## Rules

- Search wiki knowledge, not source-code implementation; route code to `code-search`.
- Read `wiki/hot.md` first. Read actual canonical pages and the cited raw lines behind recent hits before synthesis; snippets and scores are discovery, not evidence.
- Pure answering and `query`/`status` are read-only. Never automatically build per query. Recent scope works without an index; explicit index maintenance writes derived cache only.
- Label canonical knowledge and provisional recent information separately. Never state an observation as consolidated knowledge.
- File answers only when explicitly requested or approved. General conversation saving routes through `save`.
- State missing coverage and the specific subtopic. Do not fill vault gaps from training data; distinguish web supplementation from vault evidence.

## Choose depth and retrieval

| Answer depth | Trigger | Reads / candidate limit | Approximate cost |
|---|---|---|---|
| Quick | `query quick: ...`, simple fact/date lookup | hot + index summaries only; no pages or engine | ~1,500 tokens |
| Standard | Default, most questions | hot + candidates + 3–5 pages; `--top 8` | ~3,000 tokens |
| Deep | `query deep: ...`, thorough/comprehensive, cross-wiki comparison | hot + index + every relevant page; widen with `--top 16`, optional web | ~8,000+ tokens |

| Retrieval need | Action |
|---|---|
| Current/recent state or ingest comparison | `--scope all`; inspect both labelled blocks |
| Recent observations before index/promotion | `--scope recent` (offline BM25, no build) |
| Canonical paraphrases or cross-language meaning | `--scope canonical --mode vector` |
| Canonical meaning plus exact terms | `--scope canonical --mode hybrid` |
| Canonical identifiers or endpoint unavailable | `--scope canonical --mode bm25` |
| No canonical index/candidates | Use recent results, then scan `wiki/index.md` / domain indexes |

## Answer workflow

1. Read hot. In quick mode, use it only when sufficient; otherwise offer standard retrieval.
2. For standard/deep, query `--scope all`. Do not silently rebuild a missing/stale canonical index: `all` still returns recent observations and reports canonical unavailability.
3. Read canonical candidate pages and raw evidence behind recent candidates. Standard follows useful links to depth 2; deep widens with `--top 16` and reads every relevant result.
4. Synthesize two labelled sections when recent information matters: **Consolidated knowledge** and **Recent provisional information**. Explain conflicts, validity dates, and gaps; cite canonical pages and raw provenance.
5. Offer filing only for durable synthesis. If authorized, read [Answer filing and navigation](references/answers.md), write the cited question page, and maintain navigation/state once.

## Output

Retrieval-only: ranked canonical page/headings and/or provisional observations with source path, lines, action, and candidate state. Answers: supported synthesis with explicit scope and gaps. Build/status: observed index state only.

## References

- [Index commands and configuration](references/index.md)
- [Answer filing and navigation](references/answers.md)
- [Changing retrieval ranking: measure, then prove the gate fails](references/ranking-changes.md) — read before touching fusion, chunking, BM25 matching or the embedding model.
- [Self-contained engine](scripts/wiki-search.py)
