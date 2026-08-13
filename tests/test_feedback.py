"""Applying feedback, and the canary.

Fetching is not tested here -- it is IMAP I/O with nothing to assert that a mock
would not simply restate. What matters is that a parsed reply moves the right
state, and that the canary fires exactly once per silent stretch.
"""

import time

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    import pipeline.config as cfg
    import pipeline.db as dbmod
    monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "t.db")
    for name in ("IDEAS_DIR", "DECKS_DIR", "REJECTS_DIR"):
        monkeypatch.setattr(cfg, name, tmp_path / name.lower())
    monkeypatch.setattr(cfg, "TASTE_PATH", tmp_path / "TASTE.md")
    import pipeline.taste as taste
    monkeypatch.setattr(taste, "TASTE_PATH", tmp_path / "TASTE.md")
    return dbmod


def _seed_idea(c, n_signals=3, domain="storage"):
    for i in range(1, n_signals + 1):
        c.execute("INSERT INTO signal (id,source,external_id,text,created_utc,"
                  "harvested_utc,domain) VALUES (?,?,?,?,?,?,?)",
                  (i, "hn", str(i), "x", 1, 1, domain))
    c.execute("INSERT INTO idea (id,slug,title,body,domain,sent_utc) "
              "VALUES (1,'s','A thing','{}',?,?)", (domain, int(time.time())))
    c.executemany("INSERT INTO idea_signal VALUES (?,?)",
                  [(1, i) for i in range(1, n_signals + 1)])


class TestWeights:
    def test_boring_downweights_the_evidence(self, db):
        import pipeline.feedback as fb
        with db.connect() as c:
            _seed_idea(c)
            fb.apply(c, "boring")
            weights = [r["weight"] for r in c.execute("SELECT weight FROM signal")]
            assert all(w < 1.0 for w in weights)

    def test_building_upweights_hardest(self, db):
        import pipeline.feedback as fb
        with db.connect() as c:
            _seed_idea(c)
            fb.apply(c, "building this")
            w = c.execute("SELECT weight FROM signal LIMIT 1").fetchone()["weight"]
            assert w > 1.5, "actually building it is the strongest signal there is"

    def test_weight_is_clamped(self, db):
        import pipeline.feedback as fb
        with db.connect() as c:
            _seed_idea(c)
            for _ in range(40):
                c.execute("UPDATE idea SET verdict = NULL")
                fb.apply(c, "boring")
            w = c.execute("SELECT weight FROM signal LIMIT 1").fetchone()["weight"]
            assert w >= fb.WEIGHT_FLOOR

    def test_weight_lives_on_signals_so_it_survives_reclustering(self, db):
        """Themes are rebuilt with fresh ids every run. Anything stored on a
        theme is erased; signal ids are permanent."""
        import pipeline.feedback as fb
        with db.connect() as c:
            _seed_idea(c)
            fb.apply(c, "boring")
            c.execute("DELETE FROM theme")          # simulate a rebuild
            c.execute("DELETE FROM theme_member")
            weights = [r["weight"] for r in c.execute("SELECT weight FROM signal")]
            assert all(w < 1.0 for w in weights)


class TestGradesFromEmail:
    def test_reply_grades_a_card(self, db):
        import pipeline.feedback as fb
        import pipeline.prep as prep
        with db.connect() as c:
            prep.ensure_cards(c)
            did = fb.apply(c, "two-pointers again")
            assert ("dsa", "two-pointers", "again") in did["grades"]
            row = c.execute("SELECT reps, fails FROM review "
                            "WHERE card_id='two-pointers'").fetchone()
            assert row["reps"] == 1 and row["fails"] == 1

    def test_skip_records_nothing(self, db):
        import pipeline.feedback as fb
        import pipeline.prep as prep
        with db.connect() as c:
            prep.ensure_cards(c)
            fb.apply(c, "two-pointers skip")
            row = c.execute("SELECT reps FROM review "
                            "WHERE card_id='two-pointers'").fetchone()
            assert row["reps"] == 0

    def test_one_reply_can_do_both(self, db):
        import pipeline.feedback as fb
        import pipeline.prep as prep
        with db.connect() as c:
            prep.ensure_cards(c)
            _seed_idea(c)
            did = fb.apply(c, "two-pointers good\ndijkstra again\n\nboring")
            assert len(did["grades"]) == 2
            assert did["verdict"] == "boring"


class TestCanary:
    def _quiet(self, c, n=5, days_ago=30):
        now = int(time.time())
        for i in range(1, n + 1):
            c.execute("INSERT INTO idea (id,slug,title,body,domain,sent_utc) "
                      "VALUES (?,?,?,?,?,?)",
                      (i, f"s{i}", f"t{i}", "{}", "storage", now - 86400 * days_ago))

    def test_silent_when_there_is_nothing_to_complain_about(self, db):
        import pipeline.feedback as fb
        with db.connect() as c:
            assert fb.canary(c) is None

    def test_fires_after_sustained_silence(self, db):
        import pipeline.feedback as fb
        with db.connect() as c:
            self._quiet(c)
            msg = fb.canary(c)
            assert msg and "unanswered" in msg

    def test_does_not_nag(self, db):
        import pipeline.feedback as fb
        with db.connect() as c:
            self._quiet(c)
            assert fb.canary(c, mark=True)
            assert fb.canary(c, mark=True) is None, "fired twice for one silent stretch"

    def test_checking_does_not_consume_it(self, db):
        """A --dry-run used to mark the canary fired, permanently silencing the
        one alert whose job is noticing the system has gone stale."""
        import pipeline.feedback as fb
        with db.connect() as c:
            self._quiet(c)
            assert fb.canary(c)              # peek, as --dry-run does
            assert fb.canary(c), "a dry run consumed the canary"
            assert fb.canary(c, mark=True)   # the real send
            assert fb.canary(c, mark=True) is None

    def test_a_reply_resets_it(self, db):
        import pipeline.feedback as fb
        with db.connect() as c:
            self._quiet(c)
            fb.canary(c)
            db.set_kv(c, "last_reply_utc", str(int(time.time())))
            assert fb.canary(c) is None

    def test_answered_ideas_do_not_count(self, db):
        import pipeline.feedback as fb
        with db.connect() as c:
            self._quiet(c)
            c.execute("UPDATE idea SET verdict = 'more'")
            assert fb.canary(c) is None


class TestTasteFile:
    def test_verdict_is_recorded(self, db, tmp_path):
        import pipeline.feedback as fb
        with db.connect() as c:
            _seed_idea(c)
            fb.apply(c, "boring")
        assert "boring" in (tmp_path / "TASTE.md").read_text()
