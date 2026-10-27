---
name: kb-setup
description: >
  Configure a project knowledge base: wire the skill layout under .agents/skills/,
  make skills discoverable, set up the deterministic lint gates, and connect the
  M365 / Jira / GitLab source pipelines. Use when bootstrapping a new KB project
  or repairing an existing one. Triggers on: "set up the KB", "configure the
  knowledge base", "bootstrap kb", "repair skills", "/kb-setup".
allowed-tools: Read Write Edit Bash Grep Glob
---

# kb-setup: Configure a Project Knowledge Base

This wires the plumbing. Vault *content* structure belongs to the `wiki` skill —
this skill covers the layout, discovery, gates, and pipelines around it.

---

## 1. Skill layout

Skills live as **real content** in `.agents/skills/<name>/SKILL.md`, tracked in
the repo so they travel with it.

**Claude Code only discovers `.claude/skills/`.** `.agents/skills/` is never
scanned. Bridge them with intra-repo relative symlinks:

```bash
for s in .agents/skills/*/; do
  n=$(basename "$s")
  ln -sfn "../../.agents/skills/$n" ".claude/skills/$n"
done
```

Relative, and both ends inside the repo — so the link survives a clone. Verify
before trusting it:

```bash
for l in .claude/skills/*; do
  [ -f "$l/SKILL.md" ] || echo "BROKEN: $l"
done
```

### The self-referential symlink trap

Running that `ln -s` from inside `.agents/skills/` instead of the repo root
produces `.agents/skills/wiki -> ../../.agents/skills/wiki` — a link pointing at
itself. `ls` reports "Too many levels of symbolic links", and the skill silently
never loads. Fifteen skills sat broken in this repo for weeks this way.

Always verify resolution after creating links. A symlink that exists is not a
symlink that works.

## 2. Git tracking

`.agents/` is project content in full — skills and their colocated scripts
travel with the repo, so it is not ignored at all.

`.claude/` is the opposite: local config, except the skills bridge. That one
needs the `dir/*` + negation form, because ignoring `.claude/` outright stops
git descending into it and a later `!.claude/skills/` never matches:

```gitignore
.claude/*
!.claude/skills/
```

Confirm local settings stay out of the repo:

```bash
git check-ignore -v .claude/settings.local.json
```

### `.raw/` in, `.cache/` out

`.raw/` is committed. It is generated, never hand-edited, but `lint-contradictions.py`
globs `.raw/**/*` to decide whether an entity is sourced — ignore it and the gate
still exits 0 while testing nothing, with every entity reading as unsourced.

`.cache/` is the opposite: verbatim API payloads that the source pipelines keep
so a renderer fix does not mean re-fetching everything. It is disposable, it is
large, and it holds fields the renderers deliberately drop — `accountId`s, avatar
URLs, every custom field. Keep it out of the repo:

```gitignore
.cache/
```

### `.cache/`: derived state, tracked policy

Create this directory before any skill builds an index, mirror, scan state, or
report. It is an entirely disposable local cache, but its **ignore policy** must
travel with the vault:

```bash
mkdir -p .cache
cat > .cache/.gitignore <<'EOF'
# Derived vault state is local and may be deleted at any time.
# Keep only this sentinel tracked; ignore caches, indexes, mirrors, logs and PID files.
*
!.gitignore
EOF
```

Do **not** ignore `.cache/` outright in the root `.gitignore`: Git then
will not descend into the directory, so it cannot track the sentinel. If an
older vault has that rule, replace it with:

```gitignore
.cache/*
!.cache/.gitignore
```

Prove both halves before committing:

```bash
git check-ignore -q --no-index .cache/index.db
! git check-ignore -q --no-index .cache/.gitignore
```

## 3. Naming

| Prefix | Origin |
|---|---|
| `wiki-*` | upstream pack — vendored here, so local edits survive, but they are lost if the skill is re-copied from `~/.agents/skills/` |
| `kb-*` | project-local, owned by this repo |

## 4. Lint gates

