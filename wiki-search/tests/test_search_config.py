#!/usr/bin/env python3
"""Tests for per-search embedding settings in kb-config.yaml.

Wiki search and code search can use different embedding models and endpoints
from one config file. Each skill owns a section (`wiki_search`, `code_search`);
the shared `embeddings` section is the fallback for both.

The failure this guards against is silent and expensive: if `wiki_search` is
ignored, wiki search embeds with the wrong model, and the resulting index has
the wrong dimension — `status` still reports a healthy index, so nothing
complains until a query returns garbage.

Precedence per key: own section -> `embeddings` -> built-in default.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).parents[2]
WIKI_SCRIPT = REPO / "wiki-search" / "scripts" / "wiki-search.py"
CODE_SCRIPT = REPO / "code-search" / "scripts" / "code-search.py"


def load_module(script: Path, root: Path):
    module_name = f"search_cfg_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, script)
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


class SearchConfigCase(unittest.TestCase):
    config: str | None = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="search-config-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "wiki").mkdir()
        (self.root / "projects").mkdir()
        if self.config is not None:
            (self.root / "kb-config.yaml").write_text(self.config, encoding="utf-8")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def wiki(self):
        return load_module(WIKI_SCRIPT, self.root)

    def code(self):
        module = load_module(CODE_SCRIPT, self.root)
        return module, module.load_config(module.find_root(self.root))


class OwnSectionTests(SearchConfigCase):
    config = (
        "embeddings:\n"
        "  model: bge-m3\n"
        "  endpoint: http://shared:11434\n"
        "wiki_search:\n"
        "  model: qwen3-embedding:0.6b\n"
        "  endpoint: http://wiki-only:11434\n"
        "code_search:\n"
        "  model: nomic-embed-text\n"
        "  endpoint: http://code-only:11434\n"
    )

    def test_wiki_uses_its_own_section(self):
        module = self.wiki()
        self.assertEqual(module.DEFAULT_MODEL, "qwen3-embedding:0.6b")
        self.assertEqual(module.OLLAMA_URL, "http://wiki-only:11434")

    def test_code_uses_its_own_section(self):
        _, cfg = self.code()
        self.assertEqual(cfg.model, "nomic-embed-text")
        self.assertEqual(cfg.endpoint, "http://code-only:11434")

    def test_sections_do_not_leak_into_each_other(self):
        wiki = self.wiki()
        _, cfg = self.code()
        self.assertNotEqual(wiki.DEFAULT_MODEL, cfg.model)
        self.assertNotEqual(wiki.OLLAMA_URL, cfg.endpoint)


class FallbackTests(SearchConfigCase):
    config = (
        "embeddings:\n"
        "  model: bge-m3\n"
        "  endpoint: http://shared:11434\n"
    )

    def test_wiki_falls_back_to_shared_embeddings(self):
        module = self.wiki()
        self.assertEqual(module.DEFAULT_MODEL, "bge-m3")
        self.assertEqual(module.OLLAMA_URL, "http://shared:11434")

    def test_code_falls_back_to_shared_embeddings(self):
        _, cfg = self.code()
        self.assertEqual(cfg.model, "bge-m3")
        self.assertEqual(cfg.endpoint, "http://shared:11434")


class NoCrossLeakTests(SearchConfigCase):
    """`wiki_search` must never become a fallback for code search.

    Merging the two sections reads as a convenience and is a silent bug: code
    search would embed with the wiki model and compare against an index built
    by a different one.
    """

    config = (
        "embeddings:\n"
        "  model: bge-m3\n"
        "  endpoint: http://shared:11434\n"
        "wiki_search:\n"
        "  model: qwen3-embedding:0.6b\n"
        "  endpoint: http://wiki-only:11434\n"
    )

    def test_code_ignores_wiki_section(self):
        _, cfg = self.code()
        self.assertEqual(cfg.model, "bge-m3")
        self.assertEqual(cfg.endpoint, "http://shared:11434")


class PartialOverrideTests(SearchConfigCase):
    """A section may set only one key; the rest come from `embeddings`."""

    config = (
        "embeddings:\n"
        "  model: bge-m3\n"
        "  endpoint: http://shared:11434\n"
        "wiki_search:\n"
        "  model: qwen3-embedding:0.6b\n"
    )

    def test_wiki_overrides_model_only(self):
        module = self.wiki()
        self.assertEqual(module.DEFAULT_MODEL, "qwen3-embedding:0.6b")
        self.assertEqual(module.OLLAMA_URL, "http://shared:11434")


class BlankValueTests(SearchConfigCase):
    config = (
        "embeddings:\n"
        "  model: bge-m3\n"
        "  endpoint: http://shared:11434\n"
        "wiki_search:\n"
        "  model: '   '\n"
        "  endpoint: ''\n"
    )

    def test_blank_strings_fall_back_instead_of_being_used(self):
        module = self.wiki()
        self.assertEqual(module.DEFAULT_MODEL, "bge-m3")
        self.assertEqual(module.OLLAMA_URL, "http://shared:11434")


class DefaultTests(SearchConfigCase):
    config = None

    def test_no_config_keeps_boring_defaults(self):
        module = self.wiki()
        self.assertEqual(module.DEFAULT_MODEL, "bge-m3")
        self.assertEqual(module.OLLAMA_URL, "http://127.0.0.1:11434")


class EnvMacroTests(SearchConfigCase):
    config = (
        "wiki_search:\n"
        "  endpoint: ${SEARCH_TEST_URL:-http://macro-default:11434}\n"
    )

    def test_macros_expand_inside_the_own_section(self):
        module = self.wiki()
        self.assertEqual(module.OLLAMA_URL, "http://macro-default:11434")

    def test_environment_wins_over_the_macro_default(self):
        os.environ["SEARCH_TEST_URL"] = "http://from-env:11434"
        try:
            module = self.wiki()
        finally:
            os.environ.pop("SEARCH_TEST_URL", None)
        self.assertEqual(module.OLLAMA_URL, "http://from-env:11434")


class CodeSectionTypeTests(SearchConfigCase):
    config = "code_search:\n  - not\n  - a\n  - mapping\n"

    def test_non_mapping_section_is_rejected(self):
        module = load_module(CODE_SCRIPT, self.root)
        with self.assertRaises(module.CodeSearchError):
            module.load_config(module.find_root(self.root))


if __name__ == "__main__":
    unittest.main()


class QueryPrefixTests(SearchConfigCase):
    """Instruction-aware models need the prefix on the query, never on documents.

    Missing prefix does not raise: the model returns vectors, the index stays
    valid, and only relevance drops. On the code corpus that was MRR 0.478 vs
    0.771, so a silent regression is the default outcome and needs a test.
    """

    config = (
        "wiki_search:\n"
        "  model: qwen3-embedding:0.6b\n"
        '  query_prefix: "Instruct: retrieve the page\\nQuery:"\n'
    )

    def test_prefix_is_read_from_config(self):
        module = self.wiki()
        self.assertEqual(module.QUERY_PREFIX, "Instruct: retrieve the page\nQuery:")

    def test_prefix_defaults_to_empty(self):
        Path(self.root / "kb-config.yaml").write_text(
            "wiki_search:\n  model: bge-m3\n", encoding="utf-8"
        )
        module = self.wiki()
        self.assertEqual(module.QUERY_PREFIX, "")

    def test_non_string_prefix_is_rejected(self):
        Path(self.root / "kb-config.yaml").write_text(
            "wiki_search:\n  query_prefix: [1, 2]\n", encoding="utf-8"
        )
        with self.assertRaises(SystemExit):
            self.wiki()

    def test_query_embedding_carries_the_prefix_but_documents_do_not(self):
        module = self.wiki()
        seen = []

        def fake_embed(texts, _model):
            seen.append(list(texts))
            return [[0.5, 0.5] for _ in texts]

        module.embed_batch = fake_embed
        module.vector_rank = lambda conn, qvec, depth: []
        module.rank_chunks(None, "fts5", "saml protocolo", "vector", "m", 2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "Instruct: retrieve the page\nQuery:saml protocolo")

    def test_bm25_ranking_uses_the_raw_query(self):
        module = self.wiki()
        captured = {}

        def fake_bm25(conn, backend, qtext, depth):
            captured["qtext"] = qtext
            return []

        module.bm25_rank = fake_bm25
        module.rank_chunks(None, "fts5", "saml protocolo", "bm25", "m", 2)
        self.assertEqual(captured["qtext"], "saml protocolo")
