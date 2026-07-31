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

**The AI agent only discovers `.claude/skills/`.** `.agents/skills/` is never
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

## 6. `kb-config.yaml` — creating and maintaining it

One file at the vault root configures every pipeline. **No project-specific
value belongs in skill code**: if you find yourself editing a script to change a
URL, a project key, or an output directory, that value is missing from the
config.

### Creating one

```bash
cp <kb-skills>/kb-config.example.yaml kb-config.yaml
```

Then fill the sections you actually use and leave the rest with `enabled: false`.
A disabled pipeline is inert — its skill reports that it is off and stops,
rather than half-running.

```yaml
project:
  name: myproject
  keywords: [myproject, the terms that identify it in mail and chat]

jira:
  enabled: true
  base_url: https://yourorg.atlassian.net
  project_key: PROJ
  output_dir: .raw/jira
```

### Four rules

1. **Paths are relative to the vault root.** Absolute paths, `~`, and `..`
   traversal are rejected, and the check runs before any network call. This is
   enforced because the runtimes disagree: `Path("/vault") / "/etc/passwd"`
   yields `/etc/passwd` in Python, while PowerShell's `Join-Path` yields
   `/vault/etc/passwd`. Unenforced, one file would mean two things.
2. **No credentials, ever.** Jira reads `ATLASIAN_EMAIL` and `ATLASIAN_API_KEY`
   from the environment or `.env.local`. The config is committed; those are not.
3. **`enabled` is the on switch.** Every pipeline section has one, defaulting to
   false in the template.
4. **Missing keys stop the run.** Scripts name the key they need rather than
   guessing a default.

### Formats

Resolution order: `kb-config.yaml` → `kb-config.yml` → `kb-config.json`, and for
`kb-m365-fetch` a legacy flat `m365-config.json` last. A project can sit on JSON
indefinitely; YAML is preferred only because it carries real comments.

YAML needs a parser neither runtime ships with. Python raises an error naming the
install command; `graph-fetch.ps1` installs `powershell-yaml` on demand, the way
it already installs the Microsoft.Graph modules.

### Maintaining it

Adding a value to a pipeline means three edits, not one: the script that reads
it, `kb-config.example.yaml`, and the section table above. A key that exists in
one project's config and nowhere in the template is a key the next project will
never discover.

Validate after editing — YAML fails at the first wrong indent, and a config that
parses can still be wrong:

```bash
python3 -c "import yaml;print(yaml.safe_load(open('kb-config.yaml')).keys())"
python3 <skill>/scripts/<script>.py --dry-run   # where the script offers one
```

Common failures and what they mean:

| Message | Cause |
|---|---|
| `has no '<section>' section` | section missing or misindented at top level |
| `must be relative to the vault root` | absolute path or `~` in a path value |
| `escapes the vault root` | `..` traversal in a path value |
| `<section>.enabled is false` | pipeline off, not broken |
| `is missing: <key>` | required key absent |

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
