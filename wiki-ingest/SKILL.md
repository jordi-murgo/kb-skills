---
name: wiki-ingest
description: "Ingest sources into the wiki vault. Reads a source, extracts entities and concepts, creates or updates wiki pages, cross-references, and logs the operation. Supports files, URLs, and batch mode. Optional `review` flag adds a pre-ingest human-in-the-loop gate that flags doubtful URL content (ads, nav, off-topic) with annotated recommendations before writing to .raw/. After writing, runs a semantic verification pass (wiki-search retrieval + System One judgment) that flags contradictions, duplicates and missing cross-references against existing pages. Triggers on: ingest, process this source, add this to the wiki, read and file this, batch ingest, ingest all of these, ingest this url."
argument-hint: "[file-or-url] [review] [force] Not for converting .docx/.pdf/.pptx sources (use `doc-pipeline`) or saving the current conversation (use `save`)."
---

# wiki-ingest: Source Ingestion

Read the source. Write the wiki. Cross-reference everything. A single source typically touches 8-15 wiki pages.

**Syntax standard**: Write all wiki pages using the wiki markdown conventions. Wikilinks as `[[Note Name]]`, callouts as `> [!type] Title`, embeds as `![[file]]`, properties as YAML frontmatter. See the wiki-markdown skill for syntax reference.

---

## Delta Tracking

Before ingesting any file, check `.raw/.manifest.json` to avoid re-processing unchanged sources.

```bash
# Check if manifest exists
[ -f .raw/.manifest.json ] && echo "exists" || echo "no manifest yet"
```

**Manifest format** (create if missing):
```json
{
  "sources": {
    ".raw/articles/article-slug-2026-04-08.md": {
      "hash": "abc123",
      "ingested_at": "2026-04-08",
      "pages_created": ["wiki/sources/article-slug.md", "wiki/entities/Person.md"],
      "pages_updated": ["wiki/index.md"]
    }
  }
}
```

**Before ingesting a file:**
1. Compute a hash: `shasum -a 256 [file] | cut -d' ' -f1` (macOS) or
   `sha256sum [file] | cut -d' ' -f1` (Linux). Any stable digest works, but use
   the same one consistently — mixing md5 and sha256 makes every source look
   changed.
2. Check if the path exists in `.manifest.json` with the same hash.
3. If hash matches, skip. Report: "Already ingested (unchanged). Use `force` to re-ingest."
4. If missing or hash differs, proceed with ingest.

**After ingesting a file:**
1. Record `{hash, ingested_at, pages_created, pages_updated}` in `.manifest.json`.
2. Write the updated manifest back.

Skip delta checking if the user says "force ingest" or "re-ingest".

---

## URL Ingestion

Trigger: user passes a URL starting with `https://`.

**Flags.** Read the invocation arguments from `$ARGUMENTS` and treat these bare-word tokens as optional flags, position-independent (a token equal to the word anywhere in `$ARGUMENTS` sets the flag). The first non-flag token is the source (`$0` — the URL or file path).

- `review` — enable the **Review Gate** (see below). Off by default. Inserts a human-in-the-loop checkpoint between cleaning and `.raw/` so doubtful content (ads, nav, off-topic filler) can be flagged and pruned *before* it becomes an immutable source.
- `force` — skip delta-tracking and re-ingest even if the source hash is unchanged (see *Delta Tracking*).

> These are not formally parsed options — the AI agent does no flag validation. The skill recognizes them by scanning `$ARGUMENTS` for the exact words `review` / `force`. An unknown token is ignored, not an error. The `argument-hint` (`[file-or-url] [review] [force]`) only drives autocomplete display.

Steps:

1. **Fetch** the page using WebFetch.
2. **Clean** (optional): strip ads, nav, and clutter from the fetched page. Typically saves 40-60% tokens. Preferred cleaner: `defuddle` (`npx defuddle parse <url> --markdown`, MIT, kepano/defuddle); fall back to raw WebFetch output if it is not installed. Do not install anything without user approval.
3. **Derive slug** from the URL path (last segment, lowercased, spaces→hyphens, strip query strings).
4. **Branch on the `review` flag:**
   - **`review` NOT set (default)** — Save the cleaned markdown to `.raw/articles/[slug]-[YYYY-MM-DD].md` with the frontmatter header below, then continue to step 5.
   - **`review` set** — Run the **Review Gate** (next section) on the cleaned markdown held in `$TMPDIR`. Only after the user resolves the annotations do you write the pruned-and-cleaned result to `.raw/articles/[slug]-[YYYY-MM-DD].md` with the header below.

   Frontmatter header:
   ```markdown
   ---
   source_url: [url]
   fetched: [YYYY-MM-DD]
   ---
   ```
   > `.raw/` is immutable once written. The `review` gate operates on a working copy in `$TMPDIR`; the file that lands in `.raw/` is the final, user-approved version. Because the project hook blocks `Write`/`Edit` under `.raw/`, write the final file with a Bash heredoc/`mv`, not the Write tool.
