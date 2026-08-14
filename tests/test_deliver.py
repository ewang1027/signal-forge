"""Cadence, idempotency, and the multi-idea digest.

This module had no tests when three digests went out within 31 minutes on
2026-08-13. Every one of them was a real email carrying a different queued idea,
so from the outside it looked like the system working rather than repeating
itself. The tests that would have caught it are the first two classes here.
"""

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

UTC = ZoneInfo("UTC")


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


def _queue(conn, *slugs, domain="storage"):
    """Put unsent ideas in the queue, oldest first."""
    for i, slug in enumerate(slugs, 1):
        body = json.dumps({"title": slug.split("-")[0], "one_liner": "does a thing",
                           "problem": "p", "why_hard": "h", "evidence_refs": []})
        conn.execute(
            "INSERT INTO idea (id,slug,title,body,domain,sent_utc) "
            "VALUES (?,?,?,?,?,NULL)",
            (i, slug, slug.split("-")[0], body, domain),
        )


class _Sent:
    """Records sends instead of performing them."""

    def __init__(self):
        self.emails = []

    def install(self, monkeypatch, deliver):
        monkeypatch.setattr(deliver, "send_email",
                            lambda subject, html: self.emails.append((subject, html)))
        monkeypatch.setattr(deliver, "send_push", lambda msg: None)
        return self


def _run(monkeypatch, deliver, when: datetime, argv=()):
    """Run deliver.main() as if it were `when`."""
    monkeypatch.setattr(deliver, "datetime",
                        type("D", (), {"now": staticmethod(lambda tz=None: when)}))
    monkeypatch.setattr("sys.argv", ["deliver", *argv])
    return deliver.main()


# Anchors: 2026-08-17 is a Monday, so +1 Tue, +2 Wed, +5 Sat.
MON = datetime(2026, 8, 17, 7, 3, tzinfo=UTC)
TUE = datetime(2026, 8, 18, 7, 3, tzinfo=UTC)
WED = datetime(2026, 8, 19, 7, 3, tzinfo=UTC)
SAT = datetime(2026, 8, 22, 7, 3, tzinfo=UTC)


