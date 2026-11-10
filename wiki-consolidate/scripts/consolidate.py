#!/usr/bin/env python3
"""Inspect and transition observation-first knowledge candidates.

This script owns derived candidate-ledger state only. Semantic edits to wiki pages
remain an agent/human operation governed by the wiki-consolidate skill.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

RELATIONS = {"duplicate", "corroborates", "extends", "contradicts", "novel"}
DURABILITIES = {"durable", "ephemeral"}
ACTIONS = {
    "no_op",
    "add_evidence",
    "update_existing",
    "hold_dispute",
    "promote",
    "recent_only",
    "reject",
}
STATUSES = {"pending", "applied", "expired", "rejected"}
CANONICAL_ACTIONS = {"add_evidence", "update_existing", "hold_dispute", "promote"}
TERMINAL_STATUSES = {"applied", "expired", "rejected"}
MACRO_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def find_vault_root() -> Path:
    for base in (Path.cwd(), Path(__file__).resolve()):
        for parent in (base, *base.parents):
            if (
                (parent / "wiki").is_dir()
                and (parent / ".git").exists()
                and not (parent / "wiki" / "SKILL.md").exists()
            ):
                return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/ and .git")


def load_env_files(root: Path) -> None:
    process_keys = set(os.environ)
    for name in (".env", ".env.local"):
        path = root / name
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in process_keys:
                os.environ[key] = value.strip().strip("'\"")


def expand_macros(value):
    if isinstance(value, str):
        return MACRO_RE.sub(
            lambda match: os.environ.get(match.group(1)) or (match.group(2) or ""),
            value,
        )
    if isinstance(value, list):
        return [expand_macros(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_macros(item) for key, item in value.items()}
    return value


def load_config_section(root: Path, section: str) -> dict:
    candidates = [root / "kb-config.yaml", root / "kb-config.yml", root / "kb-config.json"]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        try:
            config = json.loads(text)
        except json.JSONDecodeError as error:
            raise SystemExit(f"{path} is not valid JSON: {error}") from error
    else:
        try:
            import yaml
        except ImportError:
            raise SystemExit(
                f"{path} needs PyYAML, which is not installed.\n"
                "Run: uv add pyyaml   (or: python3 -m pip install pyyaml)"
            )
        try:
            config = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SystemExit(f"{path} is not valid YAML: {error}") from error
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise SystemExit(f"{path} top level must be a mapping")
    value = config.get(section) or {}
    if not isinstance(value, dict):
        raise SystemExit(f"{path} section '{section}' must be a mapping")
    return expand_macros(value)


def configured_flow_dir(root: Path) -> Path:
    config = load_config_section(root, "knowledge_flow")
    raw = config.get("dir") or ".cache/knowledge-flow"
    if not isinstance(raw, str) or not raw.strip():
        raise SystemExit("error: knowledge_flow.dir must be a non-empty path string")
    if os.path.isabs(raw) or raw.startswith("~") or ".." in Path(raw).parts:
        raise SystemExit("error: knowledge_flow.dir must be vault-relative without '..' or '~'")
    return root / raw


def load_latest(path: Path, id_field: str) -> dict[str, dict]:
    if not path.is_file():
        return {}
    latest: dict[str, dict] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, raw in enumerate(source, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as error:
                raise SystemExit(f"error: {path}:{line_number}: invalid JSON: {error.msg}") from error
            if not isinstance(row, dict):
                raise SystemExit(f"error: {path}:{line_number}: expected a JSON object")
            stable_id = row.get(id_field)
            if not isinstance(stable_id, str) or not stable_id:
                raise SystemExit(f"error: {path}:{line_number}: missing {id_field}")
            row = dict(row)
            row["_line"] = line_number
            latest[stable_id] = row
    return latest


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as destination:
        destination.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_row(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def validate_candidate(row: dict) -> None:
    if row.get("relation") not in RELATIONS:
        raise SystemExit(f"error: invalid relation {row.get('relation')!r}")
    if row.get("durability") not in DURABILITIES:
        raise SystemExit(f"error: invalid durability {row.get('durability')!r}")
    if row.get("action") not in ACTIONS:
        raise SystemExit(f"error: invalid action {row.get('action')!r}")
    if row.get("status") not in STATUSES:
        raise SystemExit(f"error: invalid status {row.get('status')!r}")
    observation_ids = row.get("observation_ids")
    if not isinstance(observation_ids, list) or not observation_ids:
        raise SystemExit("error: candidate needs at least one observation_id")
    if row["action"] == "recent_only" and row["status"] == "applied":
        raise SystemExit("error: recent_only stays pending until it expires, is rejected, or is promoted")
    if row["action"] == "reject" and row["status"] != "rejected":
        raise SystemExit("error: reject action requires status rejected")


def candidate_is_open(candidate: dict) -> bool:
    if candidate.get("action") == "hold_dispute" and candidate.get("status") == "applied":
        return True
    return candidate.get("status", "pending") not in TERMINAL_STATUSES


def candidate_detail(candidate: dict, observations: dict[str, dict]) -> dict:
    detail = clean_row(candidate)
    detail["observations"] = []
    for observation_id in candidate.get("observation_ids") or []:
        observation = observations.get(observation_id)
        if observation:
            detail["observations"].append(
                {
                    "observation_id": observation_id,
                    "claim": observation.get("claim"),
                    "source_path": observation.get("source_path"),
                    "source_lines": observation.get("source_lines"),
                    "observed_at": observation.get("observed_at"),
                    "valid_to": observation.get("valid_to"),
                }
            )
    return detail


def metrics(candidates: list[dict]) -> dict:
    total = len(candidates)
    by_action = Counter(candidate.get("action", "unknown") for candidate in candidates)
    by_relation = Counter(candidate.get("relation", "unknown") for candidate in candidates)
    canonical_applied = sum(
        1
        for candidate in candidates
        if candidate.get("status") == "applied" and candidate.get("action") in CANONICAL_ACTIONS
    )
    duplicates = sum(1 for candidate in candidates if candidate.get("relation") == "duplicate")
    open_dates = [
        candidate.get("decided_at", "")[:10]
        for candidate in candidates
        if candidate_is_open(candidate) and candidate.get("decided_at")
    ]
    oldest_open_days = None
    if open_dates:
        try:
            oldest_open_days = (date.today() - date.fromisoformat(min(open_dates))).days
        except ValueError:
            oldest_open_days = None
    return {
        "total_candidates": total,
        "open_candidates": sum(1 for candidate in candidates if candidate_is_open(candidate)),
        "knowledge_yield": round(canonical_applied / total, 4) if total else 0.0,
        "duplicate_rate": round(duplicates / total, 4) if total else 0.0,
        "oldest_open_days": oldest_open_days,
        "by_action": dict(sorted(by_action.items())),
        "by_relation": dict(sorted(by_relation.items())),
    }


def cmd_queue(args, observations: dict, candidates: dict) -> int:
    selected = [
        candidate
        for candidate in candidates.values()
        if args.include_terminal or candidate_is_open(candidate)
    ]
    selected.sort(
        key=lambda candidate: (
            0 if candidate_is_open(candidate) else 1,
            0 if candidate.get("action") == "hold_dispute" else 1,
            candidate.get("decided_at", ""),
            candidate.get("candidate_id", ""),
        )
    )
    detailed = [candidate_detail(candidate, observations) for candidate in selected]
    groups: dict[str, list[str]] = defaultdict(list)
    for candidate in detailed:
        matched_pages = candidate.get("matched_pages") or []
        group = matched_pages[0] if matched_pages else "(unmatched)"
        groups[group].append(candidate["candidate_id"])
    payload = {
        "metrics": metrics(list(candidates.values())),
        "groups": dict(sorted(groups.items())),
        "candidates": detailed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    report = payload["metrics"]
    print(
        f"candidates: {report['total_candidates']} total, {report['open_candidates']} open, "
        f"knowledge_yield={report['knowledge_yield']:.3f}, duplicate_rate={report['duplicate_rate']:.3f}"
    )
    for group, candidate_ids in payload["groups"].items():
        print(f"\n{group}")
        for candidate_id in candidate_ids:
            candidate = next(item for item in detailed if item["candidate_id"] == candidate_id)
            claims = [item.get("claim") for item in candidate["observations"] if item.get("claim")]
            claim = claims[0] if claims else "(missing observation)"
            print(
                f"  {candidate_id}  {candidate.get('relation')} / {candidate.get('action')} / "
                f"{candidate.get('status')}  {claim}"
            )
    return 0


def cmd_decide(args, candidates_path: Path, candidates: dict) -> int:
    existing = candidates.get(args.candidate_id)
    if existing is None:
        print(f"error: unknown candidate {args.candidate_id}", file=sys.stderr)
        return 1
    row = clean_row(existing)
    for key in ("relation", "durability", "action", "status"):
        value = getattr(args, key)
        if value is not None:
            row[key] = value
    if args.reason is not None:
        row["reason"] = args.reason
    if args.matched_page is not None:
        row["matched_pages"] = list(dict.fromkeys(args.matched_page))
    row["decided_at"] = utc_now()
    validate_candidate(row)
    append_row(candidates_path, row)
    if args.json:
        print(json.dumps(row, ensure_ascii=False, indent=2))
    else:
        print(f"updated {args.candidate_id}: {row['action']} / {row['status']}")
    return 0


def parse_day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def cmd_expire(args, observations: dict, candidates_path: Path, candidates: dict) -> int:
    expirable = []
    for candidate in candidates.values():
        if candidate.get("action") != "recent_only" or candidate.get("status", "pending") != "pending":
            continue
        dates = []
        for observation_id in candidate.get("observation_ids") or []:
            observed_at = (observations.get(observation_id) or {}).get("observed_at")
            if isinstance(observed_at, str) and len(observed_at) >= 10:
                try:
                    dates.append(date.fromisoformat(observed_at[:10]))
                except ValueError:
                    pass
        if dates and max(dates) < args.before:
            expirable.append(candidate)
    expirable.sort(key=lambda candidate: candidate.get("candidate_id", ""))
    if args.apply:
        for candidate in expirable:
            row = clean_row(candidate)
            row["status"] = "expired"
            row["decided_at"] = utc_now()
            row["reason"] = f"Recent-only observations predate retention cutoff {args.before.isoformat()}."
            validate_candidate(row)
            append_row(candidates_path, row)
    payload = {
        "cutoff": args.before.isoformat(),
        "apply": args.apply,
        "expired_count": len(expirable),
        "candidate_ids": [candidate["candidate_id"] for candidate in expirable],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        mode = "expired" if args.apply else "would expire"
        print(f"{mode}: {len(expirable)} candidate(s) before {args.before.isoformat()}")
        for candidate_id in payload["candidate_ids"]:
            print(f"  {candidate_id}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    queue = subparsers.add_parser("queue", help="show active candidates grouped by canonical owner")
    queue.add_argument("--include-terminal", action="store_true")
    queue.add_argument("--json", action="store_true")

    decide = subparsers.add_parser("decide", help="append a candidate transition")
    decide.add_argument("candidate_id")
    decide.add_argument("--relation", choices=sorted(RELATIONS))
    decide.add_argument("--durability", choices=sorted(DURABILITIES))
    decide.add_argument("--action", choices=sorted(ACTIONS))
    decide.add_argument("--status", choices=sorted(STATUSES))
    decide.add_argument("--reason")
    decide.add_argument("--matched-page", action="append")
    decide.add_argument("--json", action="store_true")

    expire = subparsers.add_parser("expire", help="expire old pending recent-only candidates")
    expire.add_argument("--before", type=parse_day, required=True)
    expire.add_argument("--apply", action="store_true", help="append expiry transitions")
    expire.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = find_vault_root()
    load_env_files(root)
    flow_dir = configured_flow_dir(root)
    observations_path = flow_dir / "observations.jsonl"
    candidates_path = flow_dir / "candidates.jsonl"
    observations = load_latest(observations_path, "observation_id")
    candidates = load_latest(candidates_path, "candidate_id")
    if args.command == "queue":
        return cmd_queue(args, observations, candidates)
    if args.command == "decide":
        return cmd_decide(args, candidates_path, candidates)
    return cmd_expire(args, observations, candidates_path, candidates)


if __name__ == "__main__":
    sys.exit(main())
