# kb-skills

Upstream source of truth for the knowledge-base skills used by project vaults.

Projects consume these by copying them into `.agents/skills/`. Those copies are
forks: edit them freely for project needs, but changes that should benefit every
project belong **here**, then get propagated back out.

## Layout

Skills live at the repository root, one directory each:

```
kb-skills/
├── kb-config.example.json   ← per-project config template
├── wiki/                    ← vault scaffolding + routing
├── wiki-ingest/             ← add a source to the vault
├── wiki-lint/               ← health check + deterministic gates
│   └── scripts/             ← run-lint.py + the four lint-*.py checks
├── wiki-query/  wiki-fold/  wiki-issues/
├── save/  canvas/  defuddle/  doc-pipeline/  autoresearch/
├── research-brief/  obsidian-bases/  obsidian-markdown/  visualize/
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
cp -R /path/to/kb-skills/<skill> <project>/.agents/skills/<skill>
ln -sfn "../../.agents/skills/<skill>" "<project>/.claude/skills/<skill>"
```

The symlink is what makes the skill discoverable — `.claude/skills/` is the only
path Claude Code scans. Verify it resolves; a symlink that exists is not a
symlink that works:

```bash
for l in .claude/skills/*; do [ -f "$l/SKILL.md" ] || echo "BROKEN: $l"; done
```

See `kb-setup/SKILL.md` for the full wiring procedure and its failure modes.

## Configuration

Nothing project-specific belongs in skill code. Copy `kb-config.example.yaml` to
`kb-config.yaml` at the vault root and fill it in:

| Section | Drives |
|---|---|
| `project` | name and keywords used to match project content |
| `jira` | `kb-jira-sync` — base URL, project key, output dir |
| `gitlab_wiki` | `kb-publish` — wiki repo, branch, VPN precondition |
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

Each pipeline section has an `enabled` flag and refuses to run when it is false
or when a required key is missing, naming the key.

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
