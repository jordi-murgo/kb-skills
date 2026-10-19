#!/usr/bin/env python3
"""Vet atomic claims before they enter the wiki using a local /v1/systemone decision model.

Each claim is scored with four noul questions (grounded, durable, duplicate,
sensitive). Verdicts: accept = grounded and durable and not sensitive and not
duplicate; reject = sensitive or not grounded; otherwise review.
"""

import argparse
import os
import json
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git, and wiki/ is not a skill.

    A skill directory named "wiki" lives under .agents/skills/ and this
    repository itself contains wiki/SKILL.md; either would otherwise satisfy
    the wiki/ + .git test and silently resolve to the wrong tree.
    """
    p = Path.cwd()
    for parent in [p] + list(p.parents):
        if (
            (parent / "wiki").is_dir()
            and (parent / ".git").exists()
            and not (parent / "wiki" / "SKILL.md").exists()
        ):
            return parent
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        if (
            (parent / "wiki").is_dir()
            and (parent / ".git").exists()
            and not (parent / "wiki" / "SKILL.md").exists()
        ):
            return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/ and .git")

def load_env_files(root: Path) -> None:
    """Fill os.environ from <root>/.env then <root>/.env.local (later file wins;
    keys set in the process environment always win over both). Plain KEY=VALUE
    lines, optional leading 'export ', '#' comments, blank lines ignored."""
    process_keys = set(os.environ)
    for name in (".env", ".env.local"):
        path = root / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in process_keys:
                os.environ[key] = value

MACRO_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_macros(value):
    """Expand ${VAR} / ${VAR:-default} in strings (unset or empty -> default,
    else ""). Non-strings pass through unchanged."""
    if isinstance(value, str):
        return MACRO_RE.sub(
            lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value
        )
    return value


def load_config_section(root: Path, section: str) -> dict:
    """Read one optional section of kb-config.{yaml,yml,json} at the vault root.
    Missing file or section -> {}. ${VAR} macros expand from the environment
    (process env, then .env/.env.local loaded above). Kept inline per the
    no-shared-import rule: a skill copied on its own must keep working."""
    candidates = [root / "kb-config.yaml", root / "kb-config.yml", root / "kb-config.json"]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path} is not valid JSON: {e}")
    else:
        try:
            import yaml
        except ImportError:
            raise SystemExit(
                f"{path} needs PyYAML, which is not installed.\n"
                "Run: uv add pyyaml   (or: python3 -m pip install pyyaml)"
            )
        try:
            cfg = yaml.safe_load(text)
        except Exception as e:
            raise SystemExit(f"{path} is not valid YAML: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path} must contain a mapping at the top level")
    sec = cfg.get(section) or {}
    if not isinstance(sec, dict):
        raise SystemExit(f"{path}: '{section}' must be a mapping")
    return {k: expand_macros(v) for k, v in sec.items()}


REPO_ROOT = find_vault_root()
load_env_files(REPO_ROOT)
_CFG = load_config_section(REPO_ROOT, "decisions")


VAULT_INDEX_PATH = REPO_ROOT / "wiki" / "index.md"
VAULT_INDEX_CHARS = 4000
QUOTE_MAX_CHARS = 800
CLAIMS_PER_REQUEST = 16
MAX_BODY_BYTES = 60 * 1024
DIMS = ("grounded", "durable", "duplicate", "sensitive")

# Per-axis thresholds. A single 0.5 cut for every axis is what the JevAgentHarness
# paper identifies as the cause of a 19-point success loss: delegation without a
# fitted threshold is worse than not delegating. The axes are also asymmetric on
# purpose — letting a secret through costs more than losing a page, so `sensitive`
# rejects low while `grounded` demands strong support.
#
# MEASURED LIMIT (clef-flash:9b, 23 labelled cases, 2026-10-19): the `grounded`
# axis does not separate support from non-support. Unsupported quotes scored
# 0.47–0.93 and supported ones 0.59–0.96 — overlapping distributions. Raising the
# cut to 0.90 reaches a 10% false-positive rate at the cost of 40% of real claims.
# The threshold is therefore a *recall/precision dial*, not a fix: an agent must
# never treat `accept` on this axis as proof that the quote supports the claim.
# Run scripts/calibration.py after any model change and read the overlap.
DEFAULT_THRESHOLDS = {
    "grounded": 0.90,   # quote must explicitly support the claim
    "durable": 0.60,    # stable fact vs transient note
    "duplicate": 0.75,  # index coverage
    "sensitive": 0.30,  # low bar: prefer over-flagging secrets
}
# When a probability lands inside the band around its threshold, the axis is not
# decided: the verdict drops to `review` instead of collapsing to a bit. The band
# stays narrow on purpose — at ±0.10 against a 0.90 `grounded` threshold nothing
# could ever be accepted.
DEFAULT_BAND = 0.05

INSTRUCTIONS = {
    "grounded": (
        "Decide whether the verbatim quote directly supports the claim's full "
        "substance. Answer true only if the quote explicitly states what the "
        "claim asserts; answer false if the quote is absent, unrelated, covers "
        "only part of the claim, or is too vague to verify it.",
        "The quote explicitly states the substance of the claim.",
        "The quote is missing, unrelated, partial, or too vague.",
    ),
    "durable": (
        "Decide whether this claim will still matter roughly a month from now. "
        "Answer true if it records a stable fact, decision, definition, or "
        "configuration that future work depends on; answer false if it is "
        "transient state, ephemeral news, or a passing observation.",
        "A stable fact, decision, or configuration with lasting value.",
        "Transient or ephemeral information nobody will need in a month.",
    ),
    "duplicate": (
        "Decide whether the vault index provided in the state already covers "
        "this claim's substance. Answer true if the index already contains the "
        "same information or a page plainly about it; answer false if the claim "
        "adds information not represented in the index.",
        "The vault index already covers the claim's substance.",
        "The claim adds information absent from the vault index.",
    ),
    "sensitive": (
        "Decide whether the claim or its quote contains sensitive material. "
        "Answer true if either contains an API key, token, password, private "
        "key, or other credential, or personal data such as real names, "
        "emails, or addresses; answer false if both are free of secrets and "
        "personal data.",
        "Contains a secret, credential, or personal data.",
        "Contains no secret, credential, or personal data.",
    ),
}


def resolve_thresholds(cfg):
    """Merge kb-config decisions.thresholds over DEFAULT_THRESHOLDS.

    Accepts a mapping per axis and an optional `band` for the uncertainty window.
    Unknown axes or out-of-range values fail loudly: a typo silently reverting to
    the default would recreate the calibration bug this replaced.
    """
    raw = cfg.get("thresholds") or {}
    if not isinstance(raw, dict):
        raise SystemExit("error: decisions.thresholds must be a mapping")
    thresholds = dict(DEFAULT_THRESHOLDS)
    band = DEFAULT_BAND
    for key, value in raw.items():
        if key == "band":
            band = value
            continue
        if key not in DIMS:
            raise SystemExit(
                f"error: decisions.thresholds has unknown axis '{key}' (expected one of {', '.join(DIMS)} or band)"
            )
        thresholds[key] = value
    for key, value in thresholds.items():
        if not isinstance(value, (int, float)) or not 0 < float(value) < 1:
            raise SystemExit(
                f"error: decisions.thresholds.{key} must be a number between 0 and 1, got {value!r}"
            )
        thresholds[key] = float(value)
    if not isinstance(band, (int, float)) or not 0 <= float(band) < 0.5:
        raise SystemExit(f"error: decisions.thresholds.band must be a number in [0, 0.5), got {band!r}")
    return thresholds, float(band)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Score atomic wiki claims against a local systemone decision model."
    )
    parser.add_argument(
        "--claims",
        help="Path to a JSON array of {claim, quote, target_page} objects; reads stdin when omitted.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of the human table."
    )
    parser.add_argument(
        "--model",
        default=_CFG.get("model") or "clef-flash:9b",
        help="systemone model tag (kb-config decisions.model, default clef-flash:9b).",
    )
    parser.add_argument(
        "--endpoint",
        default=_CFG.get("endpoint") or "http://127.0.0.1:11434",
        help="inference server base URL (kb-config decisions.endpoint);"
        " /v1/systemone is appended.",
    )
    return parser.parse_args(argv)


def load_claims(source):
    try:
        data = json.load(source)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: invalid JSON claims input: {exc}")
    if not isinstance(data, list) or not data:
        raise SystemExit("error: claims input must be a non-empty JSON array")
    claims = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), str) or not item["claim"].strip():
            raise SystemExit(f"error: claims[{i}] must be an object with a non-empty string 'claim'")
        quote = item.get("quote")
        if quote is not None and not isinstance(quote, str):
            raise SystemExit(f"error: claims[{i}].quote must be a string or null")
        target_page = item.get("target_page")
        if target_page is not None and not isinstance(target_page, str):
            raise SystemExit(f"error: claims[{i}].target_page must be a string or null")
        claims.append(
            {
                "claim": item["claim"],
                "quote": quote if quote else None,
                "target_page": target_page,
                "source": item.get("source") if isinstance(item.get("source"), str) else None,
                "skip_grounded": bool(item.get("skip_grounded")),
            }
        )
    return claims


def load_vault_index():
    try:
        return VAULT_INDEX_PATH.read_text(encoding="utf-8")[:VAULT_INDEX_CHARS]
    except OSError:
        return ""


def build_questions(chunk, base_index):
    questions = {}
    for offset, claim in enumerate(chunk):
        cid = f"c{base_index + offset}"
        for dim in DIMS:
            if dim == "grounded" and claim.get("skip_grounded"):
                continue
            text, true_text, false_text = INSTRUCTIONS[dim]
            # Question keys never reach the model, so the instruction itself
            # must name the claim it judges or every answer maps to any claim.
            instructions = f"Consider ONLY claim [{cid}] and its quote in the state. {text}"
            questions[f"{cid}_{dim}"] = {
                "type": "noul",
                "instructions": instructions,
                "criteria": {"true": true_text, "false": false_text},
            }
    return questions


def chunk_requests(claims, vault_index, model):
    """Yield (chunk, base_index) batches that respect the question cap and body size."""
    start = 0
    while start < len(claims):
        size = CLAIMS_PER_REQUEST
        while True:
            chunk = claims[start : start + size]
            payload = make_payload(chunk, start, vault_index, model)
            body = json.dumps(payload, ensure_ascii=False)
            if len(body.encode("utf-8")) <= MAX_BODY_BYTES or size == 1:
                if size > 1 and len(body.encode("utf-8")) > MAX_BODY_BYTES:
                    raise SystemExit(
                        f"error: claim {start} alone exceeds the {MAX_BODY_BYTES}-byte request body limit"
                    )
                break
            size = max(1, size // 2)
        yield chunk, start, payload
        start += len(chunk)


def make_payload(chunk, base_index, vault_index, model):
    state_claims = [
        {"id": f"c{base_index + offset}", "claim": c["claim"], "quote": (c["quote"] or "")[:QUOTE_MAX_CHARS]}
        for offset, c in enumerate(chunk)
    ]
    return {
        "model": model,
        "state": {
            "purpose": "wiki claim vetting",
            "vault_index": vault_index,
            "claims": state_claims,
        },
        "questions": build_questions(chunk, base_index),
    }


def is_transient_error(exc):
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError)):
        return True
    return isinstance(exc, urllib.error.HTTPError) and exc.code >= 500


def call_systemone(endpoint, payload, question_ids):
    if not endpoint.endswith("/v1/systemone"):
        endpoint = endpoint.rstrip("/") + "/v1/systemone"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = None
    for attempt in (1, 2):
        try:
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                result = json.loads(response.read().decode("utf-8"))
            answers = result.get("answers")
            if not isinstance(answers, dict):
                raise ValueError("response is missing the 'answers' record")
            for qid in question_ids:
                answer = answers.get(qid)
                if not isinstance(answer, dict) or not isinstance(answer.get("noul"), (int, float)):
                    raise ValueError(f"response is missing a usable noul answer for '{qid}'")
            return {qid: float(answers[qid]["noul"]) for qid in question_ids}
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if not is_transient_error(exc) and not isinstance(exc, ValueError):
                raise SystemExit(f"error: systemone call failed: {exc}")
            if attempt == 2:
                break
    raise SystemExit(f"error: systemone call failed after retry: {last_error}")


def verdict_for(probs, thresholds=None, band=DEFAULT_BAND, skip_grounded=False):
    """Turn per-axis probabilities into a verdict plus the uncertainty it carries.

    Each axis is compared against its own threshold. If a probability sits inside
    `band` of its threshold the axis is undecided, and the claim goes to `review`
    rather than being forced into a bit — that is where the previous 0.5 cut lost
    the signal that would have caught the bad acceptances.
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    bits = {}
    undecided = []
    for dim in DIMS:
        if dim == "grounded" and skip_grounded:
            bits[dim] = True
            continue
        prob = probs.get(dim)
        if prob is None:
            bits[dim] = False
            undecided.append(dim)
            continue
        limit = thresholds[dim]
        bits[dim] = prob >= limit
        if abs(prob - limit) < band:
            undecided.append(dim)

    if bits["sensitive"]:
        verdict = "reject"
    elif undecided:
        verdict = "review"
    elif not bits["grounded"]:
        verdict = "reject"
    elif bits["durable"] and not bits["duplicate"]:
        verdict = "accept"
    else:
        verdict = "review"

    # Distance to the nearest decision boundary, 0 = right on the edge.
    decided = [abs(probs[d] - thresholds[d]) for d in DIMS if d in probs]
    confidence = round(min(decided), 3) if decided else 0.0
    return verdict, bits, {"undecided_axes": undecided, "confidence": confidence}


