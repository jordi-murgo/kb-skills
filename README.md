# kb-skills

Upstream source of truth for the knowledge-base skills used by project vaults.

Projects consume these by copying them into `.agents/skills/`. Those copies are
forks: edit them freely for project needs, but changes that should benefit every
project belong **here**, then get propagated back out.

## Layout

Skills live at the repository root, one directory each:

```
kb-skills/
├── kb-config.example.yaml   ← per-project config template, self-documented
├── .env.example             ← credential/env-template for config macros
├── wiki/                    ← vault scaffolding + routing
├── wiki-ingest/             ← add a source to the vault
├── wiki-lint/               ← health check + deterministic gates
│   └── scripts/             ← run-lint.py + the five lint-*.py checks
├── wiki-query/  wiki-fold/  wiki-issues/  wiki-markdown/
├── wiki-semsearch/          ← hybrid BM25+vector vault search
│   └── scripts/
├── code-search/             ← self-contained lexical/vector source search
│   └── scripts/             ← code-search.py; data in .vault-meta/code-search/
├── wiki-vetting/            ← claim vetting (System One) + lifecycle audit
│   └── scripts/
├── wiki-sync/               ← stale file-linked claims vs git history
│   └── scripts/
├── wiki-git-ingest/         ← upstream changelogs/releases → claims
│   └── scripts/
├── ego-browser-research/    ← browser-rendered and session-bound source evidence
│   └── scripts/
├── vampirize/               ← evidence, project-fit and licence-gated reuse review
│   └── scripts/
├── save/  doc-pipeline/  autoresearch/  research-brief/
├── kb-setup/                ← wire a project's KB plumbing
├── kb-publish/              ← publish the vault to a GitLab Wiki
│   └── scripts/
├── kb-jira-sync/            ← refresh raw Jira data
│   └── scripts/
└── kb-m365-fetch/           ← pull mail / Teams / SharePoint
    └── scripts/
```

Scripts are **colocated with the skill that owns them**. A skill copied on its
own into a project stays functional.

## Installing into a project

```bash
npx skills add jordi-murgo/kb-skills
```

This copies the skills into your project's `.agents/skills/` and wires the
symlinks that make them discoverable. Verify they resolved; a symlink that
exists is not a symlink that works:

```bash
for l in .claude/skills/*; do [ -f "$l/SKILL.md" ] || echo "BROKEN: $l"; done
```

See `kb-setup/SKILL.md` for the full wiring procedure and its failure modes.

## Updating a vault from this upstream

`kb-skills` is canonical. Before synchronising, separate project adaptations
from improvements that every vault should receive: move the latter here first.
Then replace only the selected base skills. Replacing a skill directory, rather
than overlaying it, removes upstream files that no longer belong to the skill.

Do not apply the replacement loop to a vault with project-specific edits inside
a base skill. Review its diff first and preserve or upstream those edits. Leave
project-owned skills outside this selection untouched.

```bash
# Run from the kb-skills checkout. Set VAULT to the target vault root.
export VAULT=/path/to/project-kb

for entry in */SKILL.md; do
  skill=${entry%/SKILL.md}
  rm -rf "$VAULT/.agents/skills/$skill"
  cp -R "$skill" "$VAULT/.agents/skills/$skill"
done
```

The loop only replaces the base skill names present in this checkout.

Prove the copied skills match, then run the affected skill's smoke command from
the vault root:

```bash
for entry in */SKILL.md; do
  skill=${entry%/SKILL.md}
  diff -rq "$skill" "$VAULT/.agents/skills/$skill" || exit 1
done

(cd "$VAULT" && python3 .agents/skills/code-search/scripts/code-search.py doctor --json)
```

Commit the synchronization separately from vault content changes. This keeps
the upstream version and local adaptations reviewable.


## Keeping project state current

kb-setup installs one marked maintenance block into the project root `AGENTS.md`
(see [`kb-setup/assets/project-agents-block.md`](kb-setup/assets/project-agents-block.md)).
Every changed-content batch must update the navigation (`index.md`,
sub-indexes) plus the state views: `wiki/hot.md`, `wiki/log.md` and
`wiki/dashboard.md`. `wiki/goals.md` defines approved outcomes with observable
success criteria; `wiki/dashboard.md` records evidence-backed goal status, gaps
and next steps — it is project progress, not the lint-health view at
`wiki/meta/dashboard.md`. Schema, templates and evidence rules:
[`wiki/references/project-state.md`](wiki/references/project-state.md).

Setup preserves existing project instructions, goals and manual notes — it only
bootstraps missing state views. No-op/unchanged batches and raw-only fetches do
not duplicate log entries. No tooling generator is involved: these are
AI-agent writing instructions, nothing writes `wiki/**` or `.raw/**`
programmatically.

## Configuration

Nothing project-specific belongs in skill code. The complete reference is
[`kb-config.example.yaml`](kb-config.example.yaml) — every section and key is
self-documented there, including which skill consumes it. Copy it to
`kb-config.yaml` at the vault root and fill it in; the sections it covers:

