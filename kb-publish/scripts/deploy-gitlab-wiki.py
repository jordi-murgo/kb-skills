#!/usr/bin/env python3
"""
Deploys the wiki (wiki/) to the GitLab Wiki repository.

Usage:
    python3 scripts/deploy-gitlab-wiki.py [--dry-run] [--no-push]

    --dry-run: prepares files in a temporary directory but does not clone or push.
               Prints a summary of what would be done.
    --no-push: clones and prepares but does not push. Leaves the temporary directory for inspection.
    Default: full deployment with push.

Requirements:
    - git installed and SSH access to the wiki repo (configured in kb-config.yaml)
    - Python 3.8+
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────────────────

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
WIKI_DIR = REPO_ROOT / "wiki"

_GL = load_config(REPO_ROOT, "gitlab_wiki")

if not _GL.get("enabled", False):
    raise SystemExit(
        "gitlab_wiki.enabled is false in kb-config.json — nothing to publish.\n"
        "Set it to true once repo is configured."
    )
if not _GL.get("repo"):
    raise SystemExit("kb-config.json gitlab_wiki section is missing: repo")

WIKI_REPO = _GL["repo"]
WIKI_BRANCH = _GL.get("branch", "main")

# Target wiki platform: "github" (default) or "gitlab".
# GitHub Wiki uses Home.md (capital H) as landing page; GitLab uses home.md.
WIKI_TARGET = _GL.get("target", "github").lower()
if WIKI_TARGET not in ("github", "gitlab"):
    raise SystemExit(
        f"gitlab_wiki.target must be 'github' or 'gitlab', got '{WIKI_TARGET}'"
    )
HOME_FILENAME = "Home.md" if WIKI_TARGET == "github" else "home.md"

# Directories to exclude from the wiki
EXCLUDE_DIRS = set()

# Pattern for wikilinks: [[target]] or [[target|display text]]
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")

# Pattern for YAML frontmatter
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# ─── Utilities ──────────────────────────────────────────────────────────────


def info(msg: str) -> None:
    """Prints an informational message."""
    print(f"  • {msg}")


def ok(msg: str) -> None:
    """Prints a success message."""
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    """Prints a warning."""
    print(f"  ⚠ {msg}")


def error(msg: str) -> None:
    """Prints an error."""
    print(f"  ✗ {msg}", file=sys.stderr)


def run_cmd(cmd: list[str], cwd: str | None = None, dry_run: bool = False) -> subprocess.CompletedProcess:
    """Runs a command and returns the result. In dry-run mode only prints."""
    if dry_run:
        print(f"    [dry-run] $ {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error(f"Command failed: {' '.join(cmd)}")
            error(f"stderr: {result.stderr.strip()}")
        return result
    except FileNotFoundError:
        error(f"Command not found: {cmd[0]}. Is it installed?")
        sys.exit(1)


# ─── Build slug map ─────────────────────────────────────────────────────────


def build_slug_map(wiki_dir: Path) -> dict[str, str]:
    """
    Scans wiki_dir for .md files and builds a map:
      slug (without extension, relative to wiki/) → full file path

    Also adds entries for "bare" names (without subdirectory)
    so that [[aitor-landa]] resolves to entities/aitor-landa.
    """
    slug_map: dict[str, str] = {}
    wiki_dir_abs = wiki_dir.resolve()

    for md_file in sorted(wiki_dir.rglob("*.md")):
        # Skip files in excluded directories
        md_abs = md_file.resolve()
        try:
            rel = md_abs.relative_to(wiki_dir_abs)
        except ValueError:
            # If not a subpath (e.g. symlinks), use the original path
            rel = md_file.relative_to(wiki_dir)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue

        # Slug = relative path without .md extension
        slug = str(rel.with_suffix(""))
        slug_map[slug] = str(md_abs)

        # Also register the base name (last component) for resolving
        # wikilinks without path: [[aitor-landa]] → entities/aitor-landa
        bare_name = rel.stem
        if bare_name not in slug_map:
            slug_map[bare_name] = str(md_abs)

    return slug_map


# ─── Wikilink conversion ─────────────────────────────────────────────────────


def convert_wikilinks(content: str, slug_map: dict[str, str]) -> tuple[str, list[str]]:
    """
    Converts wikilinks to GitLab wiki format.

    - [[target]] → [[full/path]] if target resolves in slug_map
    - [[target|display]] → [[display|full/path]] if target resolves
    - If target already contains '/' it is left as is (already has a path)
    - If target starts with '.' it is left as is (raw source reference)
    - If target does not resolve, it is left as is and reported as broken

    Returns: (converted content, list of broken links)
    """
    broken_links: list[str] = []

    def replace_match(m: re.Match) -> str:
        inner = m.group(1)
        display = None
        target = inner

        # Separate target and display text if present
        if "|" in inner:
            target, display = inner.split("|", 1)
            target = target.strip()
            display = display.strip()

        # If it already has a path or is a raw source reference, leave as is
        if "/" in target or target.startswith("."):
            return m.group(0)

        # Look up in the slug map
        if target in slug_map:
            resolved = Path(slug_map[target])
            # Ensure absolute path for relative_to
            if not resolved.is_absolute():
                resolved = (WIKI_DIR.parent / resolved).resolve()
            else:
                resolved = resolved.resolve()
            wiki_abs = WIKI_DIR.resolve()
            rel = resolved.relative_to(wiki_abs)
            gitlab_slug = str(rel.with_suffix(""))

            if display:
                # GitLab: [[display|path]]
                return f"[[{display}|{gitlab_slug}]]"
            else:
                return f"[[{gitlab_slug}]]"
        else:
            # Does not resolve — broken link
            if target not in broken_links:
                broken_links.append(target)
            return m.group(0)

    result = WIKILINK_RE.sub(replace_match, content)
    return result, broken_links


# ─── Frontmatter conversion ─────────────────────────────────────────────────


def convert_frontmatter(content: str) -> str:
    """
    Converts YAML frontmatter (--- ... ---) to an HTML comment
    so it does not render as visible text in GitLab Wiki.
    """
    def replace_fm(m: re.Match) -> str:
        yaml_content = m.group(1)
        return f"<!-- frontmatter\n{yaml_content}-->\n"

    return FRONTMATTER_RE.sub(replace_fm, content)


# ─── File processing ──────────────────────────────────────────────────────────


def process_file(
    md_path: Path,
    slug_map: dict[str, str],
) -> tuple[str, list[str]]:
    """
    Reads a .md file, converts frontmatter and wikilinks.
    Returns: (processed content, list of broken links found)
    """
    content = md_path.read_text(encoding="utf-8")

    # 1. Convertir frontmatter a comentario HTML
    content = convert_frontmatter(content)

    # 2. Convertir wikilinks
    content, broken = convert_wikilinks(content, slug_map)

    return content, broken


# ─── Special page generation ────────────────────────────────────────────────


def read_dashboard_body() -> str | None:
    """
    Reads wiki/dashboard.md and returns its body without frontmatter or the first H1.
    Returns None if the file does not exist.
    """
    dashboard = WIKI_DIR / "dashboard.md"
    if not dashboard.exists():
        return None
    raw = dashboard.read_text(encoding="utf-8")
    body = FRONTMATTER_RE.sub("", raw, count=1)
    body = re.sub(r"^\s*#\s+.+?\n", "", body, count=1)
    return body.strip()


def list_wiki_sections() -> list[tuple[str, int]]:
    """
    Returns (directory_name, number of .md pages) for each first-level
    subdirectory of the wiki that contains pages, sorted alphabetically.
    """
    wiki_dir_abs = WIKI_DIR.resolve()
    sections: list[tuple[str, int]] = []
    for d in sorted(wiki_dir_abs.iterdir()):
        if not d.is_dir() or d.name in EXCLUDE_DIRS:
            continue
        pages = [
            f for f in d.rglob("*.md")
            if not any(part in EXCLUDE_DIRS for part in f.relative_to(wiki_dir_abs).parts)
        ]
        if pages:
            sections.append((d.name, len(pages)))
    return sections


def generate_home(slug_map: dict[str, str]) -> str:
    """
    Generates the home page (Home.md for GitHub, home.md for GitLab)
    with links to the main pages.
    Uses the converted wikilink format (already with full paths).
    """
    # Resolve slugs for key pages
    def link(name: str, display: str | None = None) -> str:
        if name in slug_map:
            rel = Path(slug_map[name]).relative_to(WIKI_DIR.resolve())
            slug = str(rel.with_suffix(""))
            if display:
                return f"[[{display}|{slug}]]"
            return f"[[{slug}]]"
        return f"[[{name}]]"

    lines = [
        "# Project Wiki",
        "",
        "Project Wiki.",
        "",
        "---",
        "",
    ]

    dashboard_body = read_dashboard_body()
    if dashboard_body:
        resolved_body, broken = convert_wikilinks(dashboard_body, slug_map)
        if broken:
            warn(f"dashboard.md: unresolved wikilinks: {', '.join(broken)}")
        lines += [
            "## Dashboard",
            "",
            resolved_body,
            "",
            f"> Source: {link('dashboard')}",
            "",
            "---",
            "",
        ]

    lines += [
        "## Quick Navigation",
        "",
        f"- {link('index', 'Full Index')} — all wiki pages",
        f"- {link('overview', 'Executive Summary')} — project overview",
        f"- {link('hot', 'Recent Context')} — latest updates and decisions",
        f"- {link('log', 'Operation Log')} — chronological change log",
        "",
    ]

    sections = list_wiki_sections()
    if sections:
        lines += [
            "## Sections",
            "",
        ]
        for name, count in sections:
            title = name.replace("-", " ").replace("_", " ").title()
            noun = "page" if count == 1 else "pages"
            lines.append(f"- [[{title}|{name}]] — {count} {noun}")
        lines.append("")

    lines += [
        "## Sources",
        "",
    ]

    # Links to sources
    sources = [k for k in slug_map if k.startswith("sources/")]
    for s in sources:
        rel = Path(slug_map[s]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        lines.append(f"- [[{slug}]]")

    lines += [
        "",
        "## Entities",
        "",
    ]

    entities = [k for k in slug_map if k.startswith("entities/")]
    for e in entities:
        rel = Path(slug_map[e]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        lines.append(f"- [[{slug}]]")

    lines += [
        "",
        "## Concepts",
        "",
    ]

    concepts = [k for k in slug_map if k.startswith("concepts/")]
    for c in concepts:
        rel = Path(slug_map[c]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        lines.append(f"- [[{slug}]]")

    lines += [
        "",
        "---",
        "",
        "> This wiki is automatically deployed from the repository.",
        f"> Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    return "\n".join(lines)


def generate_directory_indexes(slug_map: dict[str, str]) -> list[str]:
    """
    Generates flat index pages for each subdirectory of the wiki.

    GitLab Wiki (Gollum) does not recognize _index.md as a subdirectory
    landing page: the URL /wikis/jira-issues looks for a page with the exact
    slug 'jira-issues'. Without this page, the URL returns 404 even if the
    directory with child pages exists.

    For each first-level subdirectory under wiki/ that does NOT already have
    a flat page with the same name, <dir>.md is generated at the wiki root
    with a listing of child pages.

    Returns the list of generated filenames (without path).
    """
    wiki_dir_abs = WIKI_DIR.resolve()
    generated: list[str] = []

    # First-level subdirectories (excluding empty directories)
    subdirs = sorted(
        d for d in wiki_dir_abs.iterdir()
        if d.is_dir() and d.name not in EXCLUDE_DIRS and any(d.rglob("*.md"))
    )

    for subdir in subdirs:
        dir_name = subdir.name
        planar_slug = dir_name  # e.g. "jira-issues"

        # If a flat page with this slug already exists, do not generate a duplicate
        if planar_slug in slug_map:
            continue

        # Collect child pages (slug relative to wiki)
        child_slugs: list[str] = []
        for md_file in sorted(subdir.rglob("*.md")):
            if any(part in EXCLUDE_DIRS for part in md_file.relative_to(wiki_dir_abs).parts):
                continue
            rel = md_file.relative_to(wiki_dir_abs)
            child_slug = str(rel.with_suffix(""))
            child_slugs.append(child_slug)

        if not child_slugs:
            continue

        # Readable title
        title = dir_name.replace("-", " ").replace("_", " ").title()

        lines = [
            f"# {title}",
            "",
            f"Pages in section **{dir_name}**:",
            "",
        ]
        for slug in child_slugs:
            lines.append(f"- [[{slug}]]")

        lines.append("")

        # Writing directly to the temporary directory is NOT possible here
        # because it does not exist yet. The caller (prepare_files) does it.
        # We return the content so prepare_files can write it.
        generated.append(planar_slug)

    return generated


def extract_page_title(md_file: Path) -> str:
    """Extracts the page title: frontmatter, H1, or filename."""
    content = md_file.read_text(encoding="utf-8")

    title_match = re.search(r"^title:\s*(.+?)\s*$", content, re.MULTILINE)
    if title_match:
        return title_match.group(1).strip().strip('"\'')

    heading_match = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
    if heading_match:
        return heading_match.group(1).strip()

    return md_file.stem.replace("-", " ").replace("_", " ").title()


def natural_sort_key(value: str) -> list[str | int]:
    """Sorts text with numbers naturally: 1, 2, 3, 10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def escape_wikilink_display(title: str) -> str:
    """Escapes characters that break the visible text of a Gollum wikilink."""
    return (
        title.replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("|", "&#124;")
    )


