"""SQLite store for one crawled site.

Deliberately not Postgres. This service holds one site at a time, embeddings
live in a numpy matrix in memory, and the database is a single file that can be
committed, copied to Railway, or thrown away. Zero shared infrastructure with
any other project.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS site (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    root_url    TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    brief       TEXT NOT NULL DEFAULT '',
    facts       TEXT NOT NULL DEFAULT '',
    greeting    TEXT NOT NULL DEFAULT '',
    crawled_at  TEXT NOT NULL,
    page_count  INTEGER NOT NULL DEFAULT 0,
    dims        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pages (
    url         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    words       INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    url         TEXT NOT NULL REFERENCES pages(url) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT '',
    heading     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    embedding   BLOB
);
CREATE INDEX IF NOT EXISTS chunks_url ON chunks(url);

CREATE TABLE IF NOT EXISTS calls (
    sid         TEXT PRIMARY KEY,
    from_number TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    turns       TEXT NOT NULL DEFAULT '[]',
    outcome     TEXT NOT NULL DEFAULT 'in_progress'
);
"""


@dataclass
class Retrieved:
    url: str
    title: str
    heading: str
    text: str
    score: float


# Columns added after the first release. CREATE TABLE IF NOT EXISTS
# leaves an existing table alone, so a database built before one of
# these was added survives connect() and then fails on the first write
# with 'no such column', which reads like a code bug rather than a
# stale file.
MIGRATIONS = [
    ('site', 'facts', "TEXT NOT NULL DEFAULT ''"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, spec in MIGRATIONS:
        existing = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
        if existing and column not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {spec}')
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def pack(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack(blob: bytes, dims: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).reshape(dims)


def load_matrix(conn: sqlite3.Connection, dims: int):
    """Return (matrix, rows). Rows are aligned with matrix row order."""
    rows = conn.execute(
        "SELECT id, url, title, heading, text, embedding FROM chunks "
        "WHERE embedding IS NOT NULL ORDER BY id"
    ).fetchall()
    if not rows:
        return np.zeros((0, dims), dtype=np.float32), []
    mat = np.vstack([unpack(r["embedding"], dims) for r in rows]).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms, rows


def site_row(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM site WHERE id = 1").fetchone()


def save_site(conn, *, root_url, name, brief, greeting, crawled_at, page_count,
              dims, facts=""):
    conn.execute("DELETE FROM site")
    conn.execute(
        "INSERT INTO site (id, root_url, name, brief, facts, greeting, crawled_at,"
        " page_count, dims) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (root_url, name, brief, facts, greeting, crawled_at, page_count, dims),
    )
    conn.commit()


def record_turn(conn, sid: str, role: str, text: str, sources: list[str] | None = None):
    row = conn.execute("SELECT turns FROM calls WHERE sid = ?", (sid,)).fetchone()
    turns = json.loads(row["turns"]) if row else []
    turns.append({"role": role, "text": text, "sources": sources or []})
    conn.execute("UPDATE calls SET turns = ? WHERE sid = ?", (json.dumps(turns), sid))
    conn.commit()
