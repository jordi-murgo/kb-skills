#!/usr/bin/env python3
"""Aggregator: run all wiki lint checks and produce a summary report.

Runs each deterministic lint script and aggregates results into:
1. A console summary with per-check pass/fail status
2. A markdown report at wiki/meta/lint-report-YYYY-MM-DD.md

Usage:
  python3 scripts/run-lint.py           # console summary + write report
  python3 scripts/run-lint.py --json    # JSON only (for pipelines)
  python3 scripts/run-lint.py --no-report  # console only, don't write report
"""

import sys
import os
import subprocess
import glob
import json
from pathlib import Path
from datetime import date


def find_wiki_root(start: str = ".") -> str:
    p = Path(start).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return str(parent)
    return str(p)


def run_check(script_name: str, wiki_root: str) -> dict:
    """Run a lint script and capture its output."""
    # Siblings live next to this file, wherever the skill is installed. Resolving
    # them against wiki_root instead would return "skip" for every check the
    # moment the skill moves — and "skip" is not counted as a failure, so the
    # gate would exit 0 while testing nothing.
    script_path = str(Path(__file__).resolve().parent / script_name)
    if not os.path.isfile(script_path):
        return {
            "name": script_name,
            "status": "error",
            "output": f"script not found at {script_path}",
        }

    try:
        result = subprocess.run(
            ["python3", script_path],
            capture_output=True,
            text=True,
            cwd=wiki_root,
            timeout=30,
        )
        output = result.stdout.strip()
        if result.returncode == 0:
            return {"name": script_name, "status": "pass", "output": output}
        else:
            return {"name": script_name, "status": "fail", "output": output}
    except subprocess.TimeoutExpired:
        return {"name": script_name, "status": "error", "output": "timeout"}
    except Exception as e:
        return {"name": script_name, "status": "error", "output": str(e)}


CHECKS = [
    ("lint-dead-links.py", "Dead Links"),
    ("lint-orphans.py", "Orphan Pages"),
    ("lint-frontmatter.py", "Frontmatter Gaps"),
    ("lint-contradictions.py", "Contradictions"),
]


def count_pages(wiki_root: str) -> int:
    return len(glob.glob(f"{wiki_root}/wiki/**/*.md", recursive=True))


def generate_report(results: list[dict], wiki_root: str) -> str:
    today = date.today().isoformat()
    total_pages = count_pages(wiki_root)
    issues = sum(1 for r in results if r["status"] == "fail")
    errors = sum(1 for r in results if r["status"] == "error")

    lines = [
        "---",
        "type: meta",
        f'title: "Lint Report {today}"',
        f"created: {today}",
        f"updated: {today}",
        "tags:",
        "  - meta",
        "  - lint",
        "status: developing",
        "---",
        "",
        f"# Lint Report: {today}",
        "",
        "## Summary",
        f"- Pages scanned: {total_pages}",
        f"- Checks run: {len(results)}",
        f"- Issues found: {issues}",
        f"- Errors: {errors}",
        "",
    ]

    for result, (_, label) in zip(results, CHECKS):
        status_icon = {"pass": "✅", "fail": "❌", "error": "⚠️", "skip": "⏭️"}[
            result["status"]
        ]
        lines.append(f"## {status_icon} {label}")
        lines.append(f"**Status:** {result['status']}")
        lines.append("")
        # Indent the output as code block
        if result["output"]:
            lines.append("```")
            lines.append(result["output"])
            lines.append("```")
        lines.append("")

    return "\n".join(lines)


def main():
    wiki_root = find_wiki_root()
    json_mode = "--json" in sys.argv
    no_report = "--no-report" in sys.argv

    results = [run_check(script, wiki_root) for script, _ in CHECKS]

    if json_mode:
        print(json.dumps({"results": results, "pages": count_pages(wiki_root)}))
    else:
        # Console summary
        print("=" * 60)
        print(f"Wiki Lint Report — {date.today().isoformat()}")
        print(f"Pages: {count_pages(wiki_root)}")
        print("=" * 60)
        for result, (_, label) in zip(results, CHECKS):
            status_icon = {"pass": "✅", "fail": "❌", "error": "⚠️", "skip": "⏭️"}[
                result["status"]
            ]
            print(f"\n{status_icon} {label}: {result['status']}")
            if result["output"]:
                for line in result["output"].split("\n"):
                    print(f"  {line}")
        print("\n" + "=" * 60)
        issues = sum(1 for r in results if r["status"] in ("fail", "error"))
        if issues:
            print(f"FAILED: {issues} check(s) need attention")
        else:
            print("ALL CHECKS PASSED")

    # Write report
    if not no_report and not json_mode:
        report = generate_report(results, wiki_root)
        report_path = os.path.join(wiki_root, "wiki", "meta", f"lint-report-{date.today().isoformat()}.md")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        Path(report_path).write_text(report, encoding="utf-8")
        print(f"\nReport written to: {os.path.relpath(report_path, wiki_root)}")

    # Exit code. "error" counts as well as "fail": a check that could not run is
    # not a check that passed, and a gate that exits 0 on a broken check is worse
    # than no gate at all.
    if any(r["status"] in ("fail", "error") for r in results):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()