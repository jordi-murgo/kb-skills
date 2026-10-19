# wiki-lint — opt-in checks (loaded on demand)

Load this file only when one of the following is true:

- the vault uses DragonScale addresses (`address:` frontmatter present, or
  `scripts/allocate-address.py` + `.cache/address-counter.txt` exist) →
  read **Address Validation**;
- the user asks for duplicate detection beyond name similarity, or embeddings
  are configured → read **Semantic Tiling** and **Title Overlap**;
- the vault's `AGENTS.md`/`CLAUDE.md` references a bilingual-terminology policy
  → read **Bilingual Terminology Checks**.

**Companion scripts are not shipped with this skill.** The checks below invoke
`allocate-address.py`, `tiling-check.py`, `lint-title-overlap.py` and
`lint-terminology.py`. If a script is absent, report the check as `not
available` and continue — never invent its output, and never silently pass it
off as green. The deterministic gates that *do* ship are aggregated by
`run-lint.py`.

---

## Address Validation (DragonScale Mechanism 2 MVP)

**Opt-in feature.** Address Validation runs only if the vault is using DragonScale, detected by:

```bash
if [ -f ./scripts/allocate-address.py ] && [ -f ./.cache/address-counter.txt ]; then
  DRAGONSCALE_ADDRESSES=1
else
  DRAGONSCALE_ADDRESSES=0
fi
```

When `DRAGONSCALE_ADDRESSES=0`, skip this entire section. Missing `address:` fields are not flagged, not even informationally. Pages that happen to have an `address:` field are passed through unvalidated (treat as user-managed metadata).

When `DRAGONSCALE_ADDRESSES=1`, proceed with the rollout baseline and checks below.

Rollout baseline: **2026-04-23** (Phase 2 ship date in vaults that adopted DragonScale on that day). Vaults that adopted DragonScale later should override this baseline by setting the earliest `created:` date of any addressed page as their personal rollout date. Record the chosen baseline at the top of `.cache/legacy-pages.txt` as a commented line: `# rollout: YYYY-MM-DD`.

### Classification rule (applied per page)

Before validating anything, classify the page:

| Classification | Criteria |
|---|---|
| **Meta / fold / excluded** | File is in `wiki/folds/` OR filename in `{_index.md, index.md, log.md, hot.md, overview.md, dashboard.md, dashboard.base, Wiki Map.md, getting-started.md}`. Address not required. |
| **Post-rollout (must have address)** | `type` is not meta/fold AND frontmatter `created:` date is >= 2026-04-23 AND file path is NOT in the legacy baseline manifest. |
| **Legacy (backfill-eligible)** | `type` is not meta/fold AND frontmatter `created:` date is < 2026-04-23 OR file path IS in the legacy baseline manifest. Address not required until backfill. |

**Legacy baseline manifest**: optional file at `.cache/legacy-pages.txt`, one relative path per line. Pages listed there are treated as legacy regardless of `created:` date. Use this to grandfather pages whose `created:` metadata is wrong or missing.

### Validation checks (run in order)

1. **Format check**: any page with `address:` set must match one of:
   - `^c-[0-9]{6}$` — post-rollout creation address.
   - `^l-[0-9]{6}$` — legacy-backfill address.
   - Pages under `wiki/folds/` use `fold_id`, not `address`; do not apply the `c-`/`l-` regex there.

2. **Uniqueness check**: no two pages share the same address value. Report both paths.

3. **Counter consistency**: `python3 scripts/allocate-address.py --peek` returns the next counter value. Every observed `c-NNNNNN` must satisfy `NNNNNN < peek_value`. Violation = counter drift.

4. **Post-rollout enforcement**: every page classified as "post-rollout (must have address)" that LACKS the `address:` field is a lint **error**, not informational. This prevents the silent-regression path where a new page skips address assignment.

5. **Legacy identification**: every page classified as "legacy" that LACKS an address is informational. The lint report lists them under "Pending backfill" with total count.

6. **Address-map consistency** (`.raw/.manifest.json`): for every page path in `address_map`, the page must exist and its frontmatter `address` must match the mapping. Mismatches are errors (either a rename dropped the map update, or a manual edit diverged).

### Lint posture summary

- Pages that HAVE an address with bad format: **error**.
- Pages that HAVE colliding addresses: **error**.
- Pages classified **post-rollout** WITHOUT an address: **error**.
- Pages classified **legacy** WITHOUT an address: **informational** (expected).
- Meta and fold pages without `address`: **ignored** (not applicable).
- Counter drift (observed counter >= peek): **error**.
- Address-map mismatch: **error**.

