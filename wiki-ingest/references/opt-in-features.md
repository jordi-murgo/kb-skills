# wiki-ingest — opt-in ingestion features (loaded on demand)

Load this file only when the vault has enabled one of the features below. The
default ingest path (delta tracking, cleaning, single/batch ingest, citations,
verification) lives in `SKILL.md` and needs none of this.

| Feature | Enable signal | Section |
|---|---|---|
| Review Gate | the `review` flag was passed for a URL ingest | Review Gate |
| Bilingual terms | the vault's `AGENTS.md`/`CLAUDE.md` references a bilingual-terminology policy | Bilingual Term Detection |
| DragonScale addresses | `scripts/allocate-address.py` exists AND `.cache/` exists | Address Assignment |

**Companion scripts are not shipped with this skill.** When
`scripts/allocate-address.py` is absent, address assignment is `not available`:
create pages without an `address:` field and say so — never fabricate counters.

---

## Review Gate (opt-in via `review` flag)

A pre-ingest, human-in-the-loop checkpoint for **URL ingests**. Cleaning strips structural clutter deterministically, but vendor ads, calls-to-action, navigation remnants, and off-topic filler routinely survive and would otherwise be baked into the immutable `.raw/` source and propagate into wiki pages. The Review Gate makes the AI agent flag those passages with a recommendation and lets the user decide before anything is committed.

Run this section **only** when the `review` flag is set. It sits between cleaning (step 2) and the `.raw/` write (step 4).

### Procedure

1. **Hold the cleaned markdown in `$TMPDIR`** (e.g. `$TMPDIR/[slug]-candidate.md`) — do not write to `.raw/` yet.
2. **Read** the candidate fully and **annotate doubtful blocks inline** with an HTML comment placed on the line *immediately above* the block it refers to:
   ```markdown
   <!-- REVIEW[CODE » recommendation]: one-line reason (why it may not belong) -->
   ```
   - HTML comments are invisible in rendered markdown and are trivially removable with `rg`/grep.
   - Annotate **only** doubtful content. Do not annotate clean, on-topic prose.
   - One annotation per contiguous block. Don't pepper every sentence.
3. **Present a compact summary** to the user: a numbered list of every annotation with its `CODE`, recommendation, a short preview of the flagged block, and the reason. State the default action explicitly (all `remove`-recommended blocks will be deleted unless the user says otherwise).
4. **Apply the user's decision.** The user may accept all recommendations, keep specific blocks, or remove additional ones. Default when the user just says "go"/"ok": delete every block recommended `remove`, keep the rest.
5. **Strip all remaining `<!-- REVIEW ... -->` comments** from the working copy — annotations are a review artifact and must never reach `.raw/`.
6. **Write the pruned, comment-free markdown to `.raw/articles/...`** (Bash, not the Write tool — see the `.raw/` immutability note above), then return to URL-ingest step 5.

### Annotation codes

| Code | Meaning | Default recommendation |
|---|---|---|
| `AD` | Advertisement / vendor promo / sponsored block | `remove` |
| `CTA` | Marketing call-to-action, sign-up / "start free" pitch | `remove` |
| `NAV` | Navigation, header/footer, breadcrumb, related-links remnant | `remove` |
| `OT` | Off-topic — unrelated to the source's core subject | `remove` |
| `DUP` | Duplicate / redundant restatement already captured elsewhere | `remove` |
| `LOW` | Low-signal filler, fluff, padding | `review` |
| `BIAS` | On-topic but promotional/non-neutral framing worth noting | `keep` |
| `?` | Unclear — genuine judgment call, needs the user | `review` |

Recommendation vocabulary: `remove` (delete from the source), `keep` (leave as-is), `review` (user judgment required — no safe default).

### Example

````markdown
<!-- REVIEW[AD » remove]: MindStudio "Remy" product promo, interleaved vendor advertising, not topical -->
## Seven tools to build an app. Or just Remy.
Editor, preview, AI agents, deploy — all in one tab. Nothing to install.

<!-- REVIEW[CTA » remove]: vendor sign-up pitch / deploy upsell, not part of the methodology -->
## Deploying Your Optimized Skill with MindStudio
You can start free at mindstudio.ai.
````

