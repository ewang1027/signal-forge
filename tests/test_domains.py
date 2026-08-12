"""These exist because the first version of the lexicon used naive substring
matching, which silently classified an article about advertising as `compilers`
(via "past" -> "ast") and anything mentioning a hotel as `observability`
(via "hotel" -> "otel"). Cheap tests, expensive bug.
"""

from pipeline.domains import best_domain, is_noise_thread, pain_score, score


class TestNoSubstringFalsePositives:
    def test_short_acronyms_need_word_boundaries(self):
        text = "in the past the vast contrast of last week broadcast slowly"
        assert sum(score(text).values()) == 0

    def test_hotel_is_not_otel(self):
        assert score("we stayed at a hotel")["observability"] == 0

    def test_ability_is_not_abi(self):
        assert score("the ability to do this")["compilers"] == 0

    def test_slow_is_not_slo(self):
        assert score("the response was slow")["observability"] == 0

    def test_draft_is_not_raft(self):
        assert score("a draft proposal")["distributed"] == 0


class TestStemsStillMatch:
    def test_stem_matches_suffixes(self):
        for word in ("quantize", "quantization", "quantized"):
            assert score(f"we tried {word}")["ml-infra"] == 1

    def test_plural_stem(self):
        assert score("sharding across shards")["distributed"] >= 1


class TestDomainClassification:
    def test_distributed(self):
        d, n = best_domain("our raft implementation hit split brain in a network partition")
        assert d == "distributed" and n >= 2

    def test_observability(self):
        d, n = best_domain("opentelemetry cardinality blew up the datadog bill")
        assert d == "observability" and n >= 2

    def test_unrelated_text_has_no_domain(self):
        assert best_domain("the weather was nice and we ate lunch") == (None, 0)


class TestPain:
    def test_complaint_scores(self):
        assert pain_score("this is painful, we had to build a workaround, it was flaky") >= 3

    def test_neutral_text_scores_zero(self):
        assert pain_score("we deployed the service on tuesday and it worked") == 0

    def test_keyword_dump_has_no_pain(self):
        # the exact failure mode that motivated the second axis: domain-dense,
        # complaint-free text from a resume or a list of nouns
        dump = "cache stack heap process thread socket tcp http tls deadlock queue"
        assert score(dump)["networking"] > 0
        assert pain_score(dump) == 0


class TestNoiseThreads:
    def test_hiring_threads_rejected(self):
        assert is_noise_thread("Ask HN: Who wants to be hired? (August 2026)")
        assert is_noise_thread("Ask HN: Who is hiring?")

    def test_normal_title_kept(self):
        assert not is_noise_thread("Making 768 servers look like 1")
