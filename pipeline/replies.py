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
    unparsed: bool = False   # refused, not merely empty
    control: str | None = None   # pause | resume

    def is_empty(self) -> bool:
        return not (self.idea_verdict or self.grades or self.minutes or self.control)


# The digest prints this line just above its own footer. Cutting on an explicit
# sentinel is far more reliable than guessing at every mail client's quoting
# style -- Apple Mail wraps the attribution, Outlook sometimes puts the reply
# below the quote, and some clients send no plain-text part at all.
SENTINEL = "--- reply above this line ---"

# Phrases that only ever appear in OUR text. If they survive quote-stripping,
# the strip failed and we are about to parse our own email as feedback -- which
# would grade the whole deck off a one-word reply.
_OUR_TEXT = ("reply above this line", "on the idea:", "one per line",
             "timed problems", "patterns due", "sit down and learn")


def strip_quoted(body: str) -> str:
    """Keep only what the person actually typed."""
    if (i := body.find(SENTINEL)) != -1:
        return body[:i].strip()
    cut = len(body)
    for rx in _QUOTE_MARKERS:
        if m := rx.search(body):
            cut = min(cut, m.start())
    return body[:cut].strip()


def looks_like_our_own_email(text: str) -> bool:
    low = text.lower()
    return sum(1 for p in _OUR_TEXT if p in low) >= 2


def _longest_first(keys) -> list[str]:
    # "too easy" must be tried before "easy", or the two-word form never matches
    return sorted(keys, key=len, reverse=True)


def _word(term: str) -> str:
    """Match `term` as a whole token. Hyphens count as word characters here, so
    `intervals` cannot match inside `dp-intervals` and `trie` cannot match
    inside `retried`."""
    return rf"(?<![a-z0-9-]){re.escape(term)}(?![a-z0-9-])"


def _card_spans(line: str, cards: set[str]) -> list[tuple[int, int, str]]:
    """Every card id in the line, as (start, end, id), left to right.

    Longest id first so `dp-intervals` claims its span before `intervals` can.
    Iterating a set was worse than ambiguous -- set order is hash-randomised per
    process, so `dp-intervals hard` graded a different card depending on the run.
    """
    found: list[tuple[int, int, str]] = []
    claimed: list[tuple[int, int]] = []
    for cid in _longest_first(cards):
        for m in re.finditer(_word(cid), line):
            if any(s < m.end() and m.start() < e for s, e in claimed):
                continue
            found.append((m.start(), m.end(), cid))
            claimed.append((m.start(), m.end()))
    return sorted(found)


def _first_grade(segment: str) -> str | None:
    best: tuple[int, str] | None = None
    for word in _longest_first(GRADES):
        if m := re.search(_word(word), segment):
            if best is None or m.start() < best[0]:
                best = (m.start(), GRADES[word])
    return best[1] if best else None


def _grades_in_line(line: str, cards: set[str]) -> list[tuple[str, str]]:
    """Bind each card to the grade that follows it, not to any grade on the line.

    Scanning the whole line for one grade meant `two-pointers good, dijkstra
    again` recorded `two-pointers again` -- the user said good and FSRS logged a
    lapse. Each card owns the text from its own end to the next card's start.
    """
    spans = _card_spans(line, cards)
    out: list[tuple[str, str]] = []
    for i, (_, end, cid) in enumerate(spans):
        stop = spans[i + 1][0] if i + 1 < len(spans) else len(line)
        if grade := _first_grade(line[end:stop]):
            out.append((cid, grade))
    return out


def parse(body: str, known_cards: set[str] | None = None) -> Reply:
    """Pull structure out of a free-text reply.

    `known_cards` scopes card-id matching to real ids so an ordinary sentence
    cannot be mistaken for a grade.
    """
    text = strip_quoted(body)
    out = Reply(note=text[:500])
    if not text:
        return out

    # Belt and braces: if our own footer survived the strip, refuse rather than
    # apply the instructions we ourselves printed.
    if looks_like_our_own_email(text):
        out.note = "(quote-stripping failed; reply not parsed)"
        out.unparsed = True
        return out

    lowered = text.lower()
    cards = known_cards or set()

    graded: set[str] = set()
    plain: list[str] = []          # lines that graded nothing
    for line in lowered.splitlines():
        line = line.strip()
        if not line:
            continue
        found = [(c, g) for c, g in _grades_in_line(line, cards) if c not in graded]
        if found:
            for cid, grade in found:
                out.grades.append((cid, grade))
                graded.add(cid)
        else:
            plain.append(line)

    # An idea verdict is read only from lines that graded nothing, so
    # "two-pointers good" is not also an approval of the idea.
    remainder = "\n".join(plain)
    for word in _longest_first(IDEA_VERDICTS):
        if re.search(_word(word), remainder):
            out.idea_verdict = IDEA_VERDICTS[word]
            break

    # The canary tells you to reply `pause`, so it has to actually do something.
    for word, action in (("pause", "pause"), ("stop ideas", "pause"),
                         ("resume", "resume"), ("unpause", "resume")):
        if re.search(_word(word), lowered):
            out.control = action
            break

    if m := _TIME_RE.search(text):
        out.minutes = int(m.group(1))

    return out