### Conservative defaults

- When unsure whether content is doubtful at all, **annotate it `?` / `review`** rather than silently keeping or removing it. Surfacing is safer than a silent decision.
- Never delete a block without an annotation and a user decision. The gate's whole purpose is that removals are visible and approved.
- If the user declines the gate mid-flow ("just ingest it"), strip the comments and proceed with the full cleaned text.

---

## Bilingual Term Detection (opt-in)

**Opt-in feature.** Bilingual term detection runs only if the vault's `CLAUDE.md` references `docs/bilingual-terminology-policy.md`. Otherwise, skip this section.

When ingesting a source that contains native-language content (German, by default), step 6 above detects and classifies candidate terms before page creation.

### Candidate detection signals

A term in the source becomes a *candidate* for the termbase when any of these apply:

- Capitalized noun not yet in `wiki/index.md` and not obvious-English (recognized acronyms like API, SaaS, MVP are ignored).
- Legal reference: any token matching `§ \d+|Art\. \d+|Abs\. \d+|BGB|EStG|UStG|HGB|AktG|GewO` or similar.
- Compound containing `-DE`, `-DACH`, `KI-`, `Förder`, `Bundes-`, `Landes-`, `Steuer-`, `Gesetz`, `Verordnung`, `kammer`, `amt`.
- Recurring (≥ 2 mentions) German noun phrase that cannot be straightforwardly translated.

### Classification procedure (per candidate)

1. **Decide class** per `docs/bilingual-terminology-policy.md` § 3:
   - `term-of-art` — defined legal/regulatory meaning (`AGB`, `Gewerbesteuer`, `Geschäftsführer`).
   - `eigenname` — institution/statute/scheme without equivalent (`IHK`, `BGB`, `Mittelstand-Digital-Zentrum`).
   - `coined` — wiki's own vocabulary (project names, conventions).
   - `hybrid` — bilingual compound where the compound is the unit of meaning (`KI-Berater`, `AI-Engineer-Rolle-DE`).
   - `translatable` — descriptive vocabulary, generic concepts. No special handling.

2. **Surface the decision to the user** when judgment is involved. Format:
   ```
   New term candidate: "Vorstand"
   Proposed: dnt_class = term-of-art (German two-tier board, distinct from "board of directors")
   Aliases: ["management board", "executive board"]
   Confidence: high | medium | low
   Confirm? [y/n/edit]
   ```
   Do **not** silently add or change termbase entries.

3. **Write the page** with:
   - `dnt_class: <class>` in frontmatter
   - `lang: de` (or other native code)
   - `aliases:` containing **both** the native form and the English gloss — minimum two entries
   - First mention of the native term in the page body uses the gloss form: `IHK (Industrie- und Handelskammer, Germany's chamber of commerce)`. Subsequent mentions use the native term alone.

4. **Update `wiki/meta/termbase.md`** with a new row. If the page does not exist, create it from the template scaffold in `docs/bilingual-terminology-policy.md` § 5.

5. **Never** fabricate an English equivalent that does not exist. "I don't know the English gloss for this term" is the correct answer — record `aliases:` with only the native form and flag for the user.

### Conservative defaults

- When in doubt between `term-of-art` and `translatable`: ask. Native preservation is safer than silent translation.
- When in doubt between `term-of-art` and `eigenname`: prefer `eigenname` only for institutions/statutes/schemes. Everything else with a defined legal meaning is `term-of-art`.
- When in doubt between `hybrid` and `coined`: `hybrid` for bilingual compounds in active external use; `coined` for purely internal vocabulary.

---

---

## Address Assignment (DragonScale Mechanism 2 MVP)

**Opt-in feature**. DragonScale address assignment runs only if `scripts/allocate-address.py` is present AND `.cache/` exists. Otherwise, skip this entire section and proceed with ingest normally.

**Feature detection (run at start of every ingest)**:

```bash
if [ -f ./scripts/allocate-address.py ] && [ -d ./.cache ]; then
  DRAGONSCALE_ADDRESSES=1
else
  DRAGONSCALE_ADDRESSES=0
fi
```

