#!/usr/bin/env python3
"""wiki_search.py — local hybrid (BM25 + vector + RRF) search over the wiki vault.

Subcommands:
  build          index configured Markdown source directories into .vault-meta/wiki-search/index.db
                 (incremental)
  query TEXT     search the index; --mode hybrid|bm25|vector, --top N, --json
  status         index stats and stale pages
  eval GOLD.json score retrieval (hit/recall/precision/nDCG@k, MRR, latency) against a
                 human-authored gold query set; repeat --mode to compare modes

Fully local: BM25 via sqlite FTS5 (own inverted-index fallback when FTS5 is
unavailable), embeddings via the local Ollama /api/embed endpoint.

Config: the `embeddings` section of kb-config.yaml at the vault root (model,
endpoint base URL with /api/embed appended, db path relative to the root, and
`source_dirs` relative to the root). Values support ${VAR} / ${VAR:-default}
macros expanded from the environment (process env, then vault-root .env /
.env.local). Without config everything targets a local Ollama on 127.0.0.1:11434
and indexes `wiki/`. WIKI_SEM_NO_FTS5=1 forces the fallback inverted index.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

def find_vault_root() -> Path:
    """Walk up for the vault root: wiki/ AND .git (a skills dir also has wiki/)."""
    p = Path.cwd()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir() and (parent / ".git").exists():
            return parent
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return parent
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir() and (parent / ".git").exists():
            return parent
    for parent in [p] + list(p.parents):
        if (parent / "wiki").is_dir():
            return parent
    raise SystemExit("error: vault root not found — run inside a project with wiki/")

def load_env_files(root: Path) -> None:
    """Fill os.environ from <root>/.env then <root>/.env.local (later file wins;
    keys set in the process environment always win over both). Plain KEY=VALUE
    lines, optional leading 'export ', '#' comments, blank lines ignored."""
    process_keys = set(os.environ)
    for name in (".env", ".env.local"):
        path = root / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in process_keys:
                os.environ[key] = value

MACRO_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_macros(value):
    """Expand ${VAR} / ${VAR:-default} recursively in config values."""
    if isinstance(value, str):
        return MACRO_RE.sub(
            lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value
        )
    if isinstance(value, list):
        return [expand_macros(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_macros(item) for key, item in value.items()}
    return value


def load_config_section(root: Path, section: str) -> dict:
    """Read one optional section of kb-config.{yaml,yml,json} at the vault root.
    Missing file or section -> {}. ${VAR} macros expand from the environment
    (process env, then .env/.env.local loaded above). Kept inline per the
    no-shared-import rule: a skill copied on its own must keep working."""
    candidates = [root / "kb-config.yaml", root / "kb-config.yml", root / "kb-config.json"]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return {}
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
        except Exception as e:
            raise SystemExit(f"{path} is not valid YAML: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path} must contain a mapping at the top level")
    sec = cfg.get(section) or {}
    if not isinstance(sec, dict):
        raise SystemExit(f"{path}: '{section}' must be a mapping")
    return {k: expand_macros(v) for k, v in sec.items()}


def vault_path(root: Path, rel: str, key: str) -> Path:
    """Resolve a config path relative to the vault root; absolute/~/'..' are
    rejected, matching the contract in kb-config.example.yaml."""
    if os.path.isabs(rel) or rel.startswith("~") or ".." in Path(rel).parts:
        raise SystemExit(f"kb-config {key} must be relative to the vault root, got: {rel}")
    return root / rel


def source_directories(root: Path, config: dict) -> tuple[Path, ...]:
    """Return configured Markdown source directories, defaulting to `wiki/`."""
    raw_dirs = config.get("source_dirs", ["wiki"])
    if not isinstance(raw_dirs, list) or not raw_dirs:
        raise SystemExit(
            "kb-config embeddings.source_dirs must be a non-empty list of "
            "relative directories"
        )
    directories = []
    seen = set()
    for value in raw_dirs:
        if not isinstance(value, str) or not value:
            raise SystemExit(
                "kb-config embeddings.source_dirs entries must be non-empty "
                "relative directory strings"
            )
        directory = vault_path(root, value, "embeddings.source_dirs")
        relpath = directory.relative_to(root).as_posix()
        if relpath not in seen:
            directories.append(directory)
            seen.add(relpath)
    return tuple(directories)


def configured_source_dirs() -> list[str]:
    return [directory.relative_to(VAULT_ROOT).as_posix() for directory in SOURCE_DIRS]


def gitignored_paths(relpaths) -> set[str]:
    """Return paths excluded by the vault's Git ignore rules in one git call."""
    relpaths = list(relpaths)
    if not relpaths:
        return set()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(VAULT_ROOT),
                "check-ignore",
                "--no-index",
                "--stdin",
                "-z",
            ],
            input=("\0".join(relpaths) + "\0").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit(
            "error: git is required to honor .gitignore while collecting source files"
        )
    if result.returncode not in (0, 1):
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(
            f"error: unable to apply Git ignore rules at {VAULT_ROOT}: {detail}"
        )
    return {
        path
        for path in result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if path
    }