def main(argv=None):
    args = parse_args(argv)
    thresholds, band = resolve_thresholds(_CFG)
    source = open(args.claims, encoding="utf-8") if args.claims else sys.stdin
    with source:
        claims = load_claims(source)
    vault_index = load_vault_index()

    results = []
    for chunk, base_index, payload in chunk_requests(claims, vault_index, args.model):
        question_ids = list(payload["questions"])
        answers = call_systemone(args.endpoint, payload, question_ids)
        for offset, claim in enumerate(chunk):
            cid = f"c{base_index + offset}"
            skip = bool(claim.get("skip_grounded"))
            probs = {dim: answers[f"{cid}_{dim}"] for dim in DIMS if not (dim == "grounded" and skip)}
            verdict, bits, signal = verdict_for(
                probs, thresholds=thresholds, band=band, skip_grounded=skip
            )
            results.append(
                {
                    "claim": claim["claim"],
                    "quote": claim["quote"],
                    "target_page": claim["target_page"],
                    "source": claim.get("source"),
                    "probabilities": probs,
                    "bits": bits,
                    "verdict": verdict,
                    **signal,
                }
            )

    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in ("accept", "review", "reject")}
    if args.json:
        print(
            json.dumps(
                {
                    "model": args.model,
                    "endpoint": args.endpoint,
                    "thresholds": thresholds,
                    "band": band,
                    "counts": counts,
                    "verdicts": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    header = (
        f"{'#':>2}  {'VERDICT':<7}  {'conf':>5}  {'grounded':>8}  {'durable':>8}  "
        f"{'duplicate':>9}  {'sensitive':>9}  claim"
    )
    print(header)
    print("-" * len(header))
    for i, r in enumerate(results, 1):
        p = r["probabilities"]
        claim = r["claim"] if len(r["claim"]) <= 60 else r["claim"][:57] + "..."
        grounded = f"{p['grounded']:>8.2f}" if "grounded" in p else f"{'skip':>8}"
        print(
            f"{i:>2}  {r['verdict']:<7}  {r['confidence']:>5.2f}  {grounded}  {p['durable']:>8.2f} "
            f"  {p['duplicate']:>9.2f}  {p['sensitive']:>9.2f}  {claim}"
        )
    print(
        f"\nSummary: {counts['accept']} accept, {counts['review']} review, "
        f"{counts['reject']} reject ({len(results)} claims, model={args.model})"
    )
    print(
        "Thresholds: "
        + ", ".join(f"{dim}={thresholds[dim]:.2f}" for dim in DIMS)
        + f" (band ±{band:.2f}; undecided axes force review)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
