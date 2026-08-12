"""Prep scheduling.

The behaviour that matters is not "FSRS works" -- it does -- but that the
readiness cap and weak-area targeting do what they are supposed to, since those
are the two things this project added on top.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fsrs import Card, Rating

from pipeline.prep import MODES, load_deck


def _drive(sched, ratings):
    """Apply a rating sequence, returning the interval in days after each."""
    card, t, out = Card(), datetime.now(timezone.utc), []
    for r in ratings:
        card, _ = sched.review_card(card, r, t)
        out.append((card.due - t).total_seconds() / 86400)
        t = card.due
    return out


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A throwaway state dir. These were the tests that did not exist -- the
    entire DB layer was untested, which is how `grade()` shipped with no caller
    and every card sharing one due date."""
    import pipeline.config as cfg
    import pipeline.db as dbmod
    monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "t.db")
    for name in ("IDEAS_DIR", "DECKS_DIR", "REJECTS_DIR"):
        monkeypatch.setattr(cfg, name, tmp_path / name.lower())
    return dbmod


class TestUngradedRotation:
    """Before any grading has happened every card shares one due date. Without a
    random tie-break SQLite falls back to primary-key order and serves the same
    alphabetical ten cards forever -- 30 of 40 never appeared."""

    def test_repeated_runs_do_not_serve_an_identical_list(self, db):
        import pipeline.prep as prep
        seen = set()
        with db.connect() as c:
            prep.ensure_cards(c)
            for _ in range(12):
                day = prep.today(c)
                seen.update(card["id"] for card in day["dsa"] + day["system_design"])
        total = len(prep.load_deck("dsa")) + len(prep.load_deck("system_design"))
        assert len(seen) > total * 0.6, \
            f"only {len(seen)}/{total} cards ever shown -- the list is frozen"


class TestGrading:
    def test_grade_advances_the_schedule(self, db):
        import pipeline.prep as prep
        from fsrs import Rating
        with db.connect() as c:
            prep.ensure_cards(c)
            before = c.execute(
                "SELECT due_utc FROM review WHERE card_id='two-pointers'").fetchone()["due_utc"]
            assert prep.grade(c, "dsa", "two-pointers", Rating.Easy)
            row = c.execute(
                "SELECT due_utc, reps FROM review WHERE card_id='two-pointers'").fetchone()
            assert row["reps"] == 1
            assert row["due_utc"] > before

    def test_grade_writes_an_append_only_log(self, db):
        """FSRS cannot reschedule or optimize without history, and history
        cannot be backfilled."""
        import pipeline.prep as prep
        from fsrs import Rating
        with db.connect() as c:
            prep.ensure_cards(c)
            prep.grade(c, "dsa", "two-pointers", Rating.Good)
            prep.grade(c, "dsa", "two-pointers", Rating.Again)
            logs = c.execute("SELECT rating FROM review_log ORDER BY id").fetchall()
            assert [r["rating"] for r in logs] == [int(Rating.Good), int(Rating.Again)]

    def test_unknown_card_returns_false(self, db):
        import pipeline.prep as prep
        from fsrs import Rating
        with db.connect() as c:
            assert prep.grade(c, "dsa", "no-such-card", Rating.Good) is False

    def test_corrupt_state_does_not_lose_the_grade(self, db):
        import pipeline.prep as prep
        from fsrs import Rating
        with db.connect() as c:
            prep.ensure_cards(c)
            c.execute("UPDATE review SET state='{bad json' WHERE card_id='two-pointers'")
            assert prep.grade(c, "dsa", "two-pointers", Rating.Good)

    def test_fails_counts_every_miss_even_before_graduation(self, db):
        """A card failed from its first showing never leaves Learning, so FSRS
        `lapses` stays 0 -- using that for weak-area targeting would make the
        worst-known card invisible."""
        import pipeline.prep as prep
        from fsrs import Rating
        with db.connect() as c:
            prep.ensure_cards(c)
            for _ in range(5):
                prep.grade(c, "dsa", "two-pointers", Rating.Again)
            row = c.execute(
                "SELECT lapses, fails FROM review WHERE card_id='two-pointers'").fetchone()
            assert row["fails"] == 5
            assert row["lapses"] == 0, "strict FSRS lapse semantics preserved"


