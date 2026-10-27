# Installing and using companion tools

The self-contained `code-search` engine stays in `.cache/code-search/`. CodeGraph, AST and LSP add different capabilities; they are not requirements for lexical/vector search and are not installed by the script. Its `doctor --json` checks Git, SQLite/FTS5, PyYAML when applicable and the basic configuration: **it does not diagnose these optional toolchains**.

## Choosing the capability and getting consent

| Need | Companion | Limit |
|---|---|---|
| Symbols and call relationships | CodeGraph | Requires a project graph; does not replace type analysis |
| Java or other syntax patterns | Runtime AST or ast-grep | Structural matches, not type-resolved references |
| Java definitions, types, references and diagnostics | LSP client with Eclipse JDT LS | Requires a registered server and a correctly imported project |

Reuse the capabilities already available. If one is missing, explain what you will install, where it will write and which configuration will change; ask the user and wait for authorization. Keep the consents separate: installing the binary, modifying MCP/permissions, and indexing a project are three different decisions. Never use silent installs, an `npx` that downloads packages unexpectedly, or `codegraph install --yes`/`-y`.

## CodeGraph: installing the binary

### Recommended option: pinned, reviewed standalone bundle

For macOS/Linux the bundle ships its own Node and needs no Node/npm install. This guide pins **v1.2.0**: it does not use `main`, `latest`, or a download piped into a shell.

1. Present the official installer download and its version; never download on the user's behalf without saying so. Save the script to a temporary directory and review it without running it:

   ```bash
   tmp_dir="$(mktemp -d)"
   script="$tmp_dir/install.sh"
   curl -fsSL \
     https://raw.githubusercontent.com/colbymchenry/codegraph/v1.2.0/install.sh \
     -o "$script"
   less "$script"
   ```

2. Before running it, explain its effects and get explicit approval. By default it downloads the bundle from GitHub Releases into `~/.codegraph/versions/v1.2.0`, updates `~/.codegraph/current`, and links the executable at `~/.local/bin/codegraph`. **It replaces that version directory and removes earlier bundles.** Also check any existing `CODEGRAPH_INSTALL_DIR` and `CODEGRAPH_BIN_DIR`, which change the destinations. It does not create a project graph or wire MCP by itself.

3. Only after approval, run it in the same terminal and remove the temporary directory:

   ```bash
   env CODEGRAPH_VERSION=v1.2.0 sh "$script"
   rm -f "$script"
   rmdir "$tmp_dir"
   ```

   If the download or install fails, keep the error and still remove the temporary directory; never silently switch version or method.