def generate_directory_index_content(dir_name: str, slug_map: dict[str, str]) -> str:
    """
    Returns the Markdown content of the flat index page for a given
    subdirectory. Used by prepare_files to write <dir>.md
    at the root of the temporary directory.
    """
    wiki_dir_abs = WIKI_DIR.resolve()
    subdir = wiki_dir_abs / dir_name

    child_pages: list[tuple[str, str]] = []
    md_files = sorted(
        subdir.rglob("*.md"),
        key=lambda path: natural_sort_key(str(path.relative_to(subdir))),
    )
    for md_file in md_files:
        if any(part in EXCLUDE_DIRS for part in md_file.relative_to(wiki_dir_abs).parts):
            continue
        rel = md_file.relative_to(wiki_dir_abs)
        child_slug = str(rel.with_suffix(""))
        display_title = escape_wikilink_display(extract_page_title(md_file))
        child_pages.append((child_slug, display_title))

    title = dir_name.replace("-", " ").replace("_", " ").title()
    lines = [
        f"# {title}",
        "",
        f"{len(child_pages)} pages in section **{dir_name}**:",
        "",
    ]
    for slug, display_title in child_pages:
        lines.append(f"- [[{display_title}|{slug}]]")
    lines.append("")
    return "\n".join(lines)


