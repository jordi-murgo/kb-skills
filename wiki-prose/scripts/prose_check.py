#!/usr/bin/env python3
"""prose_check: heuristic linter for AI tells and STE-style prose (EN/ES/CA).

Finds places to look at. It is not an AI detector and it does not prove
compliance with ASD-STE100. Adapted from scripts/ste_check.py of
Dodaios/ste80-humanizer (MIT), with Spanish and Catalan tell lists added for
the kb-skills vaults, whose pages are mostly agent-written Spanish.

Stdlib only, read-only, no vault root needed: pass files (or '-' for stdin).

    python3 prose_check.py wiki/entities/foo.md
    python3 prose_check.py --max-words 30 wiki/hot.md
    cat draft.md | python3 prose_check.py -

Exit 0 clean, 1 findings, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# --- English word swaps (ste80 working list, trimmed to safe hits) -----------
EN_WORDS = {
    "delve": "examine",
    "utilize": "use",
    "utilises": "uses",
    "utilizes": "uses",
    "utilising": "using",
    "utilizing": "using",
    "leverage": "use",
    "leverages": "uses",
    "leveraging": "using",
    "seamless": "(delete)",
    "seamlessly": "(delete)",
    "pivotal": "important",
    "crucial": "important",
    "testament": "(delete) or say what it shows",
    "tapestry": "mix",
    "groundbreaking": "(fact or delete)",
    "cutting-edge": "(fact or delete)",
    "world-class": "(fact or delete)",
    "state-of-the-art": "(fact or delete)",
    "vibrant": "(delete)",
    "boasts": "has",
    "myriad": "many",
    "plethora": "many",
    "moreover": "also",
    "furthermore": "also",
    "additionally": "also",
    "showcasing": "showing",
    "underscore": "stress",
    "underscores": "stresses",
    "underscoring": "stressing",
    "fostering": "helping",
    "landscape": "field, area",
    "realm": "field, area",
    "elevate": "improve",
    "unlock": "enable",
    "embark": "start",
    "navigate": "handle",
    "holistic": "complete",
    "intricate": "complex",
    "meticulous": "careful",
    "accomplish": "do",
}

# --- Spanish / Catalan multi-word tells --------------------------------------
ES_CA_PHRASES = {
    "cabe destacar": "(delete; state the fact)",
    "cabe señalar": "(delete; state the fact)",
    "cal destacar": "(delete; state the fact)",
    "cal remarcar": "(delete; state the fact)",
    "en la actualidad": "hoy, ahora — or name the date",
    "en l'actualitat": "avui, ara — or name the date",
    "a día de hoy": "hoy",
    "a dia d'avui": "avui",
    "en el panorama actual": "hoy — or name the date",
    "en el actual panorama": "hoy — or name the date",
    "llevar a cabo": "hacer",
    "duu a terme": "fa",
    "dur a terme": "fer",
    "de cara a": "para / per a",
    "poner de relieve": "mostrar",
    "posar de manifest": "mostrar",
    "poner en relieve": "mostrar",
    "un amplio abanico": "varios",
    "un ampli ventall": "varis",
}

CLOSERS = [
    r"the future looks bright",
    r"only time will tell",
    r"el futuro se presenta",
    r"solo el tiempo dirá",
    r"la clave está en",
    r"el futur es presenta",
    r"només el temps dirà",
    r"exciting times (lie )?ahead",
    r"tiempos emocionantes",
]

# ", ensuring success" / ", garantizando el éxito" — a clause that only decorates
ING_RIDERS = re.compile(
    r",\s*(ensuring|highlighting|showcasing|reflecting|fostering|demonstrating|"
    r"underscoring|enabling|driving|empowering|streamlining|maximizing|"
    r"leapfrogging|cementing)\b",
    re.I,
)
GERUND_RIDERS = re.compile(
    r",\s*(garantizando|reflejando|destacando|asegurando|permitiendo|generando|"
    r"optimizando|impulsando|potenciando|posicionando|maximizando|agilizando|"
    r"garantint|reflectint|permetent|generant|impulsant|potenciant|"
    r"posant en relleu|poniendo en relieve|poniendo de manifiesto)\b",
    re.I,
)

NOT_X_BUT_Y = [
    re.compile(r"\bnot (just|only|merely)\b.{0,80}?\b(but|instead)\b", re.I | re.S),
    re.compile(r"\bit'?s not\b.{0,60}?\bit'?s\b", re.I),
    re.compile(r"\bno (solo|solamente|tan solo)\b.{0,80}?\bsino\b", re.I | re.S),
    re.compile(r"\bno (només|tan sols)\b.{0,80}?\bsinó\b", re.I | re.S),
]

HEDGE_STACKS = [
    re.compile(r"\bcould (potentially|possibly|conceivably)\b", re.I),
    re.compile(r"\bmight (possibly|potentially|arguably)\b", re.I),
    re.compile(r"\bpodría (posiblemente|potencialmente)\b", re.I),
    re.compile(r"\ben (ciertos|algunos) casos podría\b", re.I),
    re.compile(r"\bpodria (possiblement|potencialment)\b", re.I),
    re.compile(r"\bcabría (pensar|decir) que\b", re.I),
]

BOLD_LABEL_LIST = re.compile(r"(\n^- \*\*[^*]{1,40}\*\*: ?.{0,100}){3,}", re.S | re.M)

FENCE = re.compile(r"^\s*(```|~~~)")
SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def iter_lines_outside_fences(text: str):
    """Yield (line_number, line) outside fenced code blocks, inline code kept.

    Code blocks show commands, not prose; a tell there is not a prose tell.
    """
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            fence = None if fence == marker else (fence or marker)
            continue
        if fence is None:
            yield number, line


def strip_inline_code(line: str) -> str:
    return re.sub(r"`[^`]*`", " ", line)


def sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(paragraph) if s.strip()]


def count_words(sentence: str) -> int:
    # A number with its unit and a hyphenated pair count as one word.
    return len(sentence.split())


def check_text(text: str, max_words: int) -> list[dict]:
    findings: list[dict] = []
    paragraphs: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start_line = 1
    last_line = 1

    for number, line in iter_lines_outside_fences(text):
        plain = strip_inline_code(line)
        if plain.strip():
            if not current:
                start_line = number
            current.append(plain.strip())
        else:
            if current:
                paragraphs.append((start_line, current))
            current = []
        last_line = number
    if current:
        paragraphs.append((start_line, current))

    for start, lines in paragraphs:
        para = " ".join(lines)
        # line-level tells
        for i, ln in enumerate(lines):
            where = start + i
            for m in ING_RIDERS.finditer(ln):
                findings.append({"line": where, "rule": "ing-rider",
                                 "msg": f"shallow '-ing' rider '{m.group(1)}': delete or make it a sentence"})
            for m in GERUND_RIDERS.finditer(ln):
                findings.append({"line": where, "rule": "gerund-rider",
                                 "msg": f"gerund rider '{m.group(1)}': delete or make it a sentence"})
            if "—" in ln or " – " in ln:
                findings.append({"line": where, "rule": "dash",
                                 "msg": "em/en dash as sentence glue: use a period, comma or colon"})
            if ";" in ln:
                findings.append({"line": where, "rule": "semicolon",
                                 "msg": "semicolon: write two sentences or a list"})
            if "\u201c" in ln or "\u201d" in ln:
                findings.append({"line": where, "rule": "curly",
                                 "msg": "curly quotes: use straight quotes"})
            low = ln.lower()
            for phrase, fix in ES_CA_PHRASES.items():
                if phrase in low:
                    findings.append({"line": where, "rule": "word",
                                     "msg": f"'{phrase}' -> {fix}"})
            for word, fix in EN_WORDS.items():
                if re.search(rf"\b{word}\b", low):
                    findings.append({"line": where, "rule": "word",
                                     "msg": f"'{word}' -> {fix}"})
            for pat in HEDGE_STACKS:
                m = pat.search(ln)
                if m:
                    findings.append({"line": where, "rule": "hedge-stack",
                                     "msg": f"stacked hedges '{m.group(0).strip()}': keep one qualifier"})
            for pat in CLOSERS:
                if re.search(pat, low):
                    findings.append({"line": where, "rule": "closer",
                                     "msg": "dramatic closer: end on the last concrete fact"})
                    break
        # paragraph-level tells
        for pat in NOT_X_BUT_Y:
            if pat.search(para):
                findings.append({"line": start, "rule": "not-x-but-y",
                                 "msg": "not-X-but-Y framing: state the point"})
                break
        if BOLD_LABEL_LIST.search("\n" + "\n".join(lines) + "\n"):
            findings.append({"line": start, "rule": "bold-label-list",
                             "msg": "bold label + colon on every list item: prose, or keep bold for warnings/UI"})
        for s in sentences(para):
            n = count_words(s)
            if n > max_words:
                findings.append({"line": start, "rule": "sentence-length",
                                 "msg": f"{n} words (max {max_words}): split it"})
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", help="files to check, or '-' for stdin")
    parser.add_argument("--max-words", type=int, default=25,
                        help="sentence word cap (default %(default)s)")
    parser.add_argument("--json", action="store_true", help="JSON findings")
    args = parser.parse_args(argv)

    total = 0
    report: dict[str, list] = {}
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
        findings = check_text(text, args.max_words)
        report[name] = findings
        total += len(findings)

    if args.json:
        print(json.dumps({"findings": report, "total": total}, ensure_ascii=False))
    else:
        for name, findings in report.items():
            by_line = sorted(findings, key=lambda f: f["line"])
            print(f"{name}: {len(findings)} finding(s)")
            for f in by_line:
                print(f"  {f['line']:>4}  {f['rule']:<16} {f['msg']}")
        print(f"total: {total} finding(s)")
    return 1 if total else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)