def source_files() -> dict[str, Path]:
    """Collect configured Markdown files, excluding Git metadata and ignored paths."""
    candidates = {}
    for directory in SOURCE_DIRS:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.md"):
            if not path.is_file():
                continue
            relpath = path.relative_to(VAULT_ROOT)
            if ".git" in relpath.parts:
                continue
            candidates[relpath.as_posix()] = path
    ignored = gitignored_paths(candidates)
    return {
        relpath: candidates[relpath]
        for relpath in sorted(candidates)
        if relpath not in ignored
    }


VAULT_ROOT = find_vault_root()
load_env_files(VAULT_ROOT)
_CFG = load_config_section(VAULT_ROOT, "embeddings")
SOURCE_DIRS = source_directories(VAULT_ROOT, _CFG)
_db = _CFG.get("db")
DEFAULT_DB = vault_path(VAULT_ROOT, _db, "embeddings.db") if _db else VAULT_ROOT / ".vault-meta" / "wiki-search" / "index.db"
DEFAULT_MODEL = _CFG.get("model") or "bge-m3"
OLLAMA_URL = _CFG.get("endpoint") or "http://127.0.0.1:11434"
DB_PATH = DEFAULT_DB

EMBED_BATCH = 32
EMBED_TIMEOUT = 180
EMBED_ATTEMPTS = 2

TOKEN_RE = re.compile(r"[a-z0-9à-ÿ]+")
HEADING_RE = re.compile(r"(?m)^(#{1,3} .+)$")
H1_RE = re.compile(r"(?m)^# (.+)$")
MIN_CHUNK_CHARS = 200
MAX_CHUNK_CHARS = 4000

BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60
FUSION_DEPTH = 200


# ---------------------------------------------------------------- chunking


def page_title(relpath, text):
    m = H1_RE.search(text)
    if m:
        return m.group(1).strip()
    return Path(relpath).stem


def split_sections(text, title):
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        body = text.strip()
        return [(title, body)] if body else []
    sections = []
    if matches[0].start() > 0:
        pre = text[: matches[0].start()].strip()
        if pre:
            sections.append((title, pre))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(1).strip(), text[m.end() : end].strip()))
    return sections


def hard_split(body, limit=MAX_CHUNK_CHARS):
    if len(body) <= limit:
        return [body]
    parts, rest = [], body
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def chunk_page(relpath, text):
    """Return ordered [(heading, chunk_text)] for one page."""
    title = page_title(relpath, text)
    out = []  # (heading, body) with heading excluded from body
    pending = ""  # tiny leading content merged into the next section
    for heading, body in split_sections(text, title):
        if pending and not out:
            body = f"{pending}\n\n{heading}\n{body}".strip()
            pending = ""
        piece_len = len(heading) + len(body) + 1
        if piece_len < MIN_CHUNK_CHARS and out:
            prev_h, prev_b = out[-1]
            out[-1] = (prev_h, f"{prev_b}\n\n{heading}\n{body}".strip())
        else:
            out.append((heading, body))
    if pending and out:
        prev_h, prev_b = out[-1]
        out[-1] = (prev_h, f"{prev_b}\n\n{pending}".strip())
    elif pending and not out:
        out.append((title, pending))
    chunks = []
    for heading, body in out:
        if not body:
            continue
        for i, part in enumerate(hard_split(body)):
            prefix = heading if i == 0 else f"{heading} (cont.)"
            chunks.append((heading, f"{prefix}\n{part}"))
    return chunks


# ---------------------------------------------------------------- storage


def connect_writable():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def connect_readonly():
    return sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def ensure_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pages ("
        " path TEXT PRIMARY KEY, mtime REAL NOT NULL, sha256 TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks ("
        " id INTEGER PRIMARY KEY, path TEXT NOT NULL, heading TEXT NOT NULL,"
        " seq INTEGER NOT NULL, text TEXT NOT NULL, embedding BLOB)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")


