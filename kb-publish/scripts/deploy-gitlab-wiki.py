#!/usr/bin/env python3
"""
Despliega el wiki de Obsidian (wiki/) en el repositorio GitLab Wiki.

Uso:
    python3 scripts/deploy-gitlab-wiki.py [--dry-run] [--no-push]

    --dry-run: prepara archivos en un directorio temporal pero no clona ni hace push.
               Imprime un resumen de lo que se haría.
    --no-push: clona y prepara pero no hace push. Deja el directorio temporal para inspección.
    Por defecto: despliegue completo con push.

Requisitos:
    - git instalado y acceso SSH al wiki repo (configurado en kb-config.yaml)
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

# ─── Configuración ───────────────────────────────────────────────────────────

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

# Directorios a excluir del wiki
EXCLUDE_DIRS = {".obsidian"}

# Patrón para wikilinks: [[target]] o [[target|display text]]
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")

# Patrón para frontmatter YAML
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# ─── Utilidades ─────────────────────────────────────────────────────────────


def info(msg: str) -> None:
    """Imprime un mensaje informativo."""
    print(f"  • {msg}")


def ok(msg: str) -> None:
    """Imprime un mensaje de éxito."""
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    """Imprime una advertencia."""
    print(f"  ⚠ {msg}")


def error(msg: str) -> None:
    """Imprime un error."""
    print(f"  ✗ {msg}", file=sys.stderr)


def run_cmd(cmd: list[str], cwd: str | None = None, dry_run: bool = False) -> subprocess.CompletedProcess:
    """Ejecuta un comando y devuelve el resultado. En dry-run solo imprime."""
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
            error(f"Comando falló: {' '.join(cmd)}")
            error(f"stderr: {result.stderr.strip()}")
        return result
    except FileNotFoundError:
        error(f"Comando no encontrado: {cmd[0]}. ¿Está instalado?")
        sys.exit(1)


# ─── Construcción del mapa de slugs ─────────────────────────────────────────


def build_slug_map(wiki_dir: Path) -> dict[str, str]:
    """
    Escanea wiki_dir buscando archivos .md y construye un mapa:
      slug (sin extensión, relativo a wiki/) → ruta completa del archivo

    También añade entradas para nombres "desnudos" (sin subdirectorio)
    para que [[aitor-landa]] resuelva a entities/aitor-landa.
    """
    slug_map: dict[str, str] = {}
    wiki_dir_abs = wiki_dir.resolve()

    for md_file in sorted(wiki_dir.rglob("*.md")):
        # Saltar archivos en directorios excluidos
        md_abs = md_file.resolve()
        try:
            rel = md_abs.relative_to(wiki_dir_abs)
        except ValueError:
            # Si no es subpath (ej: symlinks), usar el path original
            rel = md_file.relative_to(wiki_dir)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue

        # Slug = ruta relativa sin extensión .md
        slug = str(rel.with_suffix(""))
        slug_map[slug] = str(md_abs)

        # También registrar el nombre base (último componente) para resolución
        # de wikilinks sin ruta: [[aitor-landa]] → entities/aitor-landa
        bare_name = rel.stem
        if bare_name not in slug_map:
            slug_map[bare_name] = str(md_abs)

    return slug_map


# ─── Conversión de wikilinks ────────────────────────────────────────────────


def convert_wikilinks(content: str, slug_map: dict[str, str]) -> tuple[str, list[str]]:
    """
    Convierte wikilinks de Obsidian a formato GitLab wiki.

    - [[target]] → [[ruta/completa]] si target resuelve en slug_map
    - [[target|display]] → [[display|ruta/completa]] si target resuelve
    - Si target ya contiene '/' se deja como está (ya tiene ruta)
    - Si target empieza con '.' se deja como está (referencia a fuente raw)
    - Si target no resuelve, se deja como está y se reporta como broken

    Devuelve: (contenido convertido, lista de broken links)
    """
    broken_links: list[str] = []

    def replace_match(m: re.Match) -> str:
        inner = m.group(1)
        display = None
        target = inner

        # Separar target y display text si existe
        if "|" in inner:
            target, display = inner.split("|", 1)
            target = target.strip()
            display = display.strip()

        # Si ya tiene ruta o es referencia a fuente raw, dejar como está
        if "/" in target or target.startswith("."):
            return m.group(0)

        # Buscar en el mapa de slugs
        if target in slug_map:
            resolved = Path(slug_map[target])
            # Asegurar ruta absoluta para relative_to
            if not resolved.is_absolute():
                resolved = (WIKI_DIR.parent / resolved).resolve()
            else:
                resolved = resolved.resolve()
            wiki_abs = WIKI_DIR.resolve()
            rel = resolved.relative_to(wiki_abs)
            gitlab_slug = str(rel.with_suffix(""))

            if display:
                # GitLab: [[display|ruta]]
                return f"[[{display}|{gitlab_slug}]]"
            else:
                return f"[[{gitlab_slug}]]"
        else:
            # No resuelve — broken link
            if target not in broken_links:
                broken_links.append(target)
            return m.group(0)

    result = WIKILINK_RE.sub(replace_match, content)
    return result, broken_links


# ─── Conversión de frontmatter ──────────────────────────────────────────────


def convert_frontmatter(content: str) -> str:
    """
    Convierte frontmatter YAML (--- ... ---) a comentario HTML
    para que no se renderice como texto visible en GitLab Wiki.
    """
    def replace_fm(m: re.Match) -> str:
        yaml_content = m.group(1)
        return f"<!-- frontmatter\n{yaml_content}-->\n"

    return FRONTMATTER_RE.sub(replace_fm, content)


# ─── Procesamiento de archivos ──────────────────────────────────────────────


def process_file(
    md_path: Path,
    slug_map: dict[str, str],
) -> tuple[str, list[str]]:
    """
    Lee un archivo .md, convierte frontmatter y wikilinks.
    Devuelve: (contenido procesado, lista de broken links encontrados)
    """
    content = md_path.read_text(encoding="utf-8")

    # 1. Convertir frontmatter a comentario HTML
    content = convert_frontmatter(content)

    # 2. Convertir wikilinks
    content, broken = convert_wikilinks(content, slug_map)

    return content, broken


# ─── Generación de páginas especiales ──────────────────────────────────────


def read_dashboard_body() -> str | None:
    """
    Lee wiki/dashboard.md y devuelve su cuerpo sin frontmatter ni el primer H1.
    Devuelve None si el archivo no existe.
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
    Devuelve (nombre_dir, nº de páginas .md) para cada subdirectorio de primer
    nivel del wiki que contenga páginas, ordenado alfabéticamente.
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
    Genera home.md — página de inicio con enlaces a las páginas principales.
    Usa el formato de wikilink convertido (ya con rutas completas).
    """
    # Resolver slugs para las páginas clave
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
        "Wiki del proyecto.",
        "",
        "---",
        "",
    ]

    dashboard_body = read_dashboard_body()
    if dashboard_body:
        resolved_body, broken = convert_wikilinks(dashboard_body, slug_map)
        if broken:
            warn(f"dashboard.md: wikilinks sin resolver: {', '.join(broken)}")
        lines += [
            "## Dashboard",
            "",
            resolved_body,
            "",
            f"> Fuente: {link('dashboard')}",
            "",
            "---",
            "",
        ]

    lines += [
        "## Navegación rápida",
        "",
        f"- {link('index', 'Índice completo')} — todas las páginas del wiki",
        f"- {link('overview', 'Resumen ejecutivo')} — visión general del proyecto",
        f"- {link('hot', 'Contexto reciente')} — últimas novedades y decisiones",
        f"- {link('log', 'Operation Log')} — registro cronológico de cambios",
        "",
    ]

    sections = list_wiki_sections()
    if sections:
        lines += [
            "## Secciones",
            "",
        ]
        for name, count in sections:
            title = name.replace("-", " ").replace("_", " ").title()
            noun = "página" if count == 1 else "páginas"
            lines.append(f"- [[{title}|{name}]] — {count} {noun}")
        lines.append("")

    lines += [
        "## Fuentes",
        "",
    ]

    # Enlaces a fuentes
    sources = [k for k in slug_map if k.startswith("sources/")]
    for s in sources:
        rel = Path(slug_map[s]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        lines.append(f"- [[{slug}]]")

    lines += [
        "",
        "## Entidades",
        "",
    ]

    entities = [k for k in slug_map if k.startswith("entities/")]
    for e in entities:
        rel = Path(slug_map[e]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        lines.append(f"- [[{slug}]]")

    lines += [
        "",
        "## Conceptos",
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
        "> Este wiki se despliega automáticamente desde el repositorio Obsidian.",
        f"> Última actualización: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    return "\n".join(lines)


def generate_directory_indexes(slug_map: dict[str, str]) -> list[str]:
    """
    Genera páginas índice planas para cada subdirectorio del wiki.

    GitLab Wiki (Gollum) no reconoce _index.md como landing de un
    subdirectorio: la URL /wikis/jira-issues busca una página con slug
    exacto 'jira-issues'. Sin esta página, la URL devuelve 404 aunque
    exista el directorio con páginas hijas.

    Para cada subdirectorio de primer nivel bajo wiki/ que NO tenga ya
    una página plana con su mismo nombre, se genera <dir>.md en la raíz
    del wiki con un listado de las páginas hijas.

    Devuelve la lista de nombres de archivos generados (sin ruta).
    """
    wiki_dir_abs = WIKI_DIR.resolve()
    generated: list[str] = []

    # Subdirectorios de primer nivel (excluyendo .obsidian y directorios vacíos)
    subdirs = sorted(
        d for d in wiki_dir_abs.iterdir()
        if d.is_dir() and d.name not in EXCLUDE_DIRS and any(d.rglob("*.md"))
    )

    for subdir in subdirs:
        dir_name = subdir.name
        planar_slug = dir_name  # ej. "jira-issues"

        # Si ya existe una página plana con este slug, no generar duplicado
        if planar_slug in slug_map:
            continue

        # Recoger páginas hijas (slug relativo al wiki)
        child_slugs: list[str] = []
        for md_file in sorted(subdir.rglob("*.md")):
            if any(part in EXCLUDE_DIRS for part in md_file.relative_to(wiki_dir_abs).parts):
                continue
            rel = md_file.relative_to(wiki_dir_abs)
            child_slug = str(rel.with_suffix(""))
            child_slugs.append(child_slug)

        if not child_slugs:
            continue

        # Título legible
        title = dir_name.replace("-", " ").replace("_", " ").title()

        lines = [
            f"# {title}",
            "",
            f"Páginas en la sección **{dir_name}**:",
            "",
        ]
        for slug in child_slugs:
            lines.append(f"- [[{slug}]]")

        lines.append("")

        # Escribir directamente en el directorio temporal NO es posible aquí
        # porque aún no existe. El llamador (prepare_files) lo hace.
        # Devolvemos el contenido para que prepare_files lo escriba.
        generated.append(planar_slug)

    return generated


def extract_page_title(md_file: Path) -> str:
    """Extrae el título de una página: frontmatter, H1 o nombre de archivo."""
    content = md_file.read_text(encoding="utf-8")

    title_match = re.search(r"^title:\s*(.+?)\s*$", content, re.MULTILINE)
    if title_match:
        return title_match.group(1).strip().strip('"\'')

    heading_match = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
    if heading_match:
        return heading_match.group(1).strip()

    return md_file.stem.replace("-", " ").replace("_", " ").title()


def natural_sort_key(value: str) -> list[str | int]:
    """Ordena texto con números de forma natural: 1, 2, 3, 10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def escape_wikilink_display(title: str) -> str:
    """Escapa caracteres que rompen el texto visible de un wikilink Gollum."""
    return (
        title.replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("|", "&#124;")
    )


