#!/usr/bin/env python3
"""wiki_semsearch.py — local hybrid (BM25 + vector + RRF) search over the wiki vault.

Subcommands:
  build          index wiki/**/*.md into .vault-meta/sem/index.db (incremental)
  query TEXT     search the index; --mode hybrid|bm25|vector, --top N, --json
  status         index stats and stale pages

Fully local: BM25 via sqlite FTS5 (own inverted-index fallback when FTS5 is
unavailable), embeddings via the local Ollama /api/embed endpoint.

Environment overrides: WIKISEM_ENDPOINT (base URL, default
http://127.0.0.1:11434), WIKISEM_MODEL (embedding model, default bge-m3),
WIKISEM_DB (index path), WIKISEM_NO_FTS5=1 (force the fallback inverted index).
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
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


VAULT_ROOT = find_vault_root()
WIKI_DIR = VAULT_ROOT / "wiki"
DEFAULT_DB = VAULT_ROOT / ".vault-meta" / "sem" / "index.db"
DEFAULT_MODEL = os.environ.get("WIKISEM_MODEL", "bge-m3")
OLLAMA_URL = os.environ.get("WIKISEM_ENDPOINT", "http://127.0.0.1:11434")
DB_PATH = Path(os.environ.get("WIKISEM_DB", str(DEFAULT_DB)))

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
    if os.environ.get("WIKISEM_NO_FTS5") == "1":
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
    if not WIKI_DIR.is_dir():
        print(f"error: wiki directory not found: {WIKI_DIR}", file=sys.stderr)
        return 1
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

    files = sorted(p for p in WIKI_DIR.rglob("*.md") if p.is_file())
    current = {p.relative_to(VAULT_ROOT).as_posix(): p for p in files}
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
    return 0


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
    effective = args.mode
    bm25_list, vec_list = [], []
    if args.mode in ("hybrid", "bm25"):
        bm25_list = bm25_rank(conn, backend, qtext, FUSION_DEPTH)
    if args.mode in ("hybrid", "vector"):
        try:
            qvec = embed_batch([qtext], model)[0]
            if dims is not None and len(qvec) != dims:
                raise RuntimeError(
                    f"query embedding dim {len(qvec)} != index dim {dims}"
                )
            vec_list = vector_rank(conn, qvec, FUSION_DEPTH)
        except RuntimeError as e:
            if args.mode == "vector":
                print(f"error: {e}", file=sys.stderr)
                return 1
            print(f"note: {e}; falling back to bm25", file=sys.stderr)
            effective = "bm25"
    if args.mode == "hybrid" and not vec_list and bm25_list:
        print("note: no vector results; falling back to bm25", file=sys.stderr)
        effective = "bm25"
    if args.mode == "hybrid" and not bm25_list and vec_list:
        print("note: no bm25 matches; falling back to vector", file=sys.stderr)
        effective = "vector"

    if effective == "bm25":
        ranked = bm25_list
    elif effective == "vector":
        ranked = vec_list
    else:
        ranked = rrf_fuse([bm25_list, vec_list])
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
    current = {
        p.relative_to(VAULT_ROOT).as_posix(): p for p in WIKI_DIR.rglob("*.md") if p.is_file()
    } if WIKI_DIR.is_dir() else {}
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
        for s in stale:
            print(f"  stale: {s['page']} ({s['reason']})")
        for p in new_pages:
            print(f"  new: {p}")
    return 0


# ---------------------------------------------------------------- cli


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="wiki_semsearch.py",
        description="Local hybrid (BM25 + vector + RRF) search over the wiki vault.",
        epilog=(
            "environment: WIKISEM_ENDPOINT (default http://127.0.0.1:11434),"
            " WIKISEM_MODEL (default bge-m3), WIKISEM_DB (index path),"
            " WIKISEM_NO_FTS5=1 (force fallback inverted index)"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="index wiki/**/*.md (incremental)")
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

    args = p.parse_args(argv)
    if args.cmd == "query":
        if not args.text.strip():
            q.error("query text must not be empty")
        if args.top < 1:
            q.error("--top must be >= 1")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "query":
        return cmd_query(args)
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
