---
name: autoresearch
description: >
  Autonomous research loop that searches, evaluates sources, records cited
  observations through wiki-ingest, and produces one synthesis candidate by
  default. Recent findings become searchable immediately without flooding the
  canonical wiki. Use research-brief to plan and wiki-consolidate to promote.
  Triggers on: "/autoresearch", "autoresearch", "research [topic]", "deep dive",
  "investigate [topic]", "research and file", "go research".
allowed-tools: Read Write Edit Glob Grep WebFetch WebSearch Bash
---

# autoresearch: Autonomous Research Loop

You are a research agent. You take a topic, run iterative web searches, synthesize findings, and file everything into the wiki. The user gets wiki pages, not a chat response.

This is based on Karpathy's autoresearch pattern: a configurable program defines your objectives. You run the loop until depth is reached. Output goes into the knowledge base.

---

## Before Starting

Read `references/program.md` to load the research objectives and constraints. This file is user-configurable. It defines what sources to prefer, how to score confidence, and any domain-specific constraints.

---

## Topic Selection

Four paths to a topic, evaluated in order:

### A. Explicit topic or task ID (always respected)

When the user passes an argument to the skill (`/autoresearch <argument>`, `autoresearch <argument>`, or `research <argument>`):

- **Argument matches `R-YYYY-NNN`** (a research-queue task ID): route through section B's task handling for that specific task. Skip the no-args concurrency / ready-set / boundary computation, but apply the **brief-presence**, **brief-version**, and **dependency** gates from section B before starting the loop. On gate failure, refuse with the canonical message — do not fall through to free-text mode.
- **Otherwise** (free-text topic): treat the argument as an ad-hoc topic. Set `QUEUE_MODE=0`, skip sections B-D, use the text verbatim as the loop input. Output goes to the ad-hoc draft path (see *Filing Results — Output path by mode*).

### B. Research-queue driven (default when no topic given, opt-in via queue file)

When `/autoresearch` is invoked WITHOUT a topic AND the vault has a research queue, default to picking the highest-priority ready task from `wiki/meta/research-queue.md`. This is the **deliberate-agenda** mode: tasks are pre-prioritized, briefed, and dependency-ordered by the user.

Feature detection (shell):

```bash
if [ -f wiki/meta/research-queue.md ] && [ -f scripts/lint-deps.py ]; then
  QUEUE_MODE=1
else
  QUEUE_MODE=0
fi
```

