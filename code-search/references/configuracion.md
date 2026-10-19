# Configuring code-search

The script can be copied together with the whole skill: it imports no modules
from other skills. It needs Python 3.10+, Git, and SQLite with FTS5. PyYAML is
only required when the configuration is YAML.

## Configuration

Add a section to the vault's `kb-config.yaml`:

```yaml
code_search:
  source_dirs:
    - projects
  # Optional: restrict languages and configuration; values replace the default list.
  # extensions: [.java, .xml, .yaml, .json, .toml, .sh]
  # Optional: inherit embeddings.model and embeddings.endpoint when omitted.
  # model: bge-m3
  # endpoint: ${WIKI_OLLAMA_URL}
```

| Key | Contract |
|---|---|
| `source_dirs` | Non-empty list of directories relative to the vault; defaults to `[projects]`. `.` includes the root only when stated. Absolute paths, `~`, `..`, missing directories and symlinks are rejected. |
| `extensions` | Non-empty list of suffixes including the dot. Defaults: `.java`, `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.go`, `.rs`, `.c`, `.h`, `.cpp`, `.hpp`, `.cs`, `.kt`, `.kts`, `.swift`, `.sh`, `.ps1`, `.sql`, `.yaml`, `.yml`, `.json`, `.xml`, `.toml`. Includes tests and configuration, not only production code. Markdown needs an explicit `.md`. |
| `model` | Explicit value → `embeddings.model` → `bge-m3`. |
| `endpoint` | HTTP(S) base URL, without credentials, query or fragment; `/api/embed` is appended. Explicit value → `embeddings.endpoint` → `http://127.0.0.1:11434`. |

`${VAR}` and `${VAR:-default}` macros expand recursively. Precedence: process
environment → `.env.local` → `.env`; those files only supply values to macros.
The `embeddings.source_dirs` selection does not change `code_search.source_dirs`.

The script resolves the root by looking for `wiki/` and `.git`, starting from the
working directory. To invoke it from outside:

```bash
python3 <skill>/scripts/code-search.py --root <vault> doctor --json
```

## Derived data and coverage

- The only location for the engine's own data is `.cache/code-search/index.db`.
  It holds no editable configuration and can be deleted to rebuild from YAML.
- Ensure `.cache/` is in `.gitignore`; never version fragments or derived vectors.
- Real Git rules apply, including negations and already-tracked files that match
  ignored patterns. Every nested repository or submodule uses its own Git rules.
- `.git` and `.cache` are never traversed; symlinks are not followed. Binary and
  non-UTF-8 files skipped are counted without showing their content.
- Line-based chunking with overlap, capped at 80 lines / 4000 characters.
  Individual longer lines are split while preserving their number; this is not
  AST parsing.
- `build` reuses embeddings for files whose SHA-256 did not change and removes
  deleted, ignored or config-excluded files. Changing model/endpoint requires
  regenerating vectors. An endpoint or transaction failure keeps the last valid
  index.
- `status` compares current configuration and content against the index. Read
  `files`, `chunks`, `embedded`, `dimensions`, `new_paths`, `stale_paths` and
  `needs_build`.

## Dependencies and consent

`doctor --json` is local: it writes nothing and does not contact the endpoint.
It returns `ok`, `dependencies`, `missing_dependencies` with
`name`/`install_hint`, and `errors`. If not ready, it exits with code 2.

When a dependency is missing, the agent must ask: "Missing <dependency>. Install
it with <suggested command>?" Do not run the command before authorization. Offer
to continue without that capability when it is not needed; never modify Python,
Git or SQLite automatically.

If the endpoint is unavailable, fix the configuration or ask before enabling a
service. Do not run `ollama pull`, do not download models and do not switch
providers on your own. BM25 can query an already-built index without an
endpoint; building a vector index does require one.

LSP, AST and CodeGraph are optional companions: the script does not install
them, and `doctor` does not diagnose their toolchains. It checks the basic
dependencies Git, SQLite/FTS5 and PyYAML when applicable, plus the
configuration. Consult CodeGraph if it is already available and indexed; if not,
request explicit authorization before installing it, modifying MCP permissions
or running `init` for the chosen project. `init` creates and builds the external
graph at `<project>/.codegraph/`, the authorized exception to engine state in
`.cache/code-search/`; it must be excluded from Git in every repository or
submodule. Never build indiscriminate vault-wide graphs. See
[installing and using companion tools](herramientas-complementarias.md).

## Local registration

Keep the skill in `.agents/skills/code-search/` and, for Claude Code, create the
relative bridge `.claude/skills/code-search -> ../../.agents/skills/code-search`.
If Gentle AI is present, refresh its index with `gentle-ai skill-registry refresh`.

A runtime may keep a skill list taken at session start. If `skill://code-search`
does not recognize it yet, load `.agents/skills/code-search/SKILL.md` by its real
path; do not reinstall the skill or rebuild the code index to work around that
runtime cache.
