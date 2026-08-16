"""The harvest path: the shared gate, the sources' request behaviour, and store.

Most of what these pin is not "does it fetch" but the things that were made
cheaper without being allowed to change: the gate still requires both axes
whichever order they run in, probes still come back in a fixed order now that
they run concurrently, and a story with no comments is not worth a request.
"""

import time

import httpx
import pytest

from pipeline.harvest import store
from pipeline.item import Item, gate
from pipeline.sources import github, hn, lobsters

COMPLAINT = (
    "our raft consensus implementation kept hitting a network partition and "
    "leader election was broken for twenty minutes. we spent weeks debugging "
    "it and the workaround is painful and flaky. there is no way to make the "
    "quorum recover cleanly on its own, so failover is still fragile."
)


def _item(**kw) -> Item:
    base = dict(source="hn", external_id="1", url="u", title="a thread",
                text=COMPLAINT, author="me", created_utc=1, engagement=0,
                query="q")
    return Item(**{**base, **kw})


class TestGate:
    """Pain is checked before domain now -- one pass over the text instead of
    eight -- which is only legitimate because both are required."""

    def test_accepts_and_annotates(self):
        item = gate(_item())
        assert item is not None
        assert item.domain == "distributed"
        assert item.domain_hits >= 2 and item.pain >= 2

    def test_domain_dense_but_painless_is_rejected(self):
        painless = ("we run raft consensus with quorum reads behind a load "
                    "balancer, and leader election is handled by etcd. the "
                    "service mesh routes rpc traffic across shards. " * 2)
        assert gate(_item(text=painless)) is None

    def test_painful_but_off_topic_is_rejected(self):
        whining = ("this was painful and frustrating, the whole thing is "
                   "broken and i gave up after wasting a week on it. there is "
                   "no way to make it work and the tooling is a nightmare. " * 2)
        assert gate(_item(text=whining)) is None

    def test_require_pain_off_still_needs_a_domain(self):
        assert gate(_item(text="a" * 400), require_pain=False) is None

    def test_short_text_is_a_quip_not_a_report(self):
        assert gate(_item(text="raft is broken and painful")) is None

    def test_hiring_threads_never_reach_the_lexicons(self):
        assert gate(_item(title="Ask HN: Who is hiring?")) is None


class TestHackerNewsProbes:
    """Probes run concurrently. They must still come back in plan order: the
    order rows are inserted is the order they get ids, and nothing should make
    that depend on which request happened to finish first."""

    @pytest.fixture(autouse=True)
    def _one_page(self, monkeypatch):
        monkeypatch.setattr(hn, "PAIN_PROBES", ["p1", "p2", "p3"])
        monkeypatch.setattr(hn, "DOMAIN_PROBES", ["d1", "d2"])

    def _hit(self, query):
        return {"hits": [{"objectID": query, "story_title": "a thread",
                          "comment_text": COMPLAINT, "author": "me",
                          "created_at_i": 1, "num_comments": 3}],
                "nbPages": 1}

    def test_results_come_back_in_plan_order(self, monkeypatch):
        def slow_search(client, query, since, *, phrase, page):
            # earlier probes finish last, so completion order != plan order
            time.sleep(0.05 if query == "p1" else 0.0)
            return self._hit(query)

        monkeypatch.setattr(hn, "_search", slow_search)
        items, _ = hn.harvest(None, 0)
        assert [i.query for i in items] == ["p1", "p2", "p3", "d1", "d2"]

    def test_one_failing_probe_does_not_lose_the_others(self, monkeypatch):
        def flaky(client, query, since, *, phrase, page):
            if query == "p2":
                raise httpx.ConnectError("boom")
            return self._hit(query)

        monkeypatch.setattr(hn, "_search", flaky)
        items, _ = hn.harvest(None, 0)
        assert [i.query for i in items] == ["p1", "p3", "d1", "d2"]

    def test_pain_probes_are_quoted_and_domain_probes_are_not(self, monkeypatch):
        seen = {}

        def record(client, query, since, *, phrase, page):
            seen[query] = phrase
            return {"hits": [], "nbPages": 1}

        monkeypatch.setattr(hn, "_search", record)
        hn.harvest(None, 0)
        assert seen == {"p1": True, "p2": True, "p3": True,
                        "d1": False, "d2": False}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, listing, details):
        self.listing, self.details = listing, details
        self.fetched: list[str] = []

    def get(self, url, **kwargs):
        if "/t/" in url:
            return FakeResponse(self.listing)
        self.fetched.append(url)
        return FakeResponse(self.details.get(url, {}))


class TestLobstersDetailFetches:
    """The detail fetch is this source's whole cost -- one request and a
    courtesy pause per story, several hundred per run."""

    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(lobsters, "TAGS", ["programming"])
        monkeypatch.setattr(lobsters, "PAGES_PER_TAG", 1)
        monkeypatch.setattr(lobsters.time, "sleep", lambda _: None)

    def _run(self, listing, details):
        client = FakeClient(listing, details)
        items, dropped = lobsters.harvest(client, 0)
        return client.fetched, items

    def test_silent_stories_are_not_fetched(self):
        listing = [{"short_id": "aaa", "comment_count": 0},
                   {"short_id": "bbb", "comment_count": 2}]
        details = {"https://lobste.rs/s/bbb.json": {
            "title": "a thread", "url": "u",
            "comments": [{"short_id": "c1", "comment": COMPLAINT,
                          "commenting_user": "me",
                          "created_at": "2026-08-10T08:10:03.585-05:00",
                          "score": 3}]}}
        fetched, items = self._run(listing, details)
        assert fetched == ["https://lobste.rs/s/bbb.json"]
        assert [i.external_id for i in items] == ["c1"]

    def test_a_missing_count_is_still_fetched(self):
        """Skipping on a field the API stopped sending would silently empty
        this source, so only an explicit zero counts."""
        listing = [{"short_id": "aaa"}]
        fetched, _ = self._run(listing, {})
        assert fetched == ["https://lobste.rs/s/aaa.json"]

    def test_a_story_shared_by_two_tags_is_fetched_once(self, monkeypatch):
        monkeypatch.setattr(lobsters, "TAGS", ["programming", "rust"])
        listing = [{"short_id": "aaa", "comment_count": 4}]
        fetched, _ = self._run(listing, {})
        assert fetched == ["https://lobste.rs/s/aaa.json"]


class TestGithubTimestamps:
    def test_z_suffix_is_utc_not_local(self):
        from datetime import datetime, timezone
        expected = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
        assert github._ts("2026-08-16T12:00:00Z") == int(expected.timestamp())

    def test_unparseable_returns_none_so_the_caller_can_fall_back(self):
        assert github._ts("") is None
        assert github._ts("garbage") is None


class TestStore:
    def _db(self, tmp_path, monkeypatch):
        import pipeline.config as cfg
        import pipeline.db as db
        monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t.db")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
        for name in ("IDEAS_DIR", "DECKS_DIR", "REJECTS_DIR"):
            monkeypatch.setattr(cfg, name, tmp_path / name.lower())

    def test_counts_only_rows_that_were_new(self, tmp_path, monkeypatch):
        self._db(tmp_path, monkeypatch)
        batch = [gate(_item(external_id="1")), gate(_item(external_id="2"))]
        assert store(batch) == 2
        assert store(batch) == 0, "re-harvesting the same items adds nothing"
        assert store(batch + [gate(_item(external_id="3"))]) == 1

    def test_empty_batch_is_free(self, tmp_path, monkeypatch):
        self._db(tmp_path, monkeypatch)
        assert store([]) == 0
