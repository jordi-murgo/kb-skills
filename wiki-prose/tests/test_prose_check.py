#!/usr/bin/env python3
"""Tests for wiki-prose prose_check.py — EN/ES/CA AI-tell linter.

Per repo rules: a check must be proven to fail on a fault before it is trusted.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prose_check.py"


class ProseCheckCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="prose-check-test-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def file(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_check(self, *paths: Path, extra: tuple[str, ...] = ()):
        return subprocess.run(
            ["python3", str(SCRIPT), *[str(p) for p in paths], *extra],
            capture_output=True, text=True,
        )

    def test_slop_english_fails(self):
        p = self.file("en.md",
            "We're thrilled to announce our groundbreaking release! "
            "It's not just an update—it's a complete reimagining. "
            "Version 2.0 leverages cutting-edge AI to deliver a seamless "
            "experience, ensuring your team accomplishes more. "
            "The future looks bright!\n")
        r = self.run_check(p)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        for rule in ("not-x-but-y", "ing-rider", "word", "closer", "dash"):
            self.assertIn(rule, r.stdout)

    def test_slop_spanish_fails(self):
        p = self.file("es.md",
            "En el panorama actual de la IA, cabe destacar que nuestra "
            "innovadora plataforma no solo garantiza resultados, sino que "
            "también asegura el éxito del equipo, garantizando así una "
            "ventaja competitiva.\n")
        r = self.run_check(p)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        for rule in ("gerund-rider", "word", "not-x-but-y", "sentence-length"):
            self.assertIn(rule, r.stdout)

    def test_slop_catalan_fails(self):
        p = self.file("ca.md",
            "Cal destacar que la plataforma no només funciona, sinó que "
            "també destaca per la seva robustesa, garantint així resultats.\n")
        r = self.run_check(p)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("'cal destacar'", r.stdout)
        self.assertIn("gerund-rider", r.stdout)
        self.assertIn("not-x-but-y", r.stdout)

    def test_clean_prose_passes(self):
        p = self.file("clean.md",
            "La gate comprueba que una ruta citada existe en disco. "
            "Solo comprueba tokens cuyo primer segmento es un directorio "
            "del vault. Los nombres sueltos no cuentan. Ejecuta la gate y "
            "lee el informe.\n")
        r = self.run_check(p)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_code_fences_and_inline_code_are_ignored(self):
        p = self.file("fence.md",
            "Texto bueno.\n\n```bash\nrun --x; it leverages the seamless API\n"
            "```\n\nCorre `prose_check.py --json` y lee el `output`.\n")
        r = self.run_check(p)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_max_words_flag(self):
        long_one = " ".join(["palabra"] * 30) + ".\n"
        p = self.file("long.md", long_one)
        self.assertEqual(self.run_check(p).returncode, 1)
        self.assertEqual(self.run_check(p, extra=("--max-words", "40")).returncode, 0)

    def test_stdin_dash(self):
        r = subprocess.run(
            ["python3", str(SCRIPT), "-"],
            input="Delve into the seamless platform.\n",
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("'delve'", r.stdout)

    def test_json_output(self):
        import json
        p = self.file("j.md", "It's not just a tool, it's a lever.\n")
        r = self.run_check(p, extra=("--json",))
        data = json.loads(r.stdout)
        self.assertEqual(data["total"], data["total"])
        self.assertGreaterEqual(data["total"], 1)
        self.assertIn("rule", data["findings"][str(p)][0])

    def test_missing_file_is_usage_error(self):
        r = self.run_check(self.dir / "nope.md")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
