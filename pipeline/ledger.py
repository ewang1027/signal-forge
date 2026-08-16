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
import sqlite3

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


# Float32 blobs, native byte order. `themes` stores signal vectors the same way,
# so these live here rather than there: the light half of the pipeline imports
# this module, and anything defined next to the encoder would drag sklearn and
# torch into the daily run.
def pack_vec(vec: np.ndarray) -> bytes:
    """Serialise for the BLOB column.

    `np.tobytes` rather than `struct.pack(f"{n}f", *vec)`: the struct form
    unpacked 384 floats into a Python argument tuple per vector, which is most
    of the cost of writing a cache whose entire purpose is to be cheap.
    """
    return np.ascontiguousarray(vec, dtype=np.float32).tobytes()


def unpack_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


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
        [(k, t, pack_vec(v)) for (k, t), v in zip(pending, vectors)],
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
    usable = [(r["title"], v) for r in rows
              if len(v := unpack_vec(r["vec"])) == dim]
    if not usable:
        return 0.0, ""

    matrix = np.vstack([v for _, v in usable])
    # embeddings are L2-normalised at encode time, so a dot product is cosine
    sims = matrix @ vec.astype(np.float32)
    best = int(np.argmax(sims))
    return float(sims[best]), usable[best][0]
