---
name: wiki-git-ingest
description: "Watch associated project repos (local clones or GitHub releases) and turn their new changelogs/releases into vetted wiki claims. Triggers on: what changed upstream, check watched repos, new releases of llama.cpp, ingest changelog, refresh upstream."
argument-hint: "repos | scan [--commit] [names...]"
---

# wiki-git-ingest: Upstream Changelog Ingest

The vault tracks not just this project but the projects it depends on. New commits/releases since the last scan become draft claims, shaped for `wiki-vetting`.

## Commands

```bash
python3 scripts/wiki-git-ingest.py repos                      # watched + last seen
python3 scripts/wiki-git-ingest.py scan [--json] [--commit] [names...]
```

The watch list is CONFIG, not state: the `repos` section of kb-config.yaml, map keyed by name, exactly one of `github: <owner>/<repo>` (releases feed, no key, no clone) or `path: <local clone>` (read-only, vault-relative allowed) per entry; optional `target_page` (default `wiki/x-radar/<name>.md`); `${VAR}` macros expand like everywhere else. Edit the yaml to watch/unwatch.

Local clones: `git log` + CHANGELOG.md version-diff, falling back to conventional commits (`feat`/`fix`/`perf`/`!`) since the last seen sha — claims quote changelog bullets verbatim, full four-axis vetting. GitHub repos: `releases.atom` — claims carry `source` (release URL) and `skip_grounded: true`.

Scan state (last seen sha/release) is derived, in `.vault-meta/repos.json` — deleting it only forces a full rescan. Remote entries are cheap; a repo without releases shows 0 new forever — watch it as a local clone for commit-level tracking.

## Workflow

1. `scan` (dry) → review.
2. Vet the interesting claims (`wiki-vetting`); write `accept`s into the `target_page` with `evidence: <source url>`.
3. `scan --commit` to advance the sync point — even for skipped claims, or they reappear.

## Notes

- Atom content is HTML-stripped; quotes truncate at 800 chars.
- On this machine the feed fetch is one HTTP GET per GitHub repo (~0.5 s); failures fail soft per repo.