class TestTimedSlotsRotate:
    """Sorting by weakness is a positive feedback loop: shown, failed, weaker,
    shown again. Simulated over 90 days it served one design card on 87."""

    def test_design_prompt_is_not_monopolized(self, db):
        import pipeline.prep as prep
        from fsrs import Rating
        seen = set()
        with db.connect() as c:
            prep.ensure_cards(c)
            worst = c.execute(
                "SELECT card_id FROM review WHERE deck='system_design' LIMIT 1").fetchone()["card_id"]
            for _ in range(6):
                prep.grade(c, "system_design", worst, Rating.Again)
            for _ in range(25):
                if p := prep.pick_design_prompt(c):
                    seen.add(p["id"])
        assert len(seen) >= 4, f"only {len(seen)} distinct design prompts in 25 draws"

    def test_problems_rotate_within_a_pattern(self, db):
        import pipeline.prep as prep
        given = set()
        with db.connect() as c:
            prep.ensure_cards(c)
            for _ in range(10):
                for p in prep.pick_problems(c, 3):
                    given.add(p["problem"])
        assert len(given) >= 10, f"only {len(given)} distinct problems over 10 days"


class TestOrphans:
    def test_renamed_card_is_reported_not_silently_dropped(self, db):
        import pipeline.prep as prep
        with db.connect() as c:
            prep.ensure_cards(c)
            c.execute("INSERT INTO review (deck, card_id, state, due_utc) "
                      "VALUES ('dsa','removed-pattern','{}',0)")
            assert ("dsa", "removed-pattern") in prep.orphans(c)

    def test_no_orphans_on_a_clean_deck(self, db):
        import pipeline.prep as prep
        with db.connect() as c:
            prep.ensure_cards(c)
            assert prep.orphans(c) == []


class TestReadinessCap:
    """While actively recruiting a callback lands with about a week's notice, so
    nothing may be scheduled far enough out to go stale. FSRS on its own will
    happily push a known card 6-12 months."""

    def test_recruiting_intervals_stay_inside_the_cap(self):
        from fsrs import Scheduler
        cfg = MODES["recruiting"]
        s = Scheduler(desired_retention=cfg.retention,
                      maximum_interval=cfg.max_interval, enable_fuzzing=False)
        intervals = _drive(s, [Rating.Good] * 15)
        assert max(intervals) <= cfg.max_interval + 1, \
            f"interval escaped the cap: {max(intervals):.1f}d > {cfg.max_interval}d"

    def test_sharp_mode_allows_long_intervals(self):
        """The cap is a deliberate choice, not a limitation -- without a
        deadline the long intervals are correct."""
        from fsrs import Scheduler
        cfg = MODES["sharp"]
        s = Scheduler(desired_retention=cfg.retention,
                      maximum_interval=cfg.max_interval, enable_fuzzing=False)
        intervals = _drive(s, [Rating.Good] * 15)
        assert max(intervals) > MODES["recruiting"].max_interval, \
            "sharp mode should schedule further out than recruiting mode"

    def test_higher_retention_means_shorter_intervals(self):
        from fsrs import Scheduler
        lo = Scheduler(desired_retention=0.85, maximum_interval=3650,
                       enable_fuzzing=False)
        hi = Scheduler(desired_retention=0.95, maximum_interval=3650,
                       enable_fuzzing=False)
        seq = [Rating.Good] * 6
        assert sum(_drive(hi, seq)) < sum(_drive(lo, seq)), \
            "raising target retention must buy more reps, not fewer"


class TestLapseBehaviour:
    def test_failure_collapses_the_interval(self):
        from fsrs import Scheduler
        s = Scheduler(maximum_interval=21, enable_fuzzing=False)
        card, t = Card(), datetime.now(timezone.utc)
        grown = 0.0
        for _ in range(6):
            card, _ = s.review_card(card, Rating.Good, t)
            grown = (card.due - t).total_seconds() / 86400  # before advancing t
            t = card.due
        assert grown > 5, f"card should be well-learned by now, interval {grown:.1f}d"

        lapsed, _ = s.review_card(card, Rating.Again, t)
        after = (lapsed.due - t).total_seconds() / 86400
        assert after < 1, f"a lapse must bring the card straight back, got {after:.2f}d"
        assert after < grown


class TestDeckContent:
    def test_both_decks_load(self):
        assert len(load_deck("dsa")) >= 20
        assert len(load_deck("system_design")) >= 15

    def test_dsa_cards_are_patterns_not_problems(self):
        """Scheduling individual problems trains recall of those problems;
        scheduling the recognition cue generalizes."""
        for card in load_deck("dsa"):
            assert card.get("cue"), f"{card['id']} has no recognition cue"
            assert card.get("trap"), f"{card['id']} has no failure mode"
            assert len(card.get("problems", [])) >= 3, \
                f"{card['id']} needs a rotating problem set"

    def test_design_cards_carry_a_rubric_and_a_trap(self):
        for card in load_deck("system_design"):
            assert len(card.get("must_cover", [])) >= 4, \
                f"{card['id']} needs enough rubric points to self-check"
            assert card.get("trap"), f"{card['id']} has no candidate-sinking trap"

    def test_card_ids_are_unique(self):
        for deck in ("dsa", "system_design"):
            ids = [c["id"] for c in load_deck(deck)]
            assert len(ids) == len(set(ids)), f"duplicate ids in {deck}"


