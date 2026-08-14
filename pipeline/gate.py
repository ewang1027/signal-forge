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
from .llm import LLMError, complete_json
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
                "first_weekend", "milestones",
                # The plain-language pass is required, not decorative. These
                # ideas are meant to sit above the reader's current level, and
                # one he cannot follow is one he cannot choose -- so an idea
                # that arrives without its ramp is not shippable.
                "in_plain_terms", "why_it_is_hard_plainly"]
    missing = [k for k in required if not idea.get(k)]
    if missing:
        return Verdict(False, "shape", f"missing fields: {', '.join(missing)}")
    milestones = idea.get("milestones")
    # Must be a list: a bare string of length >= 2 passed a len() check and the
    # digest then rendered it one character per <li>.
    if not isinstance(milestones, list) or len(milestones) < 2:
        return Verdict(False, "shape", "milestones must be a list of 2+ items")

    # A glossary of one token term is the model going through the motions. The
    # jargon is exactly what makes these unreadable, so this is the field most
    # worth being strict about.
    glossary = idea.get("glossary")
    if not isinstance(glossary, list) or len(glossary) < 3:
        return Verdict(False, "shape", "glossary must define 3+ terms")
    if any(not (isinstance(t, dict) and t.get("term") and t.get("means"))
           for t in glossary):
        return Verdict(False, "shape", "every glossary entry needs term + means")
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


PASSING_VERDICTS = {"ship", "reframe"}


def check_judged(idea: dict, domain: str | None = None) -> Verdict:
    """Prior art and feasibility, judged against a real repository search."""
    repos, searched_ok = search(idea, domain)
    template = (PROMPTS_DIR / "gate.md").read_text()
    prompt = (template
              .replace("{candidate}", json.dumps(idea, indent=2))
              .replace("{prior_art}", format_for_prompt(repos, searched_ok)))

    result = complete_json(prompt)
    if not isinstance(result, dict):
        return Verdict(False, "judged", "gate returned a non-object")

    verdict = (result.get("verdict") or "").strip().lower()
    reason = result.get("reason", "")

    # Allow-list, not a deny-list. Checking only for "kill" meant an empty
    # object, a null verdict, a truncated response, or the word "reject" all
    # shipped -- a gate whose default on malformed output is *pass* is not a
    # gate.
    if verdict not in PASSING_VERDICTS:
        return Verdict(False, "judged",
                       f"verdict {verdict or '(missing)'!r}: {reason}", result)
    if result.get("feasibility") == "unrealistic":
        return Verdict(False, "judged", f"unrealistic scope: {reason}", result)
    return Verdict(True, "judged", f"{verdict}: {reason}", result)


def apply_judgement(idea: dict, result: dict) -> dict:
    """Fold what the gate learned back into the idea."""
    out = dict(idea)
    if note := result.get("prior_art_note"):
        out["prior_art"] = note
    if closest := result.get("closest_existing"):
        out["closest_existing"] = closest
    if revised := result.get("revised_first_weekend"):
        out["first_weekend"] = revised
    out["feasibility"] = result.get("feasibility", "")
    return out


def critique_of(result: dict) -> str:
    """The gate's objection, as text to hand back for revision."""
    parts = [result.get("reason", "")]
    if r := result.get("reframe"):
        parts.append(f"Sharper version: {r}")
    if p := result.get("prior_art_note"):
        parts.append(f"Prior art: {p}")
    if f := result.get("feasibility"):
        parts.append(f"Scope reads as: {f}")
    return "\n\n".join(p for p in parts if p)


class Withdrawn(RuntimeError):
    """The revision concluded there is no hard core left once the objection is
    honestly accounted for. Better than dressing the same idea up again."""


def revise(idea: dict, result: dict) -> dict:
    """Rewrite an idea so the gate's objection no longer applies.

    A `reframe` verdict means the problem is real but the framing is wrong --
    observed critiques were "the advertised hard core is inflated, weekend 1 is
    mostly plumbing" and "the stated hard core is largely imaginary". Shipping
    that unchanged puts claims in the digest that the pipeline has already
    concluded are false; rejecting it throws away a problem the gate itself
    called real and unserved. Revising is what a person does with good feedback.
    """
    template = (PROMPTS_DIR / "revise.md").read_text()
    prompt = (template
              .replace("{candidate}", json.dumps(idea, indent=2))
              .replace("{critique}", critique_of(result)))

    revised = complete_json(prompt)
    if not isinstance(revised, dict):
        raise Withdrawn("revision returned a non-object")
    if revised.get("withdrawn"):
        raise Withdrawn(revised.get("revision_note", "no hard core survived review"))

    # The revision must not invent citations; evidence is fixed at generation.
    revised["evidence_refs"] = idea.get("evidence_refs", [])
    return revised


def run(conn: sqlite3.Connection, idea: dict, embed_fn,
        domain: str | None = None) -> tuple[bool, dict, list[Verdict]]:
    """Returns (survived, final idea, verdicts in order).

    Nothing ships carrying an objection the pipeline never addressed. A
    `reframe` gets exactly one revision pass and is then judged again; if it
    still does not earn a clean verdict, the candidate is not converging and the
    caller moves on to the next one.
    """
    verdicts: list[Verdict] = []

    for check in (lambda: check_shape(idea),
                  lambda: check_dedup(conn, idea, embed_fn)):
        v = check()
        verdicts.append(v)
        if not v.ok:
            return False, idea, verdicts

    v = check_judged(idea, domain)
    verdicts.append(v)
    if not v.ok:
        return False, idea, verdicts

    if (v.detail.get("verdict") or "").lower() != "reframe":
        return True, apply_judgement(idea, v.detail), verdicts

    # Reframe: revise once, then re-judge the revision.
    try:
        revised = revise(idea, v.detail)
    except Withdrawn as exc:
        verdicts.append(Verdict(False, "revise", f"withdrawn: {exc}"))
        return False, idea, verdicts
    except LLMError as exc:
        verdicts.append(Verdict(False, "revise", f"revision failed: {exc}"))
        return False, idea, verdicts

    note = revised.get("revision_note", "revised")
    verdicts.append(Verdict(True, "revise", note))

    shaped = check_shape(revised)
    verdicts.append(shaped)
    if not shaped.ok:
        return False, idea, verdicts

    v2 = check_judged(revised, domain)
    verdicts.append(v2)
    if not v2.ok:
        return False, idea, verdicts

    if (v2.detail.get("verdict") or "").lower() == "reframe":
        # One pass did not resolve it. Do not keep grinding on a candidate that
        # is not converging -- the next one costs nothing extra.
        verdicts.append(Verdict(False, "revise",
                                "still reframed after revision; not converging"))
        return False, idea, verdicts

    out = apply_judgement(revised, v2.detail)
    out["revision_note"] = note
    return True, out, verdicts
