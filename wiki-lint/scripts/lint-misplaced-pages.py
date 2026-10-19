#!/usr/bin/env python3
"""Check that wiki pages live in a folder matching their frontmatter type.

Config: the `layout` section of kb-config.yaml at the vault root, key
`type_folders` — mapping page type -> list of allowed top-level folders
under wiki/:

    layout:
      type_folders:
        source: [sources]
        decision: [decisions, meta]
        concept: [concepts, domains]

Optional keys:
- `exempt_folders`: top-level folders exempt from the type/folder check
  (default: [meta] — navigation/derived pages; also exempted: pages at the
  wiki/ root, sub-indexes (_index.md), .reciclaje/ archive).
- `require_type`: report pages with no `type:` frontmatter (default true).

Values support ${VAR} / ${VAR:-default} macros (process env, then vault-root
.env / .env.local), matching the other kb-skills scripts.

Without a configured mapping the check SKIPS (exit 0) — an unconfigured vault
is not a failure. With one, any page whose type is not allowed in its
top-level folder is a finding. Deterministic, read-only, stdlib only; PyYAML
accepted for reading kb-config.yaml with the install command in the error
message (precedent: lint-tag-taxonomy.py).

Lint contract (run-lint.py): exit 0 clean/skip, exit 1 findings.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

MACRO_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Always exempt: derived/navigation areas and archive
GLOBAL_EXEMPT_FOLDERS = {"meta", ".reciclaje"}
GLOBAL_EXEMPT_BASENAMES = {"index.md", "hot.md", "log.md", "goals.md", "dashboard.md", "overview.md"}


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


def parse_frontmatter_field(text: str, field: str) -> str | None:
    """Extract one scalar field from flat YAML frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith(f"{field}:"):
            value = line[len(field) + 1:].strip().strip("'\"")
            return value or None
    return None


def main() -> int:
    root = find_vault_root()
    load_env_files(root)
    cfg = load_config_section(root, "layout")

    raw_map = cfg.get("type_folders")
    if raw_map is None or raw_map == {}:
        print("SKIP: no layout.type_folders configured in kb-config.yaml")
        return 0
    if not isinstance(raw_map, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, list) and v
        and all(isinstance(f, str) and f.strip() for f in v)
        for k, v in raw_map.items()
    ):
        print("error: kb-config layout.type_folders must map type names to non-empty folder lists", file=sys.stderr)
        return 1
    type_folders = {k.strip(): [f.strip() for f in v] for k, v in raw_map.items()}

    raw_exempt = cfg.get("exempt_folders") or []
    if not isinstance(raw_exempt, list) or not all(isinstance(f, str) and f.strip() for f in raw_exempt):
        print("error: kb-config layout.exempt_folders must be a list of folder names", file=sys.stderr)
        return 1
    exempt = {f.strip() for f in raw_exempt} | GLOBAL_EXEMPT_FOLDERS
    require_type = cfg.get("require_type", True)
    if not isinstance(require_type, bool):
        print("error: kb-config layout.require_type must be true or false", file=sys.stderr)
        return 1

    wiki = root / "wiki"
    findings: list[tuple[str, str, str]] = []
    missing_type: list[str] = []
    checked = 0
    for md in sorted(wiki.rglob("*.md")):
        rel = md.relative_to(wiki)
        parts = rel.parts
        top = parts[0] if len(parts) > 1 else "(root)"
        if top in exempt or len(parts) == 1 or md.name in GLOBAL_EXEMPT_BASENAMES or md.name == "_index.md":
            continue
        checked += 1
        text = md.read_text(encoding="utf-8", errors="replace")
        ptype = parse_frontmatter_field(text, "type")
        if ptype is None:
            if require_type:
                missing_type.append(str(md.relative_to(root)))
            continue
        allowed = type_folders.get(ptype)
        if allowed is None:
            findings.append((str(md.relative_to(root)), ptype, "no mapping for this type"))
        elif top not in allowed:
            findings.append((str(md.relative_to(root)), ptype, f"type '{ptype}' not allowed in '{top}/' (expected: {', '.join(allowed)})"))

    if findings or missing_type:
        if findings:
            print(f"MISPLACED PAGES: {len(findings)}")
            for rel_path, ptype, why in sorted(findings):
                print(f"  {rel_path} — {why}")
        if missing_type:
            print(f"MISSING TYPE FIELD: {len(missing_type)}")
            for rel_path in sorted(missing_type):
                print(f"  {rel_path} — page type is not set; add type: to frontmatter")
        print("\nConfigured mapping:")
        for ptype, allowed in sorted(type_folders.items()):
            print(f"  {ptype}: {', '.join(allowed)}")
        print("Fix: move the page, set its type, or extend layout.type_folders in kb-config.yaml.")
        return 1
    print(f"OK: all {checked} non-exempt pages live in a folder matching their type")
    return 0


if __name__ == "__main__":
    sys.exit(main())