#!/usr/bin/env python3
"""Import Jira issues to the vault's raw source directory via REST API v3.

Reads the `jira` section of kb-config.json at the vault root: base_url,
project_key and output_dir. Nothing project-specific is hardcoded here.

Usage:
    set -a && . ./.env.local && set +a && \
        python3 .agents/skills/kb-jira-sync/scripts/import-jira.py

Requires ATLASIAN_EMAIL and ATLASIAN_API_KEY in environment (never in the
config file). Writes <output_dir>/<KEY>-N.md in deterministic Markdown format.
Only overwrites files when content changed (git-friendly).
"""
import json
import os
import sys
import urllib.request
import urllib.parse
import base64
from pathlib import Path

def find_repo_root() -> Path:
    """Walk up from this file to the vault root (the directory holding wiki/).

    This script lives inside a skill, so its depth below the repo root depends on
    where the skill is installed. Counting .parent levels breaks the moment it
    moves; looking for the marker does not.
    """
    for parent in Path(__file__).resolve().parents:
        # Both markers, not just wiki/: there is a skill directory named "wiki"
        # under .agents/skills/, so the wiki/ marker alone resolves to the skills
        # directory instead of the repo root.
        if (parent / "wiki").is_dir() and (parent / ".git").exists():
            return parent
    raise SystemExit("repo root not found: no ancestor has both wiki/ and .git")


def load_config(root: Path, section: str) -> dict:
    """Read one section of the vault's kb-config file.

    YAML is preferred; JSON is still accepted so consumer projects migrate at
    their own pace. Kept inline rather than in a shared module: a skill copied on
    its own into another project's .agents/skills/ has to keep working, and a
    shared import would not travel with it.
    """
    candidates = [root / "kb-config.yaml", root / "kb-config.yml", root / "kb-config.json"]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise SystemExit(
            "config not found. Looked for:\n  "
            + "\n  ".join(str(p) for p in candidates)
            + "\nCopy kb-config.example.yaml from the kb-skills repo and fill it in."
        )

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
        except yaml.YAMLError as e:
            raise SystemExit(f"{path} is not valid YAML: {e}")

    if not isinstance(cfg, dict):
        raise SystemExit(f"{path} must contain a mapping at the top level")
    if section not in cfg:
        raise SystemExit(f"{path} has no '{section}' section")
    return cfg[section]


REPO_ROOT = find_repo_root()
_JIRA = load_config(REPO_ROOT, "jira")

if not _JIRA.get("enabled", False):
    raise SystemExit(
        "jira.enabled is false in kb-config.json — nothing to import.\n"
        "Set it to true once base_url and project_key are configured."
    )

_missing = [k for k in ("base_url", "project_key") if not _JIRA.get(k)]
if _missing:
    raise SystemExit(f"kb-config.json jira section is missing: {', '.join(_missing)}")

def vault_path(root: Path, value: str, key: str) -> Path:
    """Resolve a config path value against the vault root, refusing to escape it.

    Config paths must be relative to the vault root. Absolute values are rejected
    outright: `Path("/vault") / "/etc/passwd"` yields `/etc/passwd` in Python,
    silently writing outside the vault. `..` traversal is rejected for the same
    reason. PowerShell's Join-Path does not behave the same way, so without this
    check one config file would mean two different things depending on which
    pipeline read it.
    """
    if os.path.isabs(value) or value.startswith("~"):
        raise SystemExit(
            f"kb-config.json {key} must be relative to the vault root, got: {value}"
        )
    resolved = (root / value).resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise SystemExit(
            f"kb-config.json {key} escapes the vault root, got: {value}"
        )
    return resolved


JIRA_BASE = _JIRA["base_url"].rstrip("/")
PROJECT_KEY = _JIRA["project_key"]
RAW_JIRA_DIR = vault_path(REPO_ROOT, _JIRA.get("output_dir", ".raw/jira"), "jira.output_dir")