class TestCadence:
    def test_monday_carries_the_ideas(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x", "pathdoctor-y", "archsim-z")
        assert _run(monkeypatch, deliver, MON) == 0
        assert len(sent.emails) == 1
        subject, html = sent.emails[0]
        assert subject.startswith("ideas —")
        assert html.count('class="idea"') == 3

    def test_wednesday_is_prep_only(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x")
        assert _run(monkeypatch, deliver, WED) == 0
        subject, html = sent.emails[0]
        assert subject.startswith("prep —")
        assert 'class="idea"' not in html, "ideas are Monday's, not Wednesday's"

    def test_saturday_sends(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        assert _run(monkeypatch, deliver, SAT) == 0
        assert len(sent.emails) == 1

    def test_tuesday_sends_nothing(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x")
        assert _run(monkeypatch, deliver, TUE) == 0
        assert sent.emails == []

    def test_an_off_day_does_not_burn_the_queue(self, db, monkeypatch):
        """The Tuesday run still fires -- it harvests and fetches replies. It
        must not quietly consume Monday's ideas on the way past."""
        import pipeline.deliver as deliver
        _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x")
        _run(monkeypatch, deliver, TUE)
        with db.connect() as c:
            row = c.execute("SELECT sent_utc FROM idea WHERE id = 1").fetchone()
            assert row["sent_utc"] is None

    def test_an_off_day_does_not_burn_problems(self, db, monkeypatch):
        """`prep.today()` records the problems it hands out. Calling it on a day
        with no email spends them on nobody."""
        import pipeline.deliver as deliver
        _Sent().install(monkeypatch, deliver)
        _run(monkeypatch, deliver, TUE)
        with db.connect() as c:
            n = c.execute("SELECT COUNT(*) n FROM problem_log").fetchone()["n"]
            assert n == 0

    def test_force_overrides_the_day(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        assert _run(monkeypatch, deliver, TUE, ["--force"]) == 0
        assert len(sent.emails) == 1


class TestSendsOncePerDay:
    def test_second_run_same_day_sends_nothing(self, db, monkeypatch):
        """The actual 2026-08-13 failure: three dispatches, three emails."""
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x", "pathdoctor-y", "archsim-z")

        for _ in range(3):
            _run(monkeypatch, deliver, MON)

        assert len(sent.emails) == 1, "one digest per day, however many dispatches"

    def test_repeat_dispatch_does_not_drain_the_queue(self, db, monkeypatch):
        """Each of the three real runs drained a *different* idea, which is why
        it read as three legitimate digests instead of a repeat."""
        import pipeline.deliver as deliver
        _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "a-one", "b-two", "c-three", "d-four", "e-five")

        for _ in range(3):
            _run(monkeypatch, deliver, MON)

        with db.connect() as c:
            unsent = c.execute(
                "SELECT COUNT(*) n FROM idea WHERE sent_utc IS NULL").fetchone()["n"]
        assert unsent == 2, "one digest of 3, not three digests of 1"

    def test_next_send_day_is_not_blocked(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        _run(monkeypatch, deliver, MON)
        _run(monkeypatch, deliver, WED)
        assert len(sent.emails) == 2

    def test_force_sends_twice_in_a_day(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        _run(monkeypatch, deliver, MON)
        _run(monkeypatch, deliver, MON, ["--force"])
        assert len(sent.emails) == 2

    def test_dry_run_does_not_consume_the_day(self, db, monkeypatch, tmp_path):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        monkeypatch.chdir(tmp_path)
        _run(monkeypatch, deliver, MON, ["--dry-run"])
        _run(monkeypatch, deliver, MON)
        assert len(sent.emails) == 1, "previewing must not eat the real send"


class TestReplyHandles:
    def test_handle_is_the_project_name(self):
        from pipeline.deliver import reply_id_for
        assert reply_id_for("monoblame-an-attributor-for-rust", set()) == "monoblame"

    def test_collision_with_another_idea_is_suffixed(self):
        from pipeline.deliver import reply_id_for
        assert reply_id_for("archsim-again", {"archsim"}) == "archsim2"

    def test_collision_with_a_card_id_is_avoided(self):
        """The reply parser scans one line for card ids and idea handles alike,
        so a handle equal to a card id makes every grade of that card
        ambiguous."""
        from pipeline.deliver import reply_id_for
        assert reply_id_for("trie-based-index-thing", {"trie"}) != "trie"

    def test_two_letter_name_borrows_the_next_word(self):
        from pipeline.deliver import reply_id_for
        assert reply_id_for("go-routine-leak-finder", set()) == "go-routine"

    def test_handles_are_recorded_at_send(self, db, monkeypatch):
        import pipeline.deliver as deliver
        _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x", "pathdoctor-y")
        _run(monkeypatch, deliver, MON)
        with db.connect() as c:
            got = {r["reply_id"] for r in c.execute(
                "SELECT reply_id FROM idea WHERE sent_utc IS NOT NULL")}
        assert got == {"monoblame", "pathdoctor"}


class TestQueue:
    def test_oldest_first(self, db):
        from pipeline.deliver import next_unsent
        with db.connect() as c:
            _queue(c, "a-one", "b-two", "c-three")
            rows = next_unsent(c, 2)
        assert [r["slug"] for r in rows] == ["a-one", "b-two"]

    def test_batch_shares_one_timestamp(self, db, monkeypatch):
        """`feedback.last_digest_ideas` groups on sent_utc to answer "what was
        in the last email", so the batch has to agree on one."""
        import pipeline.deliver as deliver
        _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "a-one", "b-two", "c-three")
        _run(monkeypatch, deliver, MON)
        with db.connect() as c:
            stamps = {r["sent_utc"] for r in c.execute(
                "SELECT sent_utc FROM idea WHERE sent_utc IS NOT NULL")}
        assert len(stamps) == 1

    def test_sends_what_is_queued_when_short(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "only-one")
        _run(monkeypatch, deliver, MON)
        assert sent.emails[0][1].count('class="idea"') == 1


class TestDigestBody:
    def test_every_idea_prints_its_handle(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x", "pathdoctor-y", "archsim-z")
        _run(monkeypatch, deliver, MON)
        html = sent.emails[0][1]
        for handle in ("monoblame", "pathdoctor", "archsim"):
            assert f"<code>{handle}</code>" in html, \
                "without a handle in the body there is no way to judge one of three"

    def test_subject_is_recognised_as_ours(self, db, monkeypatch):
        """`feedback._is_ours` gates whether a reply is read at all. A subject
        it does not recognise kills the feedback loop with no symptom."""
        import pipeline.deliver as deliver
        import pipeline.feedback as fb
        sent = _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x", "pathdoctor-y")
        _run(monkeypatch, deliver, MON)
        subject = sent.emails[0][0]
        assert fb._is_ours(f"Re: {subject}", set())

    def test_plain_language_comes_before_the_dense_version(self):
        """The ramp only works in this order. The reader meets the idea in
        words he knows, then the precise version, then the definitions."""
        from pipeline.deliver import render_idea
        html = render_idea({
            "title": "cgctl", "one_liner": "measures OOM warning time",
            "in_plain_terms": "PLAIN-WHAT",
            "why_it_is_hard_plainly": "PLAIN-WHY",
            "why_hard": "DENSE-WHY",
            "glossary": [{"term": "cgroup", "means": "a box with limits"}],
        }, "os-kernel")
        assert html.index("PLAIN-WHAT") < html.index("PLAIN-WHY") < html.index("DENSE-WHY")
        assert html.index("DENSE-WHY") < html.index("cgroup"), \
            "definitions belong under the section that needed them"

    def test_glossary_is_rendered(self):
        from pipeline.deliver import render_idea
        html = render_idea({
            "title": "x",
            "glossary": [{"term": "quorum", "means": "enough of them agreeing"},
                         {"term": "PSI", "means": "how long things waited"}],
        }, "distributed")
        assert "quorum" in html and "enough of them agreeing" in html
        assert "PSI" in html and "how long things waited" in html

    def test_starting_points_are_rendered(self):
        from pipeline.deliver import render_idea
        html = render_idea(
            {"title": "x", "starting_points": ["man 7 cgroups — the limits"]},
            "os-kernel")
        assert "man 7 cgroups" in html

    def test_an_older_idea_without_the_new_fields_still_renders(self):
        """Ideas queued before the plain-language pass existed are already past
        the gate and must not render a blank section or raise."""
        from pipeline.deliver import render_idea
        html = render_idea({"title": "old one", "one_liner": "x",
                            "why_hard": "dense"}, "storage")
        assert "old one" in html
        assert "Words used above" not in html
        assert "In plain terms" not in html

    def test_glossary_entries_are_escaped(self):
        from pipeline.deliver import render_idea
        html = render_idea(
            {"title": "x", "glossary": [{"term": "<script>", "means": "&"}]},
            "storage")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_paused_still_sends_prep(self, db, monkeypatch):
        import pipeline.deliver as deliver
        sent = _Sent().install(monkeypatch, deliver)
        with db.connect() as c:
            _queue(c, "monoblame-x")
            db.set_kv(c, "ideas_paused", "1")
        _run(monkeypatch, deliver, MON)
        subject, html = sent.emails[0]
        assert 'class="idea"' not in html
        assert subject.startswith("prep —")
