#!/usr/bin/env python3
"""Classify a source repository licence and gate direct code reuse.

Usage:
  license-gate.py inspect SOURCE_DIR [--json]
  license-gate.py compat SOURCE_DIR TARGET_DIR [--json]

This is a conservative engineering policy check, not legal advice. A missing,
unrecognised, proprietary, or copyleft licence never authorizes copying code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LICENSE_NAMES = (
    "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "LICENCE.md",
    "COPYING", "COPYING.md", "NOTICE",
)
MAX_LICENSE_BYTES = 512 * 1024


POLICIES = {
    "MIT": {
        "class": "permissive",
        "copy": "allowed-with-notice",
        "obligations": ["Preserve copyright and MIT licence text."],
    },
    "Apache-2.0": {
        "class": "permissive",
        "copy": "allowed-with-notice",
        "obligations": [
            "Preserve Apache-2.0 licence text.",
            "Carry upstream NOTICE content when present.",
            "State material modifications in retained notices.",
        ],
    },
    "BSD-2-Clause": {
        "class": "permissive",
        "copy": "allowed-with-notice",
        "obligations": ["Preserve copyright and BSD-2-Clause licence text."],
    },
    "BSD-3-Clause": {
        "class": "permissive",
        "copy": "allowed-with-notice",
        "obligations": ["Preserve copyright and BSD-3-Clause licence text."],
    },
    "ISC": {
        "class": "permissive",
        "copy": "allowed-with-notice",
        "obligations": ["Preserve copyright and ISC licence text."],
    },
    "Unlicense": {
        "class": "public-domain-like",
        "copy": "allowed-with-provenance",
        "obligations": ["Record upstream URL and commit for provenance."],
    },
    "CC0-1.0": {
        "class": "public-domain-like",
        "copy": "allowed-with-provenance",
        "obligations": ["Record upstream URL and commit for provenance."],
    },
    "MPL-2.0": {
        "class": "weak-copyleft",
        "copy": "manual-review",
        "obligations": [
            "Modified MPL-covered files must remain under MPL-2.0.",
            "Keep MPL code in distinct files and obtain owner approval.",
        ],
    },
    "LGPL": {
        "class": "weak-copyleft",
        "copy": "manual-review",
        "obligations": [
            "Do not copy source without a licence review.",
            "Treat linking, redistribution, and modified library source separately.",
        ],
    },
    "GPL": {
        "class": "strong-copyleft",
        "copy": "blocked",
        "obligations": [
            "Do not copy or combine source into this project without legal and owner approval.",
            "Consider a separately operated integration only after review.",
        ],
    },
    "AGPL": {
        "class": "network-copyleft",
        "copy": "blocked",
        "obligations": [
            "Do not copy or combine source into this project without legal and owner approval.",
            "Network use can trigger source-offer obligations; assess separately.",
        ],
    },
    "unknown": {
        "class": "unknown-or-proprietary",
        "copy": "blocked",
        "obligations": [
            "Do not copy code, assets, tests, or text.",
            "Use only independently verified facts and high-level ideas pending rights clarification.",
        ],
    },
}


def find_license(root: Path) -> Path | None:
    if root.is_file():
        return root
    for name in LICENSE_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def identify(text: str) -> str:
    lower = text.lower()
    if "gnu affero general public license" in lower:
        return "AGPL"
    if "gnu lesser general public license" in lower:
        return "LGPL"
    if "gnu general public license" in lower:
        return "GPL"
    if "mozilla public license" in lower and "version 2.0" in lower:
        return "MPL-2.0"
    if "apache license" in lower and "version 2.0" in lower:
        return "Apache-2.0"
    if "the unlicense" in lower or "this is free and unencumbered software" in lower:
        return "Unlicense"
    if "creative commons zero" in lower or "cc0 1.0 universal" in lower:
        return "CC0-1.0"
    if "permission is hereby granted, free of charge, to any person obtaining a copy" in lower:
        return "MIT"
    if "redistribution and use in source and binary forms" in lower:
        if "neither the name" in lower:
            return "BSD-3-Clause"
        return "BSD-2-Clause"
    if "permission to use, copy, modify, and/or distribute this software" in lower and "the software is provided \"as is\"" in lower:
        return "ISC"
    return "unknown"


def inspect(root: Path) -> dict:
    license_file = find_license(root)
    if license_file is None:
        licence = "unknown"
        result = {"path": str(root), "license_file": None}
    else:
        try:
            text = license_file.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise RuntimeError(f"cannot read {license_file}: {error}") from error
        if len(text.encode("utf-8")) > MAX_LICENSE_BYTES:
            raise RuntimeError(f"{license_file} exceeds {MAX_LICENSE_BYTES} bytes")
        licence = identify(text)
        result = {"path": str(root), "license_file": str(license_file)}
    result.update({"license": licence, **POLICIES[licence]})
    return result


def compatibility(source: Path, target: Path) -> dict:
    source_result = inspect(source)
    target_result = inspect(target)
    allowed = (
        source_result["copy"].startswith("allowed")
        and target_result["license"] != "unknown"
    )
    decision = "copy-permitted-with-obligations" if allowed else "copy-blocked"
    reason = (
        "Source is permissively licensed and the target declares a licence."
        if allowed
        else "Direct copying requires a recognised source licence and a declared target licence."
    )
    return {
        "decision": decision,
        "reason": reason,
        "source": source_result,
        "target": target_result,
    }


def emit(value: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    print(f"license: {value['license']}")
    print(f"class: {value['class']}")
    print(f"direct-code-reuse: {value['copy']}")
    print(f"license-file: {value['license_file'] or 'none'}")
    for obligation in value["obligations"]:
        print(f"- {obligation}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect", help="classify a source directory or licence file")
    inspect_parser.add_argument("source", type=Path)
    inspect_parser.add_argument("--json", action="store_true")
    compat_parser = commands.add_parser("compat", help="gate direct code reuse between source and target")
    compat_parser.add_argument("source", type=Path)
    compat_parser.add_argument("target", type=Path)
    compat_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "inspect":
            result = inspect(args.source)
            emit(result, args.json)
            return 0 if result["copy"].startswith("allowed") else 1
        result = compatibility(args.source, args.target)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"decision: {result['decision']}")
            print(f"reason: {result['reason']}")
            print(f"source: {result['source']['license']} ({result['source']['copy']})")
            print(f"target: {result['target']['license']}")
        return 0 if result["decision"] == "copy-permitted-with-obligations" else 1
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
