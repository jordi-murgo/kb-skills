#!/usr/bin/env python3
"""Check that wiki page tags come from a fixed vocabulary.

Config: the `tags` section of kb-config.yaml at the vault root:

    tags:
      allowed: [source, jira, entity, meta, security]
      aliases:
        kick-off: kickoff        # known variant -> canonical allowed tag
      case_sensitive: false

Equivalent-tag detection (beyond exact vocabulary match):
- An off-vocabulary tag listed in `aliases` resolves to its canonical
  allowed tag and passes.
- Any other off-vocabulary tag whose normalized form (accent-stripped,
  separator-removed, singularized) matches an allowed tag is a finding that
  names the canonical tag to use — diseno/diseño or kick-off/kickoff
  surface with an actionable fix instead of a bare violation.

Tag registry: every run refreshes the derived registry at
.vault-meta/tag-registry.json (per-tag page counts, first/last seen,
current off-vocabulary status) — a cache, delete anytime. `--survey` skips
the gate and prints the live inventory plus candidate equivalences
(near-duplicate clusters and alias suggestions) for curating
tags.allowed/aliases.

Values support ${VAR} / ${VAR:-default} macros expanded from the environment
(process env, then vault-root .env / .env.local). Without a configured
vocabulary the check SKIPS (exit 0) — an unconfigured vault is not a
failure. Meta/navigation pages (hot.md, index.md, log.md) and wiki/meta/
are exempt, matching lint-relations.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

MACRO_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:-([^}]*))?\}")

SKIP_BASENAMES = {"log.md", "hot.md", "index.md"}
SKIP_PARENTS = {"meta"}


def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git (a skills dir also has
    wiki/, and the kb-skills repo itself is such a false positive)."""
    cwd = Path(os.getcwd()).resolve()
    for start in (cwd, Path(__file__).resolve()):
        for parent in [start] + list(start.parents):
            if (parent / "wiki").is_dir() and (parent / ".git").exists():
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


