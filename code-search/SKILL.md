---
name: code-search
description: "Search source code in the vault's projects: locate an implementation, find where something is defined or used, index a codebase. Local lexical/vector index over source files; returns paths and line ranges. Use wiki-search instead for Markdown wiki pages. Triggers on: buscar código, localizar implementación, indexar proyectos, code search, where is this implemented, find the code."
license: Apache-2.0
metadata:
  author: kb-skills
  version: "1.0"
argument-hint: "doctor | build | status | query TEXT"
---

# code-search

## Activation

Use this skill to locate implementation inside the vault's projects. Keep
Markdown knowledge queries in `wiki-search`; the two indexes are separate and
neither substitutes for the other.

## Rules

- Run `doctor --json` before relying on the index. If it reports missing
  dependencies, ask the user whether to install them: present the suggested
  commands and wait for authorization. Never install or download models
  automatically.
- Keep engine state self-contained in `.cache/code-search/`; the one authorized
  exception is CodeGraph's external graph at `<project>/.codegraph/`. Never
  write source code, `.raw/` or `wiki/`.
- Read scope, extensions and endpoint from the `code_search` section of
  `kb-config.yaml`. Include `.` only when explicitly configured. Respect
  `.gitignore` rules, including in submodules; do not follow symlinks.
- Send fragments only to the configured embeddings endpoint. Do not switch to
  an external provider when it fails.
- A line-based index is not a call graph and does not replace type analysis.
  Initialize CodeGraph only after explicit authorization for the chosen
  project: `init` builds the graph, it does not just create metadata. Exclude
  `.codegraph/` from Git in every repository or submodule; never build
  indiscriminate vault-wide graphs or initialize external `zg` indexes.

## Tool selection

| Need | Action |
|---|---|
| Locate an implementation by meaning | `query "TEXT" --mode vector` |
| Exact terms and concepts combined | `query "TEXT" --mode hybrid` |
| Lexical search without an endpoint | `query "TEXT" --mode bm25` |
| Definitions, types or references | Runtime LSP, when available |
| Syntactic patterns | Runtime AST, when available |
| Code relationships | An available, already-indexed CodeGraph; if absent, ask for authorization before installing or initializing the project |

If a needed companion tool is missing, ask whether the user wants to enable or
install it. Never present textual matches or CodeGraph heuristics as verified
typed references; confirm those with LSP or source evidence before refactoring.

## Execution

From the vault root:

```bash
python3 .agents/skills/code-search/scripts/code-search.py doctor --json
python3 .agents/skills/code-search/scripts/code-search.py build --json
python3 .agents/skills/code-search/scripts/code-search.py status --json
python3 .agents/skills/code-search/scripts/code-search.py query "RSA credential encryption" --mode vector --top 5 --json
```

1. Check dependencies and configuration with `doctor`; it installs nothing,
   makes no network calls, and does not diagnose CodeGraph, AST or LSP.
2. If the index is missing or `status.needs_build` is true, run `build`.
   Embedding errors keep the previous index; there is no silent fallback.
3. Query, then read the returned paths and line ranges before claiming how the
   code works.

## Result

Return files and line ranges, the effective mode, the evidence you read, and
coverage limits. When blocked, name the missing dependency or endpoint and the
authorization required.

## References

- [Configuration, storage and dependencies](references/configuracion.md).
- [Installing and using CodeGraph, AST and LSP for Java](references/herramientas-complementarias.md).