def ensure_own_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS postings ("
        " term TEXT NOT NULL, chunk_id INTEGER NOT NULL, tf INTEGER NOT NULL,"
        " PRIMARY KEY (term, chunk_id)) WITHOUT ROWID"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS doc_lens ("
        " chunk_id INTEGER PRIMARY KEY, len INTEGER NOT NULL)"
    )


def decide_backend(conn):
    if os.environ.get("WIKI_SEM_NO_FTS5") == "1":
        return "own"
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts"
            " USING fts5(text, tokenize='unicode61')"
        )
        return "fts5"
    except sqlite3.OperationalError:
        return "own"


def wipe_index(conn, backend):
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM pages")
    if backend == "fts5":
        try:
            conn.execute("DELETE FROM chunks_fts")
        except sqlite3.OperationalError:
            pass
    else:
        conn.execute("DELETE FROM postings")
        conn.execute("DELETE FROM doc_lens")
    conn.execute("DELETE FROM meta WHERE key IN ('model', 'dims', 'bm25_backend')")


def delete_page_chunks(conn, backend, relpath):
    ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE path=?", (relpath,))]
    if not ids:
        return
    qmarks = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM chunks WHERE id IN ({qmarks})", ids)
    if backend == "fts5":
        try:
            conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({qmarks})", ids)
        except sqlite3.OperationalError:
            pass
    else:
        conn.execute(f"DELETE FROM postings WHERE chunk_id IN ({qmarks})", ids)
        conn.execute(f"DELETE FROM doc_lens WHERE chunk_id IN ({qmarks})", ids)


def add_own_postings(conn, chunk_id, text):
    counts = {}
    n = 0
    for tok in TOKEN_RE.findall(text.lower()):
        counts[tok] = counts.get(tok, 0) + 1
        n += 1
    conn.executemany(
        "INSERT OR REPLACE INTO postings(term, chunk_id, tf) VALUES (?,?,?)",
        [(t, chunk_id, tf) for t, tf in counts.items()],
    )
    conn.execute("INSERT OR REPLACE INTO doc_lens(chunk_id, len) VALUES (?,?)", (chunk_id, n))


# ---------------------------------------------------------------- embeddings


def embed_batch(texts, model):
    payload = json.dumps({"model": model, "input": list(texts)}).encode("utf-8")
    last_err = None
    for _ in range(EMBED_ATTEMPTS):
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/embed",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            vecs = data["embeddings"]
            if not isinstance(vecs, list) or len(vecs) != len(texts):
                raise RuntimeError(
                    f"expected {len(texts)} embeddings, got "
                    f"{len(vecs) if isinstance(vecs, list) else type(vecs).__name__}"
                )
            return vecs
        except (urllib.error.URLError, OSError, ValueError, KeyError, RuntimeError) as e:
            last_err = e
            time.sleep(1.0)
    raise RuntimeError(f"ollama /api/embed failed after {EMBED_ATTEMPTS} attempts: {last_err}")


