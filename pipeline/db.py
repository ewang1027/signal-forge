"""SQLite schema and connection.

One table per concept, no ORM. The corpus is append-mostly and the queries are
simple, so the indirection would not pay for itself.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DB_PATH, ensure_state_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS signal (
    id            INTEGER PRIMARY KEY,
    source        TEXT    NOT NULL,   -- hn | github | arxiv | reddit | lobsters
    external_id   TEXT    NOT NULL,   -- source-native id, for idempotent harvest
    url           TEXT,
    title         TEXT,
    text          TEXT    NOT NULL,
    author        TEXT,
    created_utc   INTEGER NOT NULL,   -- when it was posted
    harvested_utc INTEGER NOT NULL,   -- when we pulled it
    engagement    INTEGER DEFAULT 0,  -- points / comments / reactions
    query         TEXT,               -- which probe surfaced it
    domain        TEXT,               -- strongest domain match, for slice rotation
    domain_hits   INTEGER DEFAULT 0,  -- how technical it looked
    pain          INTEGER DEFAULT 0,  -- complaint-marker count; orthogonal to domain
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_signal_created ON signal(created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_signal_source  ON signal(source);

-- Ideas ever sent. This is the dedup ledger; nothing is ever deleted from it.
CREATE TABLE IF NOT EXISTS idea (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    domain      TEXT,               -- which rotation slice produced it
    theme_id    INTEGER,
    sent_utc    INTEGER,
    verdict     TEXT,               -- feedback: more | boring | exists | too_easy | building
    verdict_utc INTEGER
);

-- Clusters of corroborating complaints. Rebuilt wholesale each run; cheap
-- enough at this corpus size and avoids incremental-clustering drift.
CREATE TABLE IF NOT EXISTS theme (
    id        INTEGER PRIMARY KEY,
    label     TEXT,
    size      INTEGER NOT NULL,
    evidence  REAL    NOT NULL,   -- recency-weighted independent voices
    domain    TEXT,
    built_utc INTEGER NOT NULL,
    weight    REAL    DEFAULT 1.0 -- adjusted by feedback; multiplies evidence
);

CREATE TABLE IF NOT EXISTS theme_member (
    theme_id  INTEGER NOT NULL,
    signal_id INTEGER NOT NULL,
    PRIMARY KEY (theme_id, signal_id)
);

CREATE INDEX IF NOT EXISTS idx_theme_evidence ON theme(evidence DESC);

-- Rotation cursor, last-run timestamps, misc counters.
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_state_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_kv(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
