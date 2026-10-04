#!/usr/bin/env python3
"""Git-aware source search with SQLite FTS5 and Ollama embeddings.

Configuration lives in kb-config.{yaml,yml,json}; derived state lives only in
.vault-meta/code-search. This script never installs tools or downloads models.
"""

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import sqlite3
except ImportError:
    sqlite3 = None

SQLITE_ERRORS = (sqlite3.Error,) if sqlite3 is not None else ()


DEFAULT_EXTENSIONS = (
    ".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".kt", ".kts", ".swift", ".sh", ".ps1", ".sql",
    ".yaml", ".yml", ".json", ".xml", ".toml",
)
CHUNK_LINES = 80
CHUNK_OVERLAP = 20
MAX_CHUNK_CHARS = 4000
EMBED_BATCH = 32
EMBED_TIMEOUT = 180
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
PREVIEW_CHARS = 400
RRF_K = 60
RANK_DEPTH = 200
MAX_TOP = 100
SCHEMA_VERSION = "1"
RESERVED_DIRS = {".git", ".vault-meta"}
MACRO_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^{}]*))?\}")
INSTALL_HINTS = {
    "git": "Install Git using your OS package manager (macOS: xcode-select --install; "
           "Debian/Ubuntu: sudo apt-get install git).",
    "sqlite_fts5": "Use a Python 3 distribution whose SQLite supports FTS5 "
                   "(SQLITE_ENABLE_FTS5), such as a supported python.org distribution.",
    "yaml": "Install optional YAML support with python3 -m pip install PyYAML, "
            "or use kb-config.json instead.",
}


class CodeSearchError(Exception):
    pass


class MissingDependency(CodeSearchError):
    def __init__(self, name):
        self.name = name
        self.install_hint = INSTALL_HINTS[name]
        super().__init__(
            f"Missing dependency: {name}. {self.install_hint} "
            "Obtain user permission before installing anything; no installation was attempted."
        )

    def record(self):
        return {"name": self.name, "install_hint": self.install_hint}


@dataclass(frozen=True)
class Config:
    root: Path
    source_dirs: tuple
    extensions: tuple
    model: str
    endpoint: str

    @property
    def index_path(self):
        return self.root / ".vault-meta" / "code-search" / "index.db"


def find_root(explicit=None):
    if explicit is not None:
        path = Path(explicit).absolute()
        if path.is_symlink() or not path.is_dir():
            raise CodeSearchError("--root must name an existing, non-symlink vault directory")
        return path.resolve()
    for start in (Path.cwd().resolve(), Path(__file__).resolve().parent):
        for parent in (start, *start.parents):
            if (parent / "wiki").is_dir() and (parent / ".git").exists():
                return parent
    raise CodeSearchError("Vault root not found; run inside a wiki/Git vault or pass --root")