**`scripts/lint-deps.py` does not ship with this skill.** When it is absent,
queue mode is unavailable: say so in one line ("queue mode needs
`scripts/lint-deps.py`, which is not installed") and continue with section C/D.
Never hand-verify the DAG in its place and never claim the queue was validated.

When `QUEUE_MODE=1`:

1. **Concurrency check**: refuse to start if any row already has `status: in-progress`. Surface:
   ```
   Refusing to start: R-YYYY-NNN is still in-progress (started YYYY-MM-DD).
   Mark it done/queued first, or pass an explicit topic argument.
   ```
2. **Validate the DAG**: run `python3 scripts/lint-deps.py`. If exit code != 0 (cycles, missing deps, or duplicate IDs), refuse to start and surface the lint output. The queue must be clean.
3. **Compute ready set**: run `python3 scripts/lint-deps.py --ready`. This returns IDs of tasks where `status: queued` AND all `depends_on:` are `done`, in priority order (P0 first, then FIFO by `created`).
4. **Helper failure handling**: if the helper exits non-zero, emits no output, or returns an empty list, set `QUEUE_MODE=0` and fall through to section C below. Do NOT improvise a topic.
5. **Brief presence enforcement**: read the queue table to resolve the top ID. If its `Brief` cell is `_to brief_` or empty:
   - **No-args invocation** (queue-driven selection): skip to the next ready ID. If no ready task has a brief, fall through to C.
   - **Explicit task-ID invocation** (section A routing here): refuse and surface:
     ```
     Refusing to start R-YYYY-NNN: brief is missing or _to brief_.
     Run the research-brief skill in construct mode to produce a brief first.
     ```
6. **Brief-version enforcement** (W11 gate, see `references/program.md` and the research-brief skill): read the brief file referenced by the queue row. Parse its frontmatter. If `brief_version` is absent or `!= 1`, surface:
   ```
   Refusing to start R-YYYY-NNN: brief does not carry brief_version: 1.
   Run the research-brief skill in audit mode to validate and bring it to current schema.
   ```
   - **No-args invocation**: skip to the next ready ID after surfacing the message (do not silently skip — the user must see which task was unrunnable).
   - **Explicit task-ID invocation**: refuse and stop. Do not fall through to free-text mode.
7. **Present to user**:
   ```
   Selected R-YYYY-NNN [P0/EVAL]: <title>
   Brief: <brief link>
   Program home: [[research/programs/<CODE>]]
   Depends on: <list or —>
   Proceed? (y / type a topic to override / 'next' for the next ready task / 'cancel')
   ```
8. **On confirm**:
   - Edit the queue row: flip `status: queued` → `status: in-progress`, set `started: <today>`, bump `updated:`.
   - Use the brief content as the topic input for the research loop. The brief already encodes objectives, sources, stopping condition, and deliverables — respect them verbatim.
9. **On 'next'**: move to the next ready ID, repeat from step 5.
10. **On topic override**: set `QUEUE_MODE=0` for this run, do not touch the queue, use the typed text as topic.
11. **On 'cancel'**: ask via section D (user-chosen).

**During the loop**: never edit `wiki/meta/research-queue.md`'s row schema or other rows. Only the active row's `status`, `started`, `finished`, `updated`, and `deliverables` may be touched. **Never edit eval files** (the agenda is the review loop: an agent must not rewrite its own criteria).

**On loop completion (success)**:
- Flip the row's `status: in-progress` → `status: done`.
- Set `finished: <today>`, bump `updated:`, and link the single synthesis draft in `deliverables:`.
- Record observation/candidate IDs in the synthesis; do not add provisional work to the canonical wiki log.

**On loop failure or abort**:
- Flip the row back to `status: queued`. Leave `started:` populated so retry latency is visible.
- Record the failure reason in the queue task's existing notes field when available; do not create a canonical content event.

### C. Boundary-first selection (agenda control, opt-in)
**This is agenda control, not pure memory.** This mechanism is labeled agenda control (rather than memory) because it shapes which direction the research agent moves next. Users who want a strict memory-layer subset should omit this path entirely.

When `/autoresearch` is invoked WITHOUT a topic AND the vault has adopted DragonScale, default to surfacing the frontier of the vault as a set of candidate topics the user can accept, override, or decline.

Feature detection (shell):

```bash
if [ -x ./scripts/boundary-score.py ] && [ -d ./.cache ] && command -v python3 >/dev/null 2>&1; then
  BOUNDARY_MODE=1
else
  BOUNDARY_MODE=0
fi
```

**`scripts/boundary-score.py` does not ship with this skill.** When it is
absent, boundary mode is unavailable: say so in one line and fall through to
section D. Do not score the frontier by hand and present it as the helper's
output.

When `BOUNDARY_MODE=1` AND `QUEUE_MODE=0`:

1. Run `./scripts/boundary-score.py --json --top 5`. Returns the top 5 frontier pages by `boundary_score = (out_degree - in_degree) * recency_weight`.
2. **Helper failure handling**: if the helper exits non-zero, emits invalid JSON, or returns an empty `results` array, set `BOUNDARY_MODE=0` and fall through to section D below. Do NOT prompt the user with an empty candidate list, and do NOT improvise a topic.
3. Present the candidate list to the user: "Your top frontier pages are: [list]. Research which one? (1-5, or type a topic to override, or say 'cancel' to be asked normally.)"
4. If the user picks 1-5, use the selected page's title as the topic.
5. If the user types free text, use that.
6. If the user cancels or does not choose, fall through to D.

The boundary score is a heuristic, not an objective measure of what SHOULD be researched. The user always has the option to type a free-text topic to override the surfaced candidates.

**Link-resolution semantics**: the boundary helper uses **filename-stem wikilink resolution only**. `[[Foo]]` is counted as an edge to `Foo.md` anywhere in the vault. Aliases declared via frontmatter `aliases:` are **not** parsed. Folder-qualified links (e.g. `[[notes/Foo]]`) are resolved by stem only. This matches the wiki's link resolution behavior for unique filenames but does not implement full alias resolution.

### D. User-chosen (final fallback)
When `QUEUE_MODE=0` AND `BOUNDARY_MODE=0`, or the user declined every queue/frontier pick, ask: "What topic should I research?"

---

## Research Loop

```
Input: topic (from Topic Selection, above)

Round 1. Broad search
1. Decompose topic into 3-5 distinct search angles
2. For each angle: run 2-3 WebSearch queries
3. For top 2-3 results per angle: WebFetch the page
4. Extract from each: key claims, entities, concepts, open questions

Round 2. Gap fill
5. Identify what's missing or contradicted from Round 1
6. Run targeted searches for each gap (max 5 queries)
7. Fetch top results for each gap

Round 3. Synthesis check (optional, if gaps remain)
8. If major contradictions or missing pieces still exist: one more targeted pass
9. Otherwise: proceed to filing

Max rounds: 3 (as set in program.md). Stop when depth is reached or max rounds hit.
```

---

## Filing Results

### Output path by mode

- Queue task: `wiki/meta/draft-<task-id>/questions/research-<slug>.md`.
- Ad-hoc topic: `wiki/meta/draft-adhoc-<slug>-<YYYY-MM-DD-HHMM>/questions/research-<slug>.md` with `MAX_DEPTH: 1`, `MAX_SOURCES: 5`.

Draft paths are review artifacts, not canonical knowledge. Their sourced observations remain searchable through `wiki-search --scope recent|all` while the draft waits for consolidation.

### Default output: observations plus one synthesis

1. For every selected source, run the `wiki-ingest` URL/file path so immutable evidence, atomic observations, candidate routing, and provenance use one contract. Do not invent a second research-only schema.
2. Create exactly one synthesis draft for the topic. It groups findings, contradictions, confidence, and open questions without copying full source summaries.
3. Create **no entity or concept page by default**. A mention never qualifies. Propose a separate durable page only when it owns reusable knowledge that cannot fit an existing canonical page.
4. Create a source page only when the source is itself a reusable artifact: standard, paper, ADR, contract, design, dataset, or long-lived reference. Ordinary articles remain raw evidence plus observations.
5. Search canonical and recent knowledge before proposing any page. Duplicate findings become `no_op`; independent evidence becomes `add_evidence`; temporary facts remain `recent_only`.

### Synthesis draft

```markdown
---
type: question
title: "Research: [Topic]"
question: "[Research question]"
answer_quality: draft
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags:
  - research
  - [topic-tag]
maturity: developing
verification: unreviewed
related: []
sources: []
---

# Research: [Topic]

## Consolidated knowledge
[What existing canonical pages already establish, with wikilinks.]

## Recent provisional findings
- [Atomic finding] — observation `obs-...`; evidence `^[.raw/path:L1-L2]`; candidate action.

## Contradictions
- [Conflicting claims, dates, scope, candidate ID, and what would resolve them.]

## Synthesis
[Cross-source interpretation. Mark inference explicitly.]

## Open questions
- [Unresolved evidence gap.]
```

### After research

1. Do not add draft paths to `wiki/index.md`, domain indexes, `wiki/hot.md`, or `wiki/dashboard.md`; unreviewed research is not canonical project evidence.
2. Do not create a wiki log entry for observation-only or draft-only output. The observation/candidate ledgers and research queue carry that provisional history.
3. If queue-driven, mark the active row `done`, set `finished`/`updated`, and link the synthesis draft plus observation/candidate IDs. Re-run the DAG lint.
4. Add one pending-commit proposal containing only files actually changed by this run. Keep coordinator files out unless canonical consolidation changed them.
5. Hand the candidate set to `wiki-consolidate`. Promotion, evidence merges, disputes, expiry, and canonical navigation happen there—not inside autoresearch.

---

## Report to User

```text
Research complete: [Topic]
Rounds: N | Searches: N | Sources processed: N
Observations: N | Candidates: no_op N, evidence N, update N, dispute N, promote N, recent_only N, reject N
Draft synthesis: wiki/meta/draft-.../questions/research-....md
Canonical pages created: 0
Canonical pages updated: 0
Next: review/promote with wiki-consolidate
```

If consolidation happened in the same explicitly requested run, replace the zero counts with the exercised changes and report every affected page.

---

## Constraints

All limits live in `references/program.md` (loop caps, confidence scoring, source
preference) — read it there rather than from a second copy here, so the two cannot
drift. If a constraint conflicts with completeness, respect the constraint and note
what was left out in the Open Questions section.
