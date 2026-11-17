#!/usr/bin/env python3
"""Tests for kb-publish export-okf.py — OKF v0.2 bundle export.

Per repo rules: a script must be proven to fail on a fault before it is
trusted. Here the faults are the safety contract itself: existing or protected
destinations, invalid pages (no bundle may be created), fabricated trust
events, and symlinked paths. Nothing is ever deleted or adopted.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "export-okf.py"


class ExportOkfCase(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="export-okf-test-"))
        (self.vault / "wiki" / "entities").mkdir(parents=True)
        (self.vault / ".git").mkdir()
        self.out = self.vault / ".cache" / "bundle"

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def page(self, name: str, frontmatter: str, body: str = "Body.\n"):
        path = self.vault / "wiki" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
        return path

    def run_export(self, *extra):
        return subprocess.run(
            ["python3", str(SCRIPT), "--out", str(self.out), *extra],
            cwd=self.vault, capture_output=True, text=True,
        )

    def test_clean_export_passes_without_fabricated_trust(self):
        self.page("entities/a.md",
                  'type: entity\ntitle: "A"\nmaturity: mature\nverification: supported\n'
                  'tags:\n  - entity\ncreated: 2026-01-01\nupdated: 2026-02-02\n')
        r = self.run_export()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = (self.out / "entities" / "a.md").read_text(encoding="utf-8")
        self.assertIn("type: entity", out)
        self.assertIn("x-kb:", out)
        self.assertIn("verification: supported", out)  # label preserved
        self.assertIn("created: 2026-01-01", out)  # preserved under x-kb
        self.assertNotIn("verified:", out)  # no synthesized trust event
        self.assertNotIn("process:wiki-vetting", out)
        self.assertNotIn("generated:", out)

    def test_page_without_type_creates_no_bundle(self):
        self.page("entities/good.md", "type: entity\n")
        self.page("entities/nope.md", 'title: "NoType"\n')
        r = self.run_export()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no `type`", r.stderr)
        self.assertFalse(self.out.exists(), "invalid pages must not produce a bundle")

    def test_failing_source_leaves_existing_bundle_untouched(self):
        self.page("entities/old.md", "type: entity\n")
        r = self.run_export()
        self.assertEqual(r.returncode, 0)
        bundle_before = (self.out / "entities" / "old.md").read_text(encoding="utf-8")
        self.page("entities/broken.md", 'title: "NoType"\n')
        r = self.run_export()  # destination exists AND source is now invalid
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            (self.out / "entities" / "old.md").read_text(encoding="utf-8"),
            bundle_before)
        self.assertFalse((self.out / "entities" / "broken.md").exists())

    def test_reusing_destination_fails_without_changing_bundle(self):
        self.page("entities/a.md", "type: entity\n")
        self.assertEqual(self.run_export().returncode, 0)
        bundle_before = (self.out / "entities" / "a.md").read_text(encoding="utf-8")
        self.page("entities/b.md", "type: entity\n")
        r = self.run_export()
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stderr)
        self.assertEqual(
            (self.out / "entities" / "a.md").read_text(encoding="utf-8"),
            bundle_before)
        self.assertFalse((self.out / "entities" / "b.md").exists())

    def test_maturity_maps_to_status(self):
        self.page("seed.md", "type: concept\nmaturity: seed\n")
        self.run_export()
        self.assertIn("status: draft", (self.out / "seed.md").read_text(encoding="utf-8"))
        self.page("stable.md", "type: concept\nmaturity: evergreen\n")
        r = self.run_export("--out", str(self.vault / ".cache" / "other"))
        stable = (self.vault / ".cache" / "other" / "stable.md").read_text(encoding="utf-8")
        self.assertNotIn("status:", stable)  # stable is the default and omitted

    def test_superseded_decision_overrides(self):
        self.page("d.md", "type: decision\nmaturity: mature\ndecision_status: superseded\n")
        self.run_export()
        self.assertIn("status: deprecated", (self.out / "d.md").read_text(encoding="utf-8"))

    def test_sources_map_to_okf_entries(self):
        self.page("s.md", 'type: source\nsources:\n  - "[[.raw/jira/BI-1.md]]"\n  - ".raw/mail/x.md"\n')
        self.run_export()
        out = (self.out / "s.md").read_text(encoding="utf-8")
        self.assertIn('id: "-raw-jira-BI-1-md"', out)
        self.assertIn("resource: .raw/jira/BI-1.md", out)
        self.assertIn("resource: .raw/mail/x.md", out)

    def test_wikilinks_rewrite_to_bundle_paths(self):
        self.page("entities/a.md", 'type: entity\ntitle: "A"\n', body="See [[b]].\n")
        self.page("concepts/b.md", "type: concept\n")
        self.run_export()
        out = (self.out / "entities" / "a.md").read_text(encoding="utf-8")
        self.assertIn("[b](/concepts/b.md)", out)

    def test_unresolved_wikilink_passes_through(self):
        self.page("a.md", "type: entity\n", body="See [[nowhere]].\n")
        self.run_export()
        self.assertIn("[[nowhere]]", (self.out / "a.md").read_text(encoding="utf-8"))

    def test_default_out_is_cache(self):
        self.page("a.md", "type: entity\n")
        r = subprocess.run(
            ["python3", str(SCRIPT)],
            cwd=self.vault, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.vault / ".cache" / "okf-export" / "a.md").is_file())

    def test_symlink_in_wiki_tree_refused(self):
        self.page("a.md", "type: entity\n")
        (self.vault / "wiki" / "link.md").symlink_to(self.vault / "wiki" / "a.md")
        r = self.run_export()
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)
        self.assertFalse(self.out.exists())


class DestinationRefusalTest(ExportOkfCase):
    """Refusal proofs: every refused destination must leave the vault
    byte-for-byte unchanged. Inherited happy-path tests still run — each
    gets a fresh vault."""


    def snapshot_tree(self) -> dict:
        return {
            str(p.relative_to(self.vault)): p.read_bytes()
            for p in sorted(self.vault.rglob("*"))
            if p.is_file()
        }

    def refuse(self, target, *needle) -> None:
        self.page("a.md", "type: entity\n")
        before = self.snapshot_tree()
        r = subprocess.run(
            ["python3", str(SCRIPT), "--out", str(target)],
            cwd=self.vault, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        for text in needle:
            self.assertIn(text, r.stderr)
        # byte-for-byte: no source or secret read/changed through the refusal
        self.assertEqual(self.snapshot_tree(), before)

    def test_wiki_tree_refused(self):
        self.refuse(self.vault / "wiki", "already exists")  # exists AND protected

    def test_raw_tree_refused(self):
        (self.vault / ".raw").mkdir()
        self.refuse(self.vault / ".raw" / "out", "outside protected vault content")

    def test_git_tree_refused(self):
        self.refuse(self.vault / ".git" / "out", "outside protected vault content")

    def test_vault_root_refused(self):
        self.refuse(self.vault, "already exists")  # exists AND is the vault root

    def test_ancestor_of_vault_refused(self):
        self.refuse(self.vault.parent, "already exists")  # exists AND is an ancestor

    def test_existing_sibling_directory_refused(self):
        existing = self.vault / ".cache" / "already"
        existing.mkdir(parents=True)
        (existing / "keep.md").write_text("keep\n")
        self.refuse(existing, "output already exists")
        self.assertTrue((existing / "keep.md").exists())

    def test_existing_empty_directory_refused(self):
        empty = self.vault / ".cache" / "empty"
        empty.mkdir(parents=True)
        self.refuse(empty, "output already exists")

    def test_dangling_symlink_counts_as_existing(self):
        dangling = self.vault / ".cache" / "dangling"
        dangling.parent.mkdir(parents=True)
        dangling.symlink_to(self.vault / ".cache" / "never")
        self.refuse(dangling, "output already exists")

    def test_symlink_component_in_output_path_refused(self):
        real_dir = self.vault / "realdir"
        real_dir.mkdir()
        link = self.vault / "cache-link"
        link.symlink_to(real_dir)
        self.refuse(link / "bundle", "outside protected vault content")

    def test_symlinked_cache_directory_refused(self):
        real_cache = self.vault / "real-cache"
        real_cache.mkdir()
        (self.vault / ".cache").symlink_to(real_cache)
        self.refuse(self.vault / ".cache" / "okf", "outside protected vault content")


if __name__ == "__main__":
    unittest.main()
