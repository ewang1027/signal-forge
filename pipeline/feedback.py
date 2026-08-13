"""Stage 7 -- inbound replies become theme weights, card grades, and taste.

A push-only system gets ignored within a month. This is the loop that makes it
compound instead of decay, and it is also what makes the prep track
self-driving: without it, nothing ever calls `prep.grade`.

Delivery is IMAP polling rather than a webhook. A webhook needs a public
endpoint and something running to receive it; this system is a cron job with no
server, and IMAP is in the standard library.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from html import unescape

from fsrs import Rating

from . import prep, taste
from .config import DIGEST_TO
from .db import connect, get_kv, set_kv
from .replies import parse

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.environ.get("IMAP_USER", DIGEST_TO)
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
IMAP_TIMEOUT = int(os.environ.get("IMAP_TIMEOUT", "30"))

# How far a single verdict moves the weight of the evidence behind an idea.
#
# Balanced so the geometric mean sits at ~1.0: the first cut averaged 0.84, so
# an "average" verdict quietly punished the evidence and the dial only ever
# drifted down. `exists` and `too_hard` are near-neutral on purpose -- they
# judge the IDEA, not whether the underlying complaint is real, and a
# well-corroborated pain point should not become worse evidence because a bad
# idea was written about it. (`exists` is the prior-art gate's problem.)
WEIGHT_STEP: dict[str, float] = {
    "building": 1.8,   # the strongest possible signal: it got built
    "more": 1.3,
    "too_hard": 1.0,
    "exists": 0.95,
    "too_easy": 0.85,
    "boring": 0.6,
}

WEIGHT_FLOOR, WEIGHT_CEIL = 0.1, 4.0

RATINGS = {"again": Rating.Again, "hard": Rating.Hard,
           "good": Rating.Good, "easy": Rating.Easy}


def _decode(raw) -> str:
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return str(raw or "")


@dataclass
class Inbound:
    uid: bytes
    message_id: str
    subject: str
    body: str


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(text)


def _body_of(msg: email.message.Message) -> str:
    """Plain text if present, otherwise flattened HTML.

    Some clients send HTML only. Returning "" for those meant the reply was
    marked read, recorded as processed, and silently discarded.
    """
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True) or b""
        text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
        return _html_to_text(text) if msg.get_content_type() == "text/html" else text

    html_fallback = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain":
            return text
        html_fallback = html_fallback or _html_to_text(text)
    return html_fallback


def _open() -> imaplib.IMAP4_SSL:
    # Without a timeout a hung server blocks the whole scheduled run.
    conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_TIMEOUT)
    conn.login(IMAP_USER, IMAP_PASSWORD)
    return conn


def fetch() -> list[Inbound]:
    """Unread replies. Does NOT mark them read -- see `mark_seen`.

    Marking inside the fetch put the flag before the DB commit, so any failure
    while applying rolled back the work and left the messages read: lost with
    no trace, and the `feedback` dedup table never even got a row. Reading with
    BODY.PEEK and flagging only after a successful commit makes the run
    at-least-once, which is what the dedup table is there to absorb.
    """
    if not IMAP_PASSWORD:
        print("IMAP_PASSWORD unset; skipping fetch", file=sys.stderr)
        return []

    out: list[Inbound] = []
    conn = _open()
    try:
        conn.select(IMAP_FOLDER)
        typ, data = conn.uid("SEARCH", None, "UNSEEN", "SUBJECT", '"Re:"')
        if typ != "OK":
            return []
        for uid in (data[0].split() if data and data[0] else []):
            typ, raw = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            body = _body_of(msg)
            mid = _decode(msg.get("Message-ID"))
            if not mid:
                # Sequence numbers restart every session, so they collide across
                # runs. Hash the content instead.
                mid = "sha:" + hashlib.sha1(
                    (_decode(msg.get("Date")) + body).encode()).hexdigest()[:24]
            out.append(Inbound(uid, mid, _decode(msg.get("Subject")), body))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


def mark_seen(uids: list[bytes]) -> None:
    """Flag messages read, after their effects are safely committed."""
    if not uids or not IMAP_PASSWORD:
        return
    conn = _open()
    try:
        conn.select(IMAP_FOLDER)
        for uid in uids:
            conn.uid("STORE", uid, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def adjust_theme_weight(conn: sqlite3.Connection, idea_id: int, factor: float) -> int:
    """Scale the weight of the evidence an idea was built from.

    Weight lives on signals, not themes: themes are rebuilt with fresh
    identities on every run, so anything stored on them is erased. Signal ids
    are permanent, and a theme's weight is derived from its members.
    """
    rows = conn.execute(
        "SELECT signal_id FROM idea_signal WHERE idea_id = ?", (idea_id,)
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE signal SET weight = MAX(?, MIN(?, weight * ?)) WHERE id = ?",
            (WEIGHT_FLOOR, WEIGHT_CEIL, factor, r["signal_id"]),
        )
    return len(rows)


_RE_PREFIX = re.compile(r"^\s*((re|fwd?|aw|sv|antw)\s*:\s*)+", re.I)


def idea_for(conn: sqlite3.Connection, subject: str) -> sqlite3.Row | None:
    """Match the reply to the idea it is actually about.

    The subject carries the idea's title -- `deliver` uses it verbatim. Ideas go
    out Mon and Thu, so a Thursday-evening reply to Monday's digest would
    otherwise put its verdict, its weight change and its taste line on the wrong
    idea entirely.
    """
    stripped = _RE_PREFIX.sub("", subject or "").strip()
    if stripped:
        row = conn.execute(
            "SELECT * FROM idea WHERE sent_utc IS NOT NULL AND title = ? "
            "ORDER BY sent_utc DESC LIMIT 1", (stripped,)
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM idea WHERE sent_utc IS NOT NULL "
        "ORDER BY sent_utc DESC LIMIT 1"
    ).fetchone()


def apply(conn: sqlite3.Connection, body: str, subject: str = "") -> dict:
    """Apply one reply. Returns what changed."""
    decks = prep.all_cards()
    known = {c["id"] for cards in decks.values() for c in cards}
    reply = parse(body, known)
    did: dict = {"grades": [], "verdict": None, "signals": 0}

    if reply.unparsed:
        did["unparsed"] = True
        set_kv(conn, "last_reply_utc", str(int(time.time())))
        return did

    if reply.control:
        set_kv(conn, "ideas_paused", "1" if reply.control == "pause" else "0")
        did["control"] = reply.control

    prep.ensure_cards(conn)  # a grade must not vanish because prep never ran
    for card_id, grade_word in reply.grades:
        if grade_word == "skip":
            continue
        deck = next((d for d, cards in decks.items()
                     if any(c["id"] == card_id for c in cards)), None)
        if deck and prep.grade(conn, deck, card_id, RATINGS[grade_word]):
            did["grades"].append((deck, card_id, grade_word))

    if reply.idea_verdict:
        idea = idea_for(conn, subject)
        if idea is not None:
            # Only the first verdict counts. Replying twice used to compound the
            # weights -- `boring` then `building` left them at 0.8, not 1.6.
            changed = conn.execute(
                "UPDATE idea SET verdict = ?, verdict_utc = ? "
                "WHERE id = ? AND verdict IS NULL",
                (reply.idea_verdict, int(time.time()), idea["id"]),
            ).rowcount
            if changed:
                factor = WEIGHT_STEP.get(reply.idea_verdict, 1.0)
                did["signals"] = adjust_theme_weight(conn, idea["id"], factor)
                did["verdict"] = reply.idea_verdict
                taste.record(reply.idea_verdict, idea["title"],
                             reply.note if reply.note != reply.idea_verdict else "")
            else:
                did["verdict"] = f"{reply.idea_verdict} (already answered)"

    # Stamped on any received message, not only a parsed one. "sorry, been
    # swamped" proves the human is there, and the canary is about silence.
    set_kv(conn, "last_reply_utc", str(int(time.time())))
    return did


# --- quality canary -------------------------------------------------------
#
# A system that has become irrelevant looks exactly like a system that is
# working, from the inside. The only observable difference is that nobody
# replies any more, so that is what gets watched.

SILENCE_DAYS = int(os.environ.get("CANARY_SILENCE_DAYS", "14"))
SILENCE_IDEAS = int(os.environ.get("CANARY_SILENCE_IDEAS", "4"))


def canary(conn: sqlite3.Connection, *, mark: bool = False) -> str | None:
    """Returns a message to append to the next digest, or None.

    `mark` is opt-in because this used to record itself as fired on every call,
    including `--dry-run`. Rendering a test digest therefore silenced the one
    alert whose entire job is noticing the system has gone stale -- and it
    silenced it permanently, with no output saying so.
    """
    last = int(get_kv(conn, "last_reply_utc", "0"))
    now = int(time.time())

    # Count only what has gone unanswered SINCE the last reply. Counting all
    # time reported a lifetime backlog -- "99 ideas have gone out unanswered"
    # after a year, most of them from before a reply that did arrive.
    since = last
    if not since:
        # Never replied: measure from the first send, not from the epoch. The
        # old fallback invented a fixed number and told a 9-day-old install
        # nothing had come back in 15 days.
        row = conn.execute(
            "SELECT MIN(sent_utc) t FROM idea WHERE sent_utc IS NOT NULL"
        ).fetchone()
        since = row["t"] or now

    sent = conn.execute(
        "SELECT COUNT(*) n FROM idea WHERE sent_utc IS NOT NULL "
        "AND verdict IS NULL AND sent_utc >= ?", (since,)
    ).fetchone()["n"]
    if sent < SILENCE_IDEAS:
        return None

    quiet_days = (now - since) / 86400
    if quiet_days < SILENCE_DAYS:
        return None

    # Fire once per silent stretch -- but re-arm after another full stretch, or
    # someone who never replies at all gets the warning exactly once, ever.
    fired = int(get_kv(conn, "canary_fired_utc", "0"))
    if fired > last and (now - fired) < SILENCE_DAYS * 86400:
        return None
    if mark:
        set_kv(conn, "canary_fired_utc", str(now))

    domains = conn.execute(
        "SELECT domain, COUNT(*) n FROM idea WHERE sent_utc IS NOT NULL "
        "AND verdict IS NULL AND sent_utc >= ? GROUP BY domain "
        "ORDER BY n DESC LIMIT 3", (since,)
    ).fetchall()
    breakdown = ", ".join(f"{r['domain']} x{r['n']}" for r in domains if r["domain"])

    return (
        f"{sent} ideas have gone out unanswered and nothing has come back in "
        f"{int(quiet_days)} days. Mostly {breakdown}. Either these are not "
        f"landing or the cadence is wrong. Reply with one word and it will "
        f"recalibrate: `boring` to down-weight what it has been sending, "
        f"`too easy` or `too hard` to move the difficulty, or `pause` to stop "
        f"the ideas and keep only the prep."
    )


def main() -> int:
    messages = fetch()
    if not messages:
        print("no new replies")
        return 0

    done: list[bytes] = []
    with connect() as conn:
        for msg in messages:
            seen = conn.execute(
                "SELECT 1 FROM feedback WHERE message_id = ?", (msg.message_id,)
            ).fetchone()
            if seen:
                done.append(msg.uid)
                continue
            did = apply(conn, msg.body, msg.subject)
            conn.execute(
                "INSERT OR IGNORE INTO feedback (message_id, received_utc, body, applied) "
                "VALUES (?,?,?,?)",
                (msg.message_id, int(time.time()), msg.body[:2000], json.dumps(did)),
            )
            done.append(msg.uid)
            if did.get("unparsed"):
                print(f"{msg.subject[:50]}: could not parse -- kept, not applied")
            elif not any((did["grades"], did["verdict"], did.get("control"))):
                print(f"{msg.subject[:50]}: nothing actionable -- kept as a note")
            else:
                print(f"{msg.subject[:50]}: {did}")

    # Flag read only after the transaction committed. Doing it inside fetch()
    # meant a failure mid-apply rolled back the work and left the mail read.
    mark_seen(done)
    print(f"\nprocessed {len(done)} of {len(messages)} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
