---
name: wiki-prose
description: "Write wiki prose without AI tells, in English, Spanish or Catalan: rewrite agent-sounding pages, log and hot entries, deliverables and answers into plain, human prose (STE-80 structure). Triggers on: humanize wiki text, remove AI writing, de-AI, sounds robotic, simplify prose, ASD-STE100, STE, tightened deliverable, passiu, gerundis, «cabe destacar», «cal destacar», treure l'estil IA, passiu perifràstic. For markdown syntax (wikilinks, callouts, frontmatter) use wiki-markdown; this skill owns the words."
allowed-tools: Read Write Edit Glob Grep Bash
---

# wiki-prose: prose without AI tells

Vault pages, `log.md` entries, `hot.md` bullets and deliverables are mostly
agent-written. Readers notice. This skill rewrites that prose so a careful
person could have written it, and checks drafts deterministically.

Two passes:

- **Pass 1 (§ tells)** removes AI tells — habits people read as machine writing.
- **Pass 2 (S rules)** rebuilds sentence structure the way ASD-STE100 does,
  softened to 80% (Karpathy's method; full STE is too stiff for wiki prose).

## Pass 1: the tells

Act on §1-§5 at a single sighting; the rest act when they cluster.

| § | Tell | Fix |
|---|---|---|
| 1 | Not X but Y ("no solo... sino que", "it's not X, it's Y") | state the point |
| 2 | One-line closers, dramatic fragments ("That's the real win.") | cut or merge |
| 3 | Sayings that sound deep ("at its core") | the specific claim |
| 4 | Staged run-up ("Let's dive in", "We're thrilled to announce") | delete; start with the point |
| 5 | Arguing with no one ("To be clear, I'm not saying...") | drop the defense |
| 6 | Forced rule of three; false ranges ("from X to Y" with no scale) | keep only real structure |
| 7 | Repeated sentence openings | merge or lead with the action |
| 8 | Em dash as glue (vault style already forbids it in some skills) | period, comma, colon |
| 9 | Stacked hedges ("podría posiblemente", "could potentially") | one real qualifier |
| 10 | Passive with hidden actor ("se ha llevado a cabo") | name who acts |
| 11 | AI vocabulary (delve, leverage, seamless, pivotal, landscape) | the plain word |
| 12 | Inflated significance ("marks a pivotal moment", generic bright-future closers) | keep the fact, end on it |
| 13 | Vague association ("linked to", "asociado a") | name the relation the source gives |
| 14 | Shallow riders ("-ing"/gerundio: "ensuring", "garantizando", "garantint") | delete or split into a sentence |
| 15 | Sales language (groundbreaking, world-class, innovador as filler) | say what the thing does |
| 16 | Borrowed authority ("experts argue", "la industria coincide") | name who said what, or cut |
| 17 | Copula avoidance ("serves as", "constituye") | is / are / es / és |
| 18 | Bold label + colon on every list item | prose; bold only for warnings/UI |
| 19 | Decorative headings (Title Case, emoji) | sentence case, name the content |
| 20 | Curly quotes | straight quotes |
| 21 | Chatbot residue ("I hope this helps", "¿Quieres que...?") | remove the wrapper |
| 22 | Knowledge-limit guesses ("as of my last update") | say what the source lacks, or cut |
| 23 | Heading repeated in the first line | delete that line |
| 24 | Text about the document ("la siguiente tabla compara") | write about the subject |
| 25 | Re-explaining what the reader already knows | lead with the decision |
| 26 | Outline conclusions ("pese a estos retos... sigue avanzando") | the concrete facts |

## Pass 2: STE-80 structure

| S | Rule |
|---|---|
| S1 | One idea per sentence; one action per step. |
| S2 | Instructions ≤ 20 words, descriptions ≤ 25; a number with its unit is one word. |
| S3 | Condition first, then the command. |
| S4 | Keep articles and the verb; fragments only in tables and headings. |
| S5 | Sequences and options become vertical lists; number steps. |
| S6 | Active voice; name who acts. |
| S7 | Simple tenses: present, past, "will"/"farà", imperative. |
| S8 | Action in the verb ("decide", not "take a decision"); no gerund chains. |
| S9 | Honest modals; keep the source's certainty ("pot fallar" never becomes "falla"). |
| S10 | Plain words, one meaning each. |
| S11 | One term per thing; no synonym rotation. |
| S12 | Three nouns in a row at most. |
| S13 | One topic per paragraph, ≤ 6 sentences, topic first. |
| S14 | Warnings before the step they protect. |
| S15 | No semicolons; no dashes (§8). |

## Language rules (ES / CA)

Work in the page's language; never translate while editing. The tells transfer;
the word lists do not. Common cases:

- "cabe destacar / cal destacar" → delete; state the fact.
- "no solo X, sino que Y" / "no només X, sinó que Y" → X y también Y, or just Y.
- Gerund riders: "..., garantizando la trazabilidad", "..., garantint la
  traçabilitat" → split into a sentence with a real claim, or cut.
- Passives: "se ha llevado a cabo" / "s'ha dut a terme" → who did it, in what
  tense. "llevar a cabo" → hacer; "dur a terme" → fer.
- "en la actualidad / en el panorama actual" → hoy, or the date.
- Closers: "El futuro se presenta prometedor" / "El futur es presenta
  esperançador" → end on the last fact.
- Filler: "de cara a" → para / per a; "un amplio abanico" → varios.

## Fidelity rules (non-negotiable)

Wiki prose carries claims. The rewrite must not change them.

- List the facts first (claims, numbers, names, dates, quotes, certainty).
- An added or lost fact is an error; compare against the list at the end.
- Keep certainty at its strength; keep numbers, names and identifiers exact.
- Never touch: wikilinks and their targets, code, commands, paths, URLs,
  frontmatter, citations `^[...]`, tables' data cells.
- Flag contradictions in a note; do not resolve them silently.

## When not to act

Quotes, titles, names, and pre-2022 text. Fiction and brand copy: Pass 1 only.
Legal or contractual deliverable sections: offer a plain summary beside them,
never rewrite the normative text. A writing sample from the user overrides the
style defaults, not the fidelity rules.

## Checker

`scripts/prose_check.py` — deterministic, stdlib, EN/ES/CA. Finds places to
look at; it is not an AI detector.

```bash
python3 .agents/skills/wiki-prose/scripts/prose_check.py wiki/hot.md
python3 .agents/skills/wiki-prose/scripts/prose_check.py --max-words 30 wiki/log.md
```

Use it on drafts and on pages you are about to edit; findings are hints, and a
page may keep a "finding" when the sentence earns it (a real list of three, a
quoted slop example).
