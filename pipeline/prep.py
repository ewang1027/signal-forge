"""Stage 5 -- interview prep scheduling. No LLM in the selection path.

Two decks, both scheduled by FSRS: DSA *patterns* (not individual problems, which
would only train recall of those problems) and system design.

## Why there is no target date

The plan originally ramped volume backward from an interview date. While actively
recruiting there is no such date -- a callback arrives with about a week's notice,
so the deadline is effectively "any given week". That makes a countdown the wrong
model and a **rolling readiness cap** the right one.

Concretely, FSRS optimises for long-term retention and will happily push a
well-known card out 6-12 months. That is correct for lifetime learning and wrong
when you might interview on Tuesday: a pattern last seen in September is not
fresh in November. So `maximum_interval` is capped hard and `desired_retention`
is raised, which shortens intervals and buys more reps. Those two levers are the
whole intensity dial.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler

from .config import REPO_ROOT
from .db import connect

CARDS_DIR = REPO_ROOT / "cards"
DECKS = ("dsa", "system_design")


@dataclass(frozen=True)
class Intensity:
    retention: float      # higher -> shorter intervals -> more reps
    max_interval: int     # days; the readiness cap
    daily_cap: int        # most cards in one email before it becomes wallpaper
    timed_problem: bool   # include a timed DSA problem
    timed_design: bool    # include a timed system design prompt


MODES: dict[str, Intensity] = {
    # Not interviewing. Let FSRS do what it is designed for.
    "sharp": Intensity(0.90, 180, 3, False, False),
    # Interviews expected but not imminent.
    "ramping": Intensity(0.91, 60, 6, True, False),
    # Actively recruiting: a callback can land any week, so nothing may go stale.
    "recruiting": Intensity(0.93, 21, 10, True, True),
}

MODE = os.environ.get("PREP_INTENSITY", "recruiting")


def intensity() -> Intensity:
    return MODES.get(MODE, MODES["recruiting"])


def scheduler() -> Scheduler:
    cfg = intensity()
    return Scheduler(
        desired_retention=cfg.retention,
        maximum_interval=cfg.max_interval,
        enable_fuzzing=True,
    )


def load_deck(name: str) -> list[dict]:
    path = CARDS_DIR / f"{name}.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text()).get("cards", [])


def all_cards() -> dict[str, list[dict]]:
    return {d: load_deck(d) for d in DECKS}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_cards(conn: sqlite3.Connection) -> int:
    """Register any deck card that has no review row yet. Returns count added."""
    known = {(r["deck"], r["card_id"])
             for r in conn.execute("SELECT deck, card_id FROM review")}
    added = 0
    for deck, cards in all_cards().items():
        for card in cards:
            if (deck, card["id"]) in known:
                continue
            fresh = Card()
            conn.execute(
                "INSERT INTO review (deck, card_id, state, due_utc) VALUES (?,?,?,?)",
                (deck, card["id"], json.dumps(fresh.to_dict()),
                 int(fresh.due.timestamp())),
            )
            added += 1
    return added


def due_cards(conn: sqlite3.Connection, deck: str, limit: int) -> list[sqlite3.Row]:
    """Cards due now, weakest first.

    Ordered by lapses so repeatedly-failed material resurfaces ahead of things
    merely scheduled -- that is the weak-area targeting, and it matters more
    than strict due-date order when you are cramming."""
    return conn.execute(
        """
        SELECT * FROM review
        WHERE deck = ? AND due_utc <= ?
        ORDER BY lapses DESC, due_utc ASC
        LIMIT ?
        """,
        (deck, int(_now().timestamp()), limit),
    ).fetchall()


def grade(conn: sqlite3.Connection, deck: str, card_id: str, rating: Rating) -> None:
    """Apply a review outcome and reschedule."""
    row = conn.execute(
        "SELECT * FROM review WHERE deck = ? AND card_id = ?", (deck, card_id)
    ).fetchone()
    if row is None:
        return

    card = Card.from_dict(json.loads(row["state"]))
    updated, _ = scheduler().review_card(card, rating, _now())

    conn.execute(
        """
        UPDATE review
        SET state = ?, due_utc = ?, reps = reps + 1,
            lapses = lapses + ?, last_utc = ?
        WHERE deck = ? AND card_id = ?
        """,
        (json.dumps(updated.to_dict()), int(updated.due.timestamp()),
         1 if rating == Rating.Again else 0, int(_now().timestamp()),
         deck, card_id),
    )


def pick_problem(conn: sqlite3.Connection) -> tuple[dict, str] | None:
    """A timed problem from the weakest DSA pattern.

    Deliberately drawn from the pattern with the most lapses rather than at
    random -- the cards say what you keep forgetting, so the practice should
    follow them.
    """
    cards = {c["id"]: c for c in load_deck("dsa")}
    if not cards:
        return None

    rows = conn.execute(
        """
        SELECT card_id, lapses FROM review
        WHERE deck = 'dsa'
        ORDER BY lapses DESC, RANDOM()
        LIMIT 5
        """
    ).fetchall()
    for row in rows:
        card = cards.get(row["card_id"])
        if card and card.get("problems"):
            return card, random.choice(card["problems"])
    return None


def pick_design_prompt(conn: sqlite3.Connection) -> dict | None:
    """A timed system design prompt, weakest first."""
    cards = {c["id"]: c for c in load_deck("system_design")}
    if not cards:
        return None
    row = conn.execute(
        "SELECT card_id FROM review WHERE deck='system_design' "
        "ORDER BY lapses DESC, RANDOM() LIMIT 1"
    ).fetchone()
    return cards.get(row["card_id"]) if row else None


def today(conn: sqlite3.Connection) -> dict:
    """Everything the daily email should carry."""
    cfg = intensity()
    ensure_cards(conn)

    # Split the cap between decks, favouring DSA slightly.
    dsa_n = max(1, round(cfg.daily_cap * 0.6))
    sd_n = max(1, cfg.daily_cap - dsa_n)

    dsa_lookup = {c["id"]: c for c in load_deck("dsa")}
    sd_lookup = {c["id"]: c for c in load_deck("system_design")}

    out = {
        "mode": MODE,
        "dsa": [dsa_lookup[r["card_id"]] for r in due_cards(conn, "dsa", dsa_n)
                if r["card_id"] in dsa_lookup],
        "system_design": [sd_lookup[r["card_id"]]
                          for r in due_cards(conn, "system_design", sd_n)
                          if r["card_id"] in sd_lookup],
        "problem": None,
        "design_prompt": None,
    }

    if cfg.timed_problem:
        if picked := pick_problem(conn):
            pattern, problem = picked
            out["problem"] = {"pattern": pattern["name"], "problem": problem,
                              "cue": pattern["cue"]}
    if cfg.timed_design:
        out["design_prompt"] = pick_design_prompt(conn)

    return out


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) total,
               SUM(CASE WHEN due_utc <= ? THEN 1 ELSE 0 END) due,
               SUM(reps) reps,
               SUM(lapses) lapses
        FROM review
        """,
        (int(_now().timestamp()),),
    ).fetchone()
    return {k: (row[k] or 0) for k in ("total", "due", "reps", "lapses")}


def main() -> int:
    with connect() as conn:
        added = ensure_cards(conn)
        if added:
            print(f"registered {added} new cards")
        cfg = intensity()
        print(f"mode: {MODE} (retention {cfg.retention}, "
              f"cap {cfg.max_interval}d, up to {cfg.daily_cap} cards/day)")
        print(f"stats: {stats(conn)}\n")

        day = today(conn)
        for deck in ("dsa", "system_design"):
            for card in day[deck]:
                print(f"  [{deck}] {card['name']}")
        if p := day["problem"]:
            print(f"\n  timed problem ({p['pattern']}): {p['problem']}")
        if d := day["design_prompt"]:
            print(f"  timed design: {d['prompt']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