4. Add the executable directory to this terminal's `PATH`. To make it persistent, agree on the shell startup-file change first:

   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   command -v codegraph
   env DO_NOT_TRACK=1 codegraph --version
   ```

   With the default destinations it must resolve `~/.local/bin/codegraph` and print version `1.2.0`. If another copy appears, check `PATH` precedence; never uninstall anything automatically.

The [official v1.2.0 installer](https://raw.githubusercontent.com/colbymchenry/codegraph/v1.2.0/install.sh) defines the destinations, download and bundle removal. The [official README for that version](https://github.com/colbymchenry/codegraph/blob/v1.2.0/README.md) explains the bundle and the integration steps.

### Alternative: global npm install

If the user chooses npm and authorizes the install:

```bash
npm install -g @colbymchenry/codegraph@1.2.0
```

The [official v1.2.0 manifest](https://github.com/colbymchenry/codegraph/blob/v1.2.0/package.json) declares Node **`>=20.0.0 <25.0.0`** for the npm package. Do not read the README's general statement about Node as permission to ignore that range. Check the machine's Node before choosing npm; if it does not fit, propose the standalone bundle instead of changing the user's Node. This alternative also does not configure MCP or build graphs.

## CodeGraph: integrate, index and query

### Three different operations

| Operation | Effect | Authorization |
|---|---|---|
| Standalone installer or npm | Installs the binary and its runtime | Installation and destinations explained |
| `codegraph install` | Configures selected agents: MCP, instructions and permissions | Concrete agent/config changes, with no `--yes` or `-y` |
| `codegraph init [path]` | Creates `.codegraph/` and builds the initial code graph | Project and scope explicitly chosen |

You can query with the CLI without configuring MCP. If integration is authorized, run `codegraph install` interactively, review the proposed agents and permissions, and accept no additional changes without consent.

**Telemetry:** CodeGraph v1.2.0 enables it by default; skipping the interactive installer is not the same as disabling it. Per its [official documentation](https://github.com/colbymchenry/codegraph/blob/v1.2.0/TELEMETRY.md), it sends anonymous command and indexing statistics — no code, paths, names or queries. Explain that network egress before using it. The examples below use `DO_NOT_TRACK=1` to disable it; `CODEGRAPH_TELEMETRY=0` is another option, and `codegraph telemetry off` persists the choice. An MCP client must receive the same decision in its server environment, through that client's documented configuration.

### Graph scope and storage

Prefer each project root over an indiscriminate vault-wide graph. Choose the path with the user and explain that `init` **indexes immediately**: it is not an empty registration and does not need a later `build` to start. Never force initialization of the home directory or the filesystem root.

`<project>/.codegraph/` is allowed as the authorized exception to `.cache/code-search/`: it holds CodeGraph's external data, not the script's own index. Never version it. Before initializing, verify that the chosen repository's or submodule's `.gitignore` includes:

```gitignore
.codegraph/
```

Do not rely only on the vault's `.gitignore` to protect a submodule. `init` also generates an internal `.gitignore` for the data directory; keep the exclusion explicit in every target repository.

### Example with a project path

From the vault root, replace the example project with the one that was authorized. Checking status does not create the graph:

```bash
env DO_NOT_TRACK=1 codegraph status projects/lambda-cifrado-credenciales --json
```

If it is missing, request authorization for this path before running:

```bash
env DO_NOT_TRACK=1 codegraph init projects/lambda-cifrado-credenciales
```

Once indexed, query symbols and read the returned evidence:

```bash
env DO_NOT_TRACK=1 codegraph query CredentialCipher \
  --path projects/lambda-cifrado-credenciales --limit 5 --json
```

`status` reports state and statistics; `query` searches indexed symbols. A query with no matches does not prove an implementation is absent, and a missing-graph error does not authorize `init`. Do not assume a watcher is running: check status and agree on any refresh needed. The runtime's native CodeGraph tool operates only on the **current workspace** and accepts no project path; the CLI does allow choosing one. Never use the native vault tool as if it queried a submodule automatically.

## AST: syntactic queries, optional install

If the runtime already offers AST/ast-grep for Java, use it without installing another CLI. Give the native device the language `java`, the project path and the pattern `Cipher.getInstance($ALGORITHM)`. This pattern has no equivalent in `codegraph query`: they are separate capabilities.

To get the external CLI, choose **one** option and get authorization before running it:

```bash
# macOS con Homebrew
brew install ast-grep

# npm alternative
npm install -g @ast-grep/cli

# Rust/Cargo alternative
cargo install ast-grep --locked
```

Query example, without rewriting sources:

```bash
ast-grep --pattern 'Cipher.getInstance($ALGORITHM)' --lang java \
  projects/lambda-cifrado-credenciales