def check_path(root, path):
    """Reject symlinks and non-directory ancestors, including dangling symlinks."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise CodeSearchError("Path must remain inside the vault") from None
    current = root
    for position, part in enumerate(relative.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise CodeSearchError(f"Symlink paths are not permitted: {relative.as_posix()}")
        if position < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
            raise CodeSearchError(f"Path ancestor is not a directory: {relative.as_posix()}")
    if not path.resolve().is_relative_to(root):
        raise CodeSearchError(f"Path escapes the vault: {relative.as_posix()}")


def config_file(root):
    for name in ("kb-config.yaml", "kb-config.yml", "kb-config.json"):
        path = root / name
        check_path(root, path)
        if path.exists():
            if not path.is_file():
                raise CodeSearchError(f"{name} must be a regular configuration file")
            return path
    return None


def load_env(root):
    """Process values win; .env.local overrides .env without mutating the process."""
    values = dict(os.environ)
    process_keys = set(values)
    for name in (".env", ".env.local"):
        path = root / name
        check_path(root, path)
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and key not in process_keys:
                values[key] = value
    return values


def expand_macros(value, values):
    if isinstance(value, str):
        seen = set()
        for _ in range(32):
            if not MACRO_RE.search(value):
                return value
            if value in seen:
                raise CodeSearchError("Cyclic environment macro in configuration")
            seen.add(value)
            value = MACRO_RE.sub(
                lambda match: values.get(match.group(1)) or match.group(2) or "", value
            )
        raise CodeSearchError("Environment macro nesting exceeds 32 expansions")
    if isinstance(value, list):
        return [expand_macros(item, values) for item in value]
    if isinstance(value, dict):
        return {key: expand_macros(item, values) for key, item in value.items()}
    return value


def resolve_source_dirs(root, values):
    if not isinstance(values, list) or not values:
        raise CodeSearchError("code_search.source_dirs must be a non-empty list")
    directories = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise CodeSearchError("source_dirs entries must be non-empty relative directory strings")
        relative = Path(value)
        if relative.is_absolute() or value.startswith("~") or ".." in relative.parts:
            raise CodeSearchError("source_dirs paths must be vault-relative without ~ or ..")
        if RESERVED_DIRS.intersection(relative.parts):
            raise CodeSearchError("Git metadata and .vault-meta cannot be source directories")
        directory = root / relative
        check_path(root, directory)
        if not directory.is_dir():
            raise CodeSearchError(f"Configured source directory is missing: {relative.as_posix()}")
        normalized = directory.relative_to(root).as_posix()
        if normalized not in directories:
            directories.append(normalized)
    return tuple(directories)


def load_config(root):
    path = config_file(root)
    config = {}
    if path is not None:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            try:
                config = json.loads(text)
            except ValueError:
                raise CodeSearchError(f"Invalid JSON in {path.name}") from None
        else:
            try:
                import yaml
            except ImportError:
                raise MissingDependency("yaml") from None
            try:
                config = yaml.safe_load(text)
            except yaml.YAMLError:
                raise CodeSearchError(f"Invalid YAML in {path.name}") from None
        if not isinstance(config, dict):
            raise CodeSearchError(f"{path.name} must contain a top-level mapping")
    code = config.get("code_search", {})
    embeddings = config.get("embeddings", {})
    if not isinstance(code, dict) or not isinstance(embeddings, dict):
        raise CodeSearchError("code_search and embeddings sections must be mappings")
    values = load_env(root)
    code = expand_macros(code, values)
    embeddings = expand_macros(embeddings, values)
    directories = resolve_source_dirs(root, code.get("source_dirs", ["projects"]))
    extensions = code.get("extensions", list(DEFAULT_EXTENSIONS))
    if not isinstance(extensions, list) or not extensions or any(
        not isinstance(extension, str)
        or not re.fullmatch(r"\.[A-Za-z0-9][A-Za-z0-9._+-]*", extension)
        for extension in extensions
    ):
        raise CodeSearchError("code_search.extensions must be a non-empty list of dotted suffixes")
    model = code.get("model", embeddings.get("model", "bge-m3"))
    endpoint = code.get("endpoint", embeddings.get("endpoint", "http://127.0.0.1:11434"))
    if not isinstance(model, str) or not model.strip():
        raise CodeSearchError("The embedding model must be a non-empty string")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise CodeSearchError("The embedding endpoint must be an HTTP(S) base URL")
    endpoint = endpoint.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme not in ("http", "https") or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
    ):
        raise CodeSearchError("The embedding endpoint must be an HTTP(S) base URL without credentials/query/fragment")
    return Config(root, directories, tuple(dict.fromkeys(item.lower() for item in extensions)),
                  model.strip(), endpoint)


def fts5_available():
    if sqlite3 is None:
        return False
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        return True
    except sqlite3.Error:
        return False


def yaml_available():
    try:
        import yaml
        return True
    except ImportError:
        return False


def dependency_status(root):
    path = config_file(root)
    required_yaml = path is not None and path.suffix in (".yaml", ".yml")
    statuses = {
        "git": {"available": shutil.which("git") is not None, "required": True},
        "sqlite_fts5": {"available": fts5_available(), "required": True,
                         "sqlite_version": sqlite3.sqlite_version if sqlite3 else None},
        "yaml": {"available": yaml_available(), "required": required_yaml},
    }
    missing = [
        {"name": name, "install_hint": INSTALL_HINTS[name]}
        for name, status in statuses.items() if status["required"] and not status["available"]
    ]
    return statuses, missing


def require_dependencies(root):
    _, missing = dependency_status(root)
    if missing:
        raise MissingDependency(missing[0]["name"])


def validate_storage(config):
    path = config.index_path
    check_path(config.root, path)
    for directory in (path.parent.parent, path.parent):
        if directory.exists() and not directory.is_dir():
            raise CodeSearchError("The code-search state ancestors must be directories")
    for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        check_path(config.root, candidate)
        if candidate.exists() and not candidate.is_file():
            raise CodeSearchError("The code-search database and journals must be regular files")
    return path


def doctor(root):
    statuses, missing = dependency_status(root)
    result = {"ok": False, "root": str(root), "dependencies": statuses,
              "missing_dependencies": missing, "errors": []}
    try:
        config = load_config(root)
        validate_storage(config)
        result.update({"index": str(config.index_path), "source_dirs": list(config.source_dirs),
                       "extensions": list(config.extensions), "model": config.model,
                       "endpoint": config.endpoint})
    except MissingDependency as error:
        if not any(item["name"] == error.name for item in missing):
            missing.append(error.record())
    except (CodeSearchError, OSError, UnicodeError, ValueError) as error:
        result["errors"].append(str(error))
    result["ok"] = not missing and not result["errors"]
    return result


def git_ignored(directory, names):
    if not names:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "check-ignore", "--no-index", "--stdin", "-z"],
            input=b"\0".join(os.fsencode(name) for name in names) + b"\0",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30,
        )
    except FileNotFoundError:
        raise MissingDependency("git") from None
    except subprocess.TimeoutExpired:
        raise CodeSearchError("Git ignore evaluation timed out") from None
    if result.returncode not in (0, 1):
        raise CodeSearchError("Cannot apply Git ignore rules; sources must be in a valid Git worktree")
    return {os.fsdecode(name) for name in result.stdout.split(b"\0") if name}


def skipped_counts():
    return {"binary": 0, "non_utf8": 0, "symlink": 0, "ignored_entries": 0}


def iter_source_files(config, skipped):
    """Visit selected subtrees once; Git resolves each visited directory's repository."""
    roots = tuple(Path(value) for value in config.source_dirs)
    stack = [config.root]
    while stack:
        directory = stack.pop()
        check_path(config.root, directory)
        relative = directory.relative_to(config.root)
        selected = any(relative.is_relative_to(root) for root in roots)
        with os.scandir(directory) as entries:
            entries = sorted(entries, key=lambda entry: entry.name)
        candidates = []
        for entry in entries:
            if entry.name in RESERVED_DIRS:
                continue
            relpath = relative / entry.name
            needed = selected or any(root.is_relative_to(relpath) for root in roots)
            if entry.is_symlink():
                if needed:
                    skipped["symlink"] += 1
                continue
            is_directory = entry.is_dir(follow_symlinks=False)
            if is_directory:
                if not needed:
                    continue
            elif not (selected and entry.is_file(follow_symlinks=False)
                      and entry.name.lower().endswith(config.extensions)):
                continue
            name = "./" + entry.name + ("/" if is_directory else "")
            candidates.append((name, relpath, is_directory))
        ignored = git_ignored(directory, [item[0] for item in candidates])
        children = []
        for name, relpath, is_directory in candidates:
            if name in ignored:
                skipped["ignored_entries"] += 1
            elif is_directory:
                children.append(config.root / relpath)
            else:
                yield relpath.as_posix(), config.root / relpath
        stack.extend(reversed(children))


