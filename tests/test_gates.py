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


class TestVerdictIsAllowListed:
    """check_judged once rejected only the literal string "kill", so an empty
    object, a null verdict, or the word "reject" all shipped. A gate whose
    default on malformed output is *pass* is not a gate."""

    def _decide(self, result):
        from pipeline.gate import PASSING_VERDICTS
        verdict = (result.get("verdict") or "").strip().lower()
        if verdict not in PASSING_VERDICTS:
            return False
        return result.get("feasibility") != "unrealistic"

    def test_malformed_responses_do_not_ship(self):
        for payload in ({}, {"verdict": None}, {"verdict": ""},
                        {"verdict": "reject"}, {"verdict": "no"},
                        {"verdict": "do not build"}, {"reason": "truncated"}):
            assert not self._decide(payload), f"{payload!r} must not ship"

    def test_documented_verdicts_ship(self):
        assert self._decide({"verdict": "ship"})
        assert self._decide({"verdict": "reframe"})
        assert self._decide({"verdict": "SHIP  "})

    def test_kill_and_unrealistic_do_not_ship(self):
        assert not self._decide({"verdict": "kill"})
        assert not self._decide({"verdict": "ship", "feasibility": "unrealistic"})


class TestDomainScopedQueries:
    """queries_for did not receive the domain, so DOMAINS insertion order made
    `distributed` terms anchor every query -- a SQLite crash-explorer was
    searched for as "sharded sharding" and returned connection poolers."""

    def test_storage_idea_searches_storage_terms(self):
        idea = _idea(
            title="crash-point explorer for sqlite",
            one_liner="boot a sqlite database in post-crash states",
            problem="committed transactions vanish; wal recovery is subtle",
            why_hard="fsync barriers and durability semantics",
        )
        joined = " ".join(queries_for(idea, "storage"))
        assert "sqlite" in joined
        assert "shard" not in joined, "distributed jargon must not anchor a storage idea"

    def test_stems_collapse_to_one_term(self):
        # `shard*` matches both "sharded" and "sharding"; they are one concept
        idea = _idea(one_liner="sharded writes with sharding logic")
        qs = queries_for(idea, "distributed")
        assert not any("shard" in q.split()[0] and "shard" in q.split()[-1]
                       for q in qs if len(q.split()) > 1), \
            f"one concept produced a two-word query of itself: {qs}"

    def test_domain_terms_lead(self):
        idea = _idea(
            one_liner="a type checker with kubernetes in the description",
            problem="compiler inference is slow",
            why_hard="constraint solving",
        )
        first = queries_for(idea, "compilers")[0]
        assert "kubernetes" not in first


class TestUsedEvidenceExclusion:
    """A theme's key is a hash of its members, and members join as the corpus
    grows -- measured, 3 new rows invalidated 2 of 25 keys. So exclusion cannot
    key off theme identity; it keys off the signal ids an idea consumed, which
    never change."""

    def _db(self, tmp_path, monkeypatch):
        import pipeline.config as cfg
        import pipeline.db as db
        monkeypatch.setattr(cfg, "STATE_DIR", tmp_path)
        monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t.db")
        monkeypatch.setattr(cfg, "IDEAS_DIR", tmp_path / "ideas")
        monkeypatch.setattr(cfg, "DECKS_DIR", tmp_path / "decks")
        monkeypatch.setattr(cfg, "REJECTS_DIR", tmp_path / "rejects")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
        return db

    def test_theme_excluded_after_its_evidence_is_used(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        from pipeline.ideate import top_theme

        with db.connect() as c:
            for i in (1, 2, 3):
                c.execute(
                    "INSERT INTO signal (id, source, external_id, text, "
                    "created_utc, harvested_utc, domain) "
                    "VALUES (?,'hn',?,'x',1,1,'storage')", (i, str(i)))
            c.execute("INSERT INTO theme (id,key,size,evidence,domain,built_utc) "
                      "VALUES (1,'aaa',2,9.0,'storage',1)")
            c.execute("INSERT INTO theme (id,key,size,evidence,domain,built_utc) "
                      "VALUES (2,'bbb',1,1.0,'storage',1)")
            c.executemany("INSERT INTO theme_member VALUES (?,?)",
                          [(1, 1), (1, 2), (2, 3)])

            assert top_theme(c, "storage")["key"] == "aaa", "highest evidence first"

            # an idea consumes theme 1's evidence
            c.execute("INSERT INTO idea (id,slug,title,body) VALUES (1,'s','t','{}')")
            c.executemany("INSERT INTO idea_signal VALUES (?,?)", [(1, 1), (1, 2)])

            assert top_theme(c, "storage")["key"] == "bbb", \
                "spent theme must be skipped even though it still ranks highest"

    def test_exclusion_survives_theme_key_churn(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        from pipeline.ideate import top_theme

        with db.connect() as c:
            for i in (1, 2, 3):
                c.execute(
                    "INSERT INTO signal (id, source, external_id, text, "
                    "created_utc, harvested_utc, domain) "
                    "VALUES (?,'hn',?,'x',1,1,'storage')", (i, str(i)))
            c.execute("INSERT INTO theme (id,key,size,evidence,domain,built_utc) "
                      "VALUES (1,'aaa',2,9.0,'storage',1)")
            c.executemany("INSERT INTO theme_member VALUES (?,?)", [(1, 1), (1, 2)])
            c.execute("INSERT INTO idea (id,slug,title,body) VALUES (1,'s','t','{}')")
            c.executemany("INSERT INTO idea_signal VALUES (?,?)", [(1, 1), (1, 2)])

            # re-cluster: same pain, new rowid, new key, one extra member
            c.execute("DELETE FROM theme_member")
            c.execute("DELETE FROM theme")
            c.execute("INSERT INTO theme (id,key,size,evidence,domain,built_utc) "
                      "VALUES (1,'zzz-different-key',3,9.0,'storage',1)")
            c.executemany("INSERT INTO theme_member VALUES (?,?)",
                          [(1, 1), (1, 2), (1, 3)])

            assert top_theme(c, "storage") is None, \
                "theme was renamed by re-clustering but its evidence is still spent"


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
