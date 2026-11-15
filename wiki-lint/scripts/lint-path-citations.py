#!/usr/bin/env python3
"""Check that backticked vault paths in wiki pages resolve on disk.

A wiki page routinely names the artifact it describes in backticks — the
OpenAPI contract, the test script, the values file. Nothing in the wiki lint
set resolves those, so a renamed file leaves every citation behind pointing at
a path that no longer exists (`lint-dead-links.py` only follows `[[wikilinks]]`,
and `lint-citations.py` only parses `^[...]` markers). Observed in a production
vault: an OpenAPI spec renamed to `-deprecated` while seven references across
five pages, `hot.md` among them, kept the old name.

What counts as a vault path: a backticked token whose first path segment is a
top-level directory of the vault (`wso2/`, `scripts/`, `wiki/`, `.raw/`, …).
A bare filename (`Constants.java`, `analysis_service.py`) is deliberately NOT
checked: in a vault that documents external repositories those names refer to
another repo's source and never exist here, so treating them as findings buries
the real ones.

Config (`kb-config.yaml`, optional section `path_citations`):

    path_citations:
      folders: [scripts, wso2, .raw]   # restrict to these top-level dirs
      extensions: [yaml, py, java]     # restrict to these file extensions
      ignore: ["scripts/generated/"]   # prefixes exempt from the check

Defaults: every top-level directory of the vault, a built-in extension list,
no exemptions. Values support ${VAR} / ${VAR:-default} macros (process env,
then vault-root .env / .env.local), matching the other kb-skills scripts.

Deterministic, read-only, stdlib only; PyYAML accepted for reading
kb-config.yaml with the install command in the error message.

Lint contract (run-lint.py): exit 0 clean, exit 1 findings, exit 2 usage.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

MACRO_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Backticked token: a single path-like run of characters.
TICK = re.compile(r"`([^`\n]{1,200})`")
# A path token after trimming: at least one segment plus a filename with extension.
PATH_TOKEN = re.compile(r"^(?P<path>[\w.@+-]+(?:/[\w.@+-]+)*)\.(?P<ext>[A-Za-z0-9]{1,8})$")

DEFAULT_EXTENSIONS = {
    "yaml", "yml", "json", "py", "sh", "bash", "java", "ts", "tsx", "js",
    "go", "md", "tf", "ps1", "toml", "cfg", "ini", "sql", "html", "csv",
}

# Never a vault path: absolute, home-relative, URL, placeholder, glob, macro.
SKIP_STARTS = ("/", "~", "./", "../", "http://", "https://", "${")
SKIP_CHARS = set("<>{}|*?$\\ ")
# Navigation/derived areas that legitimately name paths elsewhere.
GLOBAL_IGNORE = ("wiki/meta/", ".reciclaje/")


def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git (a skills dir also has
    wiki/, and the kb-skills repo itself is such a false positive)."""
    cwd = Path(os.getcwd()).resolve()
    for start in (cwd, Path(__file__).resolve()):
        for parent in [start] + list(start.parents):
            if (
                (parent / "wiki").is_dir()
                and (parent / ".git").exists()
                and not (parent / "wiki" / "SKILL.md").exists()
            ):
                return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/")


def load_env_files(root: Path) -> None:
    """Fill os.environ from <root>/.env then <root>/.env.local (later file
    wins; process env always wins)."""
    process_keys = set(os.environ)
    for name in (".env", ".env.local"):
        path = root / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in process_keys:
                os.environ[key] = value


def expand_macros(value):
    """Expand ${VAR} / ${VAR:-default} recursively in config values."""
    if isinstance(value, str):
        return MACRO_RE.sub(
            lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value
        )
    if isinstance(value, list):
        return [expand_macros(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_macros(item) for key, item in value.items()}
    return value


def load_config_section(root: Path, section: str) -> dict:
    """Read one optional section of kb-config.{yaml,yml,json} at the vault
    root. Missing file or section -> {}. Kept inline per the no-shared-import
    rule: a skill copied on its own must keep working."""
    candidates = [root / "kb-config.yaml", root / "kb-config.yml", root / "kb-config.json"]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path} is not valid JSON: {e}")
    else:
        try:
            import yaml
        except ImportError:
            raise SystemExit(
                f"{path} needs PyYAML, which is not installed.\n"
                "Run: uv add pyyaml   (or: python3 -m pip install pyyaml)"
            )
        try:
            cfg = yaml.safe_load(text)
        except Exception as e:
            raise SystemExit(f"{path} is not valid YAML: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path} must contain a mapping at the top level")
    sec = cfg.get(section) or {}
    if not isinstance(sec, dict):
        raise SystemExit(f"{path}: '{section}' must be a mapping")
    return {k: expand_macros(v) for k, v in sec.items()}


