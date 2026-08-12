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
