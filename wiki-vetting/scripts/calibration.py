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
import re
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


def wilson_interval(successes, total, z=1.96):
    """Wilson score interval for a proportion — stdlib only, no scipy.

    Why this exists: the sensitive axis has 3 items, so one flip moves recall
    by 33 points and the floor is a coin toss dressed as a gate. Reporting the
    interval alongside the point estimate makes the width visible; a floor is
    only a gate when the axis has enough items to be narrower than the margin
    it is supposed to protect.
    """
    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denom
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def axis_intervals(metrics):
    """95% intervals for the two gated axes, plus a too-narrow warning."""
    out = {}
    g = metrics["grounded"]
    neg = g["fp"] + g["tn"]
    out["grounded_false_positive_rate"] = {
        "n": neg,
        "interval": wilson_interval(g["fp"], neg),
    }
    s = metrics["sensitive"]
    pos = s["tp"] + s["fn"]
    out["sensitive_recall"] = {
        "n": pos,
        "interval": wilson_interval(s["tp"], pos),
    }
    # Rule from the 2026-11-24 audit: a floor needs >=10 items on its axis;
    # below that the interval is wider than the margin it gates on.
    out["too_narrow"] = [k for k, v in out.items()
                         if isinstance(v, dict) and v["n"] < 10]
    return out


def split_holdout(rows, percent=30):
    """Deterministic content-addressed split: tuning vs holdout.

    A case's side depends only on its own claim+quote, so editing one case
    never reshuffles the rest, and the holdout never has to be maintained by
    hand. Why it matters: `calibration-set.json` is what DEFAULT_THRESHOLDS
    came from, so scoring the full set reports recall of the tuning data, not
    generalisation. Measuring the holdout alone is the honest number.
    """
    import hashlib

    holdout, tuning = [], []
    for row in rows:
        key = f"{row.get('claim', '')}\0{row.get('quote', '')}".encode("utf-8")
        side = int(hashlib.sha256(key).hexdigest()[:8], 16) % 100
        (holdout if side < percent else tuning).append(row)
    return tuning, holdout


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
    _vet.reject_literal_api_key_args(list(sys.argv[1:] if argv is None else argv))
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--set", required=True, help="Labelled JSON array of claims with labels.")
    parser.add_argument(
        "--split", choices=("holdout", "tuning", "full"), default="holdout",
        help="which side of the deterministic 70/30 split to score"
             " (default holdout: the tuning side fixed the thresholds)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument("--model", default=_vet._CFG.get("model") or "clef-flash:9b")
    parser.add_argument("--endpoint", default=_vet._CFG.get("endpoint") or "http://127.0.0.1:11434")
    parser.add_argument(
        "--api-key-env",
        default=None,
        metavar="NAME",
        help="environment variable holding the bearer token for a hosted "
        "decision endpoint; a local Ollama needs none.",
    )
    args = parser.parse_args(argv)
    if args.api_key_env is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
        parser.error("--api-key-env must be an environment variable name")
    # Selection before resolution, mirroring wiki-vet's CLI.
    api_key = _vet.resolve_api_key(args.api_key_env or _vet._CONFIGURED_API_KEY_ENV)

    path = Path(args.set)
    if not path.is_file():
        raise SystemExit(f"error: calibration set not found: {path}")
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: invalid JSON in {path}: {exc}")
    if not isinstance(rows, list) or not rows:
        raise SystemExit("error: calibration set must be a non-empty JSON array")
    tuning_rows, holdout_rows = split_holdout(rows)
    scored_rows = {"holdout": holdout_rows, "tuning": tuning_rows, "full": rows}[args.split]
    if not scored_rows:
        raise SystemExit(f"error: the {args.split} side of the split is empty")
    rows = scored_rows

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
        answers = _vet.call_systemone(args.endpoint, payload, list(payload["questions"]), api_key)
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
    intervals = axis_intervals(metrics)
    payload = {
        "model": args.model,
        "endpoint": args.endpoint,
        "thresholds": thresholds,
        "band": band,
        "cases": len(results),
        "split": args.split,
        "set_size": {"tuning": len(tuning_rows), "holdout": len(holdout_rows)},
        "metrics": metrics,
        "floors": FLOORS,
        "intervals": intervals,
        "failures": failures,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        g, s = metrics["grounded"], metrics["sensitive"]
        print(f"model={args.model} cases={len(results)} split={args.split} "
              f"(tuning {len(tuning_rows)} / holdout {len(holdout_rows)}) band=±{band:.2f}")
        print(
            f"grounded   precision={g['precision']} recall={g['recall']} "
            f"FPR={g['false_positive_rate']}  (tp={g['tp']} tn={g['tn']} fp={g['fp']} fn={g['fn']})"
        )
        print(
            f"sensitive  precision={s['precision']} recall={s['recall']}  "
            f"(tp={s['tp']} tn={s['tn']} fp={s['fp']} fn={s['fn']})"
        )
        for axis, info in (("grounded FPR", intervals["grounded_false_positive_rate"]),
                           ("sensitive recall", intervals["sensitive_recall"])):
            if info["interval"]:
                print(f"  95% CI {axis}: {info['interval']} on n={info['n']}")
        if intervals["too_narrow"]:
            print("\nWARNING: floor measured on fewer than 10 items on "
                  + ", ".join(intervals["too_narrow"])
                  + " — the interval is wider than the margin it gates on."
                  " Grow the set before treating this as pass/fail.")
        if failures:
            print("\nFAILED FLOORS:")
            for failure in failures:
                print(f"  - {failure}")
        else:
            print("\nAll floors met.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