def iter_outside_fences(text: str):
    """Yield (line_number, line) for lines outside fenced code blocks.

    A path inside a fenced block is an example command, not a citation of a
    vault file; flagging it would punish pages that show how to run something
    against a path the reader creates themselves.
    """
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            fence = None if fence == marker else (fence or marker)
            continue
        if fence is None:
            yield number, line


def candidate_paths(line: str):
    """Yield the trimmed path tokens on one line."""
    for token in TICK.findall(line):
        token = token.strip()
        if not token or token.startswith(SKIP_STARTS):
            continue
        if any(ch in SKIP_CHARS for ch in token):
            continue
        if ".." in token:
            continue  # "../x" relative or "a/.../b" placeholder, not a real path
        if token.endswith("/"):
            continue
        yield token


def main() -> int:
    root = find_vault_root()
    load_env_files(root)
    cfg = load_config_section(root, "path_citations")

    ext_cfg = cfg.get("extensions")
    if ext_cfg is not None:
        if not isinstance(ext_cfg, list) or not all(isinstance(e, str) and e.strip() for e in ext_cfg):
            print("error: kb-config path_citations.extensions must be a list of extensions", file=sys.stderr)
            return 2
        extensions = {e.strip().lstrip(".").lower() for e in ext_cfg}
    else:
        extensions = set(DEFAULT_EXTENSIONS)

    ignore = list(GLOBAL_IGNORE)
    ign_cfg = cfg.get("ignore")
    if ign_cfg is not None:
        if not isinstance(ign_cfg, list) or not all(isinstance(i, str) for i in ign_cfg):
            print("error: kb-config path_citations.ignore must be a list of path prefixes", file=sys.stderr)
            return 2
        ignore = [i.strip() for i in ign_cfg if i.strip()] + ignore

    folder_cfg = cfg.get("folders")
    if folder_cfg is not None:
        if not isinstance(folder_cfg, list) or not all(isinstance(f, str) and f.strip() for f in folder_cfg):
            print("error: kb-config path_citations.folders must be a list of folder names", file=sys.stderr)
            return 2
        folders = {f.strip().strip("/") for f in folder_cfg}
    else:
        # Top-level directories of the vault that a wiki page can cite. Hidden
        # dirs are included on purpose: `.raw/` is the provenance tree and is
        # the most-cited folder of all.
        folders = {p.name for p in root.iterdir() if p.is_dir() and p.name != ".git"}

    wiki = root / "wiki"
    findings: list[tuple[str, int, str]] = []
    checked_refs = 0
    checked_pages = 0
    for md in sorted(wiki.rglob("*.md")):
        rel = md.relative_to(root).as_posix()
        if any(rel.startswith(prefix) for prefix in ignore):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        page_hit = False
        for number, line in iter_outside_fences(text):
            for token in candidate_paths(line):
                match = PATH_TOKEN.match(token)
                if not match or match.group("ext").lower() not in extensions:
                    continue
                first = match.group("path").split("/")[0]
                if "/" not in match.group("path") or first not in folders:
                    continue
                checked_refs += 1
                if (root / token).is_file():
                    continue
                findings.append((rel, number, token))
                page_hit = True
        checked_pages += page_hit

    if findings:
        by_token: dict[str, list[tuple[str, int]]] = {}
        for page, number, token in findings:
            by_token.setdefault(token, []).append((page, number))
        print(f"DEAD PATH CITATIONS: {len(by_token)} path(s), {len(findings)} reference(s) in {checked_pages} page(s)")
        for token in sorted(by_token):
            print(f"  `{token}` — {len(by_token[token])} reference(s)")
            for page, number in sorted(by_token[token])[:5]:
                print(f"      {page}:{number}")
            if len(by_token[token]) > 5:
                print(f"      … {len(by_token[token]) - 5} more")
        print("\nFix: point the citation at the file's current path, or delete the claim it supports.")
        return 1

    print(f"OK: {checked_refs} vault path citation(s) in {len(list(wiki.rglob('*.md')))} pages all resolve")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
