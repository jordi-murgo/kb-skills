#!/usr/bin/env python3
"""Regression coverage for configured wiki-semsearch source directories."""

import contextlib
import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts" / "wiki-semsearch.py"


def load_semsearch(root: Path):
    """Load the script after making the temporary vault its cwd."""
    module_name = f"wiki_semsearch_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    previous_cwd = Path.cwd()
    try:
        os.chdir(root)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous_cwd)
        sys.modules.pop(module_name, None)
    return module


def markdown(title: str) -> str:
    return f"# {title}\n\n" + ("semantic source content " * 16) + "\n"


class SourceDirectoriesTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("ignored/\n*.private.md\n", encoding="utf-8")
        for directory in ("wiki", "docs", "ignored"):
            (self.root / directory).mkdir()
        (self.root / "wiki" / "wiki.md").write_text(markdown("Wiki"), encoding="utf-8")
        (self.root / "docs" / "guide.md").write_text(markdown("Guide"), encoding="utf-8")
        (self.root / "root.md").write_text(markdown("Root"), encoding="utf-8")
        (self.root / "ignored" / "secret.md").write_text(markdown("Ignored"), encoding="utf-8")
        (self.root / "docs" / "draft.private.md").write_text(
            markdown("Ignored glob"), encoding="utf-8"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def write_config(self, source_dirs=None):
        lines = ["embeddings:", "  db: .vault-meta/sem/index.db"]
        if source_dirs is not None:
            lines.append("  source_dirs:")
            lines.extend(f"    - {directory}" for directory in source_dirs)
        (self.root / "kb-config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_build_indexes_configured_dirs_and_skips_gitignored_markdown(self):
        self.write_config(["wiki", "docs", "."])
        semsearch = load_semsearch(self.root)
        semsearch.embed_batch = lambda texts, _model: [[0.25, 0.75] for _ in texts]

        self.assertEqual(semsearch.cmd_build(SimpleNamespace(model="test", json=False)), 0)
        with sqlite3.connect(semsearch.DB_PATH) as conn:
            indexed = {row[0] for row in conn.execute("SELECT path FROM pages")}

        self.assertEqual(semsearch.configured_source_dirs(), ["wiki", "docs", "."])
        self.assertEqual(indexed, {"wiki/wiki.md", "docs/guide.md", "root.md"})

    def test_defaults_to_wiki_without_explicit_root(self):
        self.write_config()
        semsearch = load_semsearch(self.root)

        self.assertEqual(semsearch.configured_source_dirs(), ["wiki"])
        self.assertEqual(set(semsearch.source_files()), {"wiki/wiki.md"})

    def test_build_rejects_missing_configured_directory(self):
        self.write_config(["missing"])
        semsearch = load_semsearch(self.root)
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            result = semsearch.cmd_build(SimpleNamespace(model="test", json=False))

        self.assertEqual(result, 1)
        self.assertIn("configured source directory not found: missing", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
