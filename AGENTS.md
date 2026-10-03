# AGENTS.md — Conventions for agents working in `kb-skills`

This repo is the upstream source of truth for the knowledge-base skills. Projects
consume copies; changes that benefit every project belong here.

## Layout rules

- One skill per directory at the repo root: `<skill>/SKILL.md` plus whatever the
  skill owns (`scripts/`, `references/`). **Scripts are colocated with the skill
  that owns them** — a skill copied on its own into a project stays functional.
  No shared import module between skills; a helper that more than one skill
  needs gets inlined, not imported (precedent: `wiki-lint/scripts/run-lint.py`).
- SKILL.md frontmatter: `name`, `description` with trigger phrases, optional
  `argument-hint`. Body: terse, imperative, commands first, rationale only
  where a future agent would otherwise break something.

## Hard rules

1. **Vault-root resolution**: scripts walk up for `wiki/` AND `.git` — never
   count `.parent` levels. Walk **cwd first, then `__file__`**: a script run
   from a project resolves that project even when it lives in another repo
   (this repo itself contains a `wiki/` *skill* directory plus `.git`, a false
   positive for the `__file__`-first walk — that cost a debugging round).
2. **Nothing project-specific in skill code**: models, endpoints, paths and
   watch lists come from `kb-config.yaml` at the vault root or environment
   variables, with boring defaults. Copy `kb-config.example.yaml` when adding a
   section, and keep credentials in env/`.env.local`, never in config.
   Scripts load vault-root `.env` then `.env.local` at startup (process env wins,
   later file wins; `load_env_files` is inlined per skill, never imported). Copy
   `.env.example` when wiring a vault.
3. **Gates must fail**: before trusting any lint/gate, inject a fault and
   confirm a non-zero exit. A gate that has never failed has never been tested.
4. **Python stdlib only** for the wiki tooling (urllib, json, sqlite3, hashlib,
   argparse, re). PyYAML is acceptable only for reading `kb-config.yaml`, with
   the install command in the error message (precedent: `kb-jira-sync`).
5. **Never write `wiki/**` or `.raw/**`** from tooling. Derived state goes to
   `.vault-meta/` — it is a cache and must be deletable at any time.

## System One decision endpoints (wiki-vetting, wiki-git-ingest)

- `questions` is a **record keyed by question id**, never an array; array
  bodies fail with a misleading JSON parse error.
- Instructions must name the item they judge (`Consider ONLY claim [cN]`) —
  question keys never reach the model, so unnamed questions smear answers
  across items (observed: every claim "sensitive" at p≈0.97).
- Event-type claims (releases, commits) take `skip_grounded` + `source`: the
  feed is the provenance, not a quote. Content claims keep the full axes.
- Caps: 64 questions per request, 64 KiB body, batch ≤16 claims.

## Embeddings (wiki-semsearch)

Index at `.vault-meta/sem/index.db` is derived: deleting it must always be
safe. Model/endpoint from env (`WIKI_SEM_MODEL`, `WIKI_SEM_ENDPOINT`, shared
`WIKI_OLLAMA_URL`) with defaults `bge-m3` at `http://127.0.0.1:11434`. Embedding dimensions read from
SQLite `meta` come back as **strings** — cast before comparing (this bug
survived one fix; it had two call sites).

## Measurement notes worth keeping

- System One prefill on an iGPU costs ~1.4 s per 1k state tokens; keep states
  small (≤6k tokens) or requests blow both the client timeout and the host
  agent's context-handler limit.
- A 35-page vault re-embeds from scratch in ~6 s; incremental rebuilds are
  sub-second. Do not build caching layers on top of the index.

## When porting from project forks

Scripts developed inside a project (e.g. `scripts/wiki_*.py`) arrive assuming
`parent.parent` is the vault root and hardcoded local models. Porting means:
root walk (cwd-first), env/config-driven settings, and re-verification **from
the repo against a real vault** before committing — syntax-checking is not
porting.
