<!-- kb-skills:wiki-maintenance:start -->
## Wiki content maintenance

A wiki-content batch is not complete until its navigation and project-state files reflect the actual change. Apply this to ingests, saved answers/conversations, research, decisions and manual page edits, regardless of which skill made them.

Before changing content, read `wiki/goals.md`, `wiki/dashboard.md` and `wiki/hot.md` when present. Read the relevant pages and authoritative source evidence; do not rely on the cache alone.

After a changed-content batch, before reporting success:
1. Update `wiki/index.md` and affected `_index.md` entries for final, reviewed pages. Do not register unreviewed drafts as final pages.
2. Prepend one operation entry to `wiki/log.md` after its frontmatter/title, linking changed pages, source evidence and unresolved contradictions. Preserve every historical entry. Reuse the entry already produced by the active skill; do not log the same batch twice.
3. Refresh `wiki/hot.md` (at most 500 words): key recent facts, changed pages, active threads and blockers. Keep relevant ongoing context; do not just append the new source.
4. Refresh `wiki/dashboard.md` against `wiki/goals.md`: one row per approved goal with goal ID/link, status, dated evidence, gap/blocker and next concrete step; add pending decisions and relevant recent changes. Keep human-maintained notes and custom sections.

`wiki/goals.md` is the authoritative objective register, not an automatically inferred task list. Give each goal a stable ID, intended outcome, observable success criteria, priority, owner/time constraints if known, scope/non-goals and an approval or authoritative project-source reference. Reuse existing IDs. Do not invent objectives, owners, deadlines or progress percentages. Change goals only when the human or authoritative project evidence changes the objective/criteria; progress alone changes the dashboard, not the goal definition.

Dashboard goal status: `unknown` (no usable evidence), `not-started` (explicitly confirmed), `in-progress` (dated partial evidence), `blocked` (named dependency/decision), `achieved` (every success criterion has current evidence), `retired` (explicitly withdrawn goal). Missing evidence is not `not-started`; an ingested document or unreviewed draft is not proof of delivery. Surface contrary evidence and revise current status without rewriting history. Mark missing owners/dates as unknown.

If goals are absent, create `wiki/goals.md` with normal wiki frontmatter and an explicit "No approved goals recorded" state; do not populate it with guessed goals. Show that gap in the dashboard and request the missing goal decisions while still maintaining hot/log/index. Link goals and dashboard from the index; bootstrap only missing files, never overwrite existing content.

Use normal wiki frontmatter and plain Markdown/wikilinks for goals and dashboard; no Dataview dependency. Date dashboard evidence separately from the refresh timestamp. `wiki/dashboard.md` is project progress, not the lint-health view at `wiki/meta/dashboard.md`. Preserve existing custom layouts while keeping the above fields visible.

Resolve wikilinks against actual wiki filenames or wiki-relative paths, not frontmatter titles. Use `[[goals]]` and `[[dashboard]]` for the state files; a display title is an alias, not a target. Check newly written links before reporting success.

No-op/unchanged reingests, raw-only connector fetches and derived index/cache refreshes do not create new wiki-content log entries. Refreshing hot/log/dashboard is part of the original batch, never a recursive content event. Explicit maintenance operations may keep their own existing audit format; do not invent a content-addition event for them.
<!-- kb-skills:wiki-maintenance:end -->