def read_source(root, path, skipped):
    check_path(root, path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise CodeSearchError("Selected source is not a regular file")
        data = handle.read()
    if b"\0" in data:
        skipped["binary"] += 1
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        skipped["non_utf8"] += 1
        return None
    return hashlib.sha256(data).hexdigest(), text


def chunk_lines(text):
    """Bounded overlapping line windows; oversized single lines retain their line number."""
    lines = text.splitlines()
    start = 0
    while start < len(lines):
        if len(lines[start]) > MAX_CHUNK_CHARS:
            for offset in range(0, len(lines[start]), MAX_CHUNK_CHARS):
                part = lines[start][offset:offset + MAX_CHUNK_CHARS]
                if part.strip():
                    yield start + 1, start + 1, part
            start += 1
            continue
        end, length = start, 0
        while end < len(lines) and end - start < CHUNK_LINES:
            extra = len(lines[end]) + (1 if end > start else 0)
            if length + extra > MAX_CHUNK_CHARS:
                break
            length += extra
            end += 1
        body = "\n".join(lines[start:end])
        if body.strip():
            yield start + 1, end, body
        if end == len(lines):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def embed_texts(config, texts, expected_dimension=0):
    if not texts:
        return []
    request = urllib.request.Request(
        config.endpoint + "/api/embed",
        data=json.dumps({"model": config.model, "input": texts, "truncate": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=EMBED_TIMEOUT) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise CodeSearchError("Embedding response exceeds the size limit")
        data = json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        raise CodeSearchError(
            f"Embedding endpoint returned HTTP {error.code}; verify the configured service/model. "
            "No models were downloaded or services started; obtain permission before setup."
        ) from None
    except (urllib.error.URLError, OSError):
        raise CodeSearchError(
            "Embedding endpoint is unavailable; verify the configured service. "
            "No services were started; obtain permission before setup."
        ) from None
    except (UnicodeError, ValueError):
        raise CodeSearchError("Embedding endpoint returned invalid JSON") from None
    vectors = data.get("embeddings") if isinstance(data, dict) else None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise CodeSearchError("Embedding response count does not match the requested chunks")
    normalized = []
    dimension = expected_dimension
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise CodeSearchError("Embedding vectors must be non-empty numeric arrays")
        if dimension and len(vector) != dimension:
            raise CodeSearchError("Embedding dimension changed; the previous index was not modified")
        dimension = len(vector)
        try:
            valid = all(not isinstance(value, bool) and isinstance(value, (int, float))
                        and math.isfinite(value) for value in vector)
            norm = math.hypot(*vector) if valid else 0.0
        except OverflowError:
            valid, norm = False, 0.0
        if not valid:
            raise CodeSearchError("Embedding vectors must contain only finite numbers")
        if not norm or not math.isfinite(norm):
            raise CodeSearchError("Embedding vectors must have a finite, non-zero norm")
        normalized.append([value / norm for value in vector])
    return normalized


def open_index(config, writable=False):
    path = validate_storage(config)
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True)
        validate_storage(config)
        conn = sqlite3.connect(path)
    else:
        if not path.is_file():
            raise CodeSearchError("No code-search index; run build first")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def ensure_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT NOT NULL, "
        "start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, text TEXT NOT NULL, embedding BLOB NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)")


def read_meta(conn):
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise CodeSearchError("Unsupported index format; remove the derived index.db and run build")
    required = ("model", "endpoint", "source_dirs", "extensions", "chunk_lines", "chunk_overlap", "max_chunk_chars")
    if any(key not in meta for key in required):
        raise CodeSearchError("Index metadata is incomplete; remove the derived index.db and run build")
    try:
        dimensions = int(meta["dimensions"])
        if dimensions < 0:
            raise ValueError
    except (KeyError, ValueError):
        raise CodeSearchError("Invalid index dimensions; rebuild the code-search index") from None
    return meta


def settings_meta(config):
    return {
        "schema_version": SCHEMA_VERSION, "model": config.model, "endpoint": config.endpoint,
        "source_dirs": json.dumps(list(config.source_dirs)), "extensions": json.dumps(list(config.extensions)),
        "chunk_lines": str(CHUNK_LINES), "chunk_overlap": str(CHUNK_OVERLAP),
        "max_chunk_chars": str(MAX_CHUNK_CHARS),
    }


def index_counts(conn):
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunks, embedded = conn.execute(
        "SELECT COUNT(*), COUNT(embedding) FROM chunks"
    ).fetchone()
    return {"files": files, "chunks": chunks, "embedded": embedded}


def build_index(config):
    path = validate_storage(config)
    require_dependencies(config.root)
    previous, old_files = {}, {}
    if path.exists():
        with contextlib.closing(open_index(config)) as conn:
            previous = read_meta(conn)
            old_files = {
                row[0]: (row[1], row[2]) for row in conn.execute(
                    "SELECT files.path, files.sha256, COUNT(chunks.id) FROM files "
                    "LEFT JOIN chunks ON chunks.path=files.path GROUP BY files.path"
                )
            }
    expected_meta = settings_meta(config)
    vector_keys = ("model", "endpoint", "chunk_lines", "chunk_overlap", "max_chunk_chars")
    reembed = any(previous.get(key) != expected_meta[key] for key in vector_keys)
    skipped = skipped_counts()
    current, changed, pending = {}, set(), []
    for relpath, source in iter_source_files(config, skipped):
        result = read_source(config.root, source, skipped)
        if result is None:
            continue
        sha256, text = result
        current[relpath] = sha256
        if reembed or relpath not in old_files or old_files[relpath][0] != sha256:
            changed.add(relpath)
            pending.extend((relpath, start, end, body) for start, end, body in chunk_lines(text))
    removed = set(old_files) - set(current)
    reused_chunks = sum(value[1] for key, value in old_files.items() if key in current and key not in changed)
    dimensions = int(previous.get("dimensions", "0")) if not reembed else 0
    prepared = []
    for offset in range(0, len(pending), EMBED_BATCH):
        batch = pending[offset:offset + EMBED_BATCH]
        vectors = embed_texts(config, [item[3] for item in batch], expected_dimension=dimensions)
        if len(vectors) != len(batch):
            raise CodeSearchError("Embedding response count does not match the requested chunks")
        for item, vector in zip(batch, vectors):
            if not vector or (dimensions and len(vector) != dimensions):
                raise CodeSearchError("Embedding dimension mismatch; the previous index was not modified")
            dimensions = len(vector)
            prepared.append((*item, struct.pack(f"<{dimensions}f", *vector)))
    if not reused_chunks and not prepared:
        dimensions = 0
    new_database = not path.exists()
    conn = None
    try:
        conn = open_index(config, writable=True)
        conn.execute("BEGIN IMMEDIATE")
        ensure_schema(conn)
        for relpath in sorted(changed | removed):
            conn.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (relpath,))
            conn.execute("DELETE FROM chunks WHERE path=?", (relpath,))
            conn.execute("DELETE FROM files WHERE path=?", (relpath,))
        conn.executemany("INSERT INTO files(path, sha256) VALUES (?, ?)",
                         [(relpath, current[relpath]) for relpath in sorted(changed)])
        for relpath, start, end, body, vector in prepared:
            cursor = conn.execute(
                "INSERT INTO chunks(path, start_line, end_line, text, embedding) VALUES (?, ?, ?, ?, ?)",
                (relpath, start, end, body, vector),
            )
            conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (cursor.lastrowid, body))
        expected_meta["dimensions"] = str(dimensions)
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", expected_meta.items())
        counts = index_counts(conn)
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
            conn.close()
            conn = None
        if new_database and path.exists():
            path.unlink()
        raise
    finally:
        if conn is not None:
            conn.close()
    return {"ok": True, "index": str(path), **counts, "dimensions": dimensions,
            "model": config.model, "endpoint": config.endpoint,
            "source_dirs": list(config.source_dirs), "extensions": list(config.extensions),
            "updated_files": len(changed), "removed_files": len(removed),
            "reused_files": len(current) - len(changed), "reused_chunks": reused_chunks,
            "embedded_chunks": len(prepared), "skipped": skipped}