5. Proceed with **Single Source Ingest** starting at step 2 (file is now in `.raw/`).

---

## Opt-in features — load the reference, do not guess

Three features are opt-in and documented in
[`references/opt-in-features.md`](references/opt-in-features.md): the URL
Review Gate, Bilingual Term Detection and DragonScale Address Assignment.

**MANDATORY**: read that file in full before running one of them. **Do NOT
load** it for a plain ingest — the default flow below is complete on its own.

Enable signals: `review` flag (URL ingest) → Review Gate; a bilingual policy
referenced by the vault's `AGENTS.md`/`CLAUDE.md` → Bilingual Term Detection;
`scripts/allocate-address.py` + `.cache/` present → Address Assignment.
A missing companion script is reported as `not available`, never as a silent pass.

---

## Image / Vision Ingestion

Trigger: user passes an image file path (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.avif`).

Steps:

1. **Read** the image file using the Read tool. The AI agent can process images natively.
2. **Describe** the image contents: extract all text (OCR), identify key concepts, entities, diagrams, and data visible in the image.
3. **Save** the description to `.raw/images/[slug]-[YYYY-MM-DD].md`:
   ```markdown
   ---
   source_type: image
   original_file: [original path]
   fetched: YYYY-MM-DD
   ---
   # Image: [slug]

   [Full description of image contents, transcribed text, entities visible, etc.]
   ```
4. Copy the image to `_attachments/images/[slug].[ext]` if it's not already in the vault.
5. Proceed with **Single Source Ingest** on the saved description file.

Use cases: whiteboard photos, screenshots, diagrams, infographics, document scans.

---

## Single Source Ingest

Trigger: user drops a file into `.raw/` or pastes content.

Steps:

1. **Read** the source completely. Do not skim.
2. **Discuss** key takeaways with the user. Ask: "What should I emphasize? How granular?" Skip this if the user says "just ingest it."
3. **Create** source summary in `wiki/sources/`. Use the source frontmatter schema from the `wiki-markdown` skill (frontmatter section). If DragonScale addresses are enabled, assign one per `references/opt-in-features.md` (§ Address Assignment).
4. **Create or update** entity pages for every person, org, product, and repo mentioned. One page per entity.
5. **Create or update** concept pages for significant ideas and frameworks.
6. **Detect native-language term candidates** only if the vault opted in (see `references/opt-in-features.md` § Bilingual Term Detection). Classify and propose termbase additions before writing.
7. **Update** relevant domain page(s) and their `_index.md` sub-indexes.
8. **Update** `wiki/overview.md` if the big picture changed.
9. **Update** `wiki/index.md`. Add entries for all new pages.
10. **Update** `wiki/hot.md` with this ingest's context.
11. **Append** to `wiki/log.md` (new entries at the TOP):
    ```markdown
    ## [YYYY-MM-DD] ingest | Source Title
    - Source: `.raw/articles/filename.md`
    - Summary: [[Source Title]]
    - Pages created: [[Page 1]], [[Page 2]]
    - Pages updated: [[Page 3]], [[Page 4]]
    - Key insight: One sentence on what is new.
    ```
12. **Verify semantically.** Run the *Post-Creation Verification* pass below: search each new claim against the rest of the vault (wiki-search) and flag conflicts with `> [!contradiction]` callouts on both pages.
13. **Refresh** `wiki/dashboard.md` from `wiki/goals.md` and the changed evidence before reporting completion; preserve manual notes and existing goal rows, no invented objectives or progress.

---

## Batch Ingest

Trigger: user drops multiple files or says "ingest all of these."

Steps:

1. List all files to process. Confirm with user before starting.
2. Process each source following the single ingest flow. Defer cross-referencing between sources until step 3.
3. After all sources: do a cross-reference pass. Run the *Post-Creation Verification* pass below (claims from all new/updated pages at once), then look for connections between the newly ingested sources.
4. Update index, hot cache, log, and `wiki/dashboard.md` once per changed batch at the end (not per-source), then report once.
5. Report: "Processed N sources. Created X pages, updated Y pages. Here are the key connections I found."

Batch ingest is less interactive. For 30+ sources, expect significant processing time. Check in with the user after every 10 sources.

---

## Context Window Discipline

Token budget matters. Follow these rules during ingest:

- Read `wiki/hot.md` first. If it contains the relevant context, don't re-read full pages.
- Read `wiki/index.md` to find existing pages before creating new ones.
- Read only 3-5 existing pages per ingest. If you need 10+, you are reading too broadly.
- Use PATCH for surgical edits. Never re-read an entire file just to update one field.
- Keep wiki pages short. 100-300 lines max. If a page grows beyond 300 lines, split it.
- Use `wiki-search.py query "<terms>" --top 8 --mode bm25` to find specific content without
  reading full pages. There is no HTTP search endpoint in this ecosystem: the vault is
  read by agents and by Git wikis, not by Obsidian plugins.

---

## Contradictions

> [!note] Custom callout dependency
> The `[!contradiction]` callout type is a wiki convention. See the wiki-markdown skill for the custom callout types (contradiction, gap, key-insight, stale).

When new info contradicts an existing wiki page:

On the existing page, add:
```markdown
> [!contradiction] Conflict with [[New Source]]
> [[Existing Page]] claims X. [[New Source]] says Y.
> Needs resolution. Check dates, context, and primary sources.
```

On the new source summary, reference it:
```markdown
> [!contradiction] Contradicts [[Existing Page]]
> This source says Y, but existing wiki says X. See [[Existing Page]] for details.
```

Do not silently overwrite old claims. Flag and let the user decide.

---

## Post-Creation Verification (semantic)

After pages are written (single-source step 12, or the batch cross-reference pass), verify the new content against the rest of the vault with retrieval, not memory. This is the judgment layer on top of the deterministic lint gates: verdicts land in the existing conventions (contradiction callouts, typed relations, cross-links), which `wiki-lint` then enforces.

Procedure:

1. **Refresh the index** — the pass is only as good as the index: `python3 .agents/skills/wiki-search/scripts/wiki-search.py build` (incremental, sub-second on small vaults). This is the one point where wiki-ingest triggers a build: the verification reads the index, so it must be current.
2. **Extract claims** from each page this ingest created or updated: atomic factual assertions, each with a verbatim quote from the page (wiki-vetting extraction pattern). Skip navigation text, frontmatter and passages the ingest did not write.
3. **Search per claim**: `wiki-search.py query "<claim>" --top 8 --mode hybrid --json`. Drop hits from the page itself and from `wiki/meta/**`.
4. **Judge the pairs**: for each (claim, hit) pair with real topical overlap, classify via the wiki-vetting System One contract: `contradicts` / `duplicates` / `extends` / `distinct`. Hard rules from wiki-vetting apply: `questions` is a record keyed by id; each instruction names the claim and quotes the evidence chunk; ≤16 claims per request; states ≤6k tokens (iGPU prefill ~1.4 s per 1k tokens).
5. **Apply verdicts** (in the ingest session, not in a read-only subagent):
   - `contradicts` → add the `[!contradiction]` callouts on both pages (see Contradictions above) plus a typed relation `contradicts` in frontmatter. Never silently overwrite the old claim.
   - `duplicates` → merge into the stronger page and redirect links, or record why both stay. An ingest that created a duplicate means the pre-creation duplicate check missed; treat it as a defect, not a nuance.
   - `extends` → add the missing wikilink so the pages reference each other.
   - `distinct` → nothing to do.
6. **Report** verdicts with the search evidence. Unresolved contradictions stay flagged — the lint gates keep failing until resolved, and that is intended.

Steps 2–4 are read-only and MAY be dispatched to a review subagent; subagents MUST NOT call the address allocator or write pages (see `references/opt-in-features.md` § Address Assignment). Verdict application (step 5) happens in the main ingest session.

Budget guardrails: verify only pages this ingest created or updated — never the whole vault; extract only claims that assert something (typically 3–8 per page); one round only, no re-verification of pages you just edited.

---

## Source Citations

Cite the source for each sourced claim inline, so `wiki-lint` (`lint-citations.py`) can validate it:

- Whole source: `^[.raw/articles/filename.md]`
- Line range: `^[.raw/articles/filename.md:42-58]` (or `#L42-L58`)
- Paths are relative to the vault root. Cite the file actually written under `.raw/`, not a temp copy.
- Put the marker at the end of the sentence or bullet it supports. Do not put markers inside fenced code.
- Only cite line ranges you have read in the `.raw/` file. Do not guess line numbers.
- Cited sources must be tracked in `.raw/.manifest.json` with their hash recorded at ingest time (see Delta Tracking). The stale-citations gate in `wiki-lint` (`lint-stale-citations.py`) flags pages citing sources whose file changed on disk after ingest or that were never recorded — on a hash change, re-run ingest for that source, then re-validate the citations pointing at it.

---

## What Not to Do

- **Source files under `.raw/` are immutable.** Do not modify the files that users drop there (articles, transcripts, images). The `.raw/.manifest.json` delta tracker and its `address_map` (DragonScale Mechanism 2) are the only files under `.raw/` that `wiki-ingest` itself maintains. Treat every other file under `.raw/` as read-only source content.
- Do not create duplicate pages. Always check the index and search before creating.
- Do not skip the log entry for changed or new ingest content. Every changed/new ingest must be recorded.
- Do not skip the hot cache update. It is what keeps future sessions fast.
