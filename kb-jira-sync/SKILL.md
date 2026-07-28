---
name: kb-jira-sync
description: >
  Refresh the vault's raw Jira data from the Jira REST API, then reconcile the
  derived wiki issue pages against it. Triggers on: "sync jira", "import jira",
  "refresh jira issues", "update the tickets", "/kb-jira-sync".
allowed-tools: Read Edit Bash Grep Glob
---

# kb-jira-sync: Refresh Jira Source Data

Two distinct stages. Do not conflate them:

1. **Import** — `.agents/skills/kb-jira-sync/scripts/import-jira.py` refreshes `.raw/jira/` from the API.
2. **Reconcile** — the wiki pages under `wiki/jira-issues/` are *derived*, and
   the import does not touch them. They go stale silently.

---

## Stage 1: import

```bash
export ATLASIAN_EMAIL="your-email@example.com"
export ATLASIAN_API_KEY="<api token>"
python3 .agents/skills/kb-jira-sync/scripts/import-jira.py
```

Both variables are required — note the spelling, `ATLASIAN_*` with one `S`.

Everything else comes from the `jira` section of `kb-config.yaml` at the vault
root: `base_url`, `project_key` and `output_dir`. The script writes one
deterministic Markdown file per issue to `<output_dir>/<KEY>-N.md`.

It refuses to run and names the problem when `enabled` is false, the config is
missing, or a required key is absent — it never guesses a project key.

Never commit the API token. If the user pastes one into chat, use it for the run
and do not write it to any file.

## Stage 2: reconcile the wiki

After the import, diff `.raw/jira/` against `wiki/jira-issues/`:

- **Status drift** — issue closed or started in Jira but the wiki page still
  shows the old `status:`. Update the field and bump `updated:`.
- **New issues** — a `<KEY>-N.md` in `.raw/jira/` with no wiki page. Create one
  from `_templates/jira-issue.md`.
- **Canonical link** — every issue page carries its Jira URL. Preserve it.

Then update `wiki/index.md`, append to `wiki/log.md` (newest entry at the TOP),
and refresh `wiki/hot.md`.

---

## Verify, do not assume

`.raw/` is immutable: the import overwrites it, but you never hand-edit it.

After reconciling, run the gate:

```bash
python3 .agents/skills/wiki-lint/scripts/run-lint.py
```

Exit `1` means a check failed. A batch of new issue pages most often trips
`lint-orphans.py` — new pages nothing links to yet — which is a real finding, not
noise. Link them from the relevant domain or index page.

Watch for wikilink case: `[[proj-45]]` resolves, `[[PROJ-45]]` is reported
dead even though the wiki accepts both.