Lint only observes. Do NOT auto-assign missing addresses during lint. Assignment is `wiki-ingest`'s responsibility only.

### Output section in the lint report

```markdown
## Address Validation

- Counter state: `$(python3 scripts/allocate-address.py --peek)`
- Highest c- address observed: c-XXXXXX
- Post-rollout pages checked: N (X passing, Y errors)
- Legacy pages pending backfill: M

### Errors
- [[Page Name]]: invalid address format `{value}`. Expected `c-NNNNNN` or `l-NNNNNN`.
- [[Page A]] and [[Page B]] share address `c-000042`.
- [[Post-Rollout Page]]: missing address. Page created 2026-04-25 (post-rollout); address required. Run wiki-ingest or manually run `python3 scripts/allocate-address.py` and add to frontmatter.
- [[Page Name]] has address `c-000100` but counter peek is `50`. Counter drift; run `python3 scripts/allocate-address.py --rebuild`.
- `.raw/.manifest.json` maps `wiki/foo.md` -> `c-000010` but page frontmatter has `c-000012`. Resolve mismatch.

### Pending backfill (informational)
- M legacy pages without addresses. See `.cache/legacy-pages.txt` for the canonical legacy set, or filter by `created:` < 2026-04-23.
```

---

## Semantic Tiling (DragonScale Mechanism 3 MVP, opt-in)

**Opt-in feature.** Semantic tiling flags candidate duplicate *pages* (not just concept pages — see Scope below) using embedding cosine similarity. Local ollama only by default; remote endpoints require an explicit override flag.

### Detection and delegation

