---
name: kb-gmail-fetch
version: 1.0.0
description: |
  Pull the PERSONAL @gmail.com mailbox into .raw/gmail/ via the Google Workspace
  CLI (`gws`) with Gmail search syntax, so personal-mail material (hacker
  congresses, CFP calls, community threads) is captured alongside — never
  instead of — the corporate mailbox handled by kb-m365-fetch.
  Triggers on: "fetch personal gmail", "pull gmail", "sync gmail",
  "gmail fetch", "correo personal", "/kb-gmail-fetch".
allowed-tools:
  - Read
  - Bash
  - Grep
  - Glob
license: MIT
metadata:
  author: jpmo
  version: 1.0.0
---

# kb-gmail-fetch: Pull the Personal Gmail Mailbox into `.raw/`

Companion to `kb-m365-fetch`, not a replacement. The two cover different
mailboxes and must both run when the goal is complete brand coverage:

| Skill | Mailbox | Provider | Auth |
|---|---|---|---|
| `kb-m365-fetch` | GFT corporate (`@gft.com`) | Microsoft Graph | Entra ID delegated |
| **`kb-gmail-fetch`** | Personal (`@gmail.com`) | Gmail API via `gws` | Google OAuth, system keyring |

Why they are separate skills and not one flag: the identity providers share
nothing (Entra ID vs Google), the scopes are unrelated, and the material is
disjoint. Congress invitations, CFP calls and community threads arrive at the
personal address; project and corporate threads arrive at GFT. A single script
that "does both" would need two independent consent flows and would fail in the
middle of one when the other's token expires.

## Primary: `scripts/gmail-fetch.ps1`

```bash
# config defaults (keywords from kb-config.yaml, gmail section)
pwsh -File .agents/skills/kb-gmail-fetch/scripts/gmail-fetch.ps1

# ad-hoc Gmail query, window auto-appended
pwsh -File .agents/skills/kb-gmail-fetch/scripts/gmail-fetch.ps1 -Query "from:rootedcon"

# count-only, writes nothing
pwsh -File .agents/skills/kb-gmail-fetch/scripts/gmail-fetch.ps1 -Query "rfc822msgid: is:unread" -DryRun

# widen the window and cap
pwsh -File .agents/skills/kb-gmail-fetch/scripts/gmail-fetch.ps1 -Days 730 -Limit 500
```

| Parameter | Meaning |
|---|---|
| `-Config <path>` | Alternate `kb-config.yaml`. Defaults to the vault root. |
| `-Query <gmail-q>` | Raw Gmail query. Overrides config keywords. `after:`/`before:` are appended if absent. |
| `-Days <n>` | Look-back window. Config: `gmail.window.days` (365). |
| `-Limit <n>` | Max messages inspected. Config: `gmail.window.max_messages` (300). |
| `-DryRun` | Print matches, write nothing. |

## Configuration: `kb-config.yaml`, section `gmail`

```yaml
gmail:
  enabled: true
  keywords: [rootedcon, defcon, "call for papers", ...]
  window:
    days: 365
    max_messages: 300
    page_limit: 10
  body: snippet        # snippet | full
  output:
    dir: .raw/gmail
```

`keywords` are OR-joined into a single Gmail query and then scoped with
`after:<window.days>`. `body` controls whether each message is fetched as
`metadata` (headers + snippet, cheap) or `full` (base64url bodies decoded).

## Prerequisites

```bash
brew install googleworkspace-cli      # provides `gws` (0.22.5 verified)
gws auth login                        # one-time, stores the token in the system keyring
pwsh -Command 'Install-Module powershell-yaml -Scope CurrentUser -Force'   # if missing
```

Verify the session before trusting a run:

```bash
gws gmail users getProfile --params '{"userId":"me"}'
```

It must print your own address. A wrong or empty address means the keyring holds
another account's token — `gws auth logout` then log in again.

## Facts learned the hard way

### `gws` writes its banner to STDERR, data to STDOUT

Every call prints `Using keyring backend: keyring` before the JSON. It goes to
stderr, so piping stdout into a JSON parser is safe and the banner never
corrupts it. Do not merge the streams with `2>&1` before parsing.

### `--page-all` emits NDJSON, not one JSON document

Each line is a complete page object with its own `nextPageToken`. Parse line by
line; `ConvertFrom-Json` on the whole output fails. `--page-limit` defaults to
**10**, which silently truncates a broad query — a 201-match search stops at 500
results across 10 pages of 50, so set `page_limit` explicitly when the window
matters.

### A bare keyword query is not a scoped query

`--params '{"q":"rootedcon"}'` searches the **entire** mailbox (367,078 messages
on the account this skill was built against). Always pair a keyword with
`after:`/`before:`; the script appends `after:` automatically, which is why the
`-Query` override still gets a window.

### `q` cannot be combined with gmail.metadata scope

Gmail's own schema says the query parameter "cannot be used when accessing the
api using the gmail.metadata scope". If searches return `400` with a query
present but work without it, the authorized scope is metadata-only — re-run
`gws auth login` and grant `gmail.readonly`.

### Message bodies are base64url, and padding matters

`payload.body.data` is URL-safe base64. Replace `-`→`+` and `_`→`/` and
re-pad to a multiple of four before `FromBase64String`, or short bodies throw.
Multipart messages nest: walk `parts` recursively and prefer `text/plain` over
stripped HTML.

### Dedup lives in a ledger, not in the filename

`.cache/gmail/seen.json` maps message id → fetch timestamp. Without it, every
run re-writes every still-matching message under a new timestamp because Gmail
ids are stable but the output filename is not. Re-running with no new mail must
print "nothing new to write" and exit 0 — that is the correctness test.

### Never scan the whole mailbox

367k messages is not a corpus, it is the account. Every path through this script
requires either keywords or `-Query`, and both get a window. The guard exits `3`
when both are empty.

### YAML silently drops quotes, and that returns an empty result without an error

`- "call for papers"` in `kb-config.yaml` arrives as the string `call for papers`.
OR-joining it unquoted produces `(rootedcon OR ... OR call for papers OR cfp OR ...)`,
which Gmail parses as bare terms with broken grouping and answers
`{"resultSizeEstimate": 0}` — **no error, no messages**. The script re-quotes any
keyword containing a space before building the query. Verified: the same keyword
set returned 0 messages unquoted and 40 quoted.

If a full run matches nothing while a single `-Query "rootedcon"` works, suspect
this before suspecting the token or the window.

### Broad AI keywords match newsletters, not people

`mcp`, `agentic` and `ai-sec` match every newsletter that mentions them (Ollama,
tl;dr sec, WIRED, Postman, Langfuse...). With `body: snippet` that is the bulk of
what comes back. Use `exclude.senders` for the recurring offenders, or run those
keywords with `-Query "from:<someone>"` when the goal is a specific
correspondent rather than topical coverage.

### `resultSizeEstimate` is not a count

Gmail returns `201` for almost any non-trivial query; it is a saturation flag,
not a total. Never report it as "N messages found" — count the `messages` array.

## After fetching

`.raw/` is immutable — never edit what the script wrote. Hand off to
`wiki-ingest`. `.cache/gmail/seen.json` is derived state and safe to delete; doing
so only causes the next run to re-capture what it matches.