def index_status(config):
    require_dependencies(config.root)
    with contextlib.closing(open_index(config)) as conn:
        meta = read_meta(conn)
        counts = index_counts(conn)
        indexed = dict(conn.execute("SELECT path, sha256 FROM files"))
    skipped, current = skipped_counts(), {}
    for relpath, source in iter_source_files(config, skipped):
        result = read_source(config.root, source, skipped)
        if result is not None:
            current[relpath] = result[0]
    stale = sorted(path for path, sha256 in indexed.items() if current.get(path) != sha256)
    new = sorted(set(current) - set(indexed))
    configuration_stale = any(meta.get(key) != value for key, value in settings_meta(config).items())
    return {"ok": True, "index": str(config.index_path), **counts,
            "dimensions": int(meta["dimensions"]), "model": meta["model"], "endpoint": meta["endpoint"],
            "source_dirs": list(config.source_dirs), "extensions": list(config.extensions),
            "stale_paths": stale, "new_paths": new, "configuration_stale": configuration_stale,
            "needs_build": bool(stale or new or configuration_stale), "skipped": skipped}


def bm25_rank(conn, text, limit):
    tokens = list(dict.fromkeys(re.findall(r"\w+", text, flags=re.UNICODE)))
    if not tokens:
        return []
    expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    return [
        (row[0], -row[1], row[2], row[3]) for row in conn.execute(
            "SELECT chunks.id, bm25(chunks_fts), chunks.path, chunks.start_line FROM chunks_fts "
            "JOIN chunks ON chunks.id=chunks_fts.rowid WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts), chunks.path, chunks.start_line, chunks.id LIMIT ?",
            (expression, limit),
        )
    ]


