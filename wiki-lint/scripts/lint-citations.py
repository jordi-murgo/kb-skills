#!/usr/bin/env python3
"""Source citation lint: validates ^[source] markers in wiki pages.

A claim cites its source inline with a caret-bracket marker:
    ^[.raw/paper.md]              whole-source citation
    ^[.raw/paper.md:42-58]        line-range citation
    ^[.raw/paper.md#L42-L58]      same range, GitHub-style anchor

Paths are relative to the vault root. Markers inside fenced code blocks are
ignored.

Findings:
    error  missing_source   cited file does not exist
    error  malformed        empty path or unparseable marker
    error  bad_range        start < 1 or end < start
    warn   out_of_bounds    range extends past the last line of the source

Lint contract (run-lint.py): exit 0 clean or warnings only, exit 1 on any
error, stdout report, --json for machine-readable output.
Exit codes: 0 ok, 1 lint errors, 2 usage error.
"""

from __future__ import annotations

import argparse
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


def check_marker(inner: str, vault: Path):
    """Return (severity, kind, message) or None if the marker is valid."""
    spec = inner.strip()
    m = SPEC.match(spec)
    if not spec or not m or not m.group("path").strip():
        return ("error", "malformed", f"unparseable citation '^[{inner}]'")

    rel = m.group("path").strip()
    src = (vault / rel).resolve()
    if not src.is_file():
        return ("error", "missing_source", f"cited source not found: {rel}")

    a, b = m.group("a"), m.group("b")
    if a is None:
        return None
    start = int(a)
    end = int(b) if b is not None else start
    if start < 1 or end < start:
        return ("error", "bad_range", f"invalid line range {start}-{end} in {rel}")

    total = len(src.read_text(encoding="utf-8", errors="replace").splitlines())
    if end > total:
        return ("warn", "out_of_bounds",
                f"range {start}-{end} exceeds {total} lines in {rel}")
    return None


def lint(vault: Path) -> dict:
    findings = []
    pages = 0
    markers = 0
    for page in sorted((vault / "wiki").rglob("*.md")):
        pages += 1
        text = page.read_text(encoding="utf-8", errors="replace")
        rel_page = page.relative_to(vault).as_posix()
        for lineno, inner in iter_markers(text):
            markers += 1
            result = check_marker(inner, vault)
            if result:
                severity, kind, message = result
                findings.append({
                    "severity": severity,
                    "kind": kind,
                    "page": rel_page,
                    "line": lineno,
                    "message": message,
                })
    errors = sum(1 for f in findings if f["severity"] == "error")
    return {
        "pages": pages,
        "markers": markers,
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
        print(f"Citations: {result['markers']} markers in {result['pages']} pages, "
              f"{result['errors']} errors, {result['warnings']} warnings")
        for f in result["findings"]:
            print(f"  [{f['severity']}] {f['page']}:{f['line']} {f['message']}")

    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