def pack_vec(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return struct.pack(f"<{len(vec)}f", *(x / norm for x in vec))


def unpack_vec(blob):
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# ---------------------------------------------------------------- ranking


def bm25_rank(conn, backend, qtext, limit):
    toks = list(dict.fromkeys(TOKEN_RE.findall(qtext.lower())))
    if not toks:
        return []
    if backend == "fts5":
        # AND, unlike own_bm25_rank's disjunctive scoring. Switching to OR fixes
        # empty BM25 lists for natural-language queries, but with equal-weight
        # RRF it made hybrid worse on a multilingual vault (cross-language BM25
        # noise outranks correct vector hits). Change it only together with a
        # fusion change, measured with `eval` on a real gold set.
        expr = " AND ".join(f'"{t}"' for t in toks)
        try:
            rows = conn.execute(
                f"SELECT rowid, bm25(chunks_fts, {BM25_K1}, {BM25_B}) AS s"
                " FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT ?",
                (expr, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT rowid, bm25(chunks_fts) AS s"
                " FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT ?",
                (expr, limit),
            ).fetchall()
        # FTS5 bm25() is negative-better, so ascending order is relevance order.
        return [(cid, -score) for cid, score in rows]
    return own_bm25_rank(conn, toks, limit)


def own_bm25_rank(conn, toks, limit):
    row = conn.execute("SELECT COUNT(*), COALESCE(AVG(len), 0) FROM doc_lens").fetchone()
    n_docs, avg_len = row[0], row[1] or 1.0
    if not n_docs:
        return []
    scores = {}
    for tok in toks:
        df = conn.execute("SELECT COUNT(*) FROM postings WHERE term=?", (tok,)).fetchone()[0]
        if not df:
            continue
        idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
        for chunk_id, tf, dlen in conn.execute(
            "SELECT p.chunk_id, p.tf, d.len FROM postings p"
            " JOIN doc_lens d ON d.chunk_id = p.chunk_id WHERE p.term = ?",
            (tok,),
        ):
            denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * (dlen / avg_len))
            scores[chunk_id] = scores.get(chunk_id, 0.0) + idf * tf * (BM25_K1 + 1.0) / denom
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return ranked[:limit]


def vector_rank(conn, qvec, limit):
    unit = normalize(qvec)
    scored = []
    for chunk_id, blob in conn.execute(
        "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL"
    ):
        scored.append((chunk_id, sum(a * b for a, b in zip(unit, unpack_vec(blob)))))
    scored.sort(key=lambda kv: -kv[1])
    return scored[:limit]


def rrf_fuse(lists, k=RRF_K):
    scores = {}
    for lst in lists:
        for rank, (chunk_id, _score) in enumerate(lst, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def rank_map(ranked):
    return {cid: (i, score) for i, (cid, score) in enumerate(ranked, 1)}


def snippet(text, width=240):
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:width] + ("…" if len(flat) > width else "")


# ---------------------------------------------------------------- commands


def cmd_build(args):
    missing_dirs = [directory for directory in SOURCE_DIRS if not directory.is_dir()]
    if missing_dirs:
        missing = ", ".join(
            directory.relative_to(VAULT_ROOT).as_posix() for directory in missing_dirs
        )
        print(f"error: configured source directory not found: {missing}", file=sys.stderr)
        return 1
    current = source_files()
    conn = connect_writable()
    t0 = time.perf_counter()
    ensure_schema(conn)
    backend = decide_backend(conn)
    old_backend = get_meta(conn, "bm25_backend")
    old_model = get_meta(conn, "model")
    if (old_backend and old_backend != backend) or (old_model and old_model != args.model):
        wipe_index(conn, backend)
    if backend == "own":
        ensure_own_tables(conn)

    known = {
        path: (mtime, sha)
        for path, mtime, sha in conn.execute("SELECT path, mtime, sha256 FROM pages")
    }

    stats = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    fresh = []  # (chunk_id, chunk_text) needing embeddings
    for relpath in sorted(current):
        f = current[relpath]
        mtime = f.stat().st_mtime
        prev = known.get(relpath)
        if prev and prev[0] == mtime:
            stats["unchanged"] += 1
            continue
        raw = f.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if prev and prev[1] == sha:
            conn.execute("UPDATE pages SET mtime=? WHERE path=?", (mtime, relpath))
            stats["unchanged"] += 1
            continue
        if prev:
            delete_page_chunks(conn, backend, relpath)
            stats["updated"] += 1
        else:
            stats["added"] += 1
        text = raw.decode("utf-8", errors="replace")
        for seq, (heading, chunk_text) in enumerate(chunk_page(relpath, text)):
            cur = conn.execute(
                "INSERT INTO chunks(path, heading, seq, text) VALUES (?,?,?,?)",
                (relpath, heading, seq, chunk_text),
            )
            chunk_id = cur.lastrowid
            if backend == "fts5":
                conn.execute(
                    "INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", (chunk_id, chunk_text)
                )
            else:
                add_own_postings(conn, chunk_id, chunk_text)
            fresh.append((chunk_id, chunk_text))
        conn.execute(
            "INSERT INTO pages(path, mtime, sha256) VALUES (?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, sha256=excluded.sha256",
            (relpath, mtime, sha),
        )
    for relpath in sorted(set(known) - set(current)):
        delete_page_chunks(conn, backend, relpath)
        conn.execute("DELETE FROM pages WHERE path=?", (relpath,))
        stats["removed"] += 1

    embedded, embed_failures = 0, 0
    dims = get_meta(conn, "dims")
    if isinstance(dims, str):
        dims = int(dims)
    for i in range(0, len(fresh), EMBED_BATCH):
        batch = fresh[i : i + EMBED_BATCH]
        try:
            vecs = embed_batch([t for _, t in batch], args.model)
        except RuntimeError as e:
            print(f"warning: {e}", file=sys.stderr)
            embed_failures += len(batch)
            continue
        if dims is None:
            dims = len(vecs[0])
        for (chunk_id, _), vec in zip(batch, vecs):
            if len(vec) != dims:
                raise RuntimeError(
                    f"embedding dim mismatch: got {len(vec)}, expected {dims}"
                )
            conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (pack_vec(vec), chunk_id))
            embedded += 1

    n_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    set_meta(conn, "bm25_backend", backend)
    set_meta(conn, "model", args.model)
    if dims is not None:
        set_meta(conn, "dims", dims)
    set_meta(conn, "built_at", built_at)
    set_meta(conn, "build_ms", int((time.perf_counter() - t0) * 1000))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    elapsed = time.perf_counter() - t0
    db_mb = DB_PATH.stat().st_size / (1024 * 1024)
    report = {
        "pages": n_pages,
        "chunks": n_chunks,
        "added": stats["added"],
        "updated": stats["updated"],
        "unchanged": stats["unchanged"],
        "removed": stats["removed"],
        "embedded": embedded,
        "embed_failures": embed_failures,
        "source_dirs": configured_source_dirs(),
        "model": args.model,
        "bm25_backend": backend,
        "build_seconds": round(elapsed, 2),
        "db_bytes": DB_PATH.stat().st_size,
        "built_at": built_at,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"build: {n_pages} pages ({stats['added']} added, {stats['updated']} updated,"
            f" {stats['unchanged']} unchanged, {stats['removed']} removed),"
            f" {n_chunks} chunks, {embedded} embeddings"
            f"{' (' + str(embed_failures) + ' FAILED)' if embed_failures else ''}"
            f" [model {args.model}, backend {backend}] in {elapsed:.2f}s"
        )
        print(f"db: {DB_PATH} ({db_mb:.2f} MB)")
        print(f"sources: {', '.join(configured_source_dirs())}")
    return 0


