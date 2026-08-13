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
import imaplib
import json
import os
import sqlite3
import sys
import time
from email.header import decode_header, make_header

from fsrs import Rating

from . import prep, taste
from .config import DIGEST_TO
from .db import connect, get_kv, set_kv
from .replies import parse

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.environ.get("IMAP_USER", DIGEST_TO)
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")

# How far a single verdict moves the weight of the evidence behind an idea.
WEIGHT_STEP: dict[str, float] = {
    "building": 1.6,   # the strongest possible signal: it got built
    "more": 1.25,
    "exists": 0.6,     # also means the prior-art gate missed something
    "too_easy": 0.7,
    "too_hard": 0.85,
    "boring": 0.5,
}

WEIGHT_FLOOR, WEIGHT_CEIL = 0.1, 4.0

RATINGS = {"again": Rating.Again, "hard": Rating.Hard,
           "good": Rating.Good, "easy": Rating.Easy}


def _decode(raw) -> str:
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return str(raw or "")


def _body_of(msg: email.message.Message) -> str:
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", "replace")
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def fetch() -> list[tuple[str, str]]:
    """Unread replies as (message_id, plain-text body). Marks them read."""
    if not IMAP_PASSWORD:
        print("IMAP_PASSWORD unset; skipping fetch", file=sys.stderr)
        return []

    out: list[tuple[str, str]] = []
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(IMAP_USER, IMAP_PASSWORD)
        conn.select(IMAP_FOLDER)
        # Only replies to our own digests, so an unrelated unread email in the
        # inbox is never parsed as feedback.
        typ, data = conn.search(None, 'UNSEEN', 'SUBJECT', '"Re:"')
        if typ != "OK":
            return []
        for num in (data[0].split() if data and data[0] else []):
            typ, raw = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            mid = _decode(msg.get("Message-ID")) or f"nomid:{num!r}"
            out.append((mid, _body_of(msg)))
            conn.store(num, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


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


def latest_sent_idea(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM idea WHERE sent_utc IS NOT NULL "
        "ORDER BY sent_utc DESC LIMIT 1"
    ).fetchone()


def apply(conn: sqlite3.Connection, body: str) -> dict:
    """Apply one reply. Returns what changed."""
    known = {c["id"] for cards in prep.all_cards().values() for c in cards}
    reply = parse(body, known)
    did: dict = {"grades": [], "verdict": None, "signals": 0}

    for card_id, grade_word in reply.grades:
        if grade_word == "skip":
            continue
        deck = next((d for d, cards in prep.all_cards().items()
                     if any(c["id"] == card_id for c in cards)), None)
        if deck and prep.grade(conn, deck, card_id, RATINGS[grade_word]):
            did["grades"].append((deck, card_id, grade_word))

    if reply.idea_verdict:
        idea = latest_sent_idea(conn)
        if idea is not None:
            conn.execute(
                "UPDATE idea SET verdict = ?, verdict_utc = ? WHERE id = ?",
                (reply.idea_verdict, int(time.time()), idea["id"]),
            )
            factor = WEIGHT_STEP.get(reply.idea_verdict, 1.0)
            did["signals"] = adjust_theme_weight(conn, idea["id"], factor)
            did["verdict"] = reply.idea_verdict
            taste.record(reply.idea_verdict, idea["title"],
                         reply.note if reply.note != reply.idea_verdict else "")

    if not reply.is_empty():
        set_kv(conn, "last_reply_utc", str(int(time.time())))
    return did


# --- quality canary -------------------------------------------------------
#
# A system that has become irrelevant looks exactly like a system that is
# working, from the inside. The only observable difference is that nobody
# replies any more, so that is what gets watched.

SILENCE_DAYS = int(os.environ.get("CANARY_SILENCE_DAYS", "14"))
SILENCE_IDEAS = int(os.environ.get("CANARY_SILENCE_IDEAS", "4"))


def canary(conn: sqlite3.Connection) -> str | None:
    """Returns a message to append to the next digest, or None."""
    last = int(get_kv(conn, "last_reply_utc", "0"))
    now = int(time.time())

    sent = conn.execute(
        "SELECT COUNT(*) n FROM idea WHERE sent_utc IS NOT NULL AND verdict IS NULL"
    ).fetchone()["n"]
    if sent < SILENCE_IDEAS:
        return None

    quiet_days = (now - last) / 86400 if last else SILENCE_DAYS + 1
    if quiet_days < SILENCE_DAYS:
        return None

    # Only fire once per silent stretch.
    if int(get_kv(conn, "canary_fired_utc", "0")) > last:
        return None
    set_kv(conn, "canary_fired_utc", str(now))

    domains = conn.execute(
        "SELECT domain, COUNT(*) n FROM idea WHERE sent_utc IS NOT NULL "
        "AND verdict IS NULL GROUP BY domain ORDER BY n DESC LIMIT 3"
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
    with connect() as conn:
        messages = fetch()
        if not messages:
            print("no new replies")
            return 0

        applied = 0
        for mid, body in messages:
            seen = conn.execute(
                "SELECT 1 FROM feedback WHERE message_id = ?", (mid,)
            ).fetchone()
            if seen:
                continue
            did = apply(conn, body)
            conn.execute(
                "INSERT OR IGNORE INTO feedback (message_id, received_utc, body, applied) "
                "VALUES (?,?,?,?)",
                (mid, int(time.time()), body[:2000], json.dumps(did)),
            )
            applied += 1
            print(f"{mid[:40]}: {did}")

        print(f"\napplied {applied} of {len(messages)} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