def load_env():
    """Load .env.local if present."""
    env_file = REPO_ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def api_get(path: str, auth_header: str) -> dict:
    url = f"{JIRA_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": auth_header,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_post(path: str, body: dict, auth_header: str) -> dict:
    url = f"{JIRA_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": auth_header,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def adf_to_markdown(node) -> str:
    """Convert Atlassian Document Format (ADF) to simple Markdown.

    Handles type:doc with paragraphs containing text nodes.
    Falls back to string if not ADF.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return str(node)

    doc_type = node.get("type")
    if doc_type == "doc" and "content" in node:
        parts = []
        for block in node["content"]:
            parts.append(adf_block_to_markdown(block))
        return "\n\n".join(p for p in parts if p)
    return adf_block_to_markdown(node)


def adf_block_to_markdown(block: dict) -> str:
    """Convert a single ADF block to Markdown."""
    if not isinstance(block, dict):
        return str(block)

    btype = block.get("type", "")
    content = block.get("content", [])

    if btype == "paragraph":
        texts = []
        for node in content:
            if isinstance(node, dict) and node.get("type") == "text":
                texts.append(node.get("text", ""))
            elif isinstance(node, dict):
                texts.append(adf_block_to_markdown(node))
        return "".join(texts)

    if btype == "heading":
        level = block.get("attrs", {}).get("level", 1)
        texts = []
        for node in content:
            if isinstance(node, dict) and node.get("type") == "text":
                texts.append(node.get("text", ""))
        return f"{'#' * level} {''.join(texts)}"

    if btype == "bulletList":
        items = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "listItem":
                item_text = adf_block_to_markdown(item.get("content", [{}])[0] if item.get("content") else {})
                items.append(f"- {item_text}")
        return "\n".join(items)

    if btype == "orderedList":
        items = []
        for i, item in enumerate(content, 1):
            if isinstance(item, dict) and item.get("type") == "listItem":
                item_text = adf_block_to_markdown(item.get("content", [{}])[0] if item.get("content") else {})
                items.append(f"{i}. {item_text}")
        return "\n".join(items)

    if btype == "codeBlock":
        texts = []
        for node in content:
            if isinstance(node, dict) and node.get("type") == "text":
                texts.append(node.get("text", ""))
        return f"```\n{''.join(texts)}\n```"

    if btype == "blockquote":
        texts = []
        for node in content:
            texts.append(adf_block_to_markdown(node))
        return "\n".join(f"> {t}" for t in texts if t)

    if btype == "text":
        return block.get("text", "")

    # Fallback: try to extract text from content
    if content:
        texts = []
        for node in content:
            texts.append(adf_block_to_markdown(node))
        return "".join(texts)
    return ""


def fetch_all_issues(auth_header: str) -> list:
    """Fetch all issues from the configured project with pagination via POST /rest/api/3/search/jql.

    The new search/jql endpoint uses nextPageToken for pagination (not startAt).
    """
    all_issues = []
    next_page_token = None
    while True:
        body = {
            "jql": f"project = {PROJECT_KEY}",
            "fields": ["*all"],
            "maxResults": 100,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        data = api_post("/rest/api/3/search/jql", body, auth_header)
        issues = data.get("issues", [])
        all_issues.extend(issues)
        next_page_token = data.get("nextPageToken")
        if not next_page_token or not issues:
            break
    return all_issues


def fetch_comments(issue_key: str, auth_header: str) -> list:
    """Fetch comments for a single issue."""
    path = f"/rest/api/3/issue/{issue_key}/comment?maxResults=100"
    try:
        data = api_get(path, auth_header)
        return data.get("comments", [])
    except Exception:
        return []


def format_issue_markdown(issue: dict, comments: list) -> str:
    """Format an issue as Markdown matching the existing .raw/jira/ format."""
    key = issue["key"]
    fields = issue["fields"]

    summary = fields.get("summary", "")
    itype = fields.get("issuetype", {}).get("name", "")
    status = fields.get("status", {}).get("name", "")
    priority = fields.get("priority", {}).get("name", "")
    assignee = fields.get("assignee", {}).get("displayName", "Unassigned") if fields.get("assignee") else "Unassigned"
    reporter = fields.get("reporter", {}).get("displayName", "") if fields.get("reporter") else ""
    created = fields.get("created", "")
    updated = fields.get("updated", "")
    duedate = fields.get("duedate", "") or ""
    project_key = fields.get("project", {}).get("key", "")
    project_name = fields.get("project", {}).get("name", "")
    parent_key = fields.get("parent", {}).get("key", "") if fields.get("parent") else ""

    lines = [
        f"# {key}: {summary}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Type | {itype} |",
        f"| Status | {status} |",
        f"| Priority | {priority} |",
        f"| Assignee | {assignee} |",
        f"| Reporter | {reporter} |",
        f"| Created | {created} |",
        f"| Updated | {updated} |",
        f"| Due Date | {duedate} |",
        f"| Project | {project_key} — {project_name} |",
        f"| Parent | {parent_key} |",
        f"| URL | {JIRA_BASE}/browse/{key} |",
        "",
        "## Description",
        "",
    ]

    desc = adf_to_markdown(fields.get("description"))
    lines.append(desc if desc else "No description")
    lines.append("")

    # Labels
    labels = fields.get("labels", [])
    lines.append("## Labels")
    lines.append("")
    lines.append(", ".join(labels) if labels else "None")
    lines.append("")

    # Subtasks
    subtasks = fields.get("subtasks", [])
    lines.append("## Subtasks")
    lines.append("")
    if subtasks:
        for st in subtasks:
            st_key = st.get("key", "")
            st_fields = st.get("fields", {})
            st_summary = st_fields.get("summary", "") if st_fields else ""
            lines.append(f"- [{st_key}](../{st_key}.md) — {st_summary}")
    else:
        lines.append("None")
    lines.append("")

    # Issue links
    issue_links = fields.get("issuelinks", [])
    lines.append("## Issue Links")
    lines.append("")
    if issue_links:
        for link in issue_links:
            ltype = link.get("type", {})
            outward = ltype.get("outward", "relates to")
            # Try outwardIssue first, then inwardIssue
            linked = link.get("outwardIssue") or link.get("inwardIssue")
            if linked:
                lk = linked.get("key", "")
                lsum = linked.get("fields", {}).get("summary", "")
                lines.append(f"- **{outward}** → [{lk}](../{lk}.md) — {lsum}")
    else:
        lines.append("None")
    lines.append("")

    # Comments
    lines.append("## Comments")
    lines.append("")
    if comments:
        for c in sorted(comments, key=lambda x: x.get("created", "")):
            author = c.get("author", {}).get("displayName", "Unknown")
            cdate = c.get("created", "")
            body = adf_to_markdown(c.get("body"))
            lines.append(f"### {author} — {cdate}")
            lines.append("")
            lines.append(body if body else "(empty)")
            lines.append("")
    else:
        lines.append("No comments")
    lines.append("")

    return "\n".join(lines)


def main():
    load_env()

    email = os.environ.get("ATLASIAN_EMAIL", "")
    token = os.environ.get("ATLASIAN_API_KEY", "")
    if not email or not token:
        print("ERROR: ATLASIAN_EMAIL and ATLASIAN_API_KEY required in .env.local", file=sys.stderr)
        sys.exit(1)

    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    auth_header = f"Basic {auth}"

    print(f"Fetching issues from {PROJECT_KEY}...")
    issues = fetch_all_issues(auth_header)
    print(f"  Found {len(issues)} issues")

    RAW_JIRA_DIR.mkdir(parents=True, exist_ok=True)

    new_count = 0
    modified_count = 0
    unchanged_count = 0

    for issue in issues:
        key = issue["key"]
        filename = f"{key}.md"
        filepath = RAW_JIRA_DIR / filename

        print(f"  Fetching comments for {key}...", end=" ")
        comments = fetch_comments(key, auth_header)
        print(f"{len(comments)} comments")

        content = format_issue_markdown(issue, comments)
        content += "\n"  # trailing newline

        if filepath.exists():
            existing = filepath.read_text(encoding="utf-8")
            if existing == content:
                unchanged_count += 1
                continue
            modified_count += 1
        else:
            new_count += 1

        filepath.write_text(content, encoding="utf-8")

    print()
    print(f"=== Import summary ===")
    print(f"  Total issues: {len(issues)}")
    print(f"  New files:    {new_count}")
    print(f"  Modified:     {modified_count}")
    print(f"  Unchanged:    {unchanged_count}")
    print(f"  Output dir:   {RAW_JIRA_DIR}")


if __name__ == "__main__":
    main()