#!/usr/bin/env python3
"""Hermetic tests for spell_probe.py — no hunspell installation, no network.

The fake hunspell records its argv and stdin, and answers from a word list:
the same contract the real binary offers (unknown words on stdout). A missing
or broken binary is asserted to exit 2, never a silent pass.
"""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "spell_probe.py"


def load_module():
    spec = importlib.util.spec_from_file_location("spell_probe_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class FakeHunspell:
    """Builds a script that behaves like hunspell -l for the known dictionary."""


    def __init__(self, dictionary=frozenset()):
        self.dictionary = set(dictionary)
        self.calls = []
        self.bin_path = None

    def install(self, directory: Path) -> Path:
        self.bin_path = directory / "hunspell"
        self.bin_path.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"DICTIONARY = {json.dumps(sorted(self.dictionary))}\n"
            "DICTIONARY = set(DICTIONARY)\n"
            "DICTIONARY_LC = {w.lower() for w in DICTIONARY}\n"
            "stdin = sys.stdin.read()\n"
            "for word in stdin.splitlines():\n"
            "    known = word in DICTIONARY or word.lower() in DICTIONARY_LC\n"
            "    if word and not known:\n"
            "        print(word)\n",
            encoding="utf-8")
        self.bin_path.chmod(0o755)
        return self.bin_path

    def seen_words(self):
        out = []
        for argv, stdin in self.calls:
            out.extend(stdin.splitlines())
        return out


class ProbeCase(unittest.TestCase):
    """Subprocess-level tests through the real CLI."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spell-probe-test-"))
        self.fake = FakeHunspell(dictionary={
            "el", "la", "de", "i", "és", "una", "eina", "detecta", "errors",
            "ortogràfics", "importància", "formació", "govern", "sequera",
            "the", "quick", "brown", "fox", "hola", "bon", "dia", "com", "va",
            "vegeu", "clara", "amb", "dins", "titol",
        })

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_probe(self, *extra, text="El govern és una eina.\n", name="draft.md",
                  stdin=False, fake=None):
        binary = (fake or self.fake).install(self.tmp)
        if not stdin:
            (self.tmp / name).write_text(text, encoding="utf-8")
            argv = ["python3", str(SCRIPT), "--hunspell-bin", str(binary), *extra, name]
            cwd = self.tmp
        else:
            argv = ["python3", str(SCRIPT), "--hunspell-bin", str(binary), *extra, "-"]
            cwd = self.tmp
        proc = subprocess.run(argv, capture_output=True, text=True, cwd=cwd,
                              input=text if stdin else None)
        return proc

    def test_clean_text_exits_zero(self):
        r = self.run_probe("--lang", "ca")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 finding(s)", r.stdout)

    def test_unknown_word_reported_with_line(self):
        r = self.run_probe("--lang", "ca",
                           text="El govern és una eina.\nLa importancia és clara.\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("importancia", r.stdout)
        self.assertRegex(r.stdout, r"(?m)^\s*2\s+unknown-word")

    def test_fenced_code_and_inline_code_never_reach_hunspell(self):
        r = self.run_probe("--lang", "ca", text=(
            "```bash\npgvector installl --flagx\n```\n"
            "El govern és una eina amb `cmdrunpg` dins.\n"))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("pgvector", r.stdout)
        self.assertNotIn("cmdrunpg", r.stdout)
        self.assertNotIn("installl", r.stdout)

    def test_wikilinks_citations_urls_never_reach_hunspell(self):
        r = self.run_probe("--lang", "ca", text=(
            "Vegeu [[alguna-pàgina-falsa]] i ^[font-inexistent] i "
            "https://url-falsa.example/tres.\nEl govern és una eina.\n"))
        self.assertEqual(r.returncode, 0, r.stdout)
        sent = "\n".join(r.stdout.splitlines())
        for banned in ("alguna", "pàgina", "falsa", "font", "inexistent", "url", "example"):
            self.assertNotIn(banned, sent, f"{banned} leaked from a protected span")

    def test_frontmatter_ignored_and_lang_declared(self):
        r = self.run_probe(text=(
            "---\nlang: ca\ntitle: Titol-fals del document\n---\n\n"
            "El govern és una eina.\n"))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_missing_binary_exits_two_with_message(self):
        r = self.run_probe("--hunspell-bin", str(self.tmp / "no-such-bin"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("hunspell not found", r.stderr)
        self.assertIn("not a pass", r.stderr)

    def test_broken_binary_exits_two_not_silent_zero(self):
        binary = self.fake.install(self.tmp)
        # corrupt it AFTER install: run the CLI directly, not via run_probe
        # (which would reinstall the working fake)
        binary.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
        binary.chmod(0o755)
        (self.tmp / "draft.md").write_text("El govern és una eina.\n",
                                           encoding="utf-8")
        proc = subprocess.run(
            ["python3", str(SCRIPT), "--hunspell-bin", str(binary),
             "--lang", "ca", "draft.md"],
            capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("hunspell failed", proc.stderr)

    def test_unsupported_language_exits_two(self):
        r = self.run_probe("--lang", "fr")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unsupported language", r.stderr)

    def test_stdin_mode(self):
        r = self.run_probe("--lang", "ca", stdin=True,
                           text="La importancia és clara.\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("importancia", r.stdout)

    def test_json_output_shape(self):
        r = self.run_probe("--lang", "ca", "--json",
                           text="La importancia és clara.\n")
        payload = json.loads(r.stdout)
        self.assertEqual(payload["total"], 1)
        finding = payload["findings"]["draft.md"][0]
        self.assertEqual(finding["rule"], "unknown-word")
        self.assertEqual(finding["line"], 1)
        self.assertIn("importancia", finding["msg"])


class ModuleCase(unittest.TestCase):
    """Direct unit tests for masking and language resolution."""

    def setUp(self):
        self.module = load_module()

    def test_mask_line_removes_protected_spans(self):
        line = "usa `codi` i [[wiki-link]] amb https://x.example i v2.4"
        masked = self.module.mask_line(line)
        for banned in ("codi", "wiki", "link", "https", "example", "2.4"):
            self.assertNotIn(banned, masked)
        # prose words survive the mask
        self.assertIn("usa", masked)
        self.assertIn("amb", masked)

    def test_lang_precedence_cli_over_frontmatter(self):
        text = "---\nlang: es\n---\ncos\n"
        self.assertEqual(self.module.detect_lang(text, None, "ca"), "ca")
        self.assertEqual(self.module.detect_lang(text, "en", None), "en")
        self.assertEqual(self.module.detect_lang(text, None, None), "es")
        self.assertEqual(self.module.detect_lang("cos", None, None), "ca")

    def test_glossary_filters_known_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "wiki").mkdir()
            (root / ".git").mkdir()
            (root / ".cache" / "spell-probe").mkdir(parents=True)
            (root / ".cache" / "spell-probe" / "glossary-ca.txt").write_text(
                "# vault terms\nkb-skills\nvulcano\n", encoding="utf-8")
            self.assertEqual(self.module.load_glossary(root, "ca"),
                             ["kb-skills", "vulcano"])
            self.assertEqual(self.module.load_glossary(root, "es"), [])


if __name__ == "__main__":
    unittest.main()