When `DRAGONSCALE_ADDRESSES=0`, pages are created without an `address:` frontmatter field, and `wiki-lint`'s Address Validation section is skipped entirely (missing addresses are not flagged in any severity). This preserves default plugin behavior for vaults that have not adopted DragonScale.

When `DRAGONSCALE_ADDRESSES=1`, proceed with the rest of this section.

---

Every **newly created non-meta wiki page** gets a stable address in its frontmatter:

```yaml
address: c-000042
```

Format: `c-<6-digit-counter>`. The `c-` prefix stands for "creation-order counter." Zero-padded.

Rollout baseline: **2026-04-23** (Phase 2 ship date). Pages with `created:` >= this date are post-rollout and MUST have an address (unless excluded below). Pages with `created:` earlier are legacy-exempt until a deliberate backfill pass assigns `l-NNNNNN` addresses.

### Required tool: `scripts/allocate-address.py`

Address allocation is delegated to an atomic Python helper. The helper uses `fcntl.flock` on `.cache/.address.lock` to prevent read-use-increment races and recovers the counter by scanning existing frontmatter if the counter file is missing.

```bash
ADDR=$(python3 scripts/allocate-address.py)
# ADDR is now e.g. "c-000042"; counter is already incremented
```

**CRITICAL**: never use the Write or Edit tool on `.cache/address-counter.txt`. That would fire the PostToolUse hook, which runs `git add wiki/ .raw/` and can accidentally commit unrelated pending wiki changes under a generic message. Counter mutation is **only** permitted through the helper script (Bash tool).

### Helper modes

- `python3 scripts/allocate-address.py` — atomically reserves and returns the next address.
- `python3 scripts/allocate-address.py --peek` — prints the next value without reserving (safe, read-only).
- `python3 scripts/allocate-address.py --rebuild` — recomputes the counter from the highest observed `c-NNNNNN` in existing frontmatter. Never resets to 1 silently if pages already have addresses. Run this if the counter file is suspected corrupt.

### Assignment procedure (per new page)

1. Before writing a new non-meta page, call `python3 scripts/allocate-address.py` and capture the output.
2. Include `address: c-XXXXXX` in the page's frontmatter.
3. Record the path-to-address mapping in `.raw/.manifest.json` under a new top-level key `address_map` (see schema below).

### `address_map` in `.raw/.manifest.json`

```json
{
  "sources": { ... },
  "address_map": {
    "wiki/concepts/Example.md": "c-000042",
    "wiki/entities/Another.md": "c-000043"
  }
}
```

On re-ingest of the same source (whether by `--force` or a changed hash), always consult `address_map` first. If the target page path has a prior address, REUSE it. Do not allocate a new one.

On a page rename, the skill must update the `address_map` key (old path -> new path) while preserving the address value.

### Exclusions (do NOT assign an address to)

- Meta files: `_index.md`, `index.md`, `log.md`, `hot.md`, `overview.md`, `dashboard.md`, `dashboard.base`, `Wiki Map.md`, `getting-started.md`.
- Fold pages under `wiki/folds/` (they use their own deterministic `fold_id`).
- Pre-rollout legacy pages (`created:` < 2026-04-23). Legacy pages get `l-NNNNNN` addresses only via a deliberate backfill operation.

### Idempotency rules

- If a page being (re)written already has an `address:` field in its current content, REUSE it. Do not allocate a new one.
- If a source is re-ingested and `address_map` has a mapping for the target path, reuse that mapping.
- If the source has been ingested before AND the target page has no address AND the page `created:` date is post-rollout, allocate an address and record it. This covers the case where an older ingest produced a page before Phase 2 rollout; the rollout cutoff still applies (pages dated pre-2026-04-23 stay legacy).

### Concurrency policy

- **Single-writer only** in Phase 2. Do not run parallel ingests from multiple AI agent sessions or sub-agents that assign addresses. The `flock` in the helper prevents counter corruption but does not serialize page writes themselves.
- Sub-agents (e.g. general-purpose) that are dispatched for research or review MUST NOT call the allocator. They are read-only in this respect.
- Multi-writer support is a deferred feature.

### Batch ingest

Assign addresses sequentially during single-source-ingest for each source. Do not pre-reserve a block of counter values. The helper is cheap (one lock, one integer read/write).
