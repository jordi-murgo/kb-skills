#!/usr/bin/env python3
"""Export the vault's wiki/ as an Open Knowledge Format (OKF v0.2) bundle.

OKF (GoogleCloudPlatform/open-knowledge-format, Apache-2.0) is a
markdown-plus-frontmatter bundle shape for exchanging compiled knowledge
between tools (llmwiki, openknowledge CLI, OKFy and others speak it). This
exporter maps the vault's frontmatter onto it so a vault's knowledge is
portable without changing how the vault is written:

    type        -> type (OKF's only required field)
    title/tags  -> title/tags
    maturity    -> status (seed|developing -> draft, mature|evergreen -> stable)
    decision_status superseded|rejected -> deprecated (overrides maturity)
    verification stays in x-kb — no `verified` event is synthesized: the
                   exporter knows neither who verified a page nor when
    sources     -> sources[] entries with resource + id (the raw path)
    created/updated/maturity/verification -> preserved under x-kb (extensions)

Dates stay date-only under x-kb rather than being promoted to datetimes with
invented time precision. Body wikilinks are rewritten to bundle-relative
/paths.md links when the target exists; OKF consumers tolerate broken links,
so unresolved ones pass through untouched.

Output is derived state: default <vault>/.cache/okf-export/, or --out DIR.
The destination must be a NEW directory — strictly inside .cache/ or fully
outside the vault; existing directories, protected trees (wiki/, .raw/, .git/,
the vault root) and symlinked paths are refused, never deleted or adopted.
Read-only over wiki/**; never writes the vault. Stdlib only.

Usage:
    python3 export-okf.py [--out DIR] [--quiet]

Exit 0 exported, 1 refused destination / invalid page / write failure, 2 usage.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
LIST_ITEM = re.compile(r"^\s+-\s+(.*)$")
KEY_VALUE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")

STATUS_MAP = {"seed": "draft", "developing": "draft", "mature": "stable", "evergreen": "stable"}
DEPRECATED = {"superseded", "rejected"}

class ExportRefusal(Exception):
    """Safety refusal: the destination or input violates the output contract."""


_MACOS_ALIASES = {"/tmp": "/private/tmp", "/var": "/private/var", "/etc": "/private/etc"}


def _normalize_alias(path: Path) -> Path:
    """Rewrite known platform aliases (/tmp -> /private/tmp) only."""
    text = str(path)
    for alias, target in _MACOS_ALIASES.items():
        if text == alias:
            return Path(target)
        if text.startswith(alias + "/"):
            return Path(target + text[len(alias):])
    return path


def validate_output(root: Path, out: Path) -> Path:
    """Accept only a NEW directory, strictly inside .cache/ or fully outside.

    Existing destinations (dangling symlinks count), wiki/, .raw/, .git/, the
    vault root or its ancestors, and any symlink component in the ancestor
    chain are refused. Nothing is deleted, adopted or followed.
    """
    vault = root.resolve()
    candidate = _normalize_alias(Path(out))
    if not candidate.is_absolute():
        candidate = (Path.cwd().resolve() / candidate)
        candidate = _normalize_alias(candidate)

    # Symlink scan of the ancestor chain: platform aliases are already
    # rewritten, so anything left is a user symlink and is refused.
    probe = Path(candidate.anchor)
    for part in candidate.parts[1:-1]:
        probe = probe / part
        if probe.is_symlink():
            raise ExportRefusal(
                "error: output must be a new directory outside protected vault content")
    # The final component may be a dangling symlink: that counts as an
    # existing destination, not as a path component to follow.
    if candidate.is_symlink() or candidate.exists():
        raise ExportRefusal("error: output already exists; choose a new --out directory")

    protected = [vault, vault / "wiki", vault / ".raw", vault / ".git"]
    for tree in protected:
        if candidate == tree or candidate in tree.parents:
            raise ExportRefusal(
                "error: output must be a new directory outside protected vault content")
    try:
        rel = candidate.relative_to(vault)
    except ValueError:
        rel = None
    if rel is not None:
        if not rel.parts or rel.parts[0] != ".cache" or len(rel.parts) < 2:
            raise ExportRefusal(
                "error: output must be a new directory outside protected vault content")
    elif candidate in vault.parents:
        raise ExportRefusal(
            "error: output must be a new directory outside protected vault content")
    return candidate


def _walk_regular_files(tree: Path, errors: list[str]) -> list[Path]:
    """Collect files under tree, refusing symlinks instead of following them."""
    found: list[Path] = []
    stack = [tree]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            errors.append(f"{directory}: unreadable directory ({type(exc).__name__})")
            continue
        for entry in entries:
            if entry.is_symlink():
                errors.append(f"{entry}: symlink in the wiki tree refused")
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                if entry.suffix == ".md":
                    found.append(entry)
    return found


def _mkdirs_exclusive(path: Path) -> None:
    """Create missing directories at 0700, refusing to adopt existing ones."""
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def _write_new_file(path: Path, content: str) -> None:
    """Create path exclusively; never replace existing content."""
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git, cwd first, then __file__.

    The kb-skills repo itself has a wiki/ skill directory; the SKILL.md check
    rejects it, same walk as every other kb-skills script.
    """
    cwd = Path.cwd().resolve()
    for start in (cwd, Path(__file__).resolve()):
        for parent in [start] + list(start.parents):
            if (
                (parent / "wiki").is_dir()
                and (parent / ".git").exists()
                and not (parent / "wiki" / "SKILL.md").exists()
            ):
                return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/ and .git")


