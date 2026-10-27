#!/usr/bin/env python3
"""Calibration harness for wiki-vetting.

Scores a labelled claim set against the configured System One endpoint and reports
per-axis precision/recall at the decision boundary. This exists because a single
0.5 cut on every axis accepted 7 of 10 unsupported quotes as `accept` — a gate
that never measured itself cannot be trusted.

The labels use two axes the vetter decides directly:
  grounded  — does the quote explicitly support the claim?  (N = it does not)
  sensitive — does claim or quote carry a secret / personal data?

Usage:
    python3 calibration.py --set reference.json [--json] [--model TAG] [--endpoint URL]

Exit codes:
    0  all axes meet their floor
    1  at least one axis is below its floor (regression)
    2  usage / input error
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "wiki_vet", Path(__file__).resolve().parent / "wiki-vet.py"
)
_vet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vet)

# Floors: a gate that scores below these is regressing, not merely imperfect.
FLOORS = {
    "grounded.false_positive_rate": 0.20,  # unsupported claims wrongly accepted
    "sensitive.recall": 0.90,              # secrets must be caught
}


def score(results, labels):
    """Confusion counts per axis at the configured thresholds."""
    grounded = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
    sensitive = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
    for row, verdict in zip(labels, results):
        probs = verdict["probabilities"]
        g_true = row["grounded"]
        g_pred = probs.get("grounded", 0.0) >= _vet.DEFAULT_THRESHOLDS["grounded"]
        if g_true and g_pred:
            grounded["tp"] += 1
        elif g_true and not g_pred:
            grounded["fn"] += 1
        elif not g_true and g_pred:
            grounded["fp"] += 1
        else:
            grounded["tn"] += 1

        s_true = bool(row.get("sensitive"))
        s_pred = probs.get("sensitive", 0.0) >= _vet.DEFAULT_THRESHOLDS["sensitive"]
        if s_true and s_pred:
            sensitive["tp"] += 1
        elif s_true and not s_pred:
            sensitive["fn"] += 1
        elif not s_true and s_pred:
            sensitive["fp"] += 1
        else:
            sensitive["tn"] += 1

    def rate(num, den):
        return round(num / den, 3) if den else None

    return {
        "grounded": {
            **grounded,
            "precision": rate(grounded["tp"], grounded["tp"] + grounded["fp"]),
            "recall": rate(grounded["tp"], grounded["tp"] + grounded["fn"]),
            "false_positive_rate": rate(grounded["fp"], grounded["fp"] + grounded["tn"]),
        },
        "sensitive": {
            **sensitive,
            "precision": rate(sensitive["tp"], sensitive["tp"] + sensitive["fp"]),
            "recall": rate(sensitive["tp"], sensitive["tp"] + sensitive["fn"]),
        },
    }


def check_floors(metrics):
    failures = []
    fpr = metrics["grounded"]["false_positive_rate"]
    if fpr is not None and fpr > FLOORS["grounded.false_positive_rate"]:
        failures.append(
            f"grounded false_positive_rate={fpr} exceeds floor {FLOORS['grounded.false_positive_rate']}"
        )
    recall = metrics["sensitive"]["recall"]
    if recall is not None and recall < FLOORS["sensitive.recall"]:
        failures.append(
            f"sensitive recall={recall} below floor {FLOORS['sensitive.recall']}"
        )
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", required=True, help="Labelled JSON array of claims with labels.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument("--model", default=_vet._CFG.get("model") or "clef-flash:9b")
    parser.add_argument("--endpoint", default=_vet._CFG.get("endpoint") or "http://127.0.0.1:11434")
    parser.add_argument(
        "--api-key",
        default=_vet._API_KEY if hasattr(_vet, "_API_KEY") else "",
        help="bearer token for a hosted decision endpoint; a local Ollama needs none.",
    )
    args = parser.parse_args(argv)

    path = Path(args.set)
    if not path.is_file():
        raise SystemExit(f"error: calibration set not found: {path}")
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: invalid JSON in {path}: {exc}")
    if not isinstance(rows, list) or not rows:
        raise SystemExit("error: calibration set must be a non-empty JSON array")

    labels = []
    claims = []
    for i, row in enumerate(rows):
        if "grounded" not in row:
            raise SystemExit(f"error: row {i} is missing the 'grounded' label")
        labels.append(row)
        claims.append(
            {
                "claim": row["claim"],
                "quote": row.get("quote"),
                "target_page": row.get("target_page", "calibration"),
                "source": row.get("source"),
                "skip_grounded": bool(row.get("skip_grounded")),
            }
        )

    thresholds, band = _vet.resolve_thresholds(_vet._CFG)
    vault_index = _vet.load_vault_index()
    results = []
    for chunk, base_index, payload in _vet.chunk_requests(claims, vault_index, args.model):
        answers = _vet.call_systemone(args.endpoint, payload, list(payload["questions"]), args.api_key)
        for offset, claim in enumerate(chunk):
            cid = f"c{base_index + offset}"
            skip = bool(claim.get("skip_grounded"))
            probs = {
                dim: answers[f"{cid}_{dim}"]
                for dim in _vet.DIMS
                if not (dim == "grounded" and skip)
            }
            verdict, _bits, signal = _vet.verdict_for(
                probs, thresholds=thresholds, band=band, skip_grounded=skip
            )
            results.append({"probabilities": probs, "verdict": verdict, **signal})

    metrics = score(results, labels)
    failures = check_floors(metrics)
    payload = {
        "model": args.model,
        "endpoint": args.endpoint,
        "thresholds": thresholds,
        "band": band,
        "cases": len(results),
        "metrics": metrics,
        "floors": FLOORS,
        "failures": failures,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        g, s = metrics["grounded"], metrics["sensitive"]
        print(f"model={args.model} cases={len(results)} band=±{band:.2f}")
        print(
            f"grounded   precision={g['precision']} recall={g['recall']} "
            f"FPR={g['false_positive_rate']}  (tp={g['tp']} tn={g['tn']} fp={g['fp']} fn={g['fn']})"
        )
        print(
            f"sensitive  precision={s['precision']} recall={s['recall']}  "
            f"(tp={s['tp']} tn={s['tn']} fp={s['fp']} fn={s['fn']})"
        )
        if failures:
            print("\nFAILED FLOORS:")
            for failure in failures:
                print(f"  - {failure}")
        else:
            print("\nAll floors met.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