def vector_rank(conn, query_vector, dimensions, limit):
    import heapq

    def scores():
        for chunk_id, path, start, blob in conn.execute("SELECT id, path, start_line, embedding FROM chunks"):
            if blob is None or len(blob) != dimensions * 4:
                raise CodeSearchError("Stored vector dimensions are inconsistent; run build")
            vector = struct.unpack(f"<{dimensions}f", blob)
            if not all(math.isfinite(value) for value in vector):
                raise CodeSearchError("Stored vectors are invalid; rebuild the code-search index")
            score = sum(left * right for left, right in zip(query_vector, vector))
            yield chunk_id, max(-1.0, min(1.0, score)), path, start

    return heapq.nsmallest(limit, scores(), key=lambda row: (-row[1], row[2], row[3], row[0]))


def query_index(config, text, mode="hybrid", top=5):
    if mode not in ("hybrid", "vector", "bm25") or not isinstance(top, int) or not 1 <= top <= MAX_TOP:
        raise CodeSearchError(f"Choose hybrid/vector/bm25 and a top count between 1 and {MAX_TOP}")
    if not isinstance(text, str) or not text.strip():
        raise CodeSearchError("Query text must be non-empty")
    require_dependencies(config.root)
    with contextlib.closing(open_index(config)) as conn:
        meta = read_meta(conn)
        dimensions = int(meta["dimensions"])
        if mode != "bm25" and (meta.get("model") != config.model or meta.get("endpoint") != config.endpoint):
            raise CodeSearchError("Index vectors use a different model/endpoint; run build before semantic queries")
        depth = max(top, RANK_DEPTH) if mode == "hybrid" else top
        lexical = bm25_rank(conn, text, depth) if mode != "vector" else []
        semantic = []
        if mode != "bm25" and conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]:
            if not dimensions:
                raise CodeSearchError("Index vectors have no dimensions; run build")
            vector = embed_texts(config, [text], expected_dimension=dimensions)[0]
            if len(vector) != dimensions:
                raise CodeSearchError("Query embedding dimensions do not match the index; run build")
            semantic = vector_rank(conn, vector, dimensions, depth)
        if mode == "hybrid":
            fused = {}
            for ranking in (lexical, semantic):
                for rank, (chunk_id, _, path, start) in enumerate(ranking, 1):
                    previous = fused.get(chunk_id, (0.0, path, start))
                    fused[chunk_id] = (previous[0] + 1.0 / (RRF_K + rank), path, start)
            ranked = [(chunk_id, *value) for chunk_id, value in fused.items()]
            ranked.sort(key=lambda row: (-row[1], row[2], row[3], row[0]))
        else:
            ranked = lexical if mode == "bm25" else semantic
        results = []
        for chunk_id, score, _, _ in ranked[:top]:
            path, start, end, body = conn.execute(
                "SELECT path, start_line, end_line, text FROM chunks WHERE id=?", (chunk_id,)
            ).fetchone()
            results.append({"path": path, "start_line": start, "end_line": end,
                            "preview": body[:PREVIEW_CHARS], "score": score})
    return {"ok": True, "mode": mode, "model": meta["model"], "results": results}


