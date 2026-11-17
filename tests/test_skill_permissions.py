#!/usr/bin/env python3
"""Schema gate for the `allowed-tools` frontmatter of every root skill.

The declaration is metadata about which agent tools a skill expects — it is
not a sandbox or an authorization system. Its only honest claims are accuracy
and uniform shape: one space-separated scalar, tokens drawn from the approved
tool names, no YAML lists, no duplicates, no wildcards. This test enforces
exactly that structure and deliberately nothing about *what* the skills do.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NON_SKILL_DIRS = {"tests"}

# Every tool name that appears in a current or planned declaration in this
# repo. A new tool enters this set only together with the skill that needs it.
APPROVED_TOOLS = frozenset(
    {"Read", "Write", "Edit", "Glob", "Grep", "Bash",
     "WebFetch", "WebSearch", "Task"})

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
LIST_ITEM_RE = re.compile(r"^\s+-\s")


def validate_allowed_tools(text: str) -> list[str]:
    """Return schema violations for one SKILL.md's frontmatter, [] if valid.

    Parses only the leading `---` block and only the `allowed-tools` key —
    not arbitrary YAML, and never the skill prose.
    """
    errors: list[str] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ["frontmatter opening delimiter missing"]
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return ["frontmatter closing delimiter missing"]

    seen = 0
    value: str | None = None
    for i in range(1, end):
        line = lines[i]
        match = KEY_RE.match(line)
        if match and match.group(1) == "allowed-tools":
            seen += 1
            value = match.group(2).strip()
            nxt = lines[i + 1] if i + 1 < end else ""
            if not value and (LIST_ITEM_RE.match(nxt) or nxt.startswith("  ")):
                errors.append("allowed-tools uses list syntax; "
                              "use one space-separated scalar")
        elif seen and not match and LIST_ITEM_RE.match(line) and value == "":
            continue  # continuation of an (already rejected) list block
    if seen == 0:
        errors.append("allowed-tools declaration missing")
    elif seen > 1:
        errors.append(f"allowed-tools declared {seen} times")
    elif value is not None:
        if not value:
            errors.append("allowed-tools is empty")
        elif any(ch in value for ch in ",*[]{}"):
            errors.append("allowed-tools contains list/wildcard syntax")
        else:
            tokens = value.split()
            dupes = sorted({t for t in tokens if tokens.count(t) > 1})
            if dupes:
                errors.append(f"duplicate tool tokens: {', '.join(dupes)}")
            unknown = sorted(t for t in tokens if t not in APPROVED_TOOLS)
            if unknown:
                errors.append(f"unknown tool tokens: {', '.join(unknown)}")
    return errors


def skill_files() -> list[Path]:
    return sorted(REPO.glob("*/SKILL.md"))


def expected_skill_dirs() -> list[Path]:
    return sorted(
        path for path in REPO.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and path.name not in NON_SKILL_DIRS
    )


class ManifestSchemaTest(unittest.TestCase):
    def test_every_root_skill_declares_a_valid_allowed_tools_scalar(self):
        offenders = {}
        for path in skill_files():
            errors = validate_allowed_tools(path.read_text(encoding="utf-8"))
            if errors:
                offenders[path.parent.name] = errors
        self.assertEqual(
            offenders, {},
            "invalid allowed-tools declarations (must be one "
            "space-separated scalar of approved tool names): "
            f"{offenders}")

    def test_skill_discovery_covers_every_root_directory(self):
        self.assertEqual([p.parent for p in skill_files()],
                         expected_skill_dirs())


VALID = """---
name: sample
description: fixture
allowed-tools: Read Write Edit
---
body
"""


def manifest(allowed_tools_line: str) -> str:
    return f"---\nname: sample\ndescription: fixture\n{allowed_tools_line}---\nbody\n"


class MiniatureFixtureTest(unittest.TestCase):
    """Malformed miniature manifests must fail; only the schema speaks."""

    def test_valid_scalar_passes(self):
        self.assertEqual(validate_allowed_tools(manifest("allowed-tools: Read Bash\n")), [])

    def test_missing_declaration_fails(self):
        errors = validate_allowed_tools("---\nname: x\n---\nbody\n")
        self.assertTrue(any("missing" in e for e in errors), errors)

    def test_duplicate_declaration_fails(self):
        errors = validate_allowed_tools(manifest(
            "allowed-tools: Read\nallowed-tools: Bash\n"))
        self.assertTrue(any("2 times" in e for e in errors), errors)

    def test_wildcard_grant_fails(self):
        errors = validate_allowed_tools(manifest("allowed-tools: *\n"))
        self.assertTrue(any("wildcard" in e for e in errors), errors)

    def test_list_syntax_fails(self):
        errors = validate_allowed_tools(
            "---\nname: x\nallowed-tools:\n  - Read\n  - Bash\n---\n")
        self.assertTrue(any("list syntax" in e for e in errors), errors)

    def test_comma_separated_fails(self):
        errors = validate_allowed_tools(manifest("allowed-tools: Read, Bash\n"))
        self.assertTrue(any("list/wildcard" in e for e in errors), errors)

    def test_duplicate_token_fails(self):
        errors = validate_allowed_tools(manifest("allowed-tools: Read Read\n"))
        self.assertTrue(any("duplicate tool tokens" in e for e in errors), errors)

    def test_unknown_tool_fails(self):
        errors = validate_allowed_tools(manifest("allowed-tools: Read Env\n"))
        self.assertTrue(any("unknown tool tokens" in e for e in errors), errors)

    def test_empty_value_fails(self):
        errors = validate_allowed_tools(
            "---\nname: x\nallowed-tools:\n---\n")
        self.assertTrue(any("empty" in e for e in errors), errors)

    def test_missing_delimiters_fail(self):
        self.assertEqual(validate_allowed_tools("allowed-tools: Read\n"),
                         ["frontmatter opening delimiter missing"])
        self.assertEqual(validate_allowed_tools("---\nallowed-tools: Read\n"),
                         ["frontmatter closing delimiter missing"])

    def test_real_repo_has_no_other_list_declarations(self):
        for path in skill_files():
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^allowed-tools:\s*$", path)


if __name__ == "__main__":
    unittest.main()
