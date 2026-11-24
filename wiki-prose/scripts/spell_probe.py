#!/usr/bin/env python3
"""spell_probe: detect-only orthography findings via the system hunspell.

The companion of prose_check.py, with the same contract: deterministic,
stdlib, read-only. It never rewrites anything — the agent applies corrections
with the diff in view. LanguageTool's measured failure mode (substituting on
technical text wrecks it) is why this tool only reports.

Hunspell is NOT bundled. It is resolved from the system (config key
spell.hunspell_bin, else PATH); the dictionaries are likewise the system's —
LGPL/GPL dictionary data must not enter this repo. A missing binary or
dictionary for the requested language is an explicit finding, never a silent
pass (a green checker that checked nothing is the failure mode this guards).

Protected fragments are removed before detection, not restored after: code,
inline code, URLs, e-mail, paths, wikilinks, citations, callouts, frontmatter
and any word containing a digit stay invisible to the dictionary, so they
cannot produce findings. What is never sent cannot be flagged.

    python3 spell_probe.py wiki/hot.md                # lang from frontmatter/config
    python3 spell_probe.py --lang ca draft.md
    python3 spell_probe.py --lang ca --json draft.md
    cat x.md | python3 spell_probe.py --lang ca -

Exit 0 no findings · 1 findings · 2 usage/unavailable (hunspell or dictionary
missing counts as 2 with an explicit message, not a silent 0).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

FENCE_BLOCK = re.compile(r"^[ \t]*(```|~~~)[^\n]*\n.*?\n[ \t]*(```|~~~)[^\n]*",
                         re.M | re.S)


PROTECT = [
    ("inline", re.compile(r"`[^`\n]*`")),
    ("wikilink", re.compile(r"\[\[[^\]]*\]\]")),
    ("mdlink", re.compile(r"\[[^\]]*\]\([^)]*\)")),
    ("cite", re.compile(r"\^\[[^\]]*\]")),                    # wiki citation
    ("callout", re.compile(r"\[![a-z-]+\][^>]*?>")),          # obsidian callout
    ("url", re.compile(r"https?://[^\s<>\"')]+")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("path", re.compile(r"(?<![\w.])/(?:[\w.-]+/)*[\w.-]+")),
    ("digitword", re.compile(r"\b[\w.+-]*\d[\w.+-]*\b")),     # 2.4, Q4_K_M, v17
]

FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)

# lang: field (already in the vault's frontmatter contract). Full-variant
# codes are required by some dictionary installs (ca_ES vs ca); both orders
# are accepted and both are passed to hunspell.
LANG_FIELD = re.compile(r"(?m)^lang:\s*([a-z]{2,3}(?:[-_][A-Z]{2})?)\s*$")

DEFAULT_LANG = "ca"

SUPPORTED = {"ca", "es", "en"}


def mask_line(line: str) -> str:
    """Replace every protected span with a space, keeping character offsets."""
    for _kind, pattern in PROTECT:
        line = pattern.sub(" ", line)
    return line


def split_frontmatter(text: str) -> tuple[str, str]:
    match = FRONTMATTER.match(text)
    if match:
        return match.group(0), text[match.end():]
    return "", text


def detect_lang(text: str, configured: str | None, cli: str | None) -> str:
    """Declared, never detected: CLI flag wins, then frontmatter, then config.

    The wrong-language dictionary produces noise, not damage — this is a
    warning, and the caller sees which language produced the findings.
    """
    for candidate in (cli, configured):
        if candidate:
            base = candidate.replace("_", "-").split("-")[0].lower()
            return base
    match = LANG_FIELD.search(text)
    if match:
        return match.group(1).replace("_", "-").split("-")[0].lower()
    return DEFAULT_LANG


def hunspell_command(config_bin: str | None) -> str | None:
    """Resolve the hunspell binary: config key wins, else PATH. None = absent."""
    if config_bin:
        path = Path(config_bin)
        if path.is_file() and shutil.which(str(path)):
            return str(path)
        return None
    return shutil.which("hunspell")


def load_glossary(root: Path, lang: str) -> list[str]:
    """Per-vault protected words under .cache/ — never inside the skill."""
    glossary = root / ".cache" / "spell-probe" / f"glossary-{lang}.txt"
    if not glossary.is_file():
        return []
    words = []
    for line in glossary.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


def find_repo_root() -> Path | None:
    """Vault root (wiki/ AND .git, wiki/ not a skill), cwd first. None if absent.

    Optional: spell_probe works on any file without a vault; the glossary is
    simply skipped.
    """
    for start in (Path.cwd().resolve(), Path(__file__).resolve()):
        for parent in (start, *start.parents):
            if ((parent / "wiki").is_dir() and (parent / ".git").exists()
                    and not (parent / "wiki" / "SKILL.md").exists()):
                return parent
    return None

def dictionary_candidates(lang: str) -> list[str]:
    """Dictionary names to try for a base language, in order.

    Installs ship full-variant names (es_ES, en_US) or bare codes (ca);
    hunspell only opens what exists. The first variant hunspell accepts is
    used for the whole run."""
    variants = {"ca": ["ca", "ca_ES"], "es": ["es_ES", "es"],
                "en": ["en_US", "en_GB", "en"]}
    return variants.get(lang, [lang])


def run_hunspell(binary: str, lang: str, words: list[str],
                 glossary: list[str]) -> tuple[int, str, list[str]]:
    """One hunspell -l invocation for the whole text; return (rc, stderr, unknown).

    -l prints the words the dictionary does not know. Glossary filtering is
    done in code after the call: hunspell's annotate modes change the output
    format, so the plain unknown-word list plus an in-process filter is the
    contract.
    """
    errors = []
    for name in dictionary_candidates(lang):
        argv = [binary, "-d", name, "-l"]
        proc = subprocess.run(argv, input="\n".join(words) + "\n",
                              capture_output=True, text=True, timeout=120,
                              env={"PATH": ":".join([str(Path(sys.executable).parent),
                                                     "/opt/homebrew/bin",
                                                     "/usr/local/bin",
                                                     "/usr/bin", "/bin"]),
                                   "HOME": os.environ.get("HOME", ""),
                                   "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"})
        if proc.returncode == 0:
            break
        errors.append(f"{name}: {proc.stderr.strip()}")
    else:
        return proc.returncode, "; ".join(errors) or "hunspell failed", []
    unknown = [w for w in proc.stdout.splitlines() if w.strip()]
    if glossary:
        unknown = [w for w in unknown if w not in set(glossary)]
    return proc.returncode, proc.stderr, unknown


def probe_text(text: str, lang: str, binary: str, glossary: list[str]) -> list[dict]:
    """Return findings [{line, rule, msg}] for real-prose unknown words."""
    # group words by the line they came from, masking before hunspell
    lines: dict[int, list[str]] = {}
    _fm, body = split_frontmatter(text)
    body = FENCE_BLOCK.sub(lambda m: "\n".join(" " for _ in m.group(0).splitlines()),
                           body)
    for number, line in enumerate(body.splitlines(), 1):
        masked = mask_line(line)
        for word in re.findall(r"[\wÀ-ÿ'’·-]+", masked):
            if len(word) >= 2:
                lines.setdefault(number, []).append(word)
    all_words = [w for ws in lines.values() for w in ws]
    rc, stderr, unknown = run_hunspell(binary, lang, all_words, glossary)
    if rc != 0:
        raise RuntimeError(stderr or f"hunspell exited {rc}")
    unknown_set = set(unknown)
    findings = []
    seen: set[str] = set()
    for number in sorted(lines):
        for word in lines[number]:
            if word in unknown_set and word not in seen:
                seen.add(word)
                findings.append({
                    "line": number, "rule": "unknown-word",
                    "msg": f"'{word}' is not in the {lang} dictionary",
                })
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", help="files to check, or '-' for stdin")
    parser.add_argument("--lang", help="language code (ca, es, en); default: "
                                       "frontmatter lang: or ca")
    parser.add_argument("--hunspell-bin", help="hunspell binary path "
                                               "(default: spell.hunspell_bin config, else PATH)")
    parser.add_argument("--json", action="store_true", help="JSON findings")
    args = parser.parse_args(argv)

    binary = hunspell_command(args.hunspell_bin)
    if binary is None:
        print("error: hunspell not found; install it or set spell.hunspell_bin "
              "in kb-config.yaml — a missing checker is an error, not a pass",
              file=sys.stderr)
        return 2

    root = find_repo_root()
    configured = None
    if root:
        for conf in (root / "kb-config.yaml", root / "kb-config.yml"):
            if conf.is_file():
                match = re.search(r"(?m)^\s*lang:\s*(\S+)", conf.read_text(encoding="utf-8"))
                if match and "spell" in conf.read_text(encoding="utf-8"):
                    configured = match.group(1)
                break

    report: dict[str, list] = {}
    total = 0
    for name in args.files:
        if name == "-":
            text = sys.stdin.read()
        else:
            try:
                with open(name, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                print(f"error: cannot read {name}: {e}", file=sys.stderr)
                return 2
        lang = detect_lang(text, configured, args.lang)
        if lang not in SUPPORTED:
            print(f"error: unsupported language {lang!r} "
                  f"(supported: {', '.join(sorted(SUPPORTED))})", file=sys.stderr)
            return 2
        glossary = load_glossary(root, lang) if root else []
        try:
            findings = probe_text(text, lang, binary, glossary)
        except RuntimeError as e:
            print(f"error: hunspell failed for {name}: {e}", file=sys.stderr)
            return 2
        report[name] = findings
        total += len(findings)

    if args.json:
        print(json.dumps({"findings": report, "total": total}, ensure_ascii=False))
    else:
        for name, findings in report.items():
            print(f"{name}: {len(findings)} finding(s)")
            for f in sorted(findings, key=lambda f: f["line"]):
                print(f"  {f['line']:>4}  {f['rule']:<14} {f['msg']}")
        print(f"total: {total} finding(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
