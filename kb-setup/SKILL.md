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

### `.vault-meta/`: derived state, tracked policy

Create this directory before any skill builds an index, mirror, scan state, or
report. It is an entirely disposable local cache, but its **ignore policy** must
travel with the vault:

```bash
mkdir -p .vault-meta
cat > .vault-meta/.gitignore <<'EOF'
# Derived vault state is local and may be deleted at any time.
# Keep only this sentinel tracked; ignore caches, indexes, mirrors, logs and PID files.
*
!.gitignore
EOF
```

Do **not** ignore `.vault-meta/` outright in the root `.gitignore`: Git then
will not descend into the directory, so it cannot track the sentinel. If an
older vault has that rule, replace it with:

```gitignore
.vault-meta/*
!.vault-meta/.gitignore
```

Prove both halves before committing:

```bash
git check-ignore -q --no-index .vault-meta/index.db
! git check-ignore -q --no-index .vault-meta/.gitignore
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
looking for a marker instead, and require **both** `wiki/` and `.git`: there is
a skill directory named `wiki`, so the `wiki/` marker alone stops the walk at
the skills directory rather than the vault root.

`.agents/skills/wiki-lint/scripts/run-lint.py` aggregates the deterministic
checks and resolves its sibling `lint-*.py` scripts relative to its own file.

```bash
python3 .agents/skills/wiki-lint/scripts/run-lint.py           # human report
python3 .agents/skills/wiki-lint/scripts/run-lint.py --json    # pipelines
```

Exit `1` if any check fails. Checks: `lint-dead-links.py`, `lint-orphans.py`,
`lint-frontmatter.py`, `lint-contradictions.py`.

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
| Jira | `kb-jira-sync` | `jira` + `ATLASIAN_*` env |
| Wiki publish | `kb-publish` | `wiki_publish` |
| Atlassian discovery (optional) | `twg` CLI | — (reads own auth) |

All of them write to `.raw/`, which is immutable. `wiki-ingest` turns `.raw/`
into wiki pages. Nothing else writes to `wiki/` from a pipeline.

### 5.1 `twg` CLI — Atlassian discovery (optional)

The [`twg` CLI](https://developer.atlassian.com/cloud/twg-cli/) provides graph
traversal, semantic search (Rovo), and context discovery across Atlassian
Cloud (Jira, Confluence, Bitbucket, Goals, Assets). It is an optional
accelerator for `kb-jira-sync`'s reconcile phase — not a dependency.

**Installation:**

```bash
# Install twg (one-time, per machine)
curl -fsSL https://teamwork-graph.atlassian.com/cli/install | bash

# Verify
twg doctor
```

**Project-local credentials with `TWG_CONFIG_DIR`:**

TWG supports `TWG_CONFIG_DIR` to store credentials inside the project
directory (gitignored) instead of the global `~/.config/twg/`. This isolates
per-project Atlassian sessions and enables multi-site/multi-tenant workflows
— each project can authenticate against a different Atlassian site without
colliding with the global config.

Setup (one-time, interactive — opens browser):

```bash
# 1. Create the project-local TWG config directory
mkdir -p .twg

# 2. Login against the project's Atlassian site
TWG_CONFIG_DIR=.twg twg login --site <site-prefix>