Scripts are **colocated with the skill that owns them**, at
`.agents/skills/<skill>/scripts/`, not in a shared top-level `scripts/`.

A script that lives inside a skill must not derive the repo root by counting
`.parent` levels — its depth depends on where the skill is installed. Walk up
for markers instead: `wiki/` **and** `.git`, and reject a candidate whose
`wiki/SKILL.md` exists. The first two markers alone are ambiguous — a skill
directory is named `wiki`, and this upstream repository is itself a git
checkout containing that directory. Walk from `cwd` first, then from the
script's own location, and fail loudly when no candidate matches.

`.agents/skills/wiki-lint/scripts/run-lint.py` aggregates the deterministic
checks and resolves its sibling `lint-*.py` scripts relative to its own file.

```bash
python3 .agents/skills/wiki-lint/scripts/run-lint.py           # human report
python3 .agents/skills/wiki-lint/scripts/run-lint.py --json    # pipelines
```

Exit `1` if any check fails. Checks: ten, enumerated in `wiki-lint/SKILL.md` § Lint Checks
(`lint-dead-links.py`, `lint-orphans.py`,
`lint-frontmatter.py`, `lint-contradictions.py`, `lint-relations.py`, `lint-empty-sections.py`,
`lint-citations.py`, `lint-stale-citations.py`, `lint-tag-taxonomy.py`, `lint-misplaced-pages.py`).

### Verify a gate before trusting it

A gate reporting "pass" may be passing vacuously. The sharpest version of this:
if `run_check` cannot find a sibling script it returns a non-`fail` status, so a
relocated script set would report four skipped checks and **exit 0** — a green
gate testing nothing. Missing scripts therefore count as failures here.

Prove the gate fails on a fault:
copy the vault to a scratch dir (**including `.raw/`** — `lint-contradictions.py`
reads it to validate entity sourcing, and without it every entity reads as
unsourced), inject one fault, confirm the matching check fires and the exit code
is `1`. Use a fresh copy per fault, or the tests contaminate each other.

### Known gate behaviour

- **Wikilinks are case-sensitive**: `[[keycloak]]` resolves, `[[Keycloak]]` is
  reported dead. the wiki accepts both, so the vault can look fine and still
  fail the gate. Fix links to match the filename exactly.
- `wiki-lint/SKILL.md` documents Title Case filenames (`Machine Learning.md`)
  while this vault uses lowercase-hyphen (`keycloak.md`). The vault wins.
- These optional scripts are referenced by `wiki-lint` but absent here:
  `lint-title-overlap.py`, `lint-terminology.py`, `allocate-address.py`,
  `tiling-check.py`. Their features are unavailable, not broken.

## 5. Source pipelines

| Source | Skill | Config section |
|---|---|---|
| M365 mail / Teams / SharePoint | `kb-m365-fetch` | `m365` |
| Jira | `kb-jira-sync` | `jira` + `ATLASSIAN_*` env |
| Wiki publish | `kb-publish` | `wiki_publish` |
| Atlassian discovery (optional) | `twg` CLI | — (reads own auth) |

The acquirers write fetched/rendered source into `.raw/` (treated as immutable
afterwards). `wiki-ingest` turns `.raw/` into wiki pages. No analysis tooling
writes `wiki/`; the one exception is `run-lint.py`'s report under `wiki/meta/`
(suppress with `--no-report`).

### 5.1 `twg` CLI — Atlassian discovery (optional)

Optional accelerator for `kb-jira-sync`'s reconcile phase, not a dependency.
Installation, project-local `TWG_CONFIG_DIR` credentials, `.gitignore` entries
and verification steps live in the owning skill:
[`kb-jira-sync` → Stage 0 — TWG Discovery](../kb-jira-sync/SKILL.md).

Skip it when the project has no Atlassian Cloud connection or the flat REST
import is sufficient. TWG is a discovery tool, not a pipeline: it writes
neither `.raw/` nor `wiki/`.

## 6. `kb-config.yaml` — current supported contract

One file at the vault root configures every pipeline. **No project-specific
value belongs in skill code**: if you need to change a URL, project key, model,
endpoint, source directory, or output path, it belongs here.

