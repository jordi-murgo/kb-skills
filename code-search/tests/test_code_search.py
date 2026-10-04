"""Permanent boundary tests; embedding fixtures never contact a real service."""

import contextlib
import importlib.util
import io
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "code-search.py"
SPEC = importlib.util.spec_from_file_location("code_search_under_test", SCRIPT)
CODE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CODE
SPEC.loader.exec_module(CODE)


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(previous)


class VaultCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "projects").mkdir()
        (self.root / "wiki").mkdir()
        environment = mock.patch.dict(
            os.environ,
            {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root),
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def write_config(self, code=None, embeddings=None):
        document = {}
        if code is not None:
            document["code_search"] = code
        if embeddings is not None:
            document["embeddings"] = embeddings
        self.write("kb-config.json", json.dumps(document))
        return CODE.load_config(self.root)

    def external_directory(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    def main_json(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = CODE.main(["--root", str(self.root), *arguments, "--json"])
        return exit_code, json.loads(stdout.getvalue())


class GitVaultCase(VaultCase):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("Git is required for native ignore boundary tests")
        self.git("init", "-q")
        self.git("config", "core.excludesFile", os.devnull)

    def git(self, *arguments, directory=None):
        return subprocess.run(
            ["git", "-C", str(directory or self.root), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout

    def selection(self, config):
        skipped = CODE.skipped_counts()
        return dict(CODE.iter_source_files(config, skipped)), skipped


class IndexedVaultCase(GitVaultCase):
    def setUp(self):
        super().setUp()
        if not CODE.fts5_available():
            self.skipTest("SQLite FTS5 is required for index boundary tests")
        self.settings = {"extensions": [".py"], "model": "fixture-model",
                         "endpoint": "http://127.0.0.1:11434"}
        self.config = self.write_config(self.settings)

    @staticmethod
    def embeddings(config, texts, expected_dimension=0):
        return [[1.0, 0.0] for _ in texts]

    def build(self, config=None, embeddings=None):
        with mock.patch.object(CODE, "embed_texts", side_effect=embeddings or self.embeddings) as endpoint:
            result = CODE.build_index(config or self.config)
        return result, endpoint

    def indexed_paths(self):
        with contextlib.closing(sqlite3.connect(self.config.index_path)) as connection:
            return {row[0] for row in connection.execute("SELECT path FROM files")}


class ConfigTests(VaultCase):
    def test_default_selection_does_not_inherit_embeddings_source_dirs(self):
        config = self.write_config(embeddings={"source_dirs": ["."], "model": "shared-model",
                                              "endpoint": "https://embeddings.example/service"})
        self.assertEqual(config.source_dirs, ("projects",))
        self.assertNotIn(".md", config.extensions)
        self.assertEqual(config.model, "shared-model")
        self.assertEqual(config.endpoint, "https://embeddings.example/service")
        self.assertFalse((self.root / ".vault-meta").exists())

    def test_explicit_root_and_overlapping_directories_are_accepted(self):
        (self.root / "projects" / "src").mkdir()
        config = self.write_config({"source_dirs": ["projects", ".", "projects/src", "projects"],
                                    "extensions": [".py", ".md", ".py"]})
        self.assertEqual(config.source_dirs, ("projects", ".", "projects/src"))
        self.assertEqual(config.extensions, (".py", ".md"))
        self.assertEqual(CODE.find_root(str(self.root)), self.root)

    def test_root_discovery_prefers_cwd_vault_over_script_repository(self):
        (self.root / ".git").mkdir()
        upstream = self.external_directory()
        (upstream / "wiki").mkdir()
        (upstream / ".git").mkdir()
        with working_directory(self.root / "projects"), mock.patch.object(CODE, "__file__", str(upstream / "script.py")):
            self.assertEqual(CODE.find_root(), self.root)

    def test_explicit_root_rejects_missing_file_and_symlink(self):
        ordinary_file = self.write("file.py", "pass\n")
        link = self.root / "linked-vault"
        link.symlink_to(self.external_directory(), target_is_directory=True)
        for value in (self.root / "missing", ordinary_file, link):
            with self.subTest(value=value), self.assertRaises(CODE.CodeSearchError):
                CODE.find_root(value)

    def test_source_dir_types_and_unsafe_paths_are_rejected(self):
        for value in (None, [], "projects", [""], [42], [True], ["/tmp"], ["~/projects"],
                      [".."], ["projects/../wiki"], ["missing"], [".git"], [".vault-meta"]):
            with self.subTest(value=value), self.assertRaises(CODE.CodeSearchError):
                self.write_config({"source_dirs": value})
        self.assertFalse((self.root / ".vault-meta").exists())

    def test_source_symlink_and_symlink_ancestor_are_rejected(self):
        external = self.external_directory()
        (external / "src").mkdir()
        (self.root / "projects" / "link").symlink_to(external, target_is_directory=True)
        for source in ("projects/link", "projects/link/src"):
            with self.subTest(source=source), self.assertRaises(CODE.CodeSearchError):
                self.write_config({"source_dirs": [source]})

    def test_config_symlink_is_rejected_without_reading_external_config(self):
        external = self.external_directory() / "config.json"
        external.write_text('{"code_search":{"source_dirs":["."]}}', encoding="utf-8")
        (self.root / "kb-config.json").symlink_to(external)
        with self.assertRaises(CODE.CodeSearchError):
            CODE.load_config(self.root)

    def test_extensions_and_section_types_are_rejected(self):
        for extensions in (None, [], ".py", ["py"], ["."], [1], [".py/file"], [".p y"]):
            with self.subTest(extensions=extensions), self.assertRaises(CODE.CodeSearchError):
                self.write_config({"extensions": extensions})
        for document in ([], {"code_search": None}, {"embeddings": []}):
            self.write("kb-config.json", json.dumps(document))
            with self.subTest(document=document), self.assertRaises(CODE.CodeSearchError):
                CODE.load_config(self.root)

    def test_macros_are_recursive_and_env_precedence_is_local_then_process(self):
        self.write(".env", "MODEL=file-model\nHOST=http://file.example\nSUFFIX=.java\n")
        self.write(".env.local", "export MODEL='local-model'\nHOST=https://local.example\nREMOTE=${HOST}\nSUFFIX=.py\n")
        before = dict(os.environ)
        config = self.write_config(
            {"source_dirs": ["${DIR:-projects}"], "extensions": ["${SUFFIX}"], "model": "${MODEL}"},
            {"endpoint": "${REMOTE}", "model": "not-selected"},
        )
        self.assertEqual(config.model, "local-model")
        self.assertEqual(config.endpoint, "https://local.example")
        self.assertEqual(config.extensions, (".py",))
        self.assertEqual(dict(os.environ), before)
        with mock.patch.dict(os.environ, {"MODEL": "process-model", "HOST": "https://process.example"}):
            config = CODE.load_config(self.root)
        self.assertEqual(config.model, "process-model")
        self.assertEqual(config.endpoint, "https://process.example")

    def test_environment_is_only_a_macro_input_and_explicit_settings_override_shared(self):
        with mock.patch.dict(os.environ, {"CODE_SEARCH_MODEL": "not-config", "WIKI_SEM_ENDPOINT": "not-config"}):
            config = self.write_config(
                {"model": "explicit-model", "endpoint": "https://explicit.example/"},
                {"model": "shared-model", "endpoint": "https://shared.example"},
            )
        self.assertEqual(config.model, "explicit-model")
        self.assertEqual(config.endpoint, "https://explicit.example")

    def test_cyclic_macros_are_rejected(self):
        with self.assertRaises(CODE.CodeSearchError):
            CODE.expand_macros({"nested": ["${A}"]}, {"A": "${B}", "B": "${A}"})

    def test_embedding_setting_types_and_unsafe_urls_are_rejected(self):
        for setting in ({"model": None}, {"model": ""}, {"endpoint": 42}, {"endpoint": ""},
                        {"endpoint": "file:///tmp/model"}, {"endpoint": "http://user:secret@host"},
                        {"endpoint": "http://host?input=1"}, {"endpoint": "http://host#fragment"}):
            with self.subTest(setting=setting), self.assertRaises(CODE.CodeSearchError):
                self.write_config(setting)


class SelectionTests(GitVaultCase):
    def test_overlaps_deduplicate_and_explicit_root_includes_root_files(self):
        self.write("root.py", "root source\n")
        self.write("projects/a.py", "project source\n")
        self.write("projects/src/b.py", "nested source\n")
        self.write("wiki/guide.md", "explicit markdown\n")
        config = self.write_config({"source_dirs": ["projects", ".", "projects/src"],
                                    "extensions": [".py", ".md"]})
        selected = list(CODE.iter_source_files(config, CODE.skipped_counts()))
        paths = [path for path, _ in selected]
        self.assertEqual(set(paths), {"root.py", "projects/a.py", "projects/src/b.py", "wiki/guide.md"})
        self.assertEqual(len(paths), len(set(paths)))
        default = self.write_config({"extensions": [".py"]})
        self.assertEqual(set(self.selection(default)[0]), {"projects/a.py", "projects/src/b.py"})

    def test_nested_ignore_negation_and_tracked_ignored_files_use_native_rules(self):
        self.write(".gitignore", "ignored/\n*.private.py\n")
        self.write("projects/.gitignore", "*.generated.py\n!important.generated.py\nblocked/\n")
        for path in ("projects/keep.py", "projects/hidden.private.py", "projects/output.generated.py",
                     "projects/important.generated.py", "ignored/hidden.py", "projects/blocked/keep.py"):
            self.write(path, "source\n")
        self.write("projects/blocked/.gitignore", "!keep.py\n")
        self.git("add", "-f", "projects/hidden.private.py")
        config = self.write_config({"source_dirs": ["."], "extensions": [".py"]})
        selected, skipped = self.selection(config)
        self.assertEqual(set(selected), {"projects/keep.py", "projects/important.generated.py"})
        self.assertGreater(skipped["ignored_entries"], 0)

    def test_ignored_artifact_directory_is_not_traversed(self):
        self.write(".gitignore", "target/\n")
        self.write("projects/target/deep/hidden.py", "must not be visited\n")
        self.write("projects/keep.py", "visible\n")
        config = self.write_config({"extensions": [".py"]})
        actual_scandir = os.scandir
        visited = []

        def recording_scandir(path):
            visited.append(Path(path))
            self.assertNotIn("target", Path(path).relative_to(self.root).parts)
            return actual_scandir(path)

        with mock.patch.object(CODE.os, "scandir", side_effect=recording_scandir):
            selected, _ = self.selection(config)
        self.assertEqual(set(selected), {"projects/keep.py"})
        self.assertTrue(visited)

    def test_nested_repositories_with_git_directory_or_gitfile_use_child_ignores(self):
        self.write(".gitignore", "*.outer.py\n")
        for name, gitfile in (("nested", False), ("submodule", True)):
            child = self.root / "projects" / name
            child.mkdir()
            if gitfile:
                metadata = self.root / ".git" / "modules" / name
                metadata.parent.mkdir(parents=True, exist_ok=True)
                self.git("init", "-q", "--separate-git-dir", str(metadata), str(child))
                self.assertTrue((child / ".git").is_file())
            else:
                self.git("init", "-q", str(child))
            self.git("config", "core.excludesFile", os.devnull, directory=child)
            self.write(f"projects/{name}/.gitignore", "*.private.py\ntarget/\n")
            self.write(f"projects/{name}/keep.outer.py", "child rules take precedence\n")
            self.write(f"projects/{name}/hide.private.py", "ignored in child\n")
            self.write(f"projects/{name}/target/hide.py", "artifact\n")
            self.git("add", "-f", "hide.private.py", directory=child)
        config = self.write_config({"extensions": [".py"]})
        selected, _ = self.selection(config)
        self.assertEqual(set(selected), {"projects/nested/keep.outer.py", "projects/submodule/keep.outer.py"})

    def test_reserved_metadata_and_symlinks_are_never_selected(self):
        outside = self.external_directory()
        (outside / "secret.py").write_text("outside source\n", encoding="utf-8")
        self.write("projects/keep.py", "safe\n")
        self.write(".vault-meta/code-search/derived.py", "derived state\n")
        self.write("projects/.vault-meta/nested.py", "nested derived state\n")
        self.write(".git/internal.py", "Git metadata\n")
        (self.root / "projects" / "link.py").symlink_to(outside / "secret.py")
        (self.root / "projects" / "linked-directory").symlink_to(outside, target_is_directory=True)
        (self.root / "projects" / "internal-link.py").symlink_to(self.root / "projects" / "keep.py")
        config = self.write_config({"source_dirs": ["."], "extensions": [".py"]})
        selected, skipped = self.selection(config)
        self.assertEqual(set(selected), {"projects/keep.py"})
        self.assertEqual(skipped["symlink"], 3)

    def test_non_utf8_binary_and_unselected_suffixes_do_not_leak_content(self):
        self.write("projects/binary.py", b"binary-private\x00payload")
        self.write("projects/invalid.py", b"invalid-private\xffpayload")
        self.write("projects/valid.py", "valid unicode: café\n")
        self.write("projects/not-selected.md", "documentation\n")
        config = self.write_config({"extensions": [".py"]})
        selected, skipped = self.selection(config)
        readable = {}
        for path, source in selected.items():
            result = CODE.read_source(self.root, source, skipped)
            if result is not None:
                readable[path] = result
        self.assertEqual(set(readable), {"projects/valid.py"})
        self.assertEqual(skipped["binary"], 1)
        self.assertEqual(skipped["non_utf8"], 1)
        self.assertNotIn("private", json.dumps(skipped))

    def test_source_read_rejects_paths_outside_root(self):
        outside = self.external_directory() / "outside.py"
        outside.write_text("outside\n", encoding="utf-8")
        with self.assertRaises(CODE.CodeSearchError):
            CODE.read_source(self.root, outside, CODE.skipped_counts())


class ChunkTests(unittest.TestCase):
    def test_line_windows_are_bounded_overlapping_and_cover_every_line(self):
        lines = [f"line {number}" for number in range(1, 14)]
        with mock.patch.object(CODE, "CHUNK_LINES", 5), mock.patch.object(CODE, "CHUNK_OVERLAP", 2), \
                mock.patch.object(CODE, "MAX_CHUNK_CHARS", 1000):
            chunks = list(CODE.chunk_lines("\n".join(lines) + "\n"))
        covered = set()
        for start, end, body in chunks:
            self.assertLessEqual(end - start + 1, 5)
            self.assertEqual(body, "\n".join(lines[start - 1:end]))
            covered.update(range(start, end + 1))
        self.assertEqual(covered, set(range(1, len(lines) + 1)))
        for previous, current in zip(chunks, chunks[1:]):
            self.assertEqual(previous[1] - current[0] + 1, 2)

    def test_character_bound_preserves_long_single_line_and_line_numbers(self):
        lines = ["before", "x" * 31, "after"]
        with mock.patch.object(CODE, "MAX_CHUNK_CHARS", 10), mock.patch.object(CODE, "CHUNK_OVERLAP", 0):
            chunks = list(CODE.chunk_lines("\n".join(lines)))
        self.assertTrue(all(len(body) <= 10 for _, _, body in chunks))
        self.assertEqual("".join(body for start, end, body in chunks if start == end == 2), lines[1])
        self.assertEqual({line for start, end, _ in chunks for line in range(start, end + 1)}, {1, 2, 3})

    def test_empty_whitespace_and_last_line_without_newline(self):
        self.assertEqual(list(CODE.chunk_lines("")), [])
        self.assertEqual(list(CODE.chunk_lines(" \n\t\n")), [])
        self.assertEqual(list(CODE.chunk_lines("one\ntwo")), [(1, 2, "one\ntwo")])


class EmbeddingTests(VaultCase):
    def setUp(self):
        super().setUp()
        self.config = self.write_config({"model": "fixture-model", "endpoint": "https://embeddings.example/base"})

    def response(self, data):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(json.dumps(data).encode("utf-8"))
        return mock.patch.object(CODE.urllib.request, "build_opener", return_value=opener), opener

    def test_embed_normalizes_response_vectors(self):
        patch, _ = self.response({"embeddings": [[3, 4], [0, 2]]})
        with patch:
            vectors = CODE.embed_texts(self.config, ["alpha", "beta"], expected_dimension=2)
        self.assertEqual(vectors, [[0.6, 0.8], [0.0, 1.0]])

    def test_malformed_response_vectors_and_dimensions_are_rejected(self):
        for response in ({}, [], {"embeddings": []}, {"embeddings": [[]]},
                         {"embeddings": [[0, 0]]}, {"embeddings": [[True, 1]]},
                         {"embeddings": [["number", 1]]}, {"embeddings": [[float("nan"), 1]]},
                         {"embeddings": [[float("inf"), 1]]}, {"embeddings": [[1, 0, 0]]}):
            patch, _ = self.response(response)
            with self.subTest(response=response), patch, self.assertRaises(CODE.CodeSearchError):
                CODE.embed_texts(self.config, ["source"], expected_dimension=2)
        patch, _ = self.response({"embeddings": [[1, 0], [1, 0, 0]]})
        with patch, self.assertRaises(CODE.CodeSearchError):
            CODE.embed_texts(self.config, ["first", "second"])

    def test_http_failure_has_no_retry_download_or_installation(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            self.config.endpoint, 404, "missing model", {}, io.BytesIO(b"private source body")
        )
        with mock.patch.object(CODE.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(CODE.subprocess, "run") as commands:
            with self.assertRaises(CODE.CodeSearchError) as caught:
                CODE.embed_texts(self.config, ["private source"])
        opener.open.assert_called_once()
        commands.assert_not_called()
        self.assertNotIn("private source", str(caught.exception))
        self.assertFalse((self.root / ".vault-meta").exists())


class DependencyTests(VaultCase):
    def test_doctor_reports_missing_git_and_fts5_without_disk_or_network_changes(self):
        with mock.patch.object(CODE.shutil, "which", return_value=None), \
                mock.patch.object(CODE, "fts5_available", return_value=False), \
                mock.patch.object(CODE.subprocess, "run") as commands, \
                mock.patch.object(CODE.urllib.request, "build_opener") as network:
            exit_code, result = self.main_json(["doctor"])
        self.assertEqual(exit_code, 2)
        self.assertFalse(result["ok"])
        self.assertEqual({item["name"] for item in result["missing_dependencies"]}, {"git", "sqlite_fts5"})
        self.assertTrue(all(item["install_hint"] for item in result["missing_dependencies"]))
        self.assertFalse(result["dependencies"]["git"]["available"])
        self.assertFalse(result["dependencies"]["sqlite_fts5"]["available"])
        self.assertFalse(result["dependencies"]["yaml"]["required"])
        commands.assert_not_called()
        network.assert_not_called()
        self.assertFalse((self.root / ".vault-meta").exists())

    def test_missing_yaml_is_required_only_for_yaml_config(self):
        self.write("kb-config.yaml", "code_search:\n  source_dirs: [projects]\n")
        with mock.patch.dict(sys.modules, {"yaml": None}), \
                mock.patch.object(CODE.shutil, "which", return_value="git"), \
                mock.patch.object(CODE, "fts5_available", return_value=True), \
                mock.patch.object(CODE.subprocess, "run") as commands:
            exit_code, result = self.main_json(["doctor"])
            self.assertEqual(exit_code, 2)
            self.assertEqual([item["name"] for item in result["missing_dependencies"]], ["yaml"])
            self.assertTrue(result["dependencies"]["yaml"]["required"])
            (self.root / "kb-config.yaml").unlink()
            self.write_config({"extensions": [".py"]})
            exit_code, result = self.main_json(["doctor"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["ok"])
        self.assertFalse(result["dependencies"]["yaml"]["available"])
        commands.assert_not_called()
        self.assertFalse((self.root / ".vault-meta").exists())

    def test_absent_sqlite_module_is_actionable(self):
        with mock.patch.object(CODE, "sqlite3", None), mock.patch.object(CODE.shutil, "which", return_value="git"):
            exit_code, result = self.main_json(["doctor"])
        self.assertEqual(exit_code, 2)
        self.assertIn("sqlite_fts5", {item["name"] for item in result["missing_dependencies"]})

    def test_build_missing_git_does_not_install_or_create_state(self):
        self.write("projects/a.py", "source\n")
        with mock.patch.object(CODE.shutil, "which", return_value=None), \
                mock.patch.object(CODE, "fts5_available", return_value=True), \
                mock.patch.object(CODE.subprocess, "run") as commands, \
                mock.patch.object(CODE, "embed_texts") as endpoint:
            exit_code, result = self.main_json(["build"])
        self.assertEqual(exit_code, 2)
        self.assertEqual([item["name"] for item in result["missing_dependencies"]], ["git"])
        commands.assert_not_called()
        endpoint.assert_not_called()
        self.assertFalse((self.root / ".vault-meta").exists())


class BuildTests(IndexedVaultCase):
    def test_incremental_sha_reuse_change_and_deletion_update_fts(self):
        self.write("projects/a.py", "alpha original\n")
        self.write("projects/b.py", "beta retained\n")
        initial, _ = self.build()
        self.assertEqual(initial["embedded_chunks"], 2)
        unchanged, endpoint = self.build()
        endpoint.assert_not_called()
        self.assertEqual(unchanged["reused_files"], 2)
        self.assertEqual(unchanged["embedded_chunks"], 0)
        self.write("projects/a.py", "alpha changed\n")
        changed, endpoint = self.build()
        self.assertEqual(changed["updated_files"], 1)
        self.assertEqual(changed["reused_files"], 1)
        endpoint.assert_called_once()
        (self.root / "projects" / "b.py").unlink()
        removed, endpoint = self.build()
        endpoint.assert_not_called()
        self.assertEqual(removed["removed_files"], 1)
        self.assertEqual(self.indexed_paths(), {"projects/a.py"})
        self.assertEqual(CODE.query_index(self.config, "beta", mode="bm25")["results"], [])
        self.assertEqual(CODE.query_index(self.config, "changed", mode="bm25")["results"][0]["path"], "projects/a.py")

    def test_newly_ignored_tracked_path_is_removed(self):
        self.write("projects/private.py", "previously selected source\n")
        self.git("add", "projects/private.py")
        self.build()
        self.write(".gitignore", "projects/private.py\n")
        result, endpoint = self.build()
        endpoint.assert_not_called()
        self.assertEqual(result["removed_files"], 1)
        self.assertEqual(self.indexed_paths(), set())
        self.assertEqual(CODE.query_index(self.config, "previously", mode="bm25")["results"], [])

    def test_directory_and_extension_selection_changes_remove_files(self):
        self.write("projects/a.py", "kept source\n")
        self.write("projects/b.xml", "excluded extension\n")
        self.write("extras/c.py", "excluded directory\n")
        first = self.write_config({**self.settings, "source_dirs": ["projects", "extras"], "extensions": [".py", ".xml"]})
        self.build(first)
        second = self.write_config(self.settings)
        result, endpoint = self.build(second)
        endpoint.assert_not_called()
        self.assertEqual(result["removed_files"], 2)
        self.assertEqual(self.indexed_paths(), {"projects/a.py"})
        self.assertEqual(CODE.query_index(second, "excluded", mode="bm25")["results"], [])

    def test_binary_replacement_removes_previous_text_and_reports_skipped(self):
        self.write("projects/a.py", "old searchable content\n")
        self.build()
        self.write("projects/a.py", b"private binary\x00content")
        result, endpoint = self.build()
        endpoint.assert_not_called()
        self.assertEqual(result["skipped"]["binary"], 1)
        self.assertEqual(result["removed_files"], 1)
        self.assertEqual(self.indexed_paths(), set())
        self.assertNotIn("private binary", json.dumps(result))

    def test_failed_initial_embedding_does_not_create_state(self):
        self.write("projects/a.py", "source\n")
        with mock.patch.object(CODE, "embed_texts", side_effect=CODE.CodeSearchError("endpoint unavailable")):
            with self.assertRaises(CODE.CodeSearchError):
                CODE.build_index(self.config)
        self.assertFalse((self.root / ".vault-meta").exists())

    def test_failure_after_successful_batch_preserves_last_good_database(self):
        self.write("projects/original.py", "old searchable source\n")
        self.build()
        before = self.config.index_path.read_bytes()
        self.write("projects/b.py", "new first chunk\n")
        self.write("projects/c.py", "new second chunk\n")
        with mock.patch.object(CODE, "EMBED_BATCH", 1), mock.patch.object(
            CODE, "embed_texts", side_effect=[[[0.0, 1.0]], CODE.CodeSearchError("endpoint failed")]
        ) as endpoint:
            with self.assertRaises(CODE.CodeSearchError):
                CODE.build_index(self.config)
        self.assertEqual(endpoint.call_count, 2)
        self.assertEqual(self.config.index_path.read_bytes(), before)
        self.assertEqual(self.indexed_paths(), {"projects/original.py"})
        self.assertEqual(CODE.query_index(self.config, "old", mode="bm25")["results"][0]["path"], "projects/original.py")
        self.assertEqual(CODE.index_status(self.config)["new_paths"], ["projects/b.py", "projects/c.py"])

    def test_unexpected_dimension_change_rolls_back_even_when_every_file_changed(self):
        self.write("projects/a.py", "original source\n")
        self.build()
        before = self.config.index_path.read_bytes()
        self.write("projects/a.py", "changed source\n")
        with mock.patch.object(CODE, "embed_texts", return_value=[[1.0, 0.0, 0.0]]):
            with self.assertRaises(CODE.CodeSearchError):
                CODE.build_index(self.config)
        self.assertEqual(self.config.index_path.read_bytes(), before)

    def test_dimension_mismatch_between_batches_creates_no_index(self):
        self.write("projects/a.py", "first source\n")
        self.write("projects/b.py", "second source\n")
        with mock.patch.object(CODE, "EMBED_BATCH", 1), \
                mock.patch.object(CODE, "embed_texts", side_effect=[[[1.0, 0.0]], [[1.0, 0.0, 0.0]]]):
            with self.assertRaises(CODE.CodeSearchError):
                CODE.build_index(self.config)
        self.assertFalse((self.root / ".vault-meta").exists())

    def test_sql_failure_rolls_back_relational_and_fts_changes(self):
        self.write("projects/a.py", "original source\n")
        self.build()
        before = self.config.index_path.read_bytes()
        self.write("projects/a.py", "replacement source\n")
        with mock.patch.object(CODE, "embed_texts", side_effect=self.embeddings), \
                mock.patch.object(CODE, "index_counts", side_effect=sqlite3.OperationalError("injected failure")):
            with self.assertRaises(sqlite3.OperationalError):
                CODE.build_index(self.config)
        self.assertEqual(self.config.index_path.read_bytes(), before)
        self.assertTrue(CODE.query_index(self.config, "original", mode="bm25")["results"])
        self.assertFalse(CODE.query_index(self.config, "replacement", mode="bm25")["results"])

    def test_model_and_endpoint_changes_reembed_unchanged_files(self):
        self.write("projects/a.py", "unchanged source\n")
        self.build()
        new_model = self.write_config({**self.settings, "model": "other-model"})
        result, endpoint = self.build(new_model, lambda config, texts, expected_dimension=0: [[1.0, 0.0, 0.0] for _ in texts])
        endpoint.assert_called_once()
        self.assertEqual(result["dimensions"], 3)
        self.assertEqual(result["reused_chunks"], 0)
        new_endpoint = self.write_config({**self.settings, "model": "other-model", "endpoint": "https://other.example"})
        result, endpoint = self.build(new_endpoint)
        endpoint.assert_called_once()
        self.assertEqual(result["dimensions"], 2)
        self.assertEqual(result["endpoint"], new_endpoint.endpoint)

    def test_empty_and_whitespace_files_require_no_embeddings(self):
        self.write("projects/empty.py", "")
        self.write("projects/blank.py", " \n\t\n")
        result, endpoint = self.build()
        endpoint.assert_not_called()
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["dimensions"], 0)
        with mock.patch.object(CODE, "embed_texts") as endpoint:
            result = CODE.query_index(self.config, "source", mode="hybrid")
        endpoint.assert_not_called()
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["results"], [])

    def test_storage_symlinks_are_rejected_before_embeddings_or_writes(self):
        outside = self.external_directory()
        outside_file = outside / "outside.db"
        outside_file.write_bytes(b"outside unchanged")
        for name in (".vault-meta", ".vault-meta/code-search", ".vault-meta/code-search/index.db",
                     ".vault-meta/code-search/index.db-journal"):
            with self.subTest(name=name):
                case = self.root / ("case-" + name.replace("/", "-"))
                (case / "projects").mkdir(parents=True)
                (case / "projects" / "a.py").write_text("source\n", encoding="utf-8")
                link = case / name
                link.parent.mkdir(parents=True, exist_ok=True)
                is_directory = name in (".vault-meta", ".vault-meta/code-search")
                link.symlink_to(outside if is_directory else outside_file, target_is_directory=is_directory)
                config = CODE.load_config(case)
                with mock.patch.object(CODE, "embed_texts") as endpoint, self.assertRaises(CODE.CodeSearchError):
                    CODE.build_index(config)
                endpoint.assert_not_called()
                self.assertEqual(set(outside.iterdir()), {outside_file})
                self.assertEqual(outside_file.read_bytes(), b"outside unchanged")

    def test_build_creates_files_only_below_fixed_state_subtree(self):
        self.write("projects/a.py", "source\n")
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        self.build()
        created = {path.relative_to(self.root) for path in self.root.rglob("*")} - before
        self.assertIn(Path(".vault-meta/code-search/index.db"), created)
        self.assertTrue(all(path == Path(".vault-meta") or path.is_relative_to(".vault-meta/code-search") for path in created))


class QueryStatusTests(IndexedVaultCase):
    def setUp(self):
        super().setUp()
        self.write("projects/alpha.py", "alpha request handler\n")
        self.write("projects/beta.py", "beta SQL storage\n")
        self.write("projects/gamma.py", "alpha cache\n")
        self.build(embeddings=self.ranked_embeddings)

    @staticmethod
    def ranked_embeddings(config, texts, expected_dimension=0):
        return [[-1.0, 0.0] if "beta" in text else [1.0, 0.0] for text in texts]

    def test_bm25_uses_fts_without_contacting_embedding_endpoint(self):
        with mock.patch.object(CODE, "embed_texts") as endpoint:
            result = CODE.query_index(self.config, "alpha \" OR (handler)*", mode="bm25", top=3)
        endpoint.assert_not_called()
        self.assertEqual(result["mode"], "bm25")
        self.assertEqual({item["path"] for item in result["results"]},
                         {"projects/alpha.py", "projects/gamma.py"})
        self.assertEqual({(item["start_line"], item["end_line"]) for item in result["results"]},
                         {(1, 1)})

    def test_vector_and_hybrid_rankings_are_real_and_deterministic(self):
        with mock.patch.object(CODE, "embed_texts", side_effect=self.ranked_embeddings) as endpoint:
            vector = CODE.query_index(self.config, "alpha", mode="vector", top=3)
            hybrid = CODE.query_index(self.config, "alpha", mode="hybrid", top=2)
            repeated = CODE.query_index(self.config, "alpha", mode="hybrid", top=2)
        self.assertEqual(endpoint.call_count, 3)
        self.assertEqual(vector["mode"], "vector")
        self.assertEqual([item["path"] for item in vector["results"]],
                         ["projects/alpha.py", "projects/gamma.py", "projects/beta.py"])
        self.assertEqual(hybrid["mode"], "hybrid")
        self.assertEqual(hybrid, repeated)
        self.assertEqual({item["path"] for item in hybrid["results"]}, {"projects/alpha.py", "projects/gamma.py"})
        self.assertTrue(all(item["score"] > 0 for item in hybrid["results"]))

    def test_model_and_endpoint_mismatch_refuse_semantic_queries_before_network(self):
        for override in ({"model": "different-model"}, {"endpoint": "https://different.example"}):
            config = self.write_config({**self.settings, **override})
            with self.subTest(override=override), mock.patch.object(CODE, "embed_texts") as endpoint:
                for mode in ("vector", "hybrid"):
                    with self.assertRaises(CODE.CodeSearchError):
                        CODE.query_index(config, "alpha", mode=mode)
                self.assertTrue(CODE.query_index(config, "alpha", mode="bm25")["results"])
            endpoint.assert_not_called()

    def test_embedding_failure_never_becomes_a_lexical_fallback(self):
        for mode in ("vector", "hybrid"):
            with self.subTest(mode=mode), mock.patch.object(
                CODE, "embed_texts", side_effect=CODE.CodeSearchError("unavailable endpoint")
            ), self.assertRaises(CODE.CodeSearchError):
                CODE.query_index(self.config, "alpha", mode=mode)

    def test_status_reports_new_changed_and_deleted_paths_without_writing(self):
        before = self.config.index_path.read_bytes()
        self.write("projects/alpha.py", "changed alpha\n")
        (self.root / "projects" / "beta.py").unlink()
        self.write("projects/new.py", "new source\n")
        with mock.patch.object(CODE, "embed_texts") as endpoint:
            result = CODE.index_status(self.config)
        endpoint.assert_not_called()
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["chunks"], 3)
        self.assertEqual(result["embedded"], 3)
        self.assertEqual(result["dimensions"], 2)
        self.assertEqual(result["stale_paths"], ["projects/alpha.py", "projects/beta.py"])
        self.assertEqual(result["new_paths"], ["projects/new.py"])
        self.assertTrue(result["needs_build"])
        self.assertEqual(self.config.index_path.read_bytes(), before)

    def test_status_reflects_configured_selection_and_vector_settings(self):
        config = self.write_config({**self.settings, "source_dirs": ["wiki"], "model": "new-model"})
        result = CODE.index_status(config)
        self.assertEqual(result["source_dirs"], ["wiki"])
        self.assertEqual(result["stale_paths"], ["projects/alpha.py", "projects/beta.py", "projects/gamma.py"])
        self.assertTrue(result["configuration_stale"])
        self.assertTrue(result["needs_build"])


class CliTests(IndexedVaultCase):
    def test_missing_index_status_and_query_do_not_create_state(self):
        with mock.patch.object(CODE, "embed_texts") as endpoint:
            for arguments in (["status"], ["query", "source", "--mode", "bm25"],
                              ["query", "source", "--mode", "vector"]):
                with self.subTest(arguments=arguments):
                    exit_code, result = self.main_json(arguments)
                    self.assertNotEqual(exit_code, 0)
                    self.assertFalse(result["ok"])
        endpoint.assert_not_called()
        self.assertFalse((self.root / ".vault-meta").exists())

    def test_complete_cli_json_interface_and_read_only_doctor(self):
        self.write("projects/a.py", "source handler\n")
        with mock.patch.object(CODE, "embed_texts", side_effect=self.embeddings):
            exit_code, built = self.main_json(["build"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(built["ok"])
        before = self.config.index_path.read_bytes()
        exit_code, doctor = self.main_json(["doctor"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(doctor["missing_dependencies"], [])
        self.assertTrue(doctor["dependencies"]["sqlite_fts5"]["available"])
        exit_code, status = self.main_json(["status"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(status["needs_build"])
        exit_code, query = self.main_json(["query", "handler", "--mode", "bm25", "--top", "1"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(query["mode"], "bm25")
        self.assertEqual(query["results"][0]["path"], "projects/a.py")
        self.assertEqual(self.config.index_path.read_bytes(), before)

    def test_invalid_top_values_fail_without_creating_index(self):
        for top in ("0", "-1", "not-an-integer", str(CODE.MAX_TOP + 1)):
            with self.subTest(top=top), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exit_code:
                    CODE.main(["--root", str(self.root), "query", "source", "--top", top])
            self.assertEqual(exit_code.exception.code, 2)
        self.assertFalse((self.root / ".vault-meta").exists())

    def test_doctor_invalid_source_directory_fails_without_installation(self):
        self.write("kb-config.json", json.dumps({"code_search": {"source_dirs": ["missing"]}}))
        exit_code, result = self.main_json(["doctor"])
        self.assertEqual(exit_code, 2)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        self.assertIn("git", result["dependencies"])
        self.assertFalse((self.root / ".vault-meta").exists())


if __name__ == "__main__":
    unittest.main()
