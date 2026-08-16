"""The embedding cache under the theme rebuild.

Clustering is rebuilt wholesale every run, which used to mean re-encoding every
row in the corpus every Sunday -- work that scaled with the corpus instead of
with the week's harvest. These tests pin the two things that make caching safe:
a row whose text changed must not keep its old vector, and a rowid reused by a
rebuilt corpus must not inherit the previous occupant's.
"""

import numpy as np

from pipeline.themes import embed_signals, signal_text


class FakeEncoder:
    """Deterministic stand-in for MiniLM: the vector is derived from the text,
    so a stale cache hit is visible as a vector that does not match its row."""

    def __init__(self, width=4):
        self.width = width
        self.calls: list[list[str]] = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        return np.array([[float(hash(t) % 97)] * self.width for t in texts],
                        dtype=np.float32)

    @property
    def encoded(self) -> int:
        return sum(len(c) for c in self.calls)


def _db(tmp_path, monkeypatch):
    import pipeline.config as cfg
    import pipeline.db as db
    monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    for name in ("IDEAS_DIR", "DECKS_DIR", "REJECTS_DIR"):
        monkeypatch.setattr(cfg, name, tmp_path / name.lower())
    return db


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT id, title, text FROM signal")]


def _insert(conn, rid, title, text):
    conn.execute(
        "INSERT INTO signal (id, source, external_id, title, text, "
        "created_utc, harvested_utc) VALUES (?,'hn',?,?,?,1,1)",
        (rid, str(rid), title, text))


class TestEmbeddingCache:
    def test_second_run_encodes_nothing(self, tmp_path, monkeypatch):
        db = _db(tmp_path, monkeypatch)
        enc = FakeEncoder()
        with db.connect() as conn:
            for i in (1, 2, 3):
                _insert(conn, i, f"t{i}", f"complaint {i}")
            first = embed_signals(conn, _rows(conn), enc)
            assert enc.encoded == 3

            second = embed_signals(conn, _rows(conn), enc)
            assert enc.encoded == 3, "cached rows were re-encoded"
            assert np.array_equal(first, second)

    def test_only_new_rows_are_encoded(self, tmp_path, monkeypatch):
        db = _db(tmp_path, monkeypatch)
        enc = FakeEncoder()
        with db.connect() as conn:
            _insert(conn, 1, "t1", "complaint 1")
            embed_signals(conn, _rows(conn), enc)

            _insert(conn, 2, "t2", "complaint 2")
            rows = _rows(conn)
            vectors = embed_signals(conn, rows, enc)

            assert enc.calls[-1] == [signal_text(rows[1])], \
                "a week of harvest should cost a week of encoding"
            assert len(vectors) == 2

    def test_edited_text_is_re_encoded(self, tmp_path, monkeypatch):
        db = _db(tmp_path, monkeypatch)
        enc = FakeEncoder()
        with db.connect() as conn:
            _insert(conn, 1, "t1", "the original complaint")
            before = embed_signals(conn, _rows(conn), enc)

            conn.execute("UPDATE signal SET text = 'a different complaint'")
            after = embed_signals(conn, _rows(conn), enc)

            assert enc.encoded == 2
            assert not np.array_equal(before, after)

    def test_reused_rowid_does_not_inherit_a_vector(self, tmp_path, monkeypatch):
        """A corpus rebuild drops the table and rowids start again at 1, so
        matching on the id alone would hand row 1's vector to a new row 1."""
        db = _db(tmp_path, monkeypatch)
        enc = FakeEncoder()
        with db.connect() as conn:
            _insert(conn, 1, "t1", "the first corpus")
            old = embed_signals(conn, _rows(conn), enc)

            conn.execute("DELETE FROM signal")
            _insert(conn, 1, "t1", "an entirely different corpus")
            new = embed_signals(conn, _rows(conn), enc)

            assert not np.array_equal(old, new)

    def test_a_new_model_invalidates_every_entry(self, tmp_path, monkeypatch):
        """Two models' vectors in one clustering run would not raise -- it would
        just produce meaningless clusters -- so the model names the cache."""
        import pipeline.themes as themes

        db = _db(tmp_path, monkeypatch)
        with db.connect() as conn:
            for i in (1, 2):
                _insert(conn, i, f"t{i}", f"complaint {i}")
            embed_signals(conn, _rows(conn), FakeEncoder(width=4))

            monkeypatch.setattr(themes, "MODEL_NAME", "some/other-encoder")
            wider = FakeEncoder(width=8)
            vectors = embed_signals(conn, _rows(conn), wider)

            assert vectors.shape == (2, 8)
            assert wider.encoded == 2

    def test_a_corrupt_blob_is_re_encoded_not_raised(self, tmp_path, monkeypatch):
        db = _db(tmp_path, monkeypatch)
        enc = FakeEncoder()
        with db.connect() as conn:
            for i in (1, 2):
                _insert(conn, i, f"t{i}", f"complaint {i}")
            embed_signals(conn, _rows(conn), enc)

            # a truncated vector would otherwise blow up np.vstack mid-run
            conn.execute("UPDATE signal_vec SET vec = ? WHERE signal_id = 2",
                         (b"\x00\x00\x00\x00",))
            vectors = embed_signals(conn, _rows(conn), enc)

            assert vectors.shape == (2, 4)

    def test_vectors_for_dropped_signals_are_pruned(self, tmp_path, monkeypatch):
        db = _db(tmp_path, monkeypatch)
        enc = FakeEncoder()
        with db.connect() as conn:
            for i in (1, 2):
                _insert(conn, i, f"t{i}", f"complaint {i}")
            embed_signals(conn, _rows(conn), enc)

            conn.execute("DELETE FROM signal WHERE id = 2")
            embed_signals(conn, _rows(conn), enc)

            left = conn.execute("SELECT COUNT(*) c FROM signal_vec").fetchone()["c"]
            assert left == 1

    def test_row_order_is_preserved(self, tmp_path, monkeypatch):
        """Vectors are zipped against rows positionally by the clusterer, so a
        cached row must come back in its own slot."""
        db = _db(tmp_path, monkeypatch)
        enc = FakeEncoder()
        with db.connect() as conn:
            for i in (1, 2, 3, 4):
                _insert(conn, i, f"t{i}", f"complaint {i}")
            # cache rows 2 and 4 only, so the next call is a mix of hit and miss
            rows = _rows(conn)
            embed_signals(conn, [rows[1], rows[3]], enc)

            mixed = embed_signals(conn, rows, enc)
            expected = FakeEncoder()([signal_text(r) for r in rows])
            assert np.array_equal(mixed, expected)
