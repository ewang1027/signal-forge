"""Reply parsing.

The failure mode that matters is not "did it parse" but "did it parse something
that was never typed" -- the quoted original is our own email coming back, and
applying its instructions as feedback would be silent, self-inflicted, and
indistinguishable from real input.
"""

from pipeline.replies import Reply, parse, strip_quoted

from pipeline.prep import all_cards
CARDS = {c["id"] for cs in all_cards().values() for c in cs}


class TestQuotedText:
    def test_gmail_style_quote_is_dropped(self):
        body = ("boring\n\n"
                "On Tue, Aug 12, 2026 at 9:01 AM signal-forge wrote:\n"
                "> more\n> two-pointers good\n")
        assert strip_quoted(body).strip() == "boring"

    def test_our_own_email_does_not_become_feedback(self):
        """The digest lists card ids and the words `more`/`boring` in its
        footer. Parsing the quote would grade every card in it."""
        body = ("more\n\n"
                "> Patterns due\n> two-pointers\n> dijkstra\n"
                "> Reply more, boring, exists, or too easy to tune what gets sent.\n")
        r = parse(body, CARDS)
        assert r.idea_verdict == "more"
        assert r.grades == [], "graded cards that only appeared in the quote"

    def test_outlook_style_marker(self):
        body = "easy\n\n-----Original Message-----\nFrom: x\nboring\n"
        assert "boring" not in strip_quoted(body)

    def test_mobile_signature_is_cut(self):
        assert strip_quoted("more\n\nSent from my iPhone") == "more"


class TestIdeaVerdicts:
    def test_plain_words(self):
        for word, expect in (("more", "more"), ("boring", "boring"),
                             ("exists", "exists"), ("building", "building")):
            assert parse(word, CARDS).idea_verdict == expect

    def test_two_word_verdict_beats_its_prefix(self):
        # "easy" alone means too_easy too, but "too easy" must not be shadowed
        assert parse("too easy", CARDS).idea_verdict == "too_easy"

    def test_aliases(self):
        assert parse("meh", CARDS).idea_verdict == "boring"
        assert parse("yes", CARDS).idea_verdict == "more"

    def test_word_boundaries(self):
        """`no` inside `nothing`, `easy` inside `easygoing`."""
        assert parse("nothing here", CARDS).idea_verdict is None
        assert parse("easygoing", CARDS).idea_verdict is None

    def test_verdict_in_a_sentence(self):
        assert parse("this one is boring honestly", CARDS).idea_verdict == "boring"


class TestGrades:
    def test_card_and_grade_on_one_line(self):
        r = parse("two-pointers good", CARDS)
        assert r.grades == [("two-pointers", "good")]

    def test_multiple_lines(self):
        r = parse("two-pointers good\ndijkstra again\n", CARDS)
        assert set(r.grades) == {("two-pointers", "good"), ("dijkstra", "again")}

    def test_unknown_card_is_ignored(self):
        assert parse("some-made-up-card good", CARDS).grades == []

    def test_grade_aliases(self):
        assert parse("dijkstra failed", CARDS).grades == [("dijkstra", "again")]
        assert parse("dijkstra solved", CARDS).grades == [("dijkstra", "good")]

    def test_grades_and_verdict_together(self):
        r = parse("two-pointers good\n\nboring", CARDS)
        assert r.grades == [("two-pointers", "good")]
        assert r.idea_verdict == "boring"

    def test_card_grade_does_not_leak_into_the_idea_verdict(self):
        """`good` is both a grade and a `more` alias. A line that grades a card
        must not also be read as approving the idea."""
        assert parse("two-pointers good", CARDS).idea_verdict is None


class TestTiming:
    def test_minutes(self):
        assert parse("solved 24m", CARDS).minutes == 24
        assert parse("took 35 minutes", CARDS).minutes == 35

    def test_no_time_is_none(self):
        assert parse("boring", CARDS).minutes is None


class TestEmpty:
    def test_blank_reply(self):
        assert parse("", CARDS).is_empty()

    def test_unparseable_reply_is_empty_but_keeps_the_note(self):
        r = parse("hey what happened to the thing", CARDS)
        assert r.is_empty()
        assert "what happened" in r.note


class TestGradeBinding:
    """Each card must get ITS OWN grade. Scanning the whole line for one grade
    meant `two-pointers good, dijkstra again` recorded `two-pointers again` --
    the user said good and FSRS logged a lapse."""

    def test_two_cards_two_grades_on_one_line(self):
        r = parse("two-pointers good, dijkstra again", CARDS)
        assert r.grades == [("two-pointers", "good"), ("dijkstra", "again")]

    def test_mid_sentence_binding(self):
        r = parse("two-pointers easy but dijkstra was hard", CARDS)
        assert r.grades == [("two-pointers", "easy"), ("dijkstra", "hard")]


class TestCardIdBoundaries:
    """Ids were matched as unanchored substrings, so `trie` fired inside
    `retried`, `entries`, `countries`."""

    def test_trie_does_not_match_inside_other_words(self):
        for text in ("retried it and finally solved it",
                     "i skimmed the entries, easy",
                     "the countries thing was trivial"):
            assert parse(text, CARDS).grades == [], text

    def test_longer_id_wins_over_its_substring(self):
        """`intervals` is a substring of `dp-intervals`, and iterating a set
        made which one matched depend on the process hash seed."""
        assert parse("dp-intervals hard", CARDS).grades == [("dp-intervals", "hard")]
        assert parse("intervals good", CARDS).grades == [("intervals", "good")]


class TestSentinel:
    def test_sentinel_cut_beats_every_quoting_style(self):
        from pipeline.replies import SENTINEL
        body = f"boring\n\n{SENTINEL}\nJust reply to this email.\nOn the idea: more"
        assert strip_quoted(body) == "boring"

    def test_surviving_footer_is_refused_not_parsed(self):
        """Wrapped attributions defeat the marker regexes. When our own footer
        survives, applying it would grade the whole deck off a one-word reply."""
        body = ("more\n\nOn Tue, Aug 12, 2026 at 9:01 AM\nsignal-forge wrote:\n"
                "On the idea: more, boring, exists, too easy\n"
                "On a card, one per line: two-pointers good\nPatterns due\n")
        r = parse(body, CARDS)
        assert r.unparsed
        assert r.grades == [] and r.idea_verdict is None


class TestControl:
    def test_pause_and_resume(self):
        assert parse("pause", CARDS).control == "pause"
        assert parse("resume", CARDS).control == "resume"
