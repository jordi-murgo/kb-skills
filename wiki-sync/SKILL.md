---
name: wiki-sync
description: "Detect wiki pages whose file-linked claims went stale after repo commits: scans for repo-file references, compares each file's last commit with the page's updated date. Triggers on: sync the wiki, what claims are stale, after commits check wiki, staleness report."
argument-hint: "[--since] [--commit]"
---

# wiki-sync: Git-Based Claim Staleness

Pages that describe repo files (scripts, configs, results) age silently. This reports the pages whose referenced files changed after the page's last `updated:`.

## Commands

```bash
python3 scripts/wiki-sync.py                 # full-history report
python3 scripts/wiki-sync.py --since         # only commits after the last sync point
python3 scripts/wiki-sync.py --commit        # report + write the sync point
python3 scripts/wiki-sync.py --json
```

State at `.vault-meta/sync.json`. Read-only for `wiki/**` and `.raw/**`.

## Workflow

1. Run a report; each stale row names page, file, commit date, page date.
2. For each: `git log -p --since=<page updated> -- <file>`, update the claims, bump `updated:`.
3. `--commit` to advance the sync point.

## Notes

- Detection is path-shaped (`dir/name.(py|ts|js|json|md|gguf|toml|yaml)`) filtered to files that exist; untracked files are skipped.
- Bare page dates are treated as UTC; a page with no parseable `updated:` is always reported stale — fix the frontmatter rather than suppressing the row.
