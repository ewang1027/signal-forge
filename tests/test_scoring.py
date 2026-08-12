"""Regression tests for the ranking layer.

Every case here corresponds to a bug that was live in the code, most found by
decomposing real scores rather than by reading the function.
"""

import time

from pipeline.ideate import UngroundedIdea, validate_refs
from pipeline.sources.lobsters import _parse_ts
from pipeline.themes import evidence_score, theme_key

NOW = int(time.time())
DAY = 86400


def row(source, author, age_days, rid=1):
    return {"id": rid, "source": source, "author": author,
            "created_utc": NOW - int(age_days * DAY)}


class TestThemeKeyIsStable:
    def test_key_depends_only_on_members(self):
        assert theme_key([3, 1, 2]) == theme_key([1, 2, 3])

    def test_different_members_differ(self):
        assert theme_key([1, 2, 3]) != theme_key([1, 2, 4])


class TestVoiceIndependence:
    def test_one_person_flooding_counts_once(self):
        flood = [row("hn", "spammer", 1, i) for i in range(10)]
        single = [row("hn", "spammer", 1)]
        assert evidence_score(flood, NOW) == evidence_score(single, NOW)

    def test_anonymous_rows_collapse_to_one_voice(self):
        # otherwise anonymity multiplies a voice instead of merely hiding it
        anon = [row("hn", "", 1, i) for i in range(5)]
        assert evidence_score(anon, NOW) < 1.01

    def test_distinct_people_accumulate(self):
        people = [row("hn", f"user{i}", 1, i) for i in range(3)]
        assert evidence_score(people, NOW) > 2.9


class TestRecency:
    def test_old_evidence_is_worth_less(self):
        fresh = [row("hn", "a", 1)]
        stale = [row("hn", "a", 720)]
        assert evidence_score(stale, NOW) < 0.1 * evidence_score(fresh, NOW)

    def test_missing_timestamp_is_dropped_not_treated_as_today(self):
        # `created_utc or now` used to convert "unknown" into maximum recency
        bad = [{"id": 1, "source": "hn", "author": "a", "created_utc": 0}]
        assert evidence_score(bad, NOW) == 0.0


class TestDiversityMultiplier:
    def test_stale_second_source_buys_little_credit(self):
        """The bug: one row the decay valued at 0.05 granted the full 1.5x,
        contributing 35% of the top theme's real score."""
        solo = [row("hn", "a", 1), row("hn", "b", 1), row("hn", "c", 1)]
        plus_stale = solo + [row("lobsters", "z", 900)]
        gain = evidence_score(plus_stale, NOW) / evidence_score(solo, NOW)
        assert gain < 1.10, f"stale row inflated score by {gain:.2f}x"

    def test_fresh_second_source_earns_real_credit(self):
        solo = [row("hn", "a", 1), row("hn", "b", 1), row("hn", "c", 1)]
        plus_fresh = solo + [row("github", "z", 1)]
        assert evidence_score(plus_fresh, NOW) / evidence_score(solo, NOW) > 1.3

    def test_single_source_gets_no_bonus(self):
        rows = [row("hn", "a", 1), row("hn", "b", 1)]
        assert abs(evidence_score(rows, NOW) - 2.0) < 0.02


class TestEvidenceRefValidation:
    def test_fabricated_urls_are_dropped(self):
        supplied = [{"url": "https://real/1"}, {"url": "https://real/2"}]
        idea = {"evidence_refs": ["https://real/1", "https://real/2",
                                  "https://news.ycombinator.com/item?id=99999999"]}
        assert validate_refs(idea, supplied) == ["https://real/1", "https://real/2"]

    def test_wholly_ungrounded_idea_is_rejected(self):
        supplied = [{"url": "https://real/1"}, {"url": "https://real/2"}]
        idea = {"evidence_refs": ["https://fake/1", "https://fake/2"]}
        try:
            validate_refs(idea, supplied)
        except UngroundedIdea:
            return
        raise AssertionError("should have refused to ship an ungrounded idea")


class TestLobstersTimestamps:
    def test_parses_iso_with_offset(self):
        assert _parse_ts("2026-08-10T08:10:03.585-05:00") > 1_700_000_000

    def test_bad_input_returns_zero_not_now(self):
        assert _parse_ts(None) == 0
        assert _parse_ts("garbage") == 0