def parse_frontmatter(text: str) -> dict:
    """Parse the vault's flat frontmatter: scalars plus block lists."""
    data: dict = {}
    m = FM_RE.match(text)
    if not m:
        return data
    key = None
    for line in m.group(1).splitlines():
        item = LIST_ITEM.match(line)
        if item and key:
            if not isinstance(data.get(key), list):
                data[key] = [data[key]] if key in data else []
            data[key].append(item.group(1).strip().strip('"'))
            continue
        kv = KEY_VALUE.match(line)
        if kv:
            key = kv.group(1)
            data[key] = kv.group(2).strip().strip('"')
    return data


def okf_sources(raw) -> list[dict]:
    """Map the vault's sources list to OKF sources entries.

    A vault entry is a path or a `[[path]]` wikilink into .raw/. The id is the
    path with separators normalised, so body footnotes can key onto it.
    """
    entries = []
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    for item in items:
        path = WIKILINK.search(item)
        resource = path.group(1).strip() if path else item.strip()
        if not resource:
            continue
        entries.append({"id": resource.replace("/", "-").replace(".", "-"), "resource": resource})
    return entries


def yaml_str(value: str) -> str:
    """Quote a scalar when YAML would misread it bare."""
    if re.search(r"[:#\[\]{}&,*?|<>=!%@`\"']|^\s|\s$|^$|^[-?]", str(value)):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(value)


def emit_frontmatter(fields: dict, lists: dict, extensions: dict) -> str:
    """Render OKF frontmatter in a stable, diff-friendly order."""
    lines = ["---"]
    for key in ("type", "title", "description", "status", "tags"):
        if key in fields:
            lines.append(f"{key}: {yaml_str(fields[key])}")
    if "sources" in lists and lists["sources"]:
        lines.append("sources:")
        for entry in lists["sources"]:
            lines.append(f"  - id: {yaml_str(entry['id'])}")
            lines.append(f"    resource: {yaml_str(entry['resource'])}")
    # No `verified`/`generated` events are emitted: this exporter cannot
    # know who verified a page or when, so metadata labels must not become
    # trust events. The original `verification` label stays in x-kb.
    if extensions:
        lines.append("x-kb:")
        for key in ("created", "updated", "maturity", "verification", "decision_status"):
            if key in extensions:
                lines.append(f"  {key}: {yaml_str(extensions[key])}")
    lines.append("---")
    return "\n".join(lines)


