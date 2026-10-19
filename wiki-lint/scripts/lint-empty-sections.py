#!/usr/bin/env python3
"""Check for headings with no content underneath.

The check is heading-level aware: a section is empty only when it has no
content of its own AND no child headings. An `##` immediately followed by an
`###` is not empty — the content lives in the child. Fenced code blocks are
content (and their `#` lines are not headings); frontmatter is not scanned.

Findings:
    error  empty_section   heading with no content and no child headings
    warn   thin_section    content under `--thin N` characters (opt-in)

The thin check is off by default: many legitimate sections are one short line
("## Uso" with a single bullet), so it is a report knob, not a gate. The error
is the gate.

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

HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")

# Navigation/derived pages: their headings are structure, not prose.
NAV_BASENAMES = {"index.md", "hot.md", "log.md", "goals.md", "dashboard.md", "overview.md"}
SKIP_TOP_LEVEL = {"meta", ".reciclaje"}


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


def body_start(lines: list[str]) -> int:
    """Index of the first line after a leading YAML frontmatter block."""
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    return 0


def sections(text: str):
    """Yield (line_number, level, title, content_lines) for each heading."""
    lines = text.split("\n")
    start = body_start(lines)
    headings = []  # (index, level, title)
    in_fence = False
    for i in range(start, len(lines)):
        line = lines[i]
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2)))

    for k, (idx, level, title) in enumerate(headings):
        end = len(lines)
        for later_idx, later_level, _ in headings[k + 1:]:
            if later_level <= level:
                end = later_idx
                break
        # Content = anything that is neither blank nor a horizontal rule.
        content = [
            line for line in lines[idx + 1:end]
            if line.strip() and not RULE.match(line)
        ]
        yield idx + 1, level, title, content


def lint_page(path: Path, thin: int | None) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    findings = []
    for lineno, level, title, content in sections(text):
        chars = sum(len(line.strip()) for line in content)
        if not content:
            findings.append({
                "severity": "error",
                "kind": "empty_section",
                "line": lineno,
                "heading": f"{'#' * level} {title}",
                "message": "heading has no content underneath",
            })
        elif thin is not None and level >= 2 and chars < thin:
            findings.append({
                "severity": "warn",
                "kind": "thin_section",
                "line": lineno,
                "heading": f"{'#' * level} {title}",
                "message": f"only {chars} characters of content (threshold {thin})",
            })
    return findings


def lint(vault: Path, thin: int | None) -> dict:
    wiki = vault / "wiki"
    pages = checked = 0
    findings = []
    for page in sorted(wiki.rglob("*.md")):
        rel = page.relative_to(wiki)
        if rel.parts and rel.parts[0] in SKIP_TOP_LEVEL:
            continue
        if len(rel.parts) == 1 and page.name in NAV_BASENAMES:
            continue
        pages += 1
        page_findings = lint_page(page, thin)
        if page_findings:
            checked += 1
        rel_path = page.relative_to(vault).as_posix()
        for finding in page_findings:
            findings.append({"page": rel_path, **finding})
    errors = sum(1 for f in findings if f["severity"] == "error")
    return {
        "pages": pages,
        "pages_with_findings": checked,
        "errors": errors,
        "warnings": len(findings) - errors,
        "findings": sorted(findings, key=lambda f: (f["page"], f["line"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument(
        "--thin", type=int, metavar="N",
        help="also warn on sections with under N characters of content (report-only)",
    )
    args = parser.parse_args()
    if args.thin is not None and args.thin < 1:
        parser.error("--thin must be a positive integer")

    result = lint(find_vault_root(), args.thin)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            f"Empty sections: {result['errors']} empty, {result['warnings']} thin, "
            f"across {result['pages_with_findings']} of {result['pages']} pages"
        )
        for f in result["findings"]:
            print(f"  [{f['severity']}] {f['page']}:{f['line']} {f['heading']} — {f['message']}")

    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