def positive_top(value):
    try:
        top = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--top must be an integer") from None
    if not 1 <= top <= MAX_TOP:
        raise argparse.ArgumentTypeError(f"--top must be between 1 and {MAX_TOP}")
    return top


def print_result(result, as_json, command):
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif command == "doctor":
        print("Dependencies/configuration ready" if result["ok"] else "Dependencies/configuration not ready")
        for item in result["missing_dependencies"]:
            print(f"{item['name']}: {item['install_hint']} Obtain user permission before installation.")
        for error in result["errors"]:
            print(error)
    elif command == "query":
        print(f"mode: {result['mode']}")
        for item in result["results"]:
            print(f"{item['path']}:{item['start_line']}-{item['end_line']}  score={item['score']:.6f}")
            print(item["preview"])
    else:
        print(f"{result['files']} files, {result['chunks']} chunks, {result['embedded']} embedded; "
              f"model={result['model']}, dimensions={result['dimensions']}")
        if command == "build":
            print(f"updated={result['updated_files']}, removed={result['removed_files']}, "
                  f"reused={result['reused_files']}, newly embedded={result['embedded_chunks']}")
        else:
            print("Run build to update the index" if result["needs_build"] else "Index is current")
            for label in ("new_paths", "stale_paths"):
                for path in result[label]:
                    print(f"{label}: {path}")
        print("skipped: " + json.dumps(result["skipped"], sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="Explicit vault root (before the subcommand)")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("doctor", "build", "status", "query"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--json", action="store_true")
        if command == "query":
            subparser.add_argument("text")
            subparser.add_argument("--mode", choices=("hybrid", "vector", "bm25"), default="hybrid")
            subparser.add_argument("--top", type=positive_top, default=5)
    args = parser.parse_args(argv)
    try:
        root = find_root(args.root)
        if args.command == "doctor":
            result = doctor(root)
        else:
            config = load_config(root)
            if args.command == "build":
                result = build_index(config)
            elif args.command == "status":
                result = index_status(config)
            else:
                result = query_index(config, args.text, args.mode, args.top)
        print_result(result, args.json, args.command)
        return 0 if result["ok"] else 2
    except (CodeSearchError, OSError, UnicodeError, ValueError, struct.error) as error:
        missing = [error.record()] if isinstance(error, MissingDependency) else []
        result = {"ok": False, "error": str(error), "missing_dependencies": missing}
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print("error: " + str(error), file=sys.stderr)
        return 2 if missing or args.command == "doctor" else 1
    except SQLITE_ERRORS as error:
        if args.json:
            print(json.dumps({"ok": False, "error": f"SQLite index error: {error}; run doctor", "missing_dependencies": []}))
        else:
            print(f"error: SQLite index error: {error}; run doctor", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