| Section | Drives |
|---|---|
| `project` | name and keywords used to match project content |
| `embeddings` | `wiki-semsearch` and `code-search` — model, endpoint, semantic source directories, and derived index location |
| `code_search` | `code-search` — code source directories/extensions; inherits embeddings model/endpoint unless overridden; cache in `.vault-meta/code-search/` |
| `repos` | `wiki-git-ingest` — native Git remote URLs or an existing local clone |
| `jira` | `kb-jira-sync` — base URL, project key, output dir |
| `wiki_publish` | `kb-publish` — wiki repo, branch, VPN precondition |
| `m365` | `kb-m365-fetch` — modules, output paths, time window |

Credentials never live in this file. Jira reads `ATLASIAN_EMAIL` and
`ATLASIAN_API_KEY` from the environment or `.env.local`.

**Every path is relative to the vault root.** Absolute paths, `~`, and `..`
traversal are rejected — the scripts refuse to run rather than write outside the
vault. This is enforced rather than left to convention because the two runtimes
disagree: `Path("/vault") / "/etc/passwd"` yields `/etc/passwd` in Python, while
PowerShell's `Join-Path` yields `/vault/etc/passwd`. The same config file would
otherwise mean two different things depending on which pipeline read it. The
check runs before any network call.

Pipelines with an `enabled` flag refuse to run when it is false. Every script
names a missing required configuration key before doing work.

### Formats

Resolution order is `kb-config.yaml` → `.yml` → `kb-config.json`, and for
`kb-m365-fetch` a legacy flat `m365-config.json` last, so vaults migrate at
their own pace.

YAML needs a parser neither runtime ships with by default. Python raises a clear
error naming the install command; `graph-fetch.ps1` installs `powershell-yaml`
on demand, the same way it already installs the Microsoft.Graph modules — but it
has to do so *before* reading the config, not alongside the Graph modules
further down.

One gotcha worth keeping: `ConvertFrom-Yaml` returns `Hashtable`, while
`ConvertFrom-Json` returns `PSCustomObject`. The script enumerates config keys
through `.PSObject.Properties`, which on a `Hashtable` yields `Count`, `Keys`
and `Values` instead of the config keys — quietly collapsing the module set. The
YAML result is round-tripped through JSON so both formats produce the same
shape.

### Derived state

Indexes, remote mirrors, scan reports and process state live in `.vault-meta/`.
They are disposable and must not be committed; the directory's `.gitignore`
sentinel is the one exception, so every clone gets the same policy:

```gitignore
# Root .gitignore
.vault-meta/*
!.vault-meta/.gitignore
```

```gitignore
# .vault-meta/.gitignore
*
!.gitignore
```

See `kb-setup/SKILL.md` for the creation and verification commands.

## Research and reuse

Use the skill that matches the evidence and decision needed:

| Need | Skill | Boundary |
|---|---|---|
| Search configured source trees by text or embedding similarity | `code-search` | `doctor` before build/query; code lives in `.vault-meta/code-search/` |
| Capture rendered public pages or research a logged-in browser session | `ego-browser-research` | Ego Browser only; explicit user handoff for authentication |
| Decide whether an external repository, post, article or product is reusable | `vampirize` | Independent source, project-fit and rights analyses; no upstream code in reports |

`vampirize` may recommend a clean-room algorithm reimplementation. It does not
permit copying code, tests, identifiers, prose or assets without a passing
licence gate and the required notices.

## Two traps worth knowing

Both were live bugs, and both fail **silently** — which is what makes them worth
writing down.

**Repo-root resolution.** A script inside a skill cannot find the vault root by
counting `.parent` levels: its depth depends on where the skill is installed.
Walk up looking for a marker instead. Requiring `wiki/` alone is not enough —
there is a skill directory named `wiki`, so the walk stops at the skills
directory. Require `wiki/` **and** `.git`.

**Vacuous gates.** `run-lint.py` resolves its sibling checks relative to its own
file, and treats a check it cannot run as a failure. An earlier version resolved
them against a fixed path and returned a non-failing status when absent, so
relocating the scripts would have made every check skip while the gate still
exited 0 — green while testing nothing.

Before trusting any gate, prove it fails: inject a fault and confirm a non-zero
exit. A gate that has never failed has never been tested.

## Acknowledgements

The wiki core skills are derived from [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian),
an open-source AI agent plugin that turns Obsidian into a self-organizing AI
second brain, based on Karpathy's LLMWiki pattern.

| Upstream skill | This repo | Notes |
|---|---|---|
| `wiki` | `wiki/` | Scaffold, architecture, routing — Obsidian references removed |
| `wiki-ingest` | `wiki-ingest/` | Source ingestion — Obsidian references removed |
| `wiki-lint` | `wiki-lint/` | Health checks, deterministic gates |
| `wiki-query` | `wiki-query/` | Hot cache → index → pages query |
| `wiki-fold` | `wiki-fold/` | Log entry rollups |
| `wiki-issues` | `wiki-issues/` | Open-issues stack |
| `save` | `save/` | Save conversation/insight to vault |
| `autoresearch` | `autoresearch/` | Karpathy-style research loop |
| `research-brief` | `research-brief/` | Brief construction/audit for autoresearch |
| `doc-pipeline` | `doc-pipeline/` | Document → markdown conversion |
| `obsidian-markdown` | `wiki-markdown/` | Renamed; documents wiki markdown conventions (wikilinks, callouts, frontmatter) |

The `kb-*` skills (`kb-setup`, `kb-jira-sync`, `kb-m365-fetch`, `kb-publish`)
are original to this repo.
