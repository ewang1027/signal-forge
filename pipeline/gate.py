"""Stage 4 -- the anti-slop gates.

Ordered by cost. Dedup is local arithmetic and runs first; the model call that
judges prior art and feasibility runs last, only on a candidate that survived
everything cheaper. Nothing here scores novelty: LLM-judged novelty
anti-correlates with what turns out to matter, so the model is asked what
already exists (a fact, checkable) rather than how novel something feels.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .config import PROMPTS_DIR
from .ledger import DUPLICATE_AT, idea_text, nearest
from .llm import complete_json
from .priorart import format_for_prompt, search


@dataclass
class Verdict:
    ok: bool
    stage: str
    reason: str
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        mark = "pass" if self.ok else "REJECT"
        return f"[{mark}] {self.stage}: {self.reason}"


def check_shape(idea: dict) -> Verdict:
    """The model occasionally returns a plausible-looking object with a missing
    or empty field. Catch it here rather than rendering a digest with a blank
    section in it."""
    required = ["title", "one_liner", "problem", "why_hard",
                "first_weekend", "milestones"]
    missing = [k for k in required if not idea.get(k)]
    if missing:
        return Verdict(False, "shape", f"missing fields: {', '.join(missing)}")
    if len(idea.get("milestones") or []) < 2:
        return Verdict(False, "shape", "fewer than 2 milestones")
    return Verdict(True, "shape", "well formed")


def check_dedup(conn: sqlite3.Connection, idea: dict, embed_fn) -> Verdict:
    """Against every idea ever generated, not just those in the DB."""
    text = idea_text(idea)
    if not text:
        return Verdict(False, "dedup", "no comparable text")

    vec = embed_fn([text])[0]
    sim, title = nearest(conn, vec)
    if sim >= DUPLICATE_AT:
        return Verdict(False, "dedup", f"{sim:.2f} similar to {title!r}",
                       {"similarity": sim, "matched": title})
    return Verdict(True, "dedup", f"nearest past idea {sim:.2f} ({title[:40]!r})",
                   {"similarity": sim, "matched": title})


def check_judged(idea: dict) -> Verdict:
    """Prior art and feasibility, judged against a real repository search."""
    repos = search(idea)
    template = (PROMPTS_DIR / "gate.md").read_text()
    prompt = (template
              .replace("{candidate}", json.dumps(idea, indent=2))
              .replace("{prior_art}", format_for_prompt(repos)))

    result = complete_json(prompt)
    verdict = (result.get("verdict") or "").lower()
    reason = result.get("reason", "")

    if verdict == "kill":
        return Verdict(False, "judged", reason, result)
    if result.get("feasibility") == "unrealistic":
        return Verdict(False, "judged", f"unrealistic scope: {reason}", result)
    return Verdict(True, "judged", f"{verdict or 'ship'}: {reason}", result)


def apply_judgement(idea: dict, result: dict) -> dict:
    """Fold what the gate learned back into the idea.

    A reframe is the interesting case: the evidence showed a real problem, so
    the idea survives, but pointed at the gap the existing tools leave rather
    than at the whole space.
    """
    out = dict(idea)
    if note := result.get("prior_art_note"):
        out["prior_art"] = note
    if closest := result.get("closest_existing"):
        out["closest_existing"] = closest
    if revised := result.get("revised_first_weekend"):
        out["first_weekend"] = revised
    if result.get("verdict") == "reframe" and result.get("reframe"):
        out["reframe"] = result["reframe"]
    out["feasibility"] = result.get("feasibility", "")
    return out


def run(conn: sqlite3.Connection, idea: dict, embed_fn) -> tuple[bool, dict, list[Verdict]]:
    """Returns (survived, possibly-revised idea, verdicts in order)."""
    verdicts: list[Verdict] = []

    for check in (lambda: check_shape(idea),
                  lambda: check_dedup(conn, idea, embed_fn)):
        v = check()
        verdicts.append(v)
        if not v.ok:
            return False, idea, verdicts

    v = check_judged(idea)
    verdicts.append(v)
    if not v.ok:
        return False, idea, verdicts

    return True, apply_judgement(idea, v.detail), verdicts