1. Check whether `tiling-check.py` exists next to this skill (or in the project's `scripts/`). Absent → report `not available` and skip.
2. Run it read-only and surface its exit code verbatim.
3. Never auto-merge or delete anything it reports.

### Scope (what the helper scans)

All Markdown pages under `wiki/` except `wiki/meta/`, `wiki/.reciclaje/` and
state files (`index.md`, `log.md`, `hot.md`, `goals.md`, `dashboard.md`,
`overview.md`, `_index.md`).

### How the helper works

- Chunks each page, embeds chunks through the configured endpoint, averages
  chunk vectors per page, then compares page vectors pairwise.
- Caches vectors by page content hash + model name, so unchanged pages are
  never re-embedded.
- Emits pairs above the configured threshold, sorted by score.

### Security posture

- Local endpoints (`127.0.0.1`, `localhost`) run without a flag.
- Any remote endpoint needs `--allow-remote`; without it the helper refuses.
  Page text leaves the machine otherwise.

### Default bands (conservative seeds, NOT calibrated)

| Band | Cosine | Meaning |
|---|---|---|
| error | >= 0.90 | Near-certain duplicate |
| review | 0.80 – 0.90 | Human reads both pages |
| below | < 0.80 | Not reported |

These are starting points. Calibrate per vault before trusting them.

### Calibration procedure (manual, one-time per vault)

1. Pick 20 pairs the helper reports across the bands.
2. Mark each as true duplicate / not duplicate.
3. Raise the error band until precision is acceptable; lower the review band
   until recall is acceptable.
4. Record the chosen bands and the sample size in the lint report so the next
   run is comparable.

### Scale

- Cold-cache cost is O(N) POSTs to ollama. Warm-cache cost is O(N^2) cosines in pure Python.
- Helper prints a warning at > 500 pages and hard-fails (exit 4) at > 5000. Revisit the implementation (batching, vectorized cosine, or external tooling) before exceeding either limit.

### Lint report embed

```markdown
## Semantic Tiling
See [[tiling-report-YYYY-MM-DD]] for the full pair listing.
- Errors (>=0.90): N pairs
- Review (0.80-0.90): M pairs
- Calibrated: true|false
```

### Invariants

- Read-only. `tiling-check.py` never modifies wiki pages.
- No auto-merge. Duplicates are listed, never resolved.
- Cache is incremental and model-scoped. Unchanged pages are not re-embedded.
- Exit codes: `0` ok, `2` usage error, `3` cache corrupt, `4` scale hard-fail, `10` ollama unreachable, `11` model missing. Surface all of them; do not collapse into a single "unknown" bucket.

---

## Title Overlap

Lightweight duplicate-page pre-filter, no embeddings. Runs `scripts/lint-title-overlap.py` over `wiki/**/*.md` and reports pairs whose filename tokens overlap above a Jaccard threshold (default 0.55).

### When to use

- Always — the script is fast (O(N²) Jaccard on tokenized stems, < 1s for 1000 pages) and dependency-free.
- Complements Semantic Tiling (Mechanism 3) at the cheap end of the precision/recall curve: catches near-duplicates with similar names that humans accidentally created, misses semantic duplicates with no shared tokens.

### Invocation

```bash
python3 scripts/lint-title-overlap.py wiki 0.55
```

The output is `score\tpath-a\tpath-b` lines, sorted by score descending. Pipe to the lint report's *Title Overlap* section.

### Report section

```markdown
## Title Overlap (Jaccard ≥ 0.55)
- 0.83  wiki/concepts/KI-Berater-Tagessatz-DE.md  ↔  wiki/concepts/KI-Berater-DACH.md
- 0.60  wiki/entities/LangDock.md                 ↔  wiki/entities/LangDock-Berlin.md
```

### Severity

- `≥ 0.80`: error — almost certainly duplicates.
- `0.55 - 0.80`: review — human eyeballs.
- `< 0.55`: not emitted.

Never auto-merge. List, let the user decide.

---

## Bilingual Terminology Checks (opt-in)

**Opt-in feature.** Bilingual checks run only if the vault's `CLAUDE.md` references `docs/bilingual-terminology-policy.md`. Otherwise, skip this section.

Combines deterministic checks (run by `scripts/lint-terminology.py`) and judgment-based checks (run in this skill body during the lint pass).

### Deterministic checks (run script first)

```bash
python3 scripts/lint-terminology.py wiki
```

The script emits a structured report with these findings:

| Check | Trigger | Severity | Suggested fix |
|---|---|---|---|
| **Missing alias** | Page has `dnt_class:` but `aliases:` has fewer than 2 entries | error | Add native form and English gloss to `aliases:` |
| **Termbase drift** | Page has `dnt_class:` but is not listed in `wiki/meta/termbase.md` | warn | Add row to termbase index |
| **Orphan termbase entry** | `wiki/meta/termbase.md` references a page that does not exist or no longer has `dnt_class:` | warn | Remove row or restore `dnt_class:` |
| **Invalid dnt_class value** | `dnt_class:` is set to anything other than `term-of-art`, `eigenname`, `coined`, `hybrid` | error | Use one of the four valid values |
| **Hyphenated-slug alias** | An alias contains spaces but the wiki convention is hyphenated; lint flags potential resolution drift | info | Hyphenate the alias OR confirm it is a display-form, not a target-form |

### Judgment-based checks (run during this skill)

After running the script, do a reading pass for these issues. They are LLM-judgment calls and cannot be deterministically detected:

1. **Inconsistent surface forms**. Scan for the same concept appearing as different strings on different pages — e.g., `IHK`, `Industrie- und Handelskammer`, `chamber of commerce`, `chambers` used interchangeably. Report the divergence; do not auto-rewrite.
2. **Missing first-use gloss**. On any page that mentions a DNT term for the first time without an inline gloss `Term (English meaning, frame)`. Report the page and the term. Do not insert a gloss automatically.
3. **Redundant glossing**. The same native term re-glossed on every mention within a page (gloss should appear once per page, then the term alone).
4. **Suspect translation**. A native term-of-art rendered as an English near-synonym that may have changed the referent — e.g., `Geschäftsführer` written as `CEO`, `Vorstand` as `board of directors`, `AGB` as `T&C`. Flag for review against the termbase.
5. **Retrieval-surface gaps**. A page about a native concept that is not reachable via its English gloss (alias missing or misspelled), or vice versa. Suggest the missing alias.

### Report section

```markdown
## Bilingual Terminology
- Deterministic: see `wiki/meta/lint-terminology-YYYY-MM-DD.txt`
- Judgment findings:
  - **Inconsistent surface form**: "IHK" / "chamber of commerce" / "chambers" appear on [[Page A]], [[Page B]], [[Page C]]. Termbase says canonical = "IHK". Suggest unifying.
  - **Missing first-use gloss**: [[Page D]] introduces `Mitbestimmung` without inline English context. Suggest: "Mitbestimmung (Germany's employee codetermination framework)".
  - **Suspect translation**: [[Page E]] writes "Vorstand" as "board of directors". Vorstand is the management board in a two-tier system; this changes the referent. Review.
```

### Invariants

- Read-only. `lint-terminology.py` never modifies wiki pages.
- No auto-rewrite of glosses or aliases. Surface, do not fix.
- "I don't know the English equivalent" is a valid finding; do not invent one.
