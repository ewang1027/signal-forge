"""Gate behaviour. Cases here are drawn from things that actually went wrong."""

import numpy as np

from pipeline.gate import check_shape
from pipeline.ledger import _pack, _unpack, idea_text
from pipeline.priorart import queries_for


def _idea(**over):
    base = {
        "title": "a thing", "one_liner": "does a thing",
        "problem": "things are hard", "why_hard": "the thing is hard",
        "first_weekend": "build the smallest thing",
        "milestones": ["w2: more", "w4: even more"],
    }
    base.update(over)
    return base


class TestShapeGate:
    def test_well_formed_passes(self):
        assert check_shape(_idea()).ok

    def test_missing_field_rejected(self):
        assert not check_shape(_idea(why_hard="")).ok

    def test_thin_milestones_rejected(self):
        assert not check_shape(_idea(milestones=["only one"])).ok

    def test_reports_which_fields(self):
        v = check_shape(_idea(title="", problem=""))
        assert "title" in v.reason and "problem" in v.reason


class TestLedgerVectors:
    def test_pack_roundtrip(self):
        vec = np.random.rand(384).astype(np.float32)
        assert np.allclose(_unpack(_pack(vec)), vec, atol=1e-6)

    def test_idea_text_uses_problem_framing_not_milestones(self):
        # two implementations of the same problem should compare as similar,
        # so milestone text must not dilute the comparison
        text = idea_text(_idea())
        assert "things are hard" in text
        assert "w2: more" not in text


class TestPriorArtQueries:
    def test_urls_do_not_leak_vocabulary(self):
        """Queries once came out as 'raft https failover' and 'k8s news
        failover' because URLs in the prose were being tokenised."""
        idea = _idea(
            problem="see https://news.ycombinator.com/item?id=123 for the raft bug",
            why_hard="kubernetes failover is hard",
        )
        for q in queries_for(idea):
            for junk in ("https", "news", "com", "item"):
                assert junk not in q.split(), f"{junk!r} leaked into {q!r}"

    def test_uses_technical_vocabulary(self):
        idea = _idea(
            title="a scheduler",
            one_liner="kubernetes container placement with gossip",
            problem="raft consensus is overkill for small clusters",
            why_hard="quorum under partition",
        )
        qs = queries_for(idea)
        assert qs, "should produce at least one query"
        joined = " ".join(qs)
        assert any(t in joined for t in ("kubernetes", "gossip", "raft", "quorum"))

    def test_queries_are_short(self):
        # GitHub ANDs terms; long queries match nothing
        idea = _idea(one_liner="kubernetes gossip raft quorum etcd sharding replication")
        assert all(len(q.split()) <= 3 for q in queries_for(idea))