def parse_tags(frontmatter: str) -> list[str]:
    """Extract tag values from a page's frontmatter block: both the inline
    list form (tags: [a, b]) and the block list form (tags:\\n  - a)."""
    lines = frontmatter.splitlines()
    tags: list[str] = []
    in_tags = False
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            break
        if line.startswith("tags:"):
            in_tags = True
            rest = line[len("tags:"):].strip()
            if rest:
                inner = rest.strip("[]")
                tags += [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
            continue
        if in_tags:
            if line.startswith((" ", "\t", "-")):
                if stripped.startswith("- "):
                    tags.append(stripped[2:].strip().strip("'\""))
                elif ":" in line and not stripped.startswith("-"):
                    in_tags = False
            else:
                in_tags = False
    return [t for t in tags if t]


def extract_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    out = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        out.append(line)
    return "\n".join(out)

def normalize_tag(tag: str) -> str:
    """Aggressive normalization for equivalence detection only (never for
    storage): strip accents, drop -/_ separators, lowercase, singularize
    -s/-es. flujo/flujos, kick-off/kickoff, diseño/diseno collapse to one key."""
    import unicodedata
    t = tag.lower()
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    t = t.replace("-", "").replace("_", "").replace(" ", "")
    if t.endswith("es") and len(t) > 4:
        t = t[:-2]
    elif t.endswith("s") and len(t) > 3:
        t = t[:-1]
    return t


def collect_page_tags(root: Path) -> list[tuple[Path, str]]:
    """All (page, tag) pairs in scan scope, sorted for stable registry output."""
    wiki = root / "wiki"
    pairs = []
    for md in sorted(wiki.rglob("*.md")):
        if md.name in SKIP_BASENAMES or md.parent.name in SKIP_PARENTS:
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        fm = extract_frontmatter(text)
        if not fm:
            continue
        for tag in parse_tags(fm):
            pairs.append((md, tag))
    return pairs


def refresh_registry(root: Path, pairs: list[tuple[Path, str]], allowed_set: set[str], aliases: dict) -> Path:
    """Write the derived tag registry to .vault-meta/tag-registry.json.
    Pure cache: recreated from scratch every run, deletable anytime, never
    holding configuration (kb-config.yaml stays the only source of truth)."""
    today = None
    import datetime
    today = datetime.date.today().isoformat()
    registry = {}
    for md, tag in pairs:
        key = tag if tag in registry else tag
        entry = registry.setdefault(key, {"count": 0, "pages": [], "first_seen": today, "last_seen": today})
        entry["count"] += 1
        rel = str(md.relative_to(root))
        if rel not in entry["pages"]:
            entry["pages"].append(rel)
    for tag, entry in registry.items():
        resolved = resolve_tag(tag, allowed_set, aliases)
        entry["status"] = "allowed" if resolved is not None else "off-vocabulary"
        canon = canonical_equivalent(tag, allowed_set)
        entry["suggested_canonical"] = canon or ""
    out = root / ".vault-meta" / "tag-registry.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(registry, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return out


def resolve_tag(tag: str, allowed_set: set[str], aliases: dict) -> str | None:
    """Exact match, then explicit alias, then (only in that order) canonical
    alias target. Returns the canonical allowed tag or None."""
    if tag in allowed_set:
        return tag
    target = aliases.get(tag)
    if target is not None and target in allowed_set:
        return target
    return None


def canonical_equivalent(tag: str, allowed_set: set[str]) -> str | None:
    """If tag is off-vocabulary but its normalized form matches exactly one
    allowed tag, return that tag (for actionable suggestions)."""
    norm = normalize_tag(tag)
    matches = [c for c in allowed_set if normalize_tag(c) == norm]
    if len(matches) == 1:
        return matches[0]
    return None


def main() -> int:
    survey = "--survey" in sys.argv
    root = find_vault_root()
    load_env_files(root)
    cfg = load_config_section(root, "tags")

    raw_allowed = cfg.get("allowed")
    raw_aliases = cfg.get("aliases") or {}
    if raw_allowed is None and not survey:
        print("SKIP: no tags.allowed vocabulary configured in kb-config.yaml")
        return 0
    if raw_allowed is not None:
        if not isinstance(raw_allowed, list) or not raw_allowed:
            print("error: kb-config tags.allowed must be a non-empty list", file=sys.stderr)
            return 1
        if not all(isinstance(v, str) and v.strip() for v in raw_allowed):
            print("error: kb-config tags.allowed entries must be non-empty strings", file=sys.stderr)
            return 1
    if not isinstance(raw_aliases, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
        for k, v in raw_aliases.items()
    ):
        print("error: kb-config tags.aliases must map non-empty strings to non-empty strings", file=sys.stderr)
        return 1

    case_sensitive = bool(cfg.get("case_sensitive", False))
    allowed = [v.strip() for v in (raw_allowed or [])]
    allowed_set = set(allowed) if case_sensitive else {v.lower() for v in allowed}
    aliases = {k.strip(): v.strip() for k, v in raw_aliases.items()}
    for alias, target in aliases.items():
        if target not in allowed_set:
            print(f"error: kb-config tags.aliases['{alias}'] targets '{target}' which is not in tags.allowed", file=sys.stderr)
            return 1

    pairs = collect_page_tags(root)
    checked = len({md for md, _ in pairs})
    refresh_registry(root, pairs, allowed_set, aliases)

    if survey:
        print(f"TAG SURVEY — {len(pairs)} tags on {checked} pages, registry written to .vault-meta/tag-registry.json")
        from collections import Counter
        counts = Counter(tag for _, tag in pairs)
        print(f"\nInventory ({len(counts)} unique):")
        for tag, n in counts.most_common():
            canon = canonical_equivalent(tag, allowed_set)
            resolved = resolve_tag(tag, allowed_set, aliases)
            status = "allowed" if resolved else "off-vocabulary"
            if resolved:
                note = "" if resolved == tag else f"  -> alias of '{resolved}'"
            elif canon:
                note = f"  -> equivalent of allowed '{canon}'; add alias or fix pages"
            else:
                note = ""
            print(f"  {n:>4}  {tag}  [{status}]{note}")
        clusters: dict[str, set[str]] = {}
        for tag in counts:
            clusters.setdefault(normalize_tag(tag), set()).add(tag)
        dups = {k: sorted(v) for k, v in clusters.items() if len(v) > 1}
        print(f"\nNear-duplicate clusters ({len(dups)}):")
        for _, variants in sorted(dups.items()):
            print(f"  {' == '.join(variants)}")
        return 0

    findings: list[tuple[str, str, str]] = []
    for md, tag in pairs:
        if resolve_tag(tag, allowed_set, aliases) is None:
            canon = canonical_equivalent(tag, allowed_set)
            findings.append((str(md.relative_to(root)), tag, canon or ""))

    if findings:
        print(f"TAG VOCABULARY VIOLATIONS: {len(findings)}")
        for rel_path, tag, canon in sorted(findings):
            hint = f" — equivalent of allowed '{canon}': use it or add tags.aliases['{tag}']" if canon else ""
            print(f"  {rel_path} — off-vocabulary tag: {tag}{hint}")
        print(f"\nAllowed vocabulary ({len(allowed)}): {', '.join(allowed)}")
        print("Fix: use an allowed tag (see hints), add tags.aliases, or extend tags.allowed in kb-config.yaml.")
        print("Registry: .vault-meta/tag-registry.json (survey: --survey)")
        return 1
    print(f"OK: all tags on {checked} pages within vocabulary ({len(allowed)} tags, {len(aliases)} aliases)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Head-piped output is normal usage (survey | head); a gate must not
        # die with a traceback when the consumer stops reading early.
        sys.exit(0)