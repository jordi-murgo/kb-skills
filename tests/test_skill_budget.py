#!/usr/bin/env python3
"""Keep every SKILL.md inside the documented token budget.

The repo documents a 5,000-token / 500-line budget for a SKILL.md, and until now
nothing enforced it: the largest skill (`autoresearch`, 4,152 tokens) sat 17%
below the cap because someone measured it once, not because a gate would stop
the next commit from doubling it.

The budget is stated in tokens, but this test measures **bytes**, because the
test suite must stay dependency-free (AGENTS.md hard rule 4: Python stdlib only).
Bytes are a sound proxy here, measured on the real corpus: the mean is 4.01
bytes/token and the densest skill observed is 3.66 bytes/token. At that worst
ratio, 18,000 bytes is 4,913 tokens — under the cap, so the gate cannot pass a
skill that is over budget.

The 500-line half of the budget is checked directly, since lines need no proxy.
"""

import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

# 5,000 tokens at the densest observed ratio (3.66 bytes/token) is 18,300 bytes;
# 18,000 leaves margin without being so loose that the gate never fires.
MAX_BYTES = 18_000
MAX_LINES = 500


# Directories at the repo root that are not skills. Everything else must be one.
NON_SKILL_DIRS = {"tests"}


def skill_files() -> list[pathlib.Path]:
    return sorted(REPO.glob("*/SKILL.md"))


def expected_skill_dirs() -> list[pathlib.Path]:
    return sorted(
        path
        for path in REPO.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and path.name not in NON_SKILL_DIRS
    )


class SkillBudgetTests(unittest.TestCase):
    def test_every_root_directory_is_a_skill(self):
        """A gate that silently scans fewer skills always passes.

        Root layout rule (AGENTS.md): one skill per directory. So a directory
        without a SKILL.md means either a skill lost its entry point or a
        non-skill directory appeared — both worth failing on. A count assertion
        was tried first and missed a deleted SKILL.md, because 20 > 15.
        """
        missing = [p.name for p in expected_skill_dirs() if not (p / "SKILL.md").is_file()]
        self.assertEqual(missing, [], f"root directories without SKILL.md: {missing}")
        self.assertEqual(
            [p.parent for p in skill_files()],
            expected_skill_dirs(),
            "SKILL.md discovery disagrees with the root directory listing",
        )

    def test_every_skill_is_under_the_byte_budget(self):
        over = [
            f"{p.parent.name}: {len(p.read_bytes())} B"
            for p in skill_files()
            if len(p.read_bytes()) > MAX_BYTES
        ]
        self.assertEqual(
            over,
            [],
            f"SKILL.md over {MAX_BYTES} bytes (move detail into an explicitly "
            f"read references/ file): {', '.join(over)}",
        )

    def test_every_skill_is_under_the_line_budget(self):
        over = [
            f"{p.parent.name}: {len(p.read_text(encoding='utf-8').splitlines())} lines"
            for p in skill_files()
            if len(p.read_text(encoding="utf-8").splitlines()) > MAX_LINES
        ]
        self.assertEqual(
            over,
            [],
            f"SKILL.md over {MAX_LINES} lines: {', '.join(over)}",
        )


if __name__ == "__main__":
    unittest.main()
