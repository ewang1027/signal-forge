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
    -- Feedback multiplier. Lives on the SIGNAL, not the theme, because themes
    -- are rebuilt with new identities every run and would lose it -- the same
    -- reason exclusion keys off signal ids. A theme's weight is derived as the
    -- mean of its members'.
    weight        REAL DEFAULT 1.0,
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
    reply_id    TEXT,               -- short handle printed in the digest, typed back to grade it
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


-- FSRS review state, one row per card. The card *content* lives in cards/*.json
-- in the public repo (it is authored curriculum, not personal); this table is
-- the record of what Ethan keeps getting wrong, which is not.
CREATE TABLE IF NOT EXISTS review (
    deck     TEXT NOT NULL,
    card_id  TEXT NOT NULL,
    state    TEXT NOT NULL,          -- serialized fsrs.Card
    due_utc  INTEGER NOT NULL,
    reps     INTEGER DEFAULT 0,
    -- Two different counters on purpose.
    -- `lapses` keeps strict FSRS semantics: Again on a card already in Review.
    -- `fails` counts every Again. A card failed from the very first showing
    -- never graduates out of Learning, so its `lapses` stays 0 forever -- using
    -- it for weak-area targeting would make the worst-known card invisible.
    lapses   INTEGER DEFAULT 0,
    fails    INTEGER DEFAULT 0,      -- weak-area signal; drives what resurfaces
    last_utc INTEGER,
    PRIMARY KEY (deck, card_id)
);

-- Every inbound reply we have processed. `message_id` makes replay harmless:
-- IMAP will hand back the same message if a run dies before flagging it.
CREATE TABLE IF NOT EXISTS feedback (
    message_id  TEXT PRIMARY KEY,
    received_utc INTEGER NOT NULL,
    body        TEXT,
    applied     TEXT            -- what we did, for auditing a misparse
);

-- Append-only history of every grade. FSRS needs this for two things that
-- cannot be reconstructed after the fact: `Scheduler.reschedule_card` (re-derive
-- a schedule when intensity changes, otherwise cards keep their capped due
-- dates forever) and `Optimizer` (fit parameters to this person rather than to
-- the population). Cheap to keep, impossible to backfill.
CREATE TABLE IF NOT EXISTS review_log (
    id         INTEGER PRIMARY KEY,
    deck       TEXT NOT NULL,
    card_id    TEXT NOT NULL,
    rating     INTEGER NOT NULL,
    state_before TEXT,
    review_utc INTEGER NOT NULL
);

-- Problems already handed out, so a pattern's rotating set actually rotates
-- instead of replaying the same question until it is memorized.
CREATE TABLE IF NOT EXISTS problem_log (
    card_id   TEXT NOT NULL,
    problem   TEXT NOT NULL,
    given_utc INTEGER NOT NULL,
    PRIMARY KEY (card_id, problem)
);

-- Which harvested rows an idea was actually built from.
--
-- This, not the theme key, is what prevents repeats. A theme's identity is
-- inherently unstable: its key is a hash of its members, and members join as
-- the corpus grows -- measured, 3 new rows invalidated 2 of 25 theme keys, so
-- over weeks every theme silently becomes "new" again and eligible for reuse.
-- The durable question is "have I already written about this evidence?", and
-- signal ids are stable forever.
CREATE TABLE IF NOT EXISTS idea_signal (
    idea_id   INTEGER NOT NULL,
    signal_id INTEGER NOT NULL,
    PRIMARY KEY (idea_id, signal_id)
);

-- Embeddings of every idea ever generated, for the dedup gate. Keyed by a
-- stable content hash so it can be rebuilt from ideas/*.json, which is the
-- real ledger -- this table is only a cache.
CREATE TABLE IF NOT EXISTS idea_vec (
    text_hash TEXT PRIMARY KEY,
    title     TEXT,
    vec       BLOB NOT NULL
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
        "weight": "REAL DEFAULT 1.0",
    },
    "theme": {
        "key": "TEXT",
        "weight": "REAL DEFAULT 1.0",
    },
    "review": {
        "fails": "INTEGER DEFAULT 0",
    },
    "idea": {
        "theme_id": "INTEGER",
        "theme_key": "TEXT",
        "reply_id": "TEXT",
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
CREATE INDEX IF NOT EXISTS idx_theme_key        ON theme(key);
CREATE INDEX IF NOT EXISTS idx_theme_member_sig ON theme_member(signal_id);
CREATE INDEX IF NOT EXISTS idx_idea_signal_sig  ON idea_signal(signal_id);
CREATE INDEX IF NOT EXISTS idx_review_due        ON review(due_utc);
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
