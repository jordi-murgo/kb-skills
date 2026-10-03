---
name: wiki-git-ingest
description: "Watch associated project repos (local clones or GitHub releases) and turn their new changelogs/releases into vetted wiki claims. Triggers on: what changed upstream, check watched repos, new releases of llama.cpp, ingest changelog, refresh upstream."
argument-hint: "repos | scan [--commit] | add NAME --github owner/repo | add NAME --path dir"
---

# wiki-git-ingest: Upstream Changelog Ingest

The vault tracks not just this project but the projects it depends on. New commits/releases since the last scan become draft claims, shaped for `wiki-vetting`.

## Commands

```bash
python3 scripts/wiki-git-ingest.py repos                      # watched + last seen
python3 scripts/wiki-git-ingest.py add NAME --github owner/repo [--target-page wiki/x-radar/x.md]
python3 scripts/wiki-git-ingest.py add NAME --path ./local-clone
python3 scripts/wiki-git-ingest.py remove NAME
python3 scripts/wiki-git-ingest.py scan [--json] [--commit] [names...]
```

Local clones: `git log` + CHANGELOG.md version-diff, falling back to conventional commits (`feat`/`fix`/`perf`/`!`) since the last seen sha — claims quote changelog bullets verbatim, full four-axis vetting. GitHub repos: `releases.atom`, no key, no clone — claims carry `source` (release URL) and `skip_grounded: true`.

State in `.vault-meta/repos.json`. Remote entries are cheap; add freely. A repo without releases shows 0 new forever — add it as a local clone for commit-level tracking.

## Workflow

1. `scan` (dry) → review.
2. Vet the interesting claims (`wiki-vetting`); write `accept`s into the `target_page` with `evidence: <source url>`.
3. `scan --commit` to advance the sync point — even for skipped claims, or they reappear.

## Notes

- Atom content is HTML-stripped; quotes truncate at 800 chars.
- On this machine the feed fetch is one HTTP GET per GitHub repo (~0.5 s); failures fail soft per repo.
