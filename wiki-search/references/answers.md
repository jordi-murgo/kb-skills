# Answer filing and navigation

Good answers compound into the wiki; do not let insights disappear into chat history. File only when the user explicitly wants the answer kept, and pure read-only answering never requires a log event.

## Frontmatter and body

Write `wiki/questions/answer-name.md` with citations and answer body:

```yaml
---
type: question
title: "Short descriptive title"
question: "The exact query as asked."
answer_quality: solid
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [question, <domain>]
related:
  - "[[page-slug]]"
sources:
  - "[[sources/relevant-source]]"
status: developing
---
```

Then write the answer as the page body. Include citations and link every mentioned concept or entity. Replace the example links with existing wiki file stems or wiki-relative paths, not frontmatter titles or `.md` extensions. Use unaliased links inside Markdown tables so `|` does not split cells.

## Post-filing navigation maintenance

1. Add an entry to `wiki/index.md` under Questions and the relevant domain `_index.md`.
2. Add one newest-first batch entry to `wiki/log.md` below its frontmatter/title. Reuse any entry already written for the same batch; preserve all older entries.
3. Refresh `wiki/hot.md` within 500 words, retaining still-relevant context, active threads and blockers.
4. Refresh `wiki/dashboard.md` from `wiki/goals.md` and reviewed evidence. Give each approved goal a row with its stable ID/link, status, dated evidence and criteria covered, gap/blocker, and next concrete step. Preserve manual notes and custom sections.

Read and follow the project's `AGENTS.md` maintenance policy when present. Goal outcomes, success criteria and scope require human or authoritative project approval; progress evidence does not rewrite them. Never invent goals, owners, deadlines or percentages. Proposed material and unreviewed drafts do not prove accepted results.

Use `unknown` when usable evidence is absent; `not-started` only with an authoritative statement; `in-progress` for dated partial proof; `blocked` for a named dependency or decision; `achieved` only when current evidence proves every criterion; `retired` only after explicit withdrawal. New contrary evidence updates current status, not historical log entries.

If goals are missing, bootstrap only missing state files with normal `type: meta` wiki frontmatter and an explicit "No approved goals recorded" state. Show the gap in the dashboard, link `[[goals]]` and `[[dashboard]]` from the index, and request missing goal decisions rather than guessing objectives.

Unchanged reingests and read-only answers do not create a content log event. Refreshing index/hot/log/dashboard belongs to the original filing batch and must not recursively log itself. This reference is self-contained; no second skill is required for answer filing.