def rewrite_wikilinks(body: str, targets: dict[str, str]) -> str:
    """Rewrite [[wikilinks]] to bundle-relative /path.md links when resolvable."""

    def sub(match):
        stem = match.group(1).strip()
        target = targets.get(stem.casefold()) or targets.get(Path(stem).stem.casefold())
        if not target:
            return match.group(0)  # OKF tolerates unresolved links
        label = stem if "|" not in match.group(0) else match.group(0).split("|")[1].rstrip("]")
        return f"[{label}](/{target}.md)"

    return WIKILINK.sub(sub, body)


def export(root: Path, out: Path) -> tuple[int, list[Path], list[str]]:
    """Render the whole bundle in memory, then write it to a NEW directory.

    Returns (written_count, page_paths, errors). On any input error nothing
    is created; on a write failure the incomplete new bundle stays on disk
    and is reported — nothing pre-existing is ever deleted or replaced.
    """
    wiki = root / "wiki"
    destination = validate_output(root, out)
    errors: list[str] = []
    pages = _walk_regular_files(wiki, errors)
    if errors:
        return 0, pages, errors

    # Resolve link targets by stem -> bundle-relative path without suffix.
    targets: dict[str, str] = {}
    texts: dict[Path, str] = {}
    for p in pages:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{p.relative_to(root)}: unreadable ({type(exc).__name__})")
            continue
        texts[p] = text
        rel = p.relative_to(wiki).with_suffix("")
        targets[p.stem.casefold()] = rel.as_posix()
        title = parse_frontmatter(text).get("title")
        if isinstance(title, str) and title:
            targets.setdefault(title.casefold(), rel.as_posix())

    rendered: list[tuple[Path, str]] = []
    for p in pages:
        text = texts.get(p)
        if text is None:
            continue  # already reported as unreadable
        data = parse_frontmatter(text)
        if not data.get("type"):
            errors.append(f"{p.relative_to(root)}: frontmatter has no `type` (OKF requires it)")
            continue
        body = text[FM_RE.match(text).end():] if FM_RE.match(text) else text

        fields: dict = {"type": data["type"]}
        if data.get("title"):
            fields["title"] = data["title"]
        status = STATUS_MAP.get(data.get("maturity", ""), "stable")
        if data.get("decision_status") in DEPRECATED:
            status = "deprecated"
        if status != "stable":
            fields["status"] = status
        # `verification: supported` stays in x-kb only: this exporter knows
        # neither who verified a page nor when, so it must not synthesize a
        # `verified` event from a metadata label.

        sources = okf_sources(data.get("sources"))
        body = rewrite_wikilinks(body, targets)
        rendered.append((p.relative_to(wiki),
                         emit_frontmatter(fields, {"sources": sources},
                                          {k: data[k] for k in (
                                              "created", "updated", "maturity",
                                              "verification", "decision_status")
                                           if k in data})
                         + "\n" + body))
    if errors:
        return 0, pages, errors

    _mkdirs_exclusive(destination)
    written = 0
    try:
        for rel, content in rendered:
            dest = destination / rel
            _mkdirs_exclusive(dest.parent)
            _write_new_file(dest, content)
            written += 1
    except OSError as exc:
        errors.append(
            f"bundle write failed after {written} page(s) "
            f"({type(exc).__name__}); incomplete bundle left at {destination}")
    return written, pages, errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Export wiki/ as an OKF v0.2 bundle")
    parser.add_argument("--out", default=None, help="output dir (default .cache/okf-export; must be new)")
    parser.add_argument("--quiet", action="store_true", help="only errors")
    args = parser.parse_args(argv)

    root = find_vault_root()
    out = Path(args.out) if args.out else root / ".cache" / "okf-export"
    try:
        written, pages, errors = export(root, out)
    except ExportRefusal as exc:
        print(exc, file=sys.stderr)
        return 1
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        print("OKF export FAILED: invalid pages or write failure; "
              "no complete bundle was produced", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"OKF v0.2 bundle: {written}/{len(pages)} pages -> {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)