class TestIntensityModes:
    def test_recruiting_is_the_tightest(self):
        r, s = MODES["recruiting"], MODES["sharp"]
        assert r.max_interval < s.max_interval
        assert r.retention > s.retention
        assert r.daily_cap > s.daily_cap

    def test_only_recruiting_includes_timed_design(self):
        assert MODES["recruiting"].timed_design
        assert not MODES["sharp"].timed_design

    def test_volume_comes_from_problems(self):
        """With ~20 patterns and a 21-day cap the deck settles at 1-2 reviews a
        day no matter how the scheduler is tuned. Practice volume has to come
        from problems."""
        assert MODES["recruiting"].problems >= 3
        assert MODES["sharp"].problems == 0


class TestNoTreadmill:
    """Ordering purely by `lapses DESC` looks like weak-area targeting and
    degenerates into a treadmill: simulated over 90 days, three always-failed
    cards took three of six slots every single day and crowded out breadth."""

    def _fresh(self, tmp_path, monkeypatch):
        import pipeline.config as cfg
        import pipeline.db as db
        import pipeline.prep as prep
        for mod in (cfg, db):
            monkeypatch.setattr(mod, "DB_PATH", tmp_path / "t.db", raising=False)
        monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
        for name in ("IDEAS_DIR", "DECKS_DIR", "REJECTS_DIR"):
            monkeypatch.setattr(cfg, name, tmp_path / name.lower())
        return db, prep

    def test_weak_cards_do_not_occupy_every_slot(self, tmp_path, monkeypatch):
        db, prep = self._fresh(tmp_path, monkeypatch)
        from fsrs import Rating

        base = datetime.now(timezone.utc)
        shown = {}
        with db.connect() as c:
            prep.ensure_cards(c)
            ids = [r["card_id"] for r in
                   c.execute("SELECT card_id FROM review WHERE deck='dsa'")]
            weak = set(ids[:3])
            for day in range(40):
                monkeypatch.setattr(prep, "_now",
                                    lambda d=day: base + timedelta(days=d))
                for row in prep.due_cards(c, "dsa", 6):
                    cid = row["card_id"]
                    shown[cid] = shown.get(cid, 0) + 1
                    prep.grade(c, "dsa", cid,
                               Rating.Again if cid in weak else Rating.Good)

        for cid in weak:
            assert shown.get(cid, 0) <= 15, \
                f"{cid} shown {shown.get(cid)} of 40 days -- that is a treadmill"

    def test_no_card_is_starved(self, tmp_path, monkeypatch):
        db, prep = self._fresh(tmp_path, monkeypatch)
        from fsrs import Rating

        base = datetime.now(timezone.utc)
        shown = set()
        with db.connect() as c:
            prep.ensure_cards(c)
            ids = [r["card_id"] for r in
                   c.execute("SELECT card_id FROM review WHERE deck='dsa'")]
            for day in range(60):
                monkeypatch.setattr(prep, "_now",
                                    lambda d=day: base + timedelta(days=d))
                for row in prep.due_cards(c, "dsa", 6):
                    shown.add(row["card_id"])
                    prep.grade(c, "dsa", row["card_id"], Rating.Good)
        assert set(ids) == shown, f"never surfaced: {set(ids) - shown}"


class TestLeeches:
    def test_repeatedly_failed_cards_leave_the_review_rotation(self, tmp_path,
                                                               monkeypatch):
        import pipeline.config as cfg
        import pipeline.db as db
        import pipeline.prep as prep
        from fsrs import Rating

        monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
        for name in ("IDEAS_DIR", "DECKS_DIR", "REJECTS_DIR"):
            monkeypatch.setattr(cfg, name, tmp_path / name.lower())

        with db.connect() as c:
            prep.ensure_cards(c)
            cid = c.execute(
                "SELECT card_id FROM review WHERE deck='dsa' LIMIT 1").fetchone()["card_id"]
            for _ in range(prep.LEECH_AT + 2):
                prep.grade(c, "dsa", cid, Rating.Again)

            assert cid in {r["card_id"] for r in prep.leeches(c, "dsa")}
            assert cid not in {r["card_id"] for r in prep.due_cards(c, "dsa", 10)}, \
                "a card failed this often needs studying, not another review"
