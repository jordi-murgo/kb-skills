#!/usr/bin/env python3
"""Stale-citation lint: flags wiki pages citing changed or uningested sources.

A claim cites its source inline with a caret-bracket marker:
    ^[.raw/paper.md]              whole-source citation
    ^[.raw/paper.md:42-58]        line-range citation
    ^[.raw/paper.md#L42-L58]      same range, GitHub-style anchor

The vault's .raw/.manifest.json maps each ingested source path to metadata
including the content hash captured at ingest time. A citation is stale when
the source on disk no longer matches that hash: the page's claims may predate
the source's current content.

Findings:
    error  untracked_source  cited source has no entry in the manifest
    error  stale_source      current md5 and sha256 both differ from the
                             manifest hash (source changed after ingest)

Sources missing from disk are skipped here: lint-citations.py already
reports them as missing_source. The manifest hash may be md5 or sha256;
a source counts as current if EITHER digest matches (case-insensitive).

Lint contract (run-lint.py): exit 0 clean, exit 1 on any error, stdout
report, --json for machine-readable output.
Exit codes: 0 ok, 1 lint errors, 2 malformed manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

MARKER = re.compile(r"\^\[([^\]\n]*)\]")
SPEC = re.compile(r"^(?P<path>.+?)(?:(?::|#L)(?P<a>\d+)(?:-L?(?P<b>\d+))?)?$")


def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git, and wiki/ is not a skill.

    A skill directory named "wiki" lives under .agents/skills/ and this
    repository itself contains wiki/SKILL.md; either would otherwise satisfy
    the wiki/ + .git test and silently resolve to the wrong tree.
    """
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


def iter_markers(text: str):
    """Yield (line_number, inner_text) for markers outside fenced code blocks."""
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in MARKER.finditer(line):
            yield lineno, m.group(1)


def load_sources(vault: Path) -> dict:
    """Return the manifest's sources map, or {} when untracked/absent."""
    path = vault / ".raw" / ".manifest.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"error: malformed manifest {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    sources = data.get("sources") if isinstance(data, dict) else None
    return sources if isinstance(sources, dict) else {}


def file_hashes(path: Path) -> dict:
    """Return {"md5": hex, "sha256": hex} for the file's current bytes."""
    md5, sha256 = hashlib.md5(), hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return {"md5": md5.hexdigest(), "sha256": sha256.hexdigest()}


def cited_paths(text: str) -> dict:
    """Map each cited source path in a page to its first marker line."""
    seen = {}
    for lineno, inner in iter_markers(text):
        spec = inner.strip()
        m = SPEC.match(spec)
        if not spec or not m or not m.group("path").strip():
            continue  # malformed markers are lint-citations.py's finding
        rel = m.group("path").strip()
        seen.setdefault(rel, lineno)
    return seen


def lint(vault: Path) -> dict:
    sources = load_sources(vault)
    findings = []
    pages = 0
    cited = set()
    for page in sorted((vault / "wiki").rglob("*.md")):
        pages += 1
        text = page.read_text(encoding="utf-8", errors="replace")
        rel_page = page.relative_to(vault).as_posix()
        for rel, lineno in cited_paths(text).items():
            cited.add(rel)
            if rel not in sources:
                findings.append({
                    "severity": "error",
                    "kind": "untracked_source",
                    "page": rel_page,
                    "line": lineno,
                    "message": f"cited source never ingested: {rel} "
                               f"(not in .raw/.manifest.json)",
                })
                continue
            src = (vault / rel).resolve()
            if not src.is_file():
                continue  # missing_source is lint-citations.py's finding
            entry = sources[rel]
            claimed = entry.get("hash") if isinstance(entry, dict) else None
            try:
                hashes = file_hashes(src)
            except OSError as exc:
                findings.append({
                    "severity": "error",
                    "kind": "stale_source",
                    "page": rel_page,
                    "line": lineno,
                    "message": f"source unreadable during rehash: {rel}: {exc}",
                })
                continue
            claimed_text = claimed if isinstance(claimed, str) else ""
            if claimed_text.strip().lower() not in (hashes["md5"], hashes["sha256"]):
                shown = claimed_text.strip() or "<missing>"
                findings.append({
                    "severity": "error",
                    "kind": "stale_source",
                    "page": rel_page,
                    "line": lineno,
                    "message": f"source changed after ingest: {rel} "
                               f"manifest hash {shown} != current "
                               f"md5 {hashes['md5']} / sha256 {hashes['sha256']}",
                })
    errors = sum(1 for f in findings if f["severity"] == "error")
    return {
        "pages": pages,
        "cited_sources": len(cited),
        "errors": errors,
        "warnings": len(findings) - errors,
        "findings": findings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    vault = find_vault_root()
    result = lint(vault)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Stale citations: {result['cited_sources']} cited sources in "
              f"{result['pages']} pages, {result['errors']} errors, "
              f"{result['warnings']} warnings")
        for f in result["findings"]:
            print(f"  [{f['severity']}] {f['page']}:{f['line']} {f['message']}")

    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
