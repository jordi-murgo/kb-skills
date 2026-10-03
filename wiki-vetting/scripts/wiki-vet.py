#!/usr/bin/env python3
"""Vet atomic claims before they enter the wiki using a local /v1/systemone decision model.

Each claim is scored with four noul questions (grounded, durable, duplicate,
sensitive). Verdicts: accept = grounded and durable and not sensitive and not
duplicate; reject = sensitive or not grounded; otherwise review.
"""

import argparse
import os
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git (a skills dir also has wiki/)."""
    p = Path.cwd()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir() and (parent / ".git").exists():
            return parent
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return parent
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir() and (parent / ".git").exists():
            return parent
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/")


REPO_ROOT = find_vault_root()
VAULT_INDEX_PATH = REPO_ROOT / "wiki" / "index.md"
VAULT_INDEX_CHARS = 4000
QUOTE_MAX_CHARS = 800
CLAIMS_PER_REQUEST = 16
MAX_BODY_BYTES = 60 * 1024
DIMS = ("grounded", "durable", "duplicate", "sensitive")

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
        default=os.environ.get("WIKI_VET_MODEL", "clef-flash:9b"),
        help="systemone model tag (env WIKI_VET_MODEL, default clef-flash:9b).",
    )
    parser.add_argument(
        "--endpoint",
        default=(
            os.environ.get("WIKI_VET_ENDPOINT")
            or os.environ.get("WIKI_OLLAMA_URL")
            or "http://127.0.0.1:11434"
        ),
        help="inference server base URL (env WIKI_VET_ENDPOINT, else WIKI_OLLAMA_URL);"
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


def verdict_for(probs, skip_grounded=False):
    bits = {dim: probs.get(dim, 1.0 if (dim == "grounded" and skip_grounded) else 0.0) >= 0.5 for dim in DIMS}
    if bits["sensitive"] or not bits["grounded"]:
        verdict = "reject"
    elif bits["durable"] and not bits["duplicate"]:
        verdict = "accept"
    else:
        verdict = "review"
    return verdict, bits


def main(argv=None):
    args = parse_args(argv)
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
            verdict, bits = verdict_for(probs, skip_grounded=skip)
            results.append(
                {
                    "claim": claim["claim"],
                    "quote": claim["quote"],
                    "target_page": claim["target_page"],
                    "source": claim.get("source"),
                    "probabilities": probs,
                    "bits": bits,
                    "verdict": verdict,
                }
            )

    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in ("accept", "review", "reject")}
    if args.json:
        print(
            json.dumps(
                {
                    "model": args.model,
                    "endpoint": args.endpoint,
                    "counts": counts,
                    "verdicts": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    header = f"{'#':>2}  {'VERDICT':<7}  {'grounded':>8}  {'durable':>8}  {'duplicate':>9}  {'sensitive':>9}  claim"
    print(header)
    print("-" * len(header))
    for i, r in enumerate(results, 1):
        p = r["probabilities"]
        claim = r["claim"] if len(r["claim"]) <= 60 else r["claim"][:57] + "..."
        grounded = f"{p['grounded']:>8.2f}" if "grounded" in p else f"{'skip':>8}"
        print(
            f"{i:>2}  {r['verdict']:<7}  {grounded}  {p['durable']:>8.2f} "
            f"  {p['duplicate']:>9.2f}  {p['sensitive']:>9.2f}  {claim}"
        )
    print(
        f"\nSummary: {counts['accept']} accept, {counts['review']} review, "
        f"{counts['reject']} reject ({len(results)} claims, model={args.model})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
