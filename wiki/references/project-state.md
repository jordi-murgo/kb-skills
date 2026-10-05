# Goals and project dashboard

Keep four distinct views: `goals.md` defines approved outcomes; `dashboard.md` evaluates those outcomes against evidence; `hot.md` is recent working context (at most 500 words); `log.md` preserves the sequence of operations. All four live under `wiki/`.

## Define goals before assessing progress

Read existing project decisions, requirements and human instructions. Reuse their goals and identifiers rather than deriving goals from document titles or recent activity. Confirm ambiguous objectives with the human. `wiki/goals/` may still contain detailed goal pages; `wiki/goals.md` is their root register, not a replacement for that folder.

Record each approved goal with:

| Field | Required meaning |
|---|---|
| ID and title | Stable `G-001`-style identifier (or the project's existing scheme); never renumber or reuse it. |
| Intended outcome | The project result, not a list of documents or agent activities. |
| Success criteria | Observable conditions and the evidence needed to accept each one. |
| Authority | Human approval or an authoritative project-source link, with decision/source date. |
| Priority | Explicit project priority; otherwise `unknown`. |
| Owner and time constraint | Approved owner/deadline/review horizon; otherwise `unknown`. |
| Scope and non-goals | What is included, excluded and dependent on an external decision. |

Keep proposed goals in a clearly separate, unapproved section. Do not include them as approved dashboard commitments. Goal definitions change only when approval/authoritative source evidence changes the outcome or criteria; progress changes do not rewrite goals. Preserve retired IDs and their retirement decision.

### Goals template

Use the existing wiki frontmatter convention. This template deliberately contains no fictitious goal row:

~~~markdown
---
type: meta
title: "Project Goals"
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [project, goals]
status: seed
related: []
---

# Project Goals

## Purpose and boundaries
State the sourced project purpose and scope; label missing decisions explicitly.

## Approved goals
No approved goals recorded. Pending human/project authority.

## Proposed goals and pending decisions
List only proposals actually made, clearly marked as unapproved.
~~~

For a real approved goal, add a `## G-001 — <approved outcome>` section with the fields in the table, and link its source using the project's established wiki-link convention. Record the approval/changed scope in the original batch's log entry. Update `related` to link real source/dashboard pages only after they exist.

## Build the dashboard

Read `wiki/goals.md`, changed pages and the evidence linked by each affected goal. Use `wiki/index.md` and the relevant pages to establish current state; hot/log provide context, not authoritative proof. Include every approved goal so missing evidence is visible. Do not promote proposed goals, drafts or mere activity to accepted results.

Use a compact plain-Markdown view that works in GitHub/GitLab without plugins:

~~~markdown
---
type: meta
title: "Project Dashboard"
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [project, dashboard]
status: seed
related: []
---

# Project Dashboard

Refreshed: YYYY-MM-DDTHH:MM:SS (include the time zone).
Goals: [[goals]].

## Goal status
| Goal ID / link | Status | Dated evidence / criteria covered | Gap or blocker | Next concrete step |
|---|---|---|---|---|

No approved goals recorded; goal definition is pending. No completion or percentage can be inferred.

## Decisions and blockers
Record evidenced decisions/dependencies requiring action and who must resolve them if known.

## Recent relevant changes
Link changes from the current content batch that affect goals or project understanding.

## Human notes
Preserve existing manually curated notes and custom sections.
~~~

Replace the empty state only when approved goals exist; then give each one a row. Link its goal definition and the pages/sections that prove the status, with the evidence date separate from the refresh time. Keep tables readable: concise row text, detailed evidence/criteria in the linked pages. Record unknown owner/date/priority as `unknown`, never fabricate values. `updated` is the actual edit date; page maturity in frontmatter is separate from goal status.

Resolve links by the actual file stem or wiki-relative path, not `title` frontmatter: use `[[goals]]`, `[[dashboard]]` and, outside tables, `[[sources/restart-probe|Restart probe report]]` only when that file exists. Use plain filename-target wikilinks without display aliases in table cells so pipe characters do not break the table. Check new link targets; preserve historical log entries rather than rewriting their old links.

### Status rules

| Status | Evidence threshold |
|---|---|
| `unknown` | Evidence is missing, ambiguous, conflicting or cannot establish current state. |
| `not-started` | An authoritative statement explicitly confirms work has not started. Absence of evidence is insufficient. |
| `in-progress` | Dated evidence supports partial progress; name criteria not yet met. |
| `blocked` | An evidenced dependency, access problem or pending decision prevents progress; identify it. |
| `achieved` | Current evidence proves every stated success criterion. A page, plan, ingested source or draft alone is insufficient. |
| `retired` | An explicit human/authoritative decision withdraws the goal; retain its ID and decision link. |

If new evidence contradicts a prior success, reopen the current status (for example `blocked` or `unknown`) and explain why. Keep prior log entries untouched. Do not display calculated completion percentages unless the project has an approved measurement rule and evidence for its numerator/denominator.

`wiki/dashboard.md` is the operational project view. Optional `wiki/meta/dashboard.md` from wiki-lint is a vault-health view; never replace one with the other.

## Refresh after a content batch

1. Finish the content edits and contradictions first. Identify what actually changed. An unchanged ingest, raw-only fetch or cache rebuild is not a new content event.
2. Update the catalog and relevant domain indexes for final, reviewed pages. Drafts remain labelled drafts and do not count as accepted goal evidence.
3. Prepend one batch log entry below log frontmatter/title; retain history. If the producing skill already recorded the batch, enrich that entry rather than adding another.
4. Refresh hot with recent facts, links, active threads and blockers; retain relevant ongoing context and keep the entire cache within 500 words.
5. Refresh goal-status rows from goal definitions and current evidence. Inspect affected rows in detail; do not carry forward stale claims. Preserve human notes, custom layout and unrelated sections. Do not rewrite approved goal definitions merely to make status easier to calculate.
6. Verify page links, goal IDs, evidence dates, unknowns, complete criterion coverage for `achieved`, preserved historical entries and manual notes. Link newly created goals/dashboard from `wiki/index.md`. Report completion only after the three state views match the content.

Bootstrap only missing files. When purpose/goals cannot be established, use the explicit empty state, surface the missing decision and still maintain hot/log/index. Refresh the batch once, not once per source, and never log each refresh as another content event. Existing audit logs for explicit folds/lints/issues keep their own operation semantics.