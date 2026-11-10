#!/usr/bin/env python3
"""Check required frontmatter dimensions and value contracts on wiki pages.

Every non-navigation page needs non-empty type, title, created, updated, tags,
maturity, and verification, with valid enum and date values. Decision pages also
require decision_status. Template skeletons under wiki/_templates/ are skipped.
"""

import sys
import os
import re
import glob
from datetime import date, datetime
from pathlib import Path


def find_wiki_root(start: str = ".") -> str:
    """Walk up for the vault root: wiki/ AND .git.

    Both markers are required. There is a skill directory named "wiki" under
    .agents/skills/, so wiki/ alone stops the walk at the skills directory
    instead of the vault root. cwd first, then this script's own location.
    """
    for base in (Path(start).resolve(), Path(__file__).resolve()):
        for parent in (base, *base.parents):
            if (
                (parent / "wiki").is_dir()
                and (parent / ".git").exists()
                and not (parent / "wiki" / "SKILL.md").exists()
            ):
                return str(parent)
    raise SystemExit(
        "error: vault root not found — run inside a project with wiki/ and .git"
    )


REQUIRED_FIELDS = ["type", "title", "created", "updated", "tags", "maturity", "verification"]
MATURITY_VALUES = {"seed", "developing", "mature", "evergreen"}
VERIFICATION_VALUES = {"unreviewed", "supported", "disputed"}
DECISION_STATUS_VALUES = {"active", "superseded", "rejected"}
DATE_FIELDS = ("created", "updated", "decision_date", "date_published")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$"
)

# Navigation and state pages allowed to have minimal frontmatter
EXEMPT_PAGES = {"hot", "index", "log", "_index", "overview", "dashboard", "goals"}
SKIP_TOP_DIRS = {"_templates", ".reciclaje"}


def parse_frontmatter(content: str) -> dict[str, str | list[str]]:
    """Parse flat YAML frontmatter into scalar strings and block lists."""
    fm: dict[str, str | list[str]] = {}
    if not content.startswith("---"):
        return fm
    lines = content.split("\n")
    current_key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            stripped = line.strip()
            if current_key is not None and stripped.startswith("-"):
                item = stripped[1:].strip().strip("'\"")
                if item:
                    existing = fm.get(current_key)
                    if isinstance(existing, list):
                        existing.append(item)
                    else:
                        fm[current_key] = [item]
            continue
        if ":" in line and not line.startswith("-"):
            key, _, raw_val = line.partition(":")
            key = key.strip()
            val = raw_val.strip()
            if val and " #" in val and not (val[:1] in ("'", '"') and val[-1:] == val[:1]):
                val = val.split(" #", 1)[0].strip()
            current_key = key
            if not val:
                fm[key] = ""
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                items = [
                    part.strip().strip("'\"")
                    for part in inner.split(",")
                    if part.strip().strip("'\"")
                ] if inner else []
                fm[key] = items
            else:
                fm[key] = val.strip("'\"")
    return fm


def field_has_value(fm: dict[str, str | list[str]], key: str) -> bool:
    if key not in fm:
        return False
    val = fm[key]
    if isinstance(val, list):
        return len(val) > 0
    return bool(val.strip())


def is_valid_date_or_datetime(raw: str, allow_datetime: bool = False) -> bool:
    if ISO_DATE_RE.match(raw):
        try:
            date.fromisoformat(raw)
            return True
        except ValueError:
            return False
    if allow_datetime and ISO_DATETIME_RE.match(raw):
        try:
            date.fromisoformat(raw[:10])
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return False


def validate_frontmatter(fm: dict[str, str | list[str]]) -> list[str]:
    issues: list[str] = []
    missing = [f for f in REQUIRED_FIELDS if not field_has_value(fm, f)]
    if missing:
        issues.append(f"missing: {', '.join(missing)}")

    maturity = fm.get("maturity")
    if isinstance(maturity, str) and maturity and maturity not in MATURITY_VALUES:
        issues.append(
            f"invalid maturity '{maturity}' (expected: {'|'.join(sorted(MATURITY_VALUES))})"
        )
    elif isinstance(maturity, list):
        issues.append("invalid maturity (must be a scalar string)")

    verification = fm.get("verification")
    if isinstance(verification, str) and verification and verification not in VERIFICATION_VALUES:
        issues.append(
            f"invalid verification '{verification}' (expected: {'|'.join(sorted(VERIFICATION_VALUES))})"
        )
    elif isinstance(verification, list):
        issues.append("invalid verification (must be a scalar string)")

    page_type = fm.get("type") if isinstance(fm.get("type"), str) else ""
    if page_type == "decision":
        dec_status = fm.get("decision_status")
        if not field_has_value(fm, "decision_status"):
            issues.append("missing: decision_status (required when type is decision)")
        elif not isinstance(dec_status, str) or dec_status not in DECISION_STATUS_VALUES:
            issues.append(
                f"invalid decision_status '{dec_status}' (expected: {'|'.join(sorted(DECISION_STATUS_VALUES))})"
            )

    for dfield in DATE_FIELDS:
        if not field_has_value(fm, dfield):
            continue
        dval = fm.get(dfield)
        if not isinstance(dval, str) or not is_valid_date_or_datetime(
            dval, allow_datetime=(dfield == "updated")
        ):
            expected = "YYYY-MM-DD (or ISO datetime on updated)" if dfield == "updated" else "YYYY-MM-DD"
            issues.append(f"invalid {dfield} '{dval}' (expected {expected})")

    return issues

def main():
    wiki_root = find_wiki_root()
    gaps = []
    checked = 0

    for md_file in sorted(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True)):
        rel_wiki = Path(md_file).relative_to(Path(wiki_root) / "wiki")
        if rel_wiki.parts and rel_wiki.parts[0] in SKIP_TOP_DIRS:
            continue
        stem = Path(md_file).stem
        if stem in EXEMPT_PAGES:
            continue
        rel_path = os.path.relpath(md_file, wiki_root)
        content = Path(md_file).read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(content)
        checked += 1

        page_issues = validate_frontmatter(fm)
        if page_issues:
            gaps.append((rel_path, page_issues))

    if gaps:
        print(f"FRONTMATTER GAPS: {len(gaps)}")
        for rel_path, page_issues in sorted(gaps):
            print(f"  {rel_path} — {'; '.join(page_issues)}")
        sys.exit(1)
    else:
        print(f"OK: all {checked} pages have required frontmatter")
        sys.exit(0)


if __name__ == "__main__":
    main()