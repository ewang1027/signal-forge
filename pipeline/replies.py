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
    # A verdict with no idea named. Unambiguous only when the digest carried
    # exactly one idea; the caller decides, because only it knows what went out.
    idea_verdict: str | None = None
    # (reply_id, verdict) -- a verdict that named the idea it is about. With
    # two or three ideas per digest this is the form that can actually be
    # applied, since guessing would move the wrong idea's evidence weights.
    idea_verdicts: list[tuple[str, str]] = field(default_factory=list)
    grades: list[tuple[str, str]] = field(default_factory=list)  # (card_id, grade)
    minutes: int | None = None
    note: str = ""
    unparsed: bool = False   # refused, not merely empty
    control: str | None = None   # pause | resume

    def is_empty(self) -> bool:
        return not (self.idea_verdict or self.idea_verdicts or self.grades
                    or self.minutes or self.control)


# The digest prints this line just above its own footer. Cutting on an explicit
# sentinel is far more reliable than guessing at every mail client's quoting
# style -- Apple Mail wraps the attribution, Outlook sometimes puts the reply
# below the quote, and some clients send no plain-text part at all.
SENTINEL = "--- reply above this line ---"

# Phrases that only ever appear in OUR text. If they survive quote-stripping,
# the strip failed and we are about to parse our own email as feedback -- which
# would grade the whole deck off a one-word reply.
_OUR_TEXT = ("reply above this line", "name it first", "one per line",
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


def _spans(line: str, terms: set[str]) -> list[tuple[int, int, str]]:
    """Every term in the line, as (start, end, term), left to right.

    Longest first so `dp-intervals` claims its span before `intervals` can.
    Iterating a set was worse than ambiguous -- set order is hash-randomised per
    process, so `dp-intervals hard` graded a different card depending on the run.
    """
    found: list[tuple[int, int, str]] = []
    claimed: list[tuple[int, int]] = []
    for term in _longest_first(terms):
        for m in re.finditer(_word(term), line):
            if any(s < m.end() and m.start() < e for s, e in claimed):
                continue
            found.append((m.start(), m.end(), term))
            claimed.append((m.start(), m.end()))
    return sorted(found)


def _first_word_of(segment: str, vocab: dict[str, str]) -> str | None:
    best: tuple[int, str] | None = None
    for word in _longest_first(vocab):
        if m := re.search(_word(word), segment):
            if best is None or m.start() < best[0]:
                best = (m.start(), vocab[word])
    return best[1] if best else None


def _bind(line: str, terms: set[str], vocab: dict[str, str]) -> list[tuple[str, str]]:
    """Bind each term to the vocabulary word that follows it, not to any word on
    the line.

    Scanning the whole line for one grade meant `two-pointers good, dijkstra
    again` recorded `two-pointers again` -- the user said good and FSRS logged a
    lapse. Each term owns the text from its own end to the next term's start.
    """
    spans = _spans(line, terms)
    out: list[tuple[str, str]] = []
    for i, (_, end, term) in enumerate(spans):
        stop = spans[i + 1][0] if i + 1 < len(spans) else len(line)
        if hit := _first_word_of(line[end:stop], vocab):
            out.append((term, hit))
    return out


def parse(body: str, known_cards: set[str] | None = None,
          known_ideas: set[str] | None = None) -> Reply:
    """Pull structure out of a free-text reply.

    `known_cards` and `known_ideas` scope matching to handles that were actually
    printed in a recent digest, so an ordinary sentence cannot be mistaken for a
    grade or a verdict.
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
    ideas = known_ideas or set()

    graded: set[str] = set()
    judged: set[str] = set()
    plain: list[str] = []          # lines that matched neither a card nor an idea
    for line in lowered.splitlines():
        line = line.strip()
        if not line:
            continue
        found = [(c, g) for c, g in _bind(line, cards, GRADES) if c not in graded]
        if found:
            for cid, grade in found:
                out.grades.append((cid, grade))
                graded.add(cid)
            continue
        # `good` is both a grade and an approval, so cards are resolved first:
        # `two-pointers good` is a card grade, `monoblame good` is a verdict.
        named = [(i, v) for i, v in _bind(line, ideas, IDEA_VERDICTS)
                 if i not in judged]
        if named:
            for handle, verdict in named:
                out.idea_verdicts.append((handle, verdict))
                judged.add(handle)
            continue
        plain.append(line)

    # A bare verdict is read only from lines that matched nothing, so
    # "two-pointers good" is not also an approval of an idea. It stays separate
    # from the named ones: with several ideas in a digest the caller has to
    # decide whether it can be attributed at all.
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