# 3. Verify
TWG_CONFIG_DIR=.twg twg doctor
```

Then add to `.gitignore`:

```gitignore
# TWG CLI project-local credentials (OAuth tokens)
.twg/
```

And add to `.env.local` (already gitignored):

```bash
# TWG CLI — project-local Atlassian credentials
TWG_CONFIG_DIR=.twg
```

After that, all `twg` commands pick up `TWG_CONFIG_DIR` from the environment
automatically. No `kb-config.yaml` section needed — TWG reads its own auth
from the config dir and resolves the site from the stored credentials.

**Verify it works:**

```bash
command -v twg >/dev/null 2>&1 && [ -f .twg/auth.conf ] && echo "twg ready" || echo "twg not found or not authenticated"
twg jira workitem get <PROJECT-KEY>-1   # should return issue JSON
twg rovo list-apps                      # should list connected apps
```

**Multi-site/multi-tenant:** each project gets its own `.twg/` with
credentials for its Atlassian site. The global `~/.config/twg/` is never
touched. To work against a different site, `cd` to that project and its
`.twg/` credentials are used automatically.

**What it adds:** see `kb-jira-sync` → Stage 0 — TWG Discovery.

**When to skip it:** if the project has no Atlassian Cloud connection, or if
the flat REST API import is sufficient. TWG is a discovery tool, not a
pipeline — it does not write to `.raw/` or `wiki/`.

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
| `embeddings` | `wiki-semsearch` | Optional; defaults are local |
| `code_search` | `code-search` | Opcional; índice independiente de código |
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
  db: .vault-meta/sem/index.db
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
`wiki-semsearch build` fail.

`code_search` indexa código y configuración, no Markdown por defecto. Admite
`source_dirs`, `extensions`, `model` y `endpoint`; modelo y endpoint heredan
`embeddings` si se omiten. Todos sus datos van a `.vault-meta/code-search/`.
Respeta las reglas Git de cada submódulo. Instala el skill completo y ejecuta
`python3 .agents/skills/code-search/scripts/code-search.py doctor --json`;
si faltan dependencias, pregunta al usuario antes de instalarlas. No descarga
modelos ni inicia servicios por su cuenta.

### Rules and defaults

1. **Paths are relative to the vault root.** Absolute paths, `~`, and `..`
   traversal are rejected before network or filesystem writes.
2. **No credentials, ever.** Jira reads `ATLASIAN_EMAIL` and `ATLASIAN_API_KEY`
   from the environment or `.env.local`; macros such as `${WIKI_OLLAMA_URL}`
   receive values from the process, `.env`, then `.env.local`.
3. **`enabled` only applies to pipeline sections** — `jira`, `wiki_publish`,
   and `m365`. `embeddings`, `code_search`, `decisions`, and `repos` are optional
   sections without an `enabled` switch.
4. **Defaults are consumer-specific.** Omit an optional section to use its
   documented defaults; when an enabled pipeline needs a value, its script
   stops and names the missing key instead of guessing.

### GitLab publisher compatibility

`wiki_publish` is the canonical publishing key. Existing vaults that run the
older `deploy-gitlab-wiki.py` or `push.sh` still require a separate
`gitlab_wiki` section with `enabled`, `target`, `repo`, `branch`,
`vpn_required`, `vpn_host`, and `vpn_private_prefix`. It is not an alias for
`wiki_publish`; do not rename an existing GitLab deployment's key until those
scripts are reconciled.

### Formats and validation

Resolution order: `kb-config.yaml` → `kb-config.yml` → `kb-config.json`; M365
also has a separate legacy flat `m365-config.json` fallback. YAML needs PyYAML;
the Python scripts print the installation command if it is absent.

Adding a supported key means updating its reader, `kb-config.example.yaml`, and
this schema. Do not copy obsolete flat M365 keys such as `m365.output_dir`:
the Graph reader consumes the nested `m365.output.*` mapping shown above.

```bash
python3 -c "import yaml; print(yaml.safe_load(open('kb-config.yaml')).keys())"
python3 .agents/skills/wiki-semsearch/scripts/wiki-semsearch.py status
```

| Message | Cause |
|---|---|
| `must be relative to the vault root` | Absolute path, `~`, or `..` in a path value |
| `configured source directory not found` | A `source_dirs` entry does not exist |
| `<section>.enabled is false` | Pipeline is off, not broken |
| `is missing: <key>` | Required value for an enabled pipeline is absent |

## 7. Verification checklist

```bash
ls .agents/skills/                                   # real dirs, no symlinks
for l in .claude/skills/*; do [ -f "$l/SKILL.md" ] || echo "BROKEN: $l"; done
git check-ignore -v .claude/settings.local.json      # still ignored
python3 .agents/skills/wiki-lint/scripts/run-lint.py                          # gates run
```

Skill registration is live: a newly linked skill becomes invocable in the same
session that created it. If one does not appear, the link is broken — check its
resolution rather than restarting.
