#!/usr/bin/env python3
"""Tests for lint-path-citations.py — backticked vault paths must resolve.

Runs against a throwaway vault fixture. Per repo rules: a gate must be proven
to fail on a fault before it is trusted.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint-path-citations.py"

FRONTMATTER = (
    "---\n"
    'type: deliverable\n'
    'title: "P"\n'
    "created: 2026-01-01\n"
    "updated: 2026-01-01\n"
    "tags:\n"
    "  - deliverable\n"
    "maturity: mature\n"
    "verification: supported\n"
    "---\n"
)


class VaultCase(unittest.TestCase):
    """A throwaway vault with wiki/, .git/ and two top-level folders."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="path-citations-test-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        (self.vault / "wso2").mkdir()
        (self.vault / "scripts").mkdir()
        (self.vault / "kb-config.yaml").write_text("project:\n  name: test\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def page(self, body: str, name: str = "page") -> Path:
        path = self.vault / "wiki" / f"{name}.md"
        path.write_text(FRONTMATTER + "\n" + body + "\n", encoding="utf-8")
        return path

    def file(self, rel: str):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
        return path

    def run_lint(self):
        return subprocess.run(
            ["python3", str(SCRIPT)],
            cwd=self.vault, capture_output=True, text=True,
        )

    # -- tests ---------------------------------------------------------------

    def test_clean_vault_passes(self):
        self.file("wso2/contract.yaml")
        self.page("The contract is `wso2/contract.yaml`.")
        result = self.run_lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all resolve", result.stdout)

    def test_missing_vault_path_fails(self):
        self.page("The contract is `wso2/contract.yaml`.")
        result = self.run_lint()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("wso2/contract.yaml", result.stdout)
        self.assertIn("wiki/page.md:12", result.stdout)

    def test_bare_filename_is_not_a_vault_path(self):
        self.page("See `Constants.java` and `analysis_service.py`.")
        result = self.run_lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_top_folder_is_not_a_vault_path(self):
        self.page("The agent writes `paso1/components_report.md`.")
        result = self.run_lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_path_inside_code_fence_is_ignored(self):
        self.page("Run it:\n\n```bash\napictl import api --oas `wso2/missing.yaml`\n```\n")
        result = self.run_lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_url_and_placeholder_are_ignored(self):
        self.page("See `https://example.com/wso2/x.yaml` and `wso2/${ENV}.yaml`.")
        result = self.run_lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ellipsis_placeholder_path_is_ignored(self):
        # Vulcano-kb writes `workspaces/.../outputs/index.html` as prose
        # shorthand; the ellipsis marks it as a placeholder, not a path.
        self.page("Identical to `workspaces/.../outputs/index.html`.")
        result = self.run_lint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repeated_path_reports_every_reference(self):
        self.file("wso2/other.yaml")
        (self.vault / "wso2" / "contract.yaml").unlink(missing_ok=True)
        self.page("A `wso2/contract.yaml`.\n\nB `wso2/contract.yaml`.")
        self.page("C `wso2/contract.yaml`.", name="second")
        result = self.run_lint()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("1 path(s), 3 reference(s) in 2 page(s)", result.stdout)

    def test_extension_filter_from_config(self):
        (self.vault / "kb-config.yaml").write_text(
            "path_citations:\n  extensions: [yaml]\n", encoding="utf-8"
        )
        self.page("Missing `scripts/run.sh` and `wso2/contract.yaml`.")
        result = self.run_lint()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("wso2/contract.yaml", result.stdout)
        self.assertNotIn("scripts/run.sh", result.stdout)

    def test_folders_filter_from_config(self):
        (self.vault / "kb-config.yaml").write_text(
            "path_citations:\n  folders: [wso2]\n", encoding="utf-8"
        )
        self.page("Missing `scripts/run.sh` and `wso2/contract.yaml`.")
        result = self.run_lint()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("wso2/contract.yaml", result.stdout)
        self.assertNotIn("scripts/run.sh", result.stdout)

    def test_ignore_prefix_from_config(self):
        (self.vault / "kb-config.yaml").write_text(
            'path_citations:\n  ignore: ["wiki/generated/"]\n', encoding="utf-8"
        )
        self.page("Missing `wso2/contract.yaml`.", name="keep")
        (self.vault / "wiki" / "generated").mkdir()
        (self.vault / "wiki" / "generated" / "g.md").write_text(
            FRONTMATTER + "\nMissing `wso2/generated.yaml`.\n", encoding="utf-8"
        )
        result = self.run_lint()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("wso2/contract.yaml", result.stdout)
        self.assertNotIn("wso2/generated.yaml", result.stdout)

    def test_bad_config_lists_type_is_usage_error(self):
        (self.vault / "kb-config.yaml").write_text(
            "path_citations:\n  extensions: yaml\n", encoding="utf-8"
        )
        self.page("`wso2/contract.yaml`")
        result = self.run_lint()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("must be a list", result.stderr)

if __name__ == "__main__":
    unittest.main()