def rank_chunks(conn, backend, qtext, mode, model, dims):
    """Shared ranking core for `query` and `eval`.

    Returns (ranked, bm25_list, vec_list, effective_mode, notes). `vector` mode
    raises RuntimeError when the query cannot be embedded; `hybrid` degrades to
    the list that has results and records why in `notes`."""
    effective = mode
    notes = []
    bm25_list, vec_list = [], []
    if mode in ("hybrid", "bm25"):
        bm25_list = bm25_rank(conn, backend, qtext, FUSION_DEPTH)
    if mode in ("hybrid", "vector"):
        try:
            qvec = embed_batch([qtext], model)[0]
            if dims is not None and len(qvec) != dims:
                raise RuntimeError(
                    f"query embedding dim {len(qvec)} != index dim {dims}"
                )
            vec_list = vector_rank(conn, qvec, FUSION_DEPTH)
        except RuntimeError as e:
            if mode == "vector":
                raise
            notes.append(f"{e}; falling back to bm25")
            effective = "bm25"
    if mode == "hybrid" and not vec_list and bm25_list:
        notes.append("no vector results; falling back to bm25")
        effective = "bm25"
    if mode == "hybrid" and not bm25_list and vec_list:
        notes.append("no bm25 matches; falling back to vector")
        effective = "vector"

    if effective == "bm25":
        ranked = bm25_list
    elif effective == "vector":
        ranked = vec_list
    else:
        ranked = rrf_fuse([bm25_list, vec_list])
    return ranked, bm25_list, vec_list, effective, notes