def generate_sidebar(slug_map: dict[str, str]) -> str:
    """
    Generates _sidebar.md — navigation sidebar for GitLab Wiki.
    Organized by sections.
    """
    def link(name: str, display: str | None = None) -> str:
        if name in slug_map:
            rel = Path(slug_map[name]).relative_to(WIKI_DIR.resolve())
            slug = str(rel.with_suffix(""))
            if display:
                return f"[[{display}|{slug}]]"
            return f"[[{slug}]]"
        return f"[[{name}]]"

    lines = [
        "## Wiki",
        "",
        "### General",
        "",
        f"- {link('home', 'Home')}",
        f"- {link('index', 'Index')}",
        f"- {link('overview', 'Overview')}",
        f"- {link('hot', 'Recent Context')}",
        f"- {link('log', 'Operation Log')}",
        "",
        "### Sources",
        "",
    ]

    sources = sorted(k for k in slug_map if k.startswith("sources/"))
    for s in sources:
        rel = Path(slug_map[s]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        # Extract readable title from filename
        display = rel.stem.replace("-", " ").replace("_", " ").title()
        lines.append(f"- [[{display}|{slug}]]")

    lines += [
        "",
        "### Concepts",
        "",
    ]

    concepts = sorted(k for k in slug_map if k.startswith("concepts/"))
    for c in concepts:
        rel = Path(slug_map[c]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        display = rel.stem.replace("-", " ").replace("_", " ").title()
        lines.append(f"- [[{display}|{slug}]]")

    lines += [
        "",
        "### Entities",
        "",
    ]

    entities = sorted(k for k in slug_map if k.startswith("entities/"))
    for e in entities:
        rel = Path(slug_map[e]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        display = rel.stem.replace("-", " ").replace("_", " ").title()
        lines.append(f"- [[{display}|{slug}]]")

    lines += [
        "",
        "### Meta",
        "",
    ]

    meta = sorted(k for k in slug_map if k.startswith("meta/"))
    for m in meta:
        rel = Path(slug_map[m]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        lines.append(f"- [[{slug}]]")

    lines += [
        "",
        "### Infrastructure",
        "",
    ]

    infra = sorted(k for k in slug_map if k.startswith("infrastructure/"))
    for i in infra:
        rel = Path(slug_map[i]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        lines.append(f"- [[{slug}]]")

    return "\n".join(lines)


# ─── Prepare files in temporary directory ───────────────────────────────────


def prepare_files(
    wiki_dir: Path,
    slug_map: dict[str, str],
    dry_run: bool = False,
) -> tuple[Path, list[str]]:
    """
    Prepares all wiki files in a temporary directory:
    1. Copies files preserving subdirectories
    2. Converts frontmatter and wikilinks
    3. Generates home page (Home.md/home.md) and _sidebar.md

    Returns: (path to temporary directory, list of global broken links)
    """
    all_broken_links: list[str] = []
    processed_count = 0

    # Create temporary directory
    tmp_dir = Path(tempfile.mkdtemp(prefix="kb-wiki-"))
    info(f"Temporary directory: {tmp_dir}")

    # Copy and process files
    for md_file in sorted(wiki_dir.rglob("*.md")):
        rel = md_file.relative_to(wiki_dir)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue

        # Create subdirectories in the destination
        dest_dir = tmp_dir / rel.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = tmp_dir / rel

        # Process the file
        content, broken = process_file(md_file, slug_map)
        all_broken_links.extend(broken)
        dest_file.write_text(content, encoding="utf-8")
        processed_count += 1

    # Generate home page (Home.md for GitHub, home.md for GitLab)
    home_content = generate_home(slug_map)
    (tmp_dir / HOME_FILENAME).write_text(home_content, encoding="utf-8")
    ok(f"Generated {HOME_FILENAME}")

    # Generate _sidebar.md
    sidebar_content = generate_sidebar(slug_map)
    (tmp_dir / "_sidebar.md").write_text(sidebar_content, encoding="utf-8")
    ok(f"Generated _sidebar.md")

    # Generate flat index pages for subdirectories without a landing page
    index_dirs = generate_directory_indexes(slug_map)
    for dir_name in index_dirs:
        index_content = generate_directory_index_content(dir_name, slug_map)
        (tmp_dir / f"{dir_name}.md").write_text(index_content, encoding="utf-8")
        ok(f"Generated {dir_name}.md (section index)")

    generated_count = 2 + len(index_dirs)  # home + sidebar + indexes
    info(f"Processed {processed_count} files + {generated_count} generated pages")
    return tmp_dir, all_broken_links


# ─── Git deployment ─────────────────────────────────────────────────────────


def deploy_to_gitlab(
    tmp_dir: Path,
    dry_run: bool = False,
    no_push: bool = False,
) -> None:
    """
    Clones the GitLab wiki repository, replaces the content,
    commits and pushes.
    """
    if dry_run:
        info("Dry-run mode — will not clone or push")
        return

    # Clone the wiki repository
    clone_dir = Path(tempfile.mkdtemp(prefix="kb-wiki-clone-"))
    info(f"Cloning wiki repo in {clone_dir}...")

    result = run_cmd(
        ["git", "clone", WIKI_REPO, str(clone_dir)],
    )
    if result.returncode != 0:
        error("Could not clone the wiki repository")
        # Clean up
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(clone_dir, ignore_errors=True)
        sys.exit(1)

    # Clean existing content (except .git)
    for item in clone_dir.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink()

    # Copy prepared files
    info("Copying prepared files...")
    for item in tmp_dir.iterdir():
        if item.is_dir():
            shutil.copytree(item, clone_dir / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, clone_dir / item.name)

    # Commit
    commit_msg = f"deploy: wiki update {datetime.now().strftime('%Y-%m-%d')}"
    info(f"Commit: {commit_msg}")

    run_cmd(["git", "add", "-A"], cwd=str(clone_dir))
    run_cmd(["git", "commit", "-m", commit_msg], cwd=str(clone_dir))

    if no_push:
        info("--no-push mode — commit made but no push")
        info(f"Inspection directory: {clone_dir}")
        info(f"To push manually: cd {clone_dir} && git push origin {WIKI_BRANCH}")
    else:
        info("Pushing...")
        result = run_cmd(
            ["git", "push", "origin", WIKI_BRANCH],
            cwd=str(clone_dir),
        )
        if result.returncode == 0:
            ok("Push completed successfully")
        else:
            error("Push failed")
            info(f"You can push manually from: {clone_dir}")

        # Clean up clone directory
        shutil.rmtree(clone_dir, ignore_errors=True)

    # Clean up temporary preparation directory
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Summary ────────────────────────────────────────────────────────────────


def print_summary(
    slug_map: dict[str, str],
    broken_links: list[str],
    dry_run: bool,
    no_push: bool,
) -> None:
    """Prints a deployment summary."""
    # Count unique pages by file path
    # slug_map has duplicate entries (bare name + full path point to the same file)
    unique_paths = set(slug_map.values())
    page_count = len(unique_paths)

    # Categorize by directory
    categories: dict[str, int] = {}
    for path in sorted(unique_paths):
        rel = Path(path).relative_to(WIKI_DIR.resolve())
        if "/" in str(rel):
            cat = str(rel).split("/")[0]
        else:
            cat = "root"
        categories[cat] = categories.get(cat, 0) + 1

    print()
    print("=" * 60)
    print("  DEPLOYMENT SUMMARY")
    print("=" * 60)
    print()
    print(f"  Mode: {'dry-run' if dry_run else 'no-push' if no_push else 'full'}")
    print(f"  Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()
    print(f"  Wiki pages: {page_count}")
    for cat, count in sorted(categories.items()):
        print(f"    {cat}: {count}")
    print()

    if broken_links:
        # Deduplicate
        unique_broken = sorted(set(broken_links))
        print(f"  ⚠ Broken links: {len(unique_broken)}")
        for link in unique_broken:
            print(f"    - [[{link}]]")
    else:
        print(f"  ✓ Broken links: 0")
    print()

    if dry_run:
        print(f"  Actions that would be performed in a real deployment:")
        print(f"    1. Clone {WIKI_REPO}")
        print(f"    2. Replace wiki content")
        print(f"    3. Commit: 'deploy: wiki update ...'")
        print(f"    4. Push to branch '{WIKI_BRANCH}'")
    print()
    print("=" * 60)


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploys the wiki to the GitLab Wiki repository",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepares files but does not clone or push. Prints summary.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Clones and prepares but does not push. Leaves directory for inspection.",
    )
    args = parser.parse_args()

    dry_run = args.dry_run
    no_push = args.no_push

    print()
    print("🚀 Wiki Deployment")
    print("=" * 60)
    print()

    # Validate that the wiki directory exists
    if not WIKI_DIR.exists():
        error(f"Wiki directory not found: {WIKI_DIR}")
        sys.exit(1)

    # 1. Build slug map
    info("Scanning wiki files...")
    slug_map = build_slug_map(WIKI_DIR)
    ok(f"Found {len(slug_map)} .md files")

    # 2. Prepare files in temporary directory
    info("Preparing files...")
    tmp_dir, broken_links = prepare_files(WIKI_DIR, slug_map, dry_run)

    # 3. Desplegar a GitLab
    deploy_to_gitlab(tmp_dir, dry_run, no_push)

    # 4. Imprimir resumen
    print_summary(slug_map, broken_links, dry_run, no_push)


if __name__ == "__main__":
    main()