### Creating one

```bash
cp <kb-skills>/kb-config.example.yaml kb-config.yaml
```

The canonical top-level sections are:

| Section | Consumer | Required when |
|---|---|---|
| `project` | M365 fetchers | Running Graph fetch; `name` is required |
| `jira` | `kb-jira-sync` | Importing Jira |
| `wiki_publish` | generic `kb-publish` deployer | Publishing with `deploy-wiki.py` |
| `m365` | `kb-m365-fetch` | Fetching M365 data |
| `embeddings` | `wiki-search` | Optional; defaults are local |
| `code_search` | `code-search` | Optional; independent code index |
| `decisions` | `wiki-vetting` | Optional; defaults are local |
| `repos` | `wiki-git-ingest` | Watching repositories |

Use this shape; omit optional sections rather than inventing alternative keys:

```yaml
project:
  name: myproject
  keywords: [myproject, add project terms]

jira:
  enabled: false
  base_url: https://yourorg.atlassian.net
  project_key: YOUR_PROJECT_KEY
  output_dir: .raw/jira

wiki_publish:
  enabled: false
  target: github # github or gitlab
  repo: git@github.com:owner/project.wiki.git
  branch: main
  vpn_required: false
  vpn_host: github.com
  vpn_private_prefix: "10."

m365:
  enabled: false
  modules:
    emails: true
    chats: true
    teams_channels: true
    attachments: true
    chat_attachments: true
    transcripts: false
    sharepoint: false
  sharepoint:
    site_host: ""
    site_path: ""
    folder_path: ""
    max_depth: 3
  output:
    dir: .raw/msoffice
    attachments_dir: .raw/msoffice/attachments
    chat_attachments_dir: .raw/msoffice/chat-attachments
    transcripts_dir: .raw/msoffice/transcripts
    sharepoint_dir: .raw/sharepoint
  window:
    hours: 24
    top: 100
    chat_limit: 50

embeddings:
  model: ${WIKI_SEM_MODEL:-bge-m3}
  endpoint: ${WIKI_OLLAMA_URL:-http://127.0.0.1:11434}
  db: .cache/wiki-search/index.db
  source_dirs:
    - wiki

code_search:
  source_dirs:
    - projects

decisions:
  model: ${WIKI_VET_MODEL:-clef-flash:9b}
  endpoint: ${WIKI_OLLAMA_URL:-http://127.0.0.1:11434}

repos: {}
```

`embeddings.source_dirs` is a non-empty list of vault-relative directories.
It defaults to `[wiki]`; add `.` only to explicitly index the vault root.
Git-ignored Markdown is excluded and a missing configured directory makes
`wiki-search build` fail.

`code_search` indexes code and configuration, not Markdown by default. It
accepts `source_dirs`, `extensions`, `model` and `endpoint`; model and endpoint
inherit from `embeddings` when omitted. All of its data lives in
`.cache/code-search/`. It respects each submodule's Git rules. Install the whole
skill and run
`python3 .agents/skills/code-search/scripts/code-search.py doctor --json`; if
dependencies are missing, ask the user before installing them. Never download
models or start services on its own.

### Rules and defaults

1. **Paths are relative to the vault root.** Absolute paths, `~`, and `..`
   traversal are rejected before network or filesystem writes.
2. **No credentials, ever.** Jira reads `ATLASSIAN_EMAIL` and `ATLASSIAN_API_KEY`
   from the environment, `.env`, or `.env.local` (scripts load both files; the
   process environment wins); macros such as `${WIKI_OLLAMA_URL}` receive values
   from the same sources.
3. **`enabled` only applies to pipeline sections** — `jira`, `wiki_publish`,
   and `m365`. `embeddings`, `code_search`, `decisions`, and `repos` are optional
   sections without an `enabled` switch.
4. **Defaults are consumer-specific.** Omit an optional section to use its
   documented defaults; when an enabled pipeline needs a value, its script
   stops and names the missing key instead of guessing.

### Wiki publisher compatibility

`wiki_publish` is the only publishing key. `deploy-wiki.py`, `deploy-gitlab-wiki.py`
and `push.sh` all read it; `push.sh` dispatches on `target` (`github` → `deploy-wiki.py`,
`gitlab` → `deploy-gitlab-wiki.py`). Vaults that still carry the older `gitlab_wiki`
section are read through a fallback, so no key rename is required — but new
configuration belongs under `wiki_publish`.

### Formats and validation

Resolution order: `kb-config.yaml` → `kb-config.yml` → `kb-config.json`; M365
also has a separate legacy flat `m365-config.json` fallback. YAML needs PyYAML;
the Python scripts print the installation command if it is absent.

Adding a supported key means updating its reader, `kb-config.example.yaml`, and
this schema. Do not copy obsolete flat M365 keys such as `m365.output_dir`:
the Graph reader consumes the nested `m365.output.*` mapping shown above.

```bash
python3 -c "import yaml; print(yaml.safe_load(open('kb-config.yaml')).keys())"
python3 .agents/skills/wiki-search/scripts/wiki-search.py status
```

| Message | Cause |
|---|---|
| `must be relative to the vault root` | Absolute path, `~`, or `..` in a path value |
| `configured source directory not found` | A `source_dirs` entry does not exist |
| `<section>.enabled is false` | Pipeline is off, not broken |
| `is missing: <key>` | Required value for an enabled pipeline is absent |

## 7. Project agent instructions and state views

Install [the maintenance block](assets/project-agents-block.md) into the PROJECT root `AGENTS.md`, not this upstream repository's instruction file. This makes content maintenance mandatory even when a task bypasses a specific ingest/save skill.

1. Read the existing project instructions and state files. Keep all unrelated rules and custom content. If a project rule conflicts with this contract, report the conflict and obtain its resolution rather than overriding it silently.
2. Create `AGENTS.md` only when absent. If both `<!-- kb-skills:wiki-maintenance:start -->` and `<!-- kb-skills:wiki-maintenance:end -->` occur exactly once in order, replace only that complete marked block with the asset's contents. If neither exists, insert the block once after existing instructions. If markers are partial, duplicated or reversed, stop the merge and report the malformed section; do not append another block or truncate the file.
3. If `CLAUDE.md` exists and the runtime reads it separately, ensure one pointer saying `Read and follow AGENTS.md for project instructions and wiki maintenance.` Preserve its other rules; do not duplicate the whole policy or create a second source of truth. Resolve conflicting old maintenance instructions explicitly.
4. Preserve existing `wiki/goals.md` and `wiki/dashboard.md`. Bootstrap only missing views using the block's required fields; use normal wiki frontmatter, plain Markdown/wikilinks, explicit unknowns and a no-approved-goals state instead of guessed project objectives. Where the installed `wiki` skill is available, follow its `references/project-state.md` templates and evidence rules. Link both views from `wiki/index.md`; do not rename or replace `wiki/goals/` detail pages or the lint-health dashboard.
5. Verify the marked block occurs once, unrelated instruction text is unchanged, existing goals/manual notes remain, and links resolve. Apply setup a second time: it must produce no further changes or duplicate instruction pointers. Exercise one changed-content batch and confirm index/hot/log/dashboard agree, then an unchanged retry and confirm no extra content log entry.

These are AI-agent writing instructions, not a generator: no tooling writes `wiki/**` or `.raw/**`. Goal definitions remain human/project-authoritative; dashboard state is evidence-derived. The installer asset is self-contained so copying `kb-setup` alone retains the completion contract.

## 8. Verification checklist

```bash
ls .agents/skills/                                   # real dirs, no symlinks
for l in .claude/skills/*; do [ -f "$l/SKILL.md" ] || echo "BROKEN: $l"; done
git check-ignore -v .claude/settings.local.json      # still ignored
python3 .agents/skills/wiki-lint/scripts/run-lint.py                          # gates run
```

Skill registration is live: a newly linked skill becomes invocable in the same
session that created it. If one does not appear, the link is broken — check its
resolution rather than restarting.
