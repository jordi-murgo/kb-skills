#!/usr/bin/env python3
"""Capture bounded, browser-rendered evidence with Ego Browser.

Commands:
  ego-research.py doctor
  ego-research.py read URL [--max-chars 12000] [--timeout-ms 25000]

The script never selects/imports a browser profile, reads cookies, or writes a
vault. Ego Browser owns session selection and browser state. `read` accepts
HTTP(S) URLs only and prints a JSON evidence record to stdout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from urllib.parse import urlsplit

RESULT_PREFIX = "EGO_RESEARCH_RESULT="
MAX_TEXT_CHARS = 50_000
EGO_SCRIPT_BODY = r'''
const task = await taskSpace("ego-browser-research");
const page = task.page("p1");
try {
  await page.goto(input.url);
  await page.waitForLoadState("domcontentloaded", { timeout: input.timeoutMs });
  const result = await page.evaluate((maxChars) => {
    const root = document.querySelector("article, main, [role='main']") || document.body;
    const text = (root?.innerText || "")
      .replace(/\n{3,}/g, "\n\n")
      .trim()
      .slice(0, maxChars);
    const meta = (selector, attribute = "content") =>
      document.querySelector(selector)?.getAttribute(attribute)?.trim() || "";
    return {
      url: location.href,
      canonical_url: meta("link[rel='canonical']", "href") || location.href,
      title: document.title.trim(),
      description: meta("meta[name='description']") || meta("meta[property='og:description']"),
      text,
    };
  }, input.maxChars);
  await task.finish({ keep: [] });
  console.log("EGO_RESEARCH_RESULT=" + JSON.stringify(result));
} catch (error) {
  console.error("ego-browser research failed: " + (error?.stack || error));
  process.exitCode = 1;
}
'''


def parse_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("URL must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise argparse.ArgumentTypeError("URL must not embed credentials")
    return value


def run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise RuntimeError("ego-browser is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"ego-browser exceeded the {timeout}s process timeout") from error


def doctor() -> int:
    result = run(["ego-browser", "--version"], timeout=10)
    if result.returncode != 0:
        print(json.dumps({"available": False, "error": result.stderr.strip()}))
        return 1
    output = result.stdout or result.stderr
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    print(json.dumps({"available": True, "version": lines[0] if lines else "unknown"}))
    return 0


def read_url(url: str, max_chars: int, timeout_ms: int) -> int:
    input_data = {"url": url, "maxChars": max_chars, "timeoutMs": timeout_ms}
    script = f"const input = {json.dumps(input_data)};\n{EGO_SCRIPT_BODY}"
    result = run(
        ["ego-browser", "nodejs", "-e", script],
        timeout=max(30, timeout_ms // 1000 + 15),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Ego Browser error"
        raise RuntimeError(detail)

    output = "\n".join((result.stdout, result.stderr))
    payload = next(
        (line[len(RESULT_PREFIX):] for line in reversed(output.splitlines()) if line.startswith(RESULT_PREFIX)),
        None,
    )
    if payload is None:
        raise RuntimeError("Ego Browser returned no evidence record")
    try:
        evidence = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("Ego Browser returned an invalid evidence record") from error
    if not isinstance(evidence, dict) or not isinstance(evidence.get("text"), str):
        raise RuntimeError("Ego Browser returned an incomplete evidence record")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="confirm that Ego Browser is runnable")
    read_parser = subcommands.add_parser("read", help="render a URL and emit bounded text evidence as JSON")
    read_parser.add_argument("url", type=parse_url)
    read_parser.add_argument("--max-chars", type=int, default=12_000)
    read_parser.add_argument("--timeout-ms", type=int, default=25_000)

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor()
    if not 1 <= args.max_chars <= MAX_TEXT_CHARS:
        parser.error(f"--max-chars must be 1..{MAX_TEXT_CHARS}")
    if not 1_000 <= args.timeout_ms <= 60_000:
        parser.error("--timeout-ms must be 1000..60000")
    try:
        return read_url(args.url, args.max_chars, args.timeout_ms)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