```

Single quotes stop the shell from expanding `$ALGORITHM`; the metavariable represents a syntax node. Use the name `ast-grep`, not `sg`, which on Linux may be a different program. Read the results as calls with that shape, not as proof of `Cipher` symbol resolution or of the argument's type. The [official ast-grep guide](https://ast-grep.github.io/guide/quick-start.html) documents installation, patterns and quoting.

## Java LSP: JDT LS needs a client

Eclipse JDT Language Server requires **Java 21 or newer to run the server**. That runtime is independent of an application's Java 17 target: do not change its `pom.xml`, toolchain or compile level to start the server. Use the server's JDK through its `JAVA_HOME`/`PATH` and leave the project's Java configuration alone.

On macOS, one authorized install option is:

```bash
brew install jdtls
```

The [official Homebrew formula](https://formulae.brew.sh/formula/jdtls) documents the command and its dependencies. For other systems, see the distribution options of the [official Eclipse JDT LS project](https://github.com/eclipse-jdtls/eclipse.jdt.ls).

Installing the executable **does not register a server in the runtime**. Configure the actual LSP client following its documentation: `jdtls` executable, stdio transport, Java 21+ environment, absolute project root and configuration/workspace arguments. Do not invent omp configuration keys or present the server as a shell search command.

Reserve absolute paths under the vault, distinct per project:

- `<absolute-vault>/.cache/code-search/lsp/<project>/configuration`
- `<absolute-vault>/.cache/code-search/lsp/<project>/workspace`

Example of the command **the LSP client** should launch, not a search you run by hand:

```bash
jdtls \
  -configuration /absolute/path/to/vault/.cache/code-search/lsp/my-project/configuration \
  -data /absolute/path/to/vault/.cache/code-search/lsp/my-project/workspace
```

Replace the example root with the real path before registering the server. `-configuration` isolates the user's configuration state; `-data` stores workspace metadata and must be unique per project. Keep `.cache/` excluded from Git. For stdio, the client must not define socket connection variables such as `CLIENT_PORT`/`CLIENT_HOST`.

After an authorized configuration, ask the client for status/diagnostics and confirm it imported the Maven/Gradle project and resolved dependencies before treating references or types as complete. Evidence must come from LSP definition, reference or diagnostic operations, not textual matches. The search engine's `doctor` does not certify this registration or JDT LS health.

## Registering a nested Maven project in OMP

OMP reads servers from `<cwd>/.omp/lsp.json`. Use the `servers.jdtls` entry; the initial `rootMarkers` filter looks at the working directory and does not search child projects automatically. For this vault layout, include both `pom.xml` and `projects/<your-project>/pom.xml`: the second activates the server from the vault, and the first lets it pick the Maven root when a Java file from the submodule is opened. Adapt the name per project.

- Set `fileTypes: [".java"]` and `languageId: "java"`.
- To pin the JDK only in the child process, set `command: "/usr/bin/env"` and start `args` with `JAVA_HOME=<absolute-server-JDK-path>` followed by the real `jdtls` path. Do not add a nonexistent `env` field to the schema or change the global `JAVA_HOME`.
- Pass `--jvm-arg=-Djava.import.generatesMetadataFilesAtProjectRoot=false` to JDT LS. In the verified version this guard is a JVM property; setting only a client-side setting does not keep Eclipse metadata out of the sources.
- Pass `-configuration` and `-data` with the absolute cache paths given above. Set `java.autobuild.enabled=false` in both `initOptions.settings` and `settings`, so it arrives before the project is imported and does not build the application automatically.
- If Maven is isolated, keep its editable configuration in `.omp/maven-settings.xml`, outside `.cache/`, and point `java.configuration.maven.globalSettings` at it. Its `localRepository` may live in `.cache/code-search/lsp/<project>/maven-repository`. Reference user settings when needed, without copying credentials or modifying them. The cache must be deletable without losing configuration.

After saving, run the LSP tool with `{"action":"reload","file":"*"}`. Verify a cross-file definition, real references, resolution of a dependency, and diagnostics on production files. Confirm the imported project keeps its original Java level; do not change the POM or run Maven tests/builds as part of activating the server.

## CodeGraph coverage and precision

Do not mistake `reindexRecommended=false` for full coverage: that indicator can be up to date while production files are missing. After syncing, compare the project's eligible files with the indexed ones and test a concrete relationship by reading both sources.

The verified 1.2.0 version indexes files containing Java `record` but does not extract all their type nodes, compact constructors or generated accessors. It may also resolve by method name when type resolution fails: in one observed project it attributed `request.environment()` to a test helper, while LSP resolved it to the `EncryptionRequest` component.

Treat graph relationships as exploration candidates, not verified typed references. Confirm with LSP or source evidence before refactoring or claiming impact; an index with no pending changes does not remove those extractor limitations. Do not alter the sources, the installed CodeGraph code or its SQL tables to hide them.