def generate_directory_index_content(dir_name: str, slug_map: dict[str, str]) -> str:
    """
    Devuelve el contenido Markdown de la página índice plana para un
    subdirectorio dado. Usado por prepare_files para escribir <dir>.md
    en la raíz del directorio temporal.
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
        f"{len(child_pages)} páginas en la sección **{dir_name}**:",
        "",
    ]
    for slug, display_title in child_pages:
        lines.append(f"- [[{display_title}|{slug}]]")
    lines.append("")
    return "\n".join(lines)


def generate_sidebar(slug_map: dict[str, str]) -> str:
    """
    Genera _sidebar.md — barra lateral de navegación para GitLab Wiki.
    Organizada por secciones.
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
        f"- {link('home', 'Inicio')}",
        f"- {link('index', 'Índice')}",
        f"- {link('overview', 'Overview')}",
        f"- {link('hot', 'Contexto reciente')}",
        f"- {link('log', 'Operation Log')}",
        "",
        "### Fuentes",
        "",
    ]

    sources = sorted(k for k in slug_map if k.startswith("sources/"))
    for s in sources:
        rel = Path(slug_map[s]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        # Extraer título legible del nombre del archivo
        display = rel.stem.replace("-", " ").replace("_", " ").title()
        lines.append(f"- [[{display}|{slug}]]")

    lines += [
        "",
        "### Conceptos",
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
        "### Entidades",
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
        "### Infraestructura",
        "",
    ]

    infra = sorted(k for k in slug_map if k.startswith("infrastructure/"))
    for i in infra:
        rel = Path(slug_map[i]).relative_to(WIKI_DIR.resolve())
        slug = str(rel.with_suffix(""))
        lines.append(f"- [[{slug}]]")

    return "\n".join(lines)


# ─── Preparación de archivos en directorio temporal ────────────────────────


def prepare_files(
    wiki_dir: Path,
    slug_map: dict[str, str],
    dry_run: bool = False,
) -> tuple[Path, list[str]]:
    """
    Prepara todos los archivos del wiki en un directorio temporal:
    1. Copia archivos preservando subdirectorios
    2. Convierte frontmatter y wikilinks
    3. Genera home.md y _sidebar.md

    Devuelve: (ruta al directorio temporal, lista de broken links global)
    """
    all_broken_links: list[str] = []
    processed_count = 0

    # Crear directorio temporal
    tmp_dir = Path(tempfile.mkdtemp(prefix="kb-wiki-"))
    info(f"Directorio temporal: {tmp_dir}")

    # Copiar y procesar archivos
    for md_file in sorted(wiki_dir.rglob("*.md")):
        rel = md_file.relative_to(wiki_dir)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue

        # Crear subdirectorios en el destino
        dest_dir = tmp_dir / rel.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = tmp_dir / rel

        # Procesar el archivo
        content, broken = process_file(md_file, slug_map)
        all_broken_links.extend(broken)
        dest_file.write_text(content, encoding="utf-8")
        processed_count += 1

    # Generar home.md
    home_content = generate_home(slug_map)
    (tmp_dir / "home.md").write_text(home_content, encoding="utf-8")
    ok(f"Generado home.md")

    # Generar _sidebar.md
    sidebar_content = generate_sidebar(slug_map)
    (tmp_dir / "_sidebar.md").write_text(sidebar_content, encoding="utf-8")
    ok(f"Generado _sidebar.md")

    # Generar páginas índice planas para subdirectorios sin landing
    index_dirs = generate_directory_indexes(slug_map)
    for dir_name in index_dirs:
        index_content = generate_directory_index_content(dir_name, slug_map)
        (tmp_dir / f"{dir_name}.md").write_text(index_content, encoding="utf-8")
        ok(f"Generado {dir_name}.md (índice de sección)")

    generated_count = 2 + len(index_dirs)  # home + sidebar + índices
    info(f"Procesados {processed_count} archivos + {generated_count} páginas generadas")
    return tmp_dir, all_broken_links


# ─── Despliegue Git ─────────────────────────────────────────────────────────


def deploy_to_gitlab(
    tmp_dir: Path,
    dry_run: bool = False,
    no_push: bool = False,
) -> None:
    """
    Clona el repositorio wiki de GitLab, reemplaza el contenido,
    hace commit y push.
    """
    if dry_run:
        info("Modo dry-run — no se clonará ni hará push")
        return

    # Clonar el repositorio wiki
    clone_dir = Path(tempfile.mkdtemp(prefix="kb-wiki-clone-"))
    info(f"Clonando wiki repo en {clone_dir}...")

    result = run_cmd(
        ["git", "clone", WIKI_REPO, str(clone_dir)],
    )
    if result.returncode != 0:
        error("No se pudo clonar el repositorio wiki")
        # Limpiar
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(clone_dir, ignore_errors=True)
        sys.exit(1)

    # Limpiar contenido existente (excepto .git)
    for item in clone_dir.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink()

    # Copiar archivos preparados
    info("Copiando archivos preparados...")
    for item in tmp_dir.iterdir():
        if item.is_dir():
            shutil.copytree(item, clone_dir / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, clone_dir / item.name)

    # Hacer commit
    commit_msg = f"deploy: wiki update {datetime.now().strftime('%Y-%m-%d')}"
    info(f"Commit: {commit_msg}")

    run_cmd(["git", "add", "-A"], cwd=str(clone_dir))
    run_cmd(["git", "commit", "-m", commit_msg], cwd=str(clone_dir))

    if no_push:
        info("Modo --no-push — commit realizado pero sin push")
        info(f"Directorio para inspección: {clone_dir}")
        info(f"Para hacer push manualmente: cd {clone_dir} && git push origin {WIKI_BRANCH}")
    else:
        info("Haciendo push...")
        result = run_cmd(
            ["git", "push", "origin", WIKI_BRANCH],
            cwd=str(clone_dir),
        )
        if result.returncode == 0:
            ok("Push completado exitosamente")
        else:
            error("Push falló")
            info(f"Puedes hacer push manualmente desde: {clone_dir}")

        # Limpiar directorio de clon
        shutil.rmtree(clone_dir, ignore_errors=True)

    # Limpiar directorio temporal de preparación
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Resumen ────────────────────────────────────────────────────────────────


def print_summary(
    slug_map: dict[str, str],
    broken_links: list[str],
    dry_run: bool,
    no_push: bool,
) -> None:
    """Imprime un resumen del despliegue."""
    # Contar páginas únicas por ruta de archivo
    # slug_map tiene entradas duplicadas (bare name + full path apuntan al mismo archivo)
    unique_paths = set(slug_map.values())
    page_count = len(unique_paths)

    # Categorizar por directorio
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
    print("  RESUMEN DE DESPLIEGUE")
    print("=" * 60)
    print()
    print(f"  Modo: {'dry-run' if dry_run else 'no-push' if no_push else 'completo'}")
    print(f"  Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()
    print(f"  Páginas en el wiki: {page_count}")
    for cat, count in sorted(categories.items()):
        print(f"    {cat}: {count}")
    print()

    if broken_links:
        # Deduplicar
        unique_broken = sorted(set(broken_links))
        print(f"  ⚠ Enlaces rotos: {len(unique_broken)}")
        for link in unique_broken:
            print(f"    - [[{link}]]")
    else:
        print(f"  ✓ Enlaces rotos: 0")
    print()

    if dry_run:
        print(f"  Acciones que se realizarían en un despliegue real:")
        print(f"    1. Clonar {WIKI_REPO}")
        print(f"    2. Reemplazar contenido del wiki")
        print(f"    3. Commit: 'deploy: wiki update ...'")
        print(f"    4. Push a branch '{WIKI_BRANCH}'")
    print()
    print("=" * 60)


# ─── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Despliega el wiki de Obsidian en el repositorio GitLab Wiki",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepara archivos pero no clona ni hace push. Imprime resumen.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Clona y prepara pero no hace push. Deja directorio para inspección.",
    )
    args = parser.parse_args()

    dry_run = args.dry_run
    no_push = args.no_push

    print()
    print("🚀 Despliegue de Wiki a GitLab")
    print("=" * 60)
    print()

    # Validar que el directorio wiki existe
    if not WIKI_DIR.exists():
        error(f"Directorio wiki no encontrado: {WIKI_DIR}")
        sys.exit(1)

    # 1. Construir mapa de slugs
    info("Escaneando archivos del wiki...")
    slug_map = build_slug_map(WIKI_DIR)
    ok(f"Encontrados {len(slug_map)} archivos .md")

    # 2. Preparar archivos en directorio temporal
    info("Preparando archivos...")
    tmp_dir, broken_links = prepare_files(WIKI_DIR, slug_map, dry_run)

    # 3. Desplegar a GitLab
    deploy_to_gitlab(tmp_dir, dry_run, no_push)

    # 4. Imprimir resumen
    print_summary(slug_map, broken_links, dry_run, no_push)


if __name__ == "__main__":
    main()
