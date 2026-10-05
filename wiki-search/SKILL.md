---
name: wiki-search
description: "Trigger: query:, what do you know about, what is, explain, summarize, based on the wiki, search the wiki, similar pages to, what relates to, find pages about, find wiki knowledge, wiki answers. Finds wiki vault pages (BM25 + vector hybrid index, hot-cache lookup) and answers questions with citations, filing approved answers back as wiki pages. For source code, use code-search instead."
argument-hint: "query [TEXT] | build | status; answers: quick | standard | deep"
---

# wiki-search

## Commands

Run from the project cwd with the whole skill copied into `.agents/skills/wiki-search/`:

```bash
python3 .agents/skills/wiki-search/scripts/wiki-search.py status
python3 .agents/skills/wiki-search/scripts/wiki-search.py query "QUESTION TERMS" --top 8 --mode vector
python3 .agents/skills/wiki-search/scripts/wiki-search.py build
```

`query TEXT` ranks candidates; it does not synthesize an answer. Quick/standard/deep are agent answer depths, not CLI switches. If using the source checkout instead, invoke `<kb-skills>/wiki-search/scripts/wiki-search.py` from the project cwd, replacing `<kb-skills>` with the checkout location. Resolve the script inside the loaded skill when installed elsewhere; do not change cwd to the skill directory. Root discovery walks cwd first, then script ancestors.

Read [Index commands and configuration](references/index.md) for switches, config/env precedence, source-directory rules, and index maintenance before using the engine.

## Rules

- Search wiki knowledge, not source-code implementation; route code to `code-search`.
- Read `wiki/hot.md` first and stop if it answers the question. Read actual returned pages/headings before synthesis; snippets and similarity scores are not evidence. Cite facts with wikilinks, e.g. `(Source: [[Page Name]])`.
- Pure answering and `query`/`status` are read-only. Never automatically build for each query. Explicit index maintenance writes derived cache only; tooling never writes `wiki/**` or `.raw/**`.
- File answers only when explicitly requested or approved. Standard answers should offer filing; deep answers should always propose retaining the result, then file when authorized. General conversation saving still routes through `save`.
- State missing coverage and the specific subtopic; offer to find/process a source. Do not fabricate or fill domain-specific wiki gaps from training data. Offer web supplementation when coverage is thin, and distinguish supplementary evidence from wiki evidence.

## Choose depth and retrieval

| Answer depth | Trigger | Reads / candidate limit | Approximate cost |
|---|---|---|---|
| Quick | `query quick: ...`, simple fact/date lookup | hot + index summaries only; no pages or engine | ~1,500 tokens |
| Standard | Default, most questions | hot + candidates + 3–5 pages; `--top 8` | ~3,000 tokens |
| Deep | `query deep: ...`, thorough/comprehensive, cross-wiki comparison | hot + index + every relevant page; widen with `--top 16`, optional web | ~8,000+ tokens |

| Retrieval need | Action |
|---|---|
| Paraphrases, related concepts, cross-language meaning | `--mode vector` |
| Meaning and exact terms together | `--mode hybrid` |
| Identifiers, filenames, symbols, exact terms, or endpoint unavailable | `--mode bm25` |
| No usable index or no useful indexed candidates | Scan `wiki/index.md` / relevant domain `_index.md` titles and descriptions |

## Answer workflow

1. Read hot. In quick mode, read index summaries only if needed, then answer without opening pages. If absent, say: "Not in quick cache. Run as standard query?"
2. For standard/deep, check `status` and choose the retrieval mode above; do not silently rebuild a missing/stale index. Use lexical retrieval during endpoint outages before manual index fallback. For narrow questions prefer domain sub-indexes; deep mode reads the master index to cover concepts, entities, sources and comparisons.
3. Read candidate pages with normal tools. Standard follows key-entity wikilinks at most depth-2. Deep reads every relevant page; `--top 16` widens discovery, not a cap on necessary reading.
4. Synthesize with citations. Keep standard reads to 3–5 pages when sufficient; read 10+ only for broad synthesis. Hot costs ~500 tokens, index ~1,000, typical pages ~300 each; stop as soon as evidence suffices.
5. Offer filing and identify gaps. If filing is authorized, read [Answer filing and navigation](references/answers.md), write the cited question page, and maintain index/sub-indexes, one batch log entry, hot and the goal-driven dashboard. Do not rewrite approved goals to fit the answer.

## Output

For retrieval-only requests, return ranked page/heading candidates. For answers, return supported synthesis with source links and explicit gaps; distinguish inference. For filing, report the saved page and completed state refresh. For build/status, report the engine's actual index state, not inferred embedding availability.

## References

- [Index commands and configuration](references/index.md)
- [Answer filing and navigation](references/answers.md)
- [Self-contained engine](scripts/wiki-search.py)
