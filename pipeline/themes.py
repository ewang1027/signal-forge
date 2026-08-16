"""Stage 2 -- cluster the corpus into pain themes and score their evidence.

This is the ranking layer, and the one place where getting the definition wrong
quietly ruins everything downstream.

Evidence density is NOT "how many rows are in this cluster". Twelve comments by
one person in one thread is one person's opinion; three reports from three
sources by three authors is a real pattern. So the score counts *independent*
voices, weights them by recency, and multiplies by how many distinct sources
corroborate -- which is also why we never rank by an LLM's novelty judgement,
since that measure has been shown to anti-correlate with what actually matters.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from collections import defaultdict
from functools import lru_cache

import numpy as np
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer

from .config import env
from .db import connect
from .ledger import pack_vec, unpack_vec

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
# Cosine distance. Swept against the real corpus: 0.65+ collapses into
# field-level topics ("everything about Rust", n=41), 0.35 is so tight almost
# nothing corroborates. 0.45 gives clusters that read as one specific recurring
# complaint, which is what a theme is supposed to be.
DISTANCE_THRESHOLD = float(env("THEME_DISTANCE", "0.45"))
MIN_CLUSTER_SIZE = 2
HALF_LIFE_DAYS = 180.0      # a complaint from 2 years ago is weaker evidence than one from today
LABEL_TERMS = 5


@lru_cache(maxsize=1)
def _model(name: str = MODEL_NAME):
    """The encoder, loaded once per process.

    Constructing a SentenceTransformer re-reads ~90 MB of weights every time.
    An ideas run embeds repeatedly -- the ledger sync, then one dedup check per
    candidate, per slice -- and was paying that load on each of them, which cost
    more than the encoding itself.
    """
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name)


def embed(texts: list[str]) -> np.ndarray:
    """Local embeddings. Imported lazily so the daily run never pays for torch."""
    return _model().encode(texts, normalize_embeddings=True,
                           show_progress_bar=False, batch_size=32)


def signal_text(row) -> str:
    """Exactly what gets embedded for one signal.

    Shared with the vector cache on purpose: the cached vector is keyed by a
    hash of this string, so changing what goes into it invalidates the cache
    instead of quietly reusing vectors of text that is no longer current.
    """
    return f"{row['title']}\n{row['text'][:1200]}"


def cache_key(text: str) -> str:
    """Cache identity for one embedded string.

    The model name is part of the hash so that swapping encoders invalidates
    every entry, rather than quietly mixing vectors from two models into one
    clustering run -- where the result would not be an error, just meaningless
    clusters.
    """
    return hashlib.sha1(f"{MODEL_NAME}\n{text}".encode()).hexdigest()


def embed_signals(conn: sqlite3.Connection, rows: list[dict],
                  embed_fn=embed) -> np.ndarray:
    """Vectors for `rows`, in row order, encoding only what is not already cached.

    Clustering is rebuilt wholesale every run, but the *input* to it barely
    changes: a signal's text is fixed at harvest, so all but the last week's
    rows were embedded identically last time. Encoding is ~65ms/row, which made
    the rebuild scale with the size of the corpus instead of with the size of
    the harvest.
    """
    texts = [signal_text(r) for r in rows]
    hashes = [cache_key(t) for t in texts]

    cached = {(r["signal_id"], r["text_hash"]): r["vec"] for r in
              conn.execute("SELECT signal_id, text_hash, vec FROM signal_vec")}

    # A vector of the wrong width is a truncated or corrupt blob. Treat it as a
    # miss rather than letting np.vstack raise halfway through an unattended run.
    dim: int | None = None
    vectors: list[np.ndarray | None] = []
    for row, key in zip(rows, hashes):
        vec = cached.get((row["id"], key))
        vec = unpack_vec(vec) if vec is not None else None
        if vec is not None:
            dim = dim if dim is not None else len(vec)
            vec = vec if len(vec) == dim else None
        vectors.append(vec)

    todo = [i for i, v in enumerate(vectors) if v is None]
    if todo:
        fresh = embed_fn([texts[i] for i in todo])
        if dim is not None and len(fresh[0]) != dim:
            # Cached vectors are a different width than what the encoder now
            # returns, despite claiming the same model. Trust the encoder.
            print("cached vectors have the wrong width; re-encoding the corpus")
            todo = list(range(len(texts)))
            fresh = embed_fn(texts)
        for i, vec in zip(todo, fresh):
            vectors[i] = np.asarray(vec, dtype=np.float32)
        conn.executemany(
            "INSERT OR REPLACE INTO signal_vec (signal_id, text_hash, vec) "
            "VALUES (?, ?, ?)",
            [(rows[i]["id"], hashes[i], pack_vec(vectors[i])) for i in todo],
        )

    # Rows dropped from the corpus would otherwise keep their vectors forever.
    conn.execute("DELETE FROM signal_vec WHERE signal_id NOT IN (SELECT id FROM signal)")

    print(f"embedded {len(todo)} new, reused {len(texts) - len(todo)} cached")
    return np.vstack(vectors)


def cluster(vectors: np.ndarray) -> np.ndarray:
    if len(vectors) < 2:
        return np.zeros(len(vectors), dtype=int)
    model = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=DISTANCE_THRESHOLD,
        metric="cosine",
        linkage="average",
    )
    return model.fit_predict(vectors)


def label_clusters(texts: list[str], labels: np.ndarray) -> dict[int, str]:
    """Top TF-IDF terms per cluster.

    Deliberately not an LLM call: labels are for human scanning, ideation reads
    the raw evidence, and a weekly LLM pass here would buy nothing.
    """
    out: dict[int, str] = {}
    by_cluster: dict[int, list[str]] = defaultdict(list)
    for text, lab in zip(texts, labels):
        by_cluster[int(lab)].append(text)

    joined = {c: " ".join(t) for c, t in by_cluster.items()}
    if not joined:
        return out

    keys = list(joined)
    try:
        vec = TfidfVectorizer(max_features=4000, stop_words="english",
                              ngram_range=(1, 2), min_df=1)
        matrix = vec.fit_transform([joined[k] for k in keys])
        vocab = np.array(vec.get_feature_names_out())
    except ValueError:
        return {k: "(unlabelled)" for k in keys}

    for row, key in enumerate(keys):
        scores = matrix[row].toarray().ravel()
        top = vocab[np.argsort(scores)[::-1][:LABEL_TERMS]]
        out[key] = ", ".join(top)
    return out


def theme_key(signal_ids: list[int]) -> str:
    """Stable identity for a cluster, derived from its members.

    Never use the rowid for this: `DELETE FROM theme` resets SQLite's rowid
    counter, so id=7 names a different cluster after every rebuild.
    """
    joined = ",".join(str(i) for i in sorted(signal_ids))
    return hashlib.sha1(joined.encode()).hexdigest()[:16]


def evidence_score(rows: list[dict], now: int) -> float:
    """Recency-weighted count of independent voices, scaled by source diversity.

    Independence is (source, author): the same person posting five times about
    their pet peeve counts once, not five times. `max` per voice bounds any
    single person at 1.0 and defeats thread-flooding.

    The diversity bonus is deliberately computed over *weighted* voices rather
    than a raw source count. Counting distinct sources lets a single maximally-
    stale row buy the full multiplier -- measured on real data, one row the
    decay valued at 0.05 was contributing 35% of the top theme's score. A source
    that contributes 5% of the evidence should buy 5% of the diversity credit.
    """
    seen: dict[tuple[str, str], float] = {}
    per_source: dict[str, float] = {}

    for r in rows:
        created = r["created_utc"]
        if not created:
            # Missing timestamp must not read as "posted today". Dropping the
            # row is safer than granting it maximum recency.
            continue
        author = r["author"]
        # All anonymous rows from one source collapse to a single voice --
        # otherwise anonymity multiplies a voice instead of just hiding it.
        key = (r["source"], author) if author else (r["source"], "\x00anon")
        age_days = max(0.0, (now - created) / 86400.0)
        weight = math.exp(-age_days / HALF_LIFE_DAYS)
        seen[key] = max(seen.get(key, 0.0), weight)
        per_source[r["source"]] = max(per_source.get(r["source"], 0.0), weight)

    if not seen:
        return 0.0

    voices = sum(seen.values())
    if len(per_source) < 2:
        return voices

    strongest = max(per_source.values())
    corroboration = (sum(per_source.values()) - strongest) / strongest
    return voices * (1.0 + 0.5 * min(corroboration, float(len(per_source) - 1)))


def build() -> list[dict]:
    now = int(time.time())
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, source, author, title, text, domain, created_utc, pain, weight "
            "FROM signal"
        )]

        if len(rows) < MIN_CLUSTER_SIZE:
            print("corpus too small to cluster")
            return []

        print(f"clustering {len(rows)} items...")
        texts = [signal_text(r) for r in rows]
        vectors = embed_signals(conn, rows)

        labels = cluster(vectors)
        names = label_clusters(texts, labels)

        by_cluster: dict[int, list[dict]] = defaultdict(list)
        for row, lab in zip(rows, labels):
            by_cluster[int(lab)].append(row)

        conn.execute("DELETE FROM theme_member")
        conn.execute("DELETE FROM theme")

        built = []
        for cid, members in by_cluster.items():
            if len(members) < MIN_CLUSTER_SIZE:
                continue
            score = evidence_score(members, now)
            # a theme's domain is the plurality vote of its members
            domain_counts: dict[str, int] = defaultdict(int)
            for m in members:
                if m["domain"]:
                    domain_counts[m["domain"]] += 1
            domain = max(domain_counts, key=domain_counts.get) if domain_counts else None

            key = theme_key([m["id"] for m in members])
            # Derived from members, not defaulted to 1.0. Inserting without it
            # silently erased every feedback adjustment on each rebuild, which
            # made `ORDER BY evidence * weight` just `evidence`.
            weight = sum(m.get("weight") or 1.0 for m in members) / len(members)
            cur = conn.execute(
                "INSERT INTO theme (key, label, size, evidence, domain, built_utc, weight) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, names.get(cid, ""), len(members), score, domain, now, weight),
            )
            theme_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO theme_member (theme_id, signal_id) VALUES (?, ?)",
                [(theme_id, m["id"]) for m in members],
            )
            built.append({"id": theme_id, "key": key, "label": names.get(cid, ""),
                          "size": len(members), "evidence": score, "domain": domain})

    built.sort(key=lambda t: t["evidence"], reverse=True)
    return built


def main() -> int:
    themes = build()
    print(f"\n{len(themes)} themes\n")
    for t in themes[:15]:
        print(f"  {t['evidence']:5.2f}  n={t['size']:<3} [{t['domain'] or '-':13}] {t['label'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
