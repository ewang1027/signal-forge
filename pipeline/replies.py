"""Parsing what comes back in an email reply.

Kept separate from fetching and applying so it can be tested without a mailbox.

The parser is deliberately forgiving. This gets typed on a phone, one-handed,
probably while doing something else -- if it demands exact syntax it will be
used twice and then never again, and an unused feedback loop is the same as no
feedback loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Verdicts on an idea. Aliases exist because nobody remembers the vocabulary.
IDEA_VERDICTS: dict[str, str] = {
    "more": "more", "yes": "more", "good": "more", "cool": "more",
    "boring": "boring", "meh": "boring", "no": "boring", "dull": "boring",
    "exists": "exists", "done": "exists", "built": "exists",
    "too easy": "too_easy", "easy": "too_easy", "trivial": "too_easy",
    "too hard": "too_hard", "huge": "too_hard",
    "building": "building", "building this": "building", "doing this": "building",
}

# Card grades. FSRS ratings plus the words people actually type.
GRADES: dict[str, str] = {
    "again": "again", "fail": "again", "failed": "again", "nope": "again",
    "hard": "hard", "struggled": "hard",
    "good": "good", "ok": "good", "got it": "good", "solved": "good",
    "easy": "easy", "trivial": "easy",
    "skip": "skip",
}

# Where a mail client starts quoting the message being replied to. Everything
# from here down is our own text coming back, and parsing it would apply the
# email's own instructions as if they were the reply.
_QUOTE_MARKERS = [
    re.compile(r"^\s*>", re.M),
    re.compile(r"^\s*On .+ wrote:\s*$", re.M | re.I),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.M | re.I),
    re.compile(r"^\s*_{10,}\s*$", re.M),
    re.compile(r"^\s*From:\s.+$", re.M),
    re.compile(r"^\s*Sent from my \w+", re.M | re.I),
]

_TIME_RE = re.compile(r"\b(\d{1,3})\s*(?:m|min|mins|minutes)\b", re.I)


@dataclass
class Reply:
    idea_verdict: str | None = None
    grades: list[tuple[str, str]] = field(default_factory=list)  # (card_id, grade)
    minutes: int | None = None
    note: str = ""

    def is_empty(self) -> bool:
        return not (self.idea_verdict or self.grades or self.minutes)


def strip_quoted(body: str) -> str:
    """Keep only what the person actually typed."""
    cut = len(body)
    for rx in _QUOTE_MARKERS:
        if m := rx.search(body):
            cut = min(cut, m.start())
    return body[:cut].strip()


def _longest_first(keys) -> list[str]:
    # "too easy" must be tried before "easy", or the two-word form never matches
    return sorted(keys, key=len, reverse=True)


def parse(body: str, known_cards: set[str] | None = None) -> Reply:
    """Pull structure out of a free-text reply.

    `known_cards` scopes card-id matching to real ids so an ordinary sentence
    cannot be mistaken for a grade.
    """
    text = strip_quoted(body)
    out = Reply(note=text[:500])
    if not text:
        return out

    lowered = text.lower()
    cards = known_cards or set()

    # Per-line first: a line naming a card and a grade is unambiguous.
    graded: set[str] = set()
    for line in lowered.splitlines():
        line = line.strip()
        if not line:
            continue
        hit = next((c for c in cards if c in line), None)
        if not hit or hit in graded:
            continue
        for word in _longest_first(GRADES):
            if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", line):
                out.grades.append((hit, GRADES[word]))
                graded.add(hit)
                break

    # Then the whole message for an idea verdict. Skip words already consumed as
    # a card grade so "two-pointers good" does not also read as "more".
    consumed = {ln for ln in lowered.splitlines()
                if any(c in ln for c in graded)}
    remainder = "\n".join(ln for ln in lowered.splitlines() if ln not in consumed)

    for word in _longest_first(IDEA_VERDICTS):
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", remainder):
            out.idea_verdict = IDEA_VERDICTS[word]
            break

    if m := _TIME_RE.search(text):
        out.minutes = int(m.group(1))

    return out
