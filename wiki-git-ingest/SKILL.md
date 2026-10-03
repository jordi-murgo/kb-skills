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

The watch list is CONFIG, not state: the `repos` section of kb-config.yaml, map keyed by name, value = **a git remote URL** — the same string git understands in `.git/config`, any forge (GFT GitLab, Telxius GitLab, Azure DevOps, Bitbucket, GitHub, ...). No APIs, no tokens: repos are tracked with git itself and YOUR credentials — a blobless bare mirror under `.vault-meta/remotes/<name>.git`, cloned once, fetched each scan; new tags (name, date, annotated-tag subject) become claims. For a repo already on this machine use a `{path: <clone>}` mapping instead (changelog diff + conventional commits). Optional `{remote: ..., target_page: ...}` mapping form; `${VAR}` macros expand like everywhere else. Edit the yaml to watch/unwatch.

Scan state is derived, in `.vault-meta/` — deleting it forces a full rescan/re-clone. A repo that tags nothing shows 0 new forever; give it tags or a local `path:`.

## Workflow

1. `scan` (dry) → review.
2. Vet the interesting claims (`wiki-vetting`); write `accept`s into the `target_page` with `evidence: <source url>`.
3. `scan --commit` to advance the sync point — even for skipped claims, or they reappear.

## Notes

- Atom content is HTML-stripped; quotes truncate at 800 chars.
- On this machine the feed fetch is one HTTP GET per GitHub repo (~0.5 s); failures fail soft per repo.
