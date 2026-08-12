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


-- Ideas ever sent. This is the dedup ledger; nothing is ever deleted from it.
CREATE TABLE IF NOT EXISTS idea (
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    domain      TEXT,               -- which rotation slice produced it
    theme_id    INTEGER,            -- rowid at generation time; NOT stable, do not join on it
    theme_key   TEXT,               -- stable content hash; this is the one to match on
    sent_utc    INTEGER,
    verdict     TEXT,               -- feedback: more | boring | exists | too_easy | building
    verdict_utc INTEGER
);

-- Clusters of corroborating complaints. Rebuilt wholesale each run; cheap
-- enough at this corpus size and avoids incremental-clustering drift.
CREATE TABLE IF NOT EXISTS theme (
    id        INTEGER PRIMARY KEY,
    -- Content-derived identity: hash of the sorted member signal_ids. The
    -- rowid CANNOT be used to identify a theme across rebuilds -- `DELETE FROM
    -- theme` resets SQLite's rowid counter to 1, so after every rebuild id=7
    -- names a completely different cluster. Anything that remembers a theme
    -- (the "already used" check in ideate) must remember this key instead.
    key       TEXT,
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


-- Rotation cursor, last-run timestamps, misc counters.
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` is a
# no-op on an existing table -- it does NOT add columns -- so without this every
# new column silently fails to appear and the next query referencing it raises
# `no such column` inside an unattended run.
MIGRATIONS: dict[str, dict[str, str]] = {
    "signal": {
        "domain": "TEXT",
        "domain_hits": "INTEGER DEFAULT 0",
        "pain": "INTEGER DEFAULT 0",
    },
    "theme": {
        "key": "TEXT",
        "weight": "REAL DEFAULT 1.0",
    },
    "idea": {
        "theme_id": "INTEGER",
        "theme_key": "TEXT",
        "verdict": "TEXT",
        "verdict_utc": "INTEGER",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if not existing:
            continue  # table not created yet; SCHEMA will have handled it
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# Indexes are applied AFTER migrations, never inside SCHEMA. An index on a
# newly-added column would otherwise run before the ALTER that creates it and
# fail with `no such column` on every existing database.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_signal_created  ON signal(created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_signal_source   ON signal(source);
CREATE INDEX IF NOT EXISTS idx_theme_evidence  ON theme(evidence DESC);
CREATE INDEX IF NOT EXISTS idx_theme_key       ON theme(key);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_state_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)   # tables only
        _migrate(conn)               # add columns missing from older DBs
        conn.executescript(INDEXES)  # now the columns are guaranteed to exist
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
