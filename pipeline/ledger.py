"""The dedup ledger -- every idea ever generated, and its embedding.

Source of truth is `ideas/*.json` on disk, NOT the `idea` table. The table is a
cache that has already been lost once (a corpus rebuild dropped `signal.db` and
left 4 files against 1 row). A dedup ledger that forgets is worse than none: it
would confidently re-ship an idea it had already sent.

This is the *backstop*, not the primary defense. Repetition is prevented mainly
by theme exclusion in `ideate.top_theme`, which is exact: an idea records the
content-hash of the theme it came from, and that theme is never used again.
Embedding similarity catches the residual case where two different themes
produce the same project. It is fuzzy by nature -- a sufficiently reworded
version of the same idea can score around 0.70 -- so it is deliberately the
second line rather than the first.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct

import numpy as np

from .config import IDEAS_DIR, env

# Similarity above which two ideas are the same idea. Cosine over MiniLM
# embeddings of title+one_liner+problem.
#
# Measured against real generations rather than guessed. The plan's initial 0.85
# let a genuine duplicate through: "torn -- a crash-point explorer for SQLite"
# and "Crash-state enumerator for SQLite" scored 0.830 and are the same project.
# Across all pairs, genuinely distinct ideas topped out at 0.605 (two different
# k8s projects), so there is a wide empty band between "related" and "identical".
# 0.75 sits in the middle of it.
DUPLICATE_AT = float(env("DEDUP_THRESHOLD", "0.75"))


def idea_text(idea: dict) -> str:
    """What we compare on. Deliberately the *problem framing*, not the whole
    body -- two ideas attacking the same pain with different implementations
    are duplicates for this purpose, and milestone text would dilute that."""
    return "\n".join([
        idea.get("title", ""),
        idea.get("one_liner", ""),
        idea.get("problem", ""),
    ]).strip()


def _pack(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec.astype(np.float32))


def _unpack(blob: bytes) -> np.ndarray:
    return np.array(struct.unpack(f"{len(blob) // 4}f", blob), dtype=np.float32)


def load_from_disk() -> list[dict]:
    """Every idea ever written, read from the JSON files."""
    out = []
    if not IDEAS_DIR.is_dir():
        return out
    for path in sorted(IDEAS_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def sync(conn: sqlite3.Connection, embed_fn) -> int:
    """Ensure every idea on disk has an embedding row. Returns count added.

    `embed_fn` is injected so callers that only need lookups do not pay for
    importing torch.
    """
    have = {r["text_hash"] for r in conn.execute("SELECT text_hash FROM idea_vec")}

    pending, texts = [], []
    for idea in load_from_disk():
        text = idea_text(idea)
        if not text:
            continue
        # NOT builtin hash(): it is salted per process, so the same idea would
        # get a new key every run and the ledger would never match anything.
        key = hashlib.sha1(text.encode()).hexdigest()
        if key in have:
            continue
        have.add(key)
        pending.append((key, idea.get("title", "")))
        texts.append(text)

    if not texts:
        return 0

    vectors = embed_fn(texts)
    conn.executemany(
        "INSERT OR IGNORE INTO idea_vec (text_hash, title, vec) VALUES (?, ?, ?)",
        [(k, t, _pack(v)) for (k, t), v in zip(pending, vectors)],
    )
    return len(texts)


def nearest(conn: sqlite3.Connection, vec: np.ndarray) -> tuple[float, str]:
    """Closest past idea to this vector. Returns (similarity, title)."""
    rows = conn.execute("SELECT title, vec FROM idea_vec").fetchall()
    if not rows:
        return 0.0, ""

    # Skip rows of a different dimension rather than letting np.vstack raise.
    # Changing the embedding model would otherwise poison the ledger and take
    # down the whole run from inside the dedup gate.
    dim = len(vec)
    usable = [(r["title"], _unpack(r["vec"])) for r in rows]
    usable = [(t, v) for t, v in usable if len(v) == dim]
    if not usable:
        return 0.0, ""

    matrix = np.vstack([v for _, v in usable])
    # embeddings are L2-normalised at encode time, so a dot product is cosine
    sims = matrix @ vec.astype(np.float32)
    best = int(np.argmax(sims))
    return float(sims[best]), usable[best][0]