def cmd_query(args):
    if not DB_PATH.exists():
        print(f"error: no index at {DB_PATH}; run 'build' first", file=sys.stderr)
        return 1
    conn = connect_readonly()
    backend = get_meta(conn, "bm25_backend") or "fts5"
    index_model = get_meta(conn, "model") or DEFAULT_MODEL
    dims = get_meta(conn, "dims")
    if isinstance(dims, str):
        dims = int(dims)
    model = args.model or index_model
    if args.model and args.model != index_model:
        print(
            f"warning: query model {args.model} != index model {index_model};"
            " cosine scores may be meaningless",
            file=sys.stderr,
        )

    qtext = args.text.strip()
    try:
        ranked, bm25_list, vec_list, effective, notes = rank_chunks(
            conn, backend, qtext, args.mode, model, dims
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    ranked = ranked[: args.top]

    bm25_map = rank_map(bm25_list)
    vec_map = rank_map(vec_list)
    results = []
    if ranked:
        cids = [cid for cid, _ in ranked]
        qmarks = ",".join("?" * len(cids))
        rows = {
            r[0]: r
            for r in conn.execute(
                f"SELECT id, path, heading, seq, text FROM chunks WHERE id IN ({qmarks})",
                cids,
            )
        }
        for rank, (cid, score) in enumerate(ranked, 1):
            _, path, heading, seq, text = rows[cid]
            item = {
                "rank": rank,
                "score": round(score, 6),
                "page": path,
                "heading": heading,
                "seq": seq,
                "snippet": snippet(text),
            }
            if cid in bm25_map:
                item["bm25_rank"] = bm25_map[cid][0]
                item["bm25_score"] = round(bm25_map[cid][1], 4)
            if cid in vec_map:
                item["vector_rank"] = vec_map[cid][0]
                item["cosine"] = round(vec_map[cid][1], 4)
            results.append(item)
    conn.close()

    payload = {
        "query": qtext,
        "mode": args.mode,
        "effective_mode": effective,
        "model": model,
        "top": args.top,
        "n_results": len(results),
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if not results:
            print("no results")
        for item in results:
            tags = []
            if "bm25_rank" in item:
                tags.append(f"bm25#{item['bm25_rank']}")
            if "vector_rank" in item:
                tags.append(f"vec#{item['vector_rank']}")
            tag = ("  [" + " ".join(tags) + "]") if tags else ""
            print(f"{item['rank']}. {item['page']} — {item['heading']}  (score {item['score']}){tag}")
            print(f"   {item['snippet']}")
    return 0


# ---------------------------------------------------------------- evaluation


def load_eval_dataset(path: Path):
    """Read a human-authored gold set: {name?, queries:[{id, text, relevant[],
    category?}]}. `relevant` holds vault-relative page paths, as `query`
    reports them in its `page` field."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise SystemExit(f"error: cannot read eval dataset {path}: {e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: eval dataset {path} is not valid JSON: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("queries"), list) or not data["queries"]:
        raise SystemExit("error: eval dataset must be an object with a non-empty 'queries' list")
    seen = set()
    queries = []
    for i, q in enumerate(data["queries"], 1):
        if not isinstance(q, dict):
            raise SystemExit(f"error: eval query #{i} must be an object")
        qid, text, relevant = q.get("id"), q.get("text"), q.get("relevant")
        if not isinstance(qid, str) or not qid.strip():
            raise SystemExit(f"error: eval query #{i} needs a non-empty string 'id'")
        if qid in seen:
            raise SystemExit(f"error: duplicate eval query id: {qid}")
        seen.add(qid)
        if not isinstance(text, str) or not text.strip():
            raise SystemExit(f"error: eval query {qid} needs a non-empty 'text'")
        if (
            not isinstance(relevant, list)
            or not relevant
            or not all(isinstance(p, str) and p.strip() for p in relevant)
        ):
            raise SystemExit(f"error: eval query {qid} needs a non-empty 'relevant' list of page paths")
        category = q.get("category")
        if category is not None and not isinstance(category, str):
            raise SystemExit(f"error: eval query {qid} 'category' must be a string")
        queries.append({
            "id": qid,
            "text": text.strip(),
            "relevant": list(dict.fromkeys(p.strip() for p in relevant)),
            "category": category or "uncategorized",
        })
    return str(data.get("name") or path.stem), queries


def dedupe_pages(paths, limit):
    """Collapse a chunk ranking into page order: a page ranks where its best
    chunk ranks."""
    pages, seen = [], set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            pages.append(path)
            if len(pages) >= limit:
                break
    return pages


def retrieval_metrics(retrieved, relevant, ks):
    """Binary-relevance metrics for one query over a page ranking."""
    rel = set(relevant)
    out = {"hit": {}, "recall": {}, "precision": {}, "ndcg": {}}
    for k in ks:
        found = [rank for rank, page in enumerate(retrieved[:k], 1) if page in rel]
        dcg = sum(1.0 / math.log2(rank + 1) for rank in found)
        idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(rel), k) + 1))
        out["hit"][k] = 1.0 if found else 0.0
        out["recall"][k] = len(found) / len(rel)
        out["precision"][k] = len(found) / k
        out["ndcg"][k] = dcg / idcg
    first = next((rank for rank, page in enumerate(retrieved, 1) if page in rel), None)
    out["first_relevant_rank"] = first
    out["rr"] = 1.0 / first if first else 0.0
    return out


def percentile(values, pct):
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def mean(values):
    return sum(values) / len(values) if values else 0.0


def evaluate_mode(conn, backend, model, dims, queries, mode, ks):
    """Run every gold query in one mode; returns the aggregate report."""
    max_k = max(ks)
    per_query = []
    for q in queries:
        t0 = time.perf_counter()
        ranked, _bm25, _vec, effective, notes = rank_chunks(
            conn, backend, q["text"], mode, model, dims
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        cids = [cid for cid, _ in ranked]
        paths = {}
        if cids:
            qmarks = ",".join("?" * len(cids))
            paths = dict(conn.execute(f"SELECT id, path FROM chunks WHERE id IN ({qmarks})", cids))
        retrieved = dedupe_pages((paths[cid] for cid in cids if cid in paths), max_k)
        metrics = retrieval_metrics(retrieved, q["relevant"], ks)
        per_query.append({
            "query_id": q["id"],
            "category": q["category"],
            "effective_mode": effective,
            "notes": notes,
            "latency_ms": round(latency_ms, 1),
            "retrieved": retrieved,
            "relevant": q["relevant"],
            "first_relevant_rank": metrics["first_relevant_rank"],
            "rr": round(metrics["rr"], 4),
            "_metrics": metrics,
        })

    def agg(rows, key):
        return {k: round(mean([r["_metrics"][key][k] for r in rows]), 4) for k in ks}

    by_category = {}
    for cat in sorted({r["category"] for r in per_query}):
        rows = [r for r in per_query if r["category"] == cat]
        by_category[cat] = {
            "n": len(rows),
            "mrr": round(mean([r["rr"] for r in rows]), 4),
            "recall_at_k": agg(rows, "recall"),
        }
    latencies = [r["latency_ms"] for r in per_query]
    report = {
        "mode": mode,
        "num_queries": len(per_query),
        "ks": ks,
        "hit_at_k": agg(per_query, "hit"),
        "recall_at_k": agg(per_query, "recall"),
        "precision_at_k": agg(per_query, "precision"),
        "ndcg_at_k": agg(per_query, "ndcg"),
        "mrr": round(mean([r["rr"] for r in per_query]), 4),
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "mean": round(mean(latencies), 1),
            "max": round(max(latencies), 1),
        },
        "fallback_queries": sum(1 for r in per_query if r["effective_mode"] != mode),
        "by_category": by_category,
        "per_query": [{k: v for k, v in r.items() if k != "_metrics"} for r in per_query],
    }
    return report


def cmd_eval(args):
    if not DB_PATH.exists():
        print(f"error: no index at {DB_PATH}; run 'build' first", file=sys.stderr)
        return 1
    name, queries = load_eval_dataset(Path(args.dataset))
    conn = connect_readonly()
    backend = get_meta(conn, "bm25_backend") or "fts5"
    index_model = get_meta(conn, "model") or DEFAULT_MODEL
    dims = get_meta(conn, "dims")
    if isinstance(dims, str):
        dims = int(dims)
    model = args.model or index_model

    indexed = {row[0] for row in conn.execute("SELECT path FROM pages")}
    missing = sorted({p for q in queries for p in q["relevant"] if p not in indexed})
    if missing:
        conn.close()
        print(
            "error: relevant pages not in the index (unreachable; fix the dataset"
            " or run 'build'):",
            file=sys.stderr,
        )
        for path in missing:
            print(f"  {path}", file=sys.stderr)
        return 1

    modes = list(dict.fromkeys(args.mode or ["hybrid"]))
    reports = []
    for mode in modes:
        try:
            reports.append(evaluate_mode(conn, backend, model, dims, queries, mode, args.ks))
        except RuntimeError as e:
            conn.close()
            print(f"error: {mode} mode: {e}", file=sys.stderr)
            return 1
    conn.close()

    payload = {
        "dataset": name,
        "num_queries": len(queries),
        "index_model": index_model,
        "query_model": model,
        "ks": args.ks,
        "reports": reports,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    max_k = max(args.ks)
    print(f"dataset {name}: {len(queries)} queries, index model {index_model}")
    head = ["mode"] + [f"hit@{k}" for k in args.ks] + [f"recall@{max_k}", f"ndcg@{max_k}", "MRR", "p50ms"]
    print("  ".join(f"{h:>9}" for h in head))
    for r in reports:
        cells = [r["mode"]] + [f"{r['hit_at_k'][k]:.3f}" for k in args.ks] + [
            f"{r['recall_at_k'][max_k]:.3f}",
            f"{r['ndcg_at_k'][max_k]:.3f}",
            f"{r['mrr']:.3f}",
            f"{r['latency_ms']['p50']:.0f}",
        ]
        print("  ".join(f"{c:>9}" for c in cells))
        if r["fallback_queries"]:
            print(f"  note: {r['fallback_queries']} {r['mode']} queries fell back to a single list")
    for r in reports:
        if len(r["by_category"]) > 1:
            print(f"\n{r['mode']} by category (MRR / recall@{max_k}):")
            for cat, c in r["by_category"].items():
                print(f"  {cat:<24} n={c['n']:<3} {c['mrr']:.3f} / {c['recall_at_k'][max_k]:.3f}")
    if args.per_query:
        for r in reports:
            print(f"\n{r['mode']} per query (first relevant rank; '-' = not in top {max_k}):")
            for q in r["per_query"]:
                rank = q["first_relevant_rank"] or "-"
                print(f"  {q['query_id']:<20} {rank!s:>3}  top: {', '.join(q['retrieved'][:3])}")
    return 0


def cmd_status(args):
    if not DB_PATH.exists():
        msg = f"no index at {DB_PATH}; run 'build' first"
        if args.json:
            print(json.dumps({"error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1
    conn = connect_readonly()
    n_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_embedded = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    model = get_meta(conn, "model")
    dims = get_meta(conn, "dims")
    backend = get_meta(conn, "bm25_backend")
    built_at = get_meta(conn, "built_at")

    stale = []
    known = {
        path: (mtime, sha)
        for path, mtime, sha in conn.execute("SELECT path, mtime, sha256 FROM pages")
    }
    current = source_files()
    for relpath, (mtime, sha) in sorted(known.items()):
        f = current.get(relpath)
        if f is None:
            stale.append({"page": relpath, "reason": "deleted"})
            continue
        if f.stat().st_mtime != mtime:
            if hashlib.sha256(f.read_bytes()).hexdigest() != sha:
                stale.append({"page": relpath, "reason": "modified"})
    new_pages = sorted(set(current) - set(known))
    conn.close()

    payload = {
        "pages": n_pages,
        "chunks": n_chunks,
        "embedded": n_embedded,
        "model": model,
        "dims": dims,
        "bm25_backend": backend,
        "source_dirs": configured_source_dirs(),
        "last_build": built_at,
        "stale": stale,
        "stale_count": len(stale),
        "new_pages": new_pages,
        "new_count": len(new_pages),
        "db_bytes": DB_PATH.stat().st_size,
        "db_path": str(DB_PATH),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"index: {DB_PATH} ({payload['db_bytes'] / (1024 * 1024):.2f} MB)")
        print(f"last build: {built_at}  model: {model}  dims: {dims}  backend: {backend}")
        print(f"pages: {n_pages}  chunks: {n_chunks}  embedded: {n_embedded}")
        print(f"stale: {len(stale)}  new: {len(new_pages)}")
        print(f"sources: {', '.join(configured_source_dirs())}")
        for s in stale:
            print(f"  stale: {s['page']} ({s['reason']})")
        for p in new_pages:
            print(f"  new: {p}")
    return 0


# ---------------------------------------------------------------- cli


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="wiki_search.py",
        description="Local hybrid (BM25 + vector + RRF) search over the wiki vault.",
        epilog=(
            "config: kb-config.yaml `embeddings` section (model / endpoint base URL /"
            " db path / source_dirs), ${VAR} macros expand from env incl. .env/.env.local;"
            " default http://127.0.0.1:11434. WIKI_SEM_NO_FTS5=1 forces the fallback inverted index"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="index configured Markdown sources (incremental)")
    b.add_argument("--model", default=DEFAULT_MODEL, help="embedding model (default %(default)s)")
    b.add_argument("--json", action="store_true", help="JSON build report")

    q = sub.add_parser("query", help="search the index")
    q.add_argument("text", help="query text")
    q.add_argument("--top", type=int, default=8, help="results to return (default %(default)s)")
    q.add_argument("--mode", choices=("hybrid", "bm25", "vector"), default="hybrid",
                   help="ranking mode (default %(default)s)")
    q.add_argument("--model", default=None, help="embedding model override (default: index model)")
    q.add_argument("--json", action="store_true", help="JSON results")

    s = sub.add_parser("status", help="index stats and stale pages")
    s.add_argument("--json", action="store_true", help="JSON status")

    e = sub.add_parser("eval", help="score retrieval against a human-authored gold query set")
    e.add_argument("dataset", help="gold set JSON: {name, queries: [{id, text, relevant: [page paths], category}]}")
    e.add_argument("--mode", action="append", choices=("hybrid", "bm25", "vector"),
                   help="mode to evaluate; repeat to compare modes (default hybrid)")
    e.add_argument("--k", default="1,3,5,10", help="comma-separated page cutoffs (default %(default)s)")
    e.add_argument("--model", default=None, help="embedding model override (default: index model)")
    e.add_argument("--json", action="store_true", help="JSON report incl. per-query results")
    e.add_argument("--per-query", action="store_true", help="also list per-query first relevant rank")

    args = p.parse_args(argv)
    if args.cmd == "query":
        if not args.text.strip():
            q.error("query text must not be empty")
        if args.top < 1:
            q.error("--top must be >= 1")
    if args.cmd == "eval":
        try:
            ks = sorted({int(part) for part in args.k.split(",") if part.strip()})
        except ValueError:
            e.error("--k must be comma-separated integers")
        if not ks or ks[0] < 1:
            e.error("--k needs at least one cutoff >= 1")
        args.ks = ks
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "query":
        return cmd_query(args)
    if args.cmd == "eval":
        return cmd_eval(args)
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
