"""The weekly ideation loop.

One run now walks several rotation slices to fill a digest. What matters here is
the budget: the loop must stop when it has enough, must not spend a model call
on a starved slice, and must not walk the whole rotation burning every domain's
themes in one sitting when the gates happen to be strict.

The slice itself (evidence, generation, gates) is covered by test_gates.
"""

import pytest


@pytest.fixture
def loop(monkeypatch):
    """Drive `main()` over a scripted sequence of slice outcomes."""
    import pipeline.ideate as ideate

    monkeypatch.setattr(ideate, "_embedder", lambda: (lambda texts: []))

    calls = {"n": 0}

    def script(outcomes):
        seq = list(outcomes)

        def fake_slice(_embed_fn):
            calls["n"] += 1
            outcome = seq.pop(0) if seq else "starved"
            return outcome, (f"idea {calls['n']}" if outcome == "shipped" else None)

        monkeypatch.setattr(ideate, "run_slice", fake_slice)
        return ideate

    return script, calls


class TestWeeklyBudget:
    def test_stops_once_the_digest_is_full(self, loop, monkeypatch):
        script, calls = loop
        ideate = script(["shipped"] * 6)
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        assert ideate.main() == 0
        assert calls["n"] == 3, "kept generating after the digest was full"

    def test_rejections_are_retried_up_to_the_ceiling(self, loop, monkeypatch):
        script, calls = loop
        ideate = script(["rejected", "rejected", "shipped", "shipped", "shipped"])
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        monkeypatch.setattr(ideate, "MAX_GENERATIONS", 5)
        assert ideate.main() == 0
        assert calls["n"] == 5

    def test_the_generation_ceiling_is_honoured(self, loop, monkeypatch):
        """Without a ceiling a strict week walks the entire rotation and burns
        every domain's themes at once."""
        script, calls = loop
        ideate = script(["rejected"] * 20)
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        monkeypatch.setattr(ideate, "MAX_GENERATIONS", 5)
        assert ideate.main() == 2
        assert calls["n"] == 5

    def test_starved_slices_are_free(self, loop, monkeypatch):
        """A starved slice makes no model call, so it must not spend budget --
        otherwise a young corpus with a few thin domains cannot fill a digest
        however much evidence the good domains have."""
        script, calls = loop
        ideate = script(["starved", "starved", "starved",
                         "shipped", "shipped", "shipped"])
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        monkeypatch.setattr(ideate, "MAX_GENERATIONS", 5)
        assert ideate.main() == 0
        assert calls["n"] == 6

    def test_an_entirely_starved_rotation_terminates(self, loop, monkeypatch):
        from pipeline.domains import ROTATION
        script, calls = loop
        ideate = script([])            # every slice starved
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        assert ideate.main() == 2
        assert calls["n"] == len(ROTATION), "walked past the end of the rotation"

    def test_nothing_shipped_exits_two(self, loop, monkeypatch):
        """The workflow treats exit 2 as success: a gate whose job is sometimes
        to let nothing through has to be allowed to do that."""
        script, _ = loop
        ideate = script(["rejected"])
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        assert ideate.main() == 2

    def test_a_short_week_still_ships_what_it_has(self, loop, monkeypatch):
        script, _ = loop
        ideate = script(["shipped", "rejected", "rejected", "rejected", "rejected"])
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        monkeypatch.setattr(ideate, "MAX_GENERATIONS", 5)
        assert ideate.main() == 0, "one idea is a digest; zero is not"


class TestOneBadSliceDoesNotSinkTheRun:
    """Each slice commits its own transaction, but an exception escaping main()
    fails the workflow step -- and `commit state` never runs, so the ideas
    already banked are thrown away with it. A revoked token on the last slice
    used to cost the whole week."""

    def test_an_exception_does_not_discard_earlier_ideas(self, monkeypatch):
        import pipeline.ideate as ideate
        monkeypatch.setattr(ideate, "_embedder", lambda: (lambda t: []))
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        monkeypatch.setattr(ideate, "MAX_GENERATIONS", 5)

        seq = ["shipped", "shipped", "boom", "shipped"]
        state = {"i": 0}

        def flaky(_embed_fn):
            outcome = seq[state["i"]]
            state["i"] += 1
            if outcome == "boom":
                raise RuntimeError("claude exited 1: OAuth token has been revoked")
            return outcome, f"idea {state['i']}"

        monkeypatch.setattr(ideate, "run_slice", flaky)
        assert ideate.main() == 0, "a mid-run failure lost the banked ideas"

    def test_every_slice_failing_still_exits_two(self, monkeypatch):
        import pipeline.ideate as ideate
        monkeypatch.setattr(ideate, "_embedder", lambda: (lambda t: []))
        monkeypatch.setattr(ideate, "IDEAS_PER_DIGEST", 3)
        monkeypatch.setattr(ideate, "MAX_GENERATIONS", 3)

        def always_boom(_embed_fn):
            raise RuntimeError("claude exited 1: OAuth token has been revoked")

        monkeypatch.setattr(ideate, "run_slice", always_boom)
        assert ideate.main() == 2, "must not raise; the workflow reads the code"
