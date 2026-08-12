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
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler, State

from .config import REPO_ROOT
from .db import connect

CARDS_DIR = REPO_ROOT / "cards"
DECKS = ("dsa", "system_design")


# A card failed this many times is not a scheduling problem -- reviewing it again
# tomorrow has already been tried and did not work. It gets pulled out of the
# review rotation and surfaced as something to sit down and learn.
LEECH_AT = int(os.environ.get("PREP_LEECH_AT", "8"))


@dataclass(frozen=True)
class Intensity:
    retention: float      # higher -> shorter intervals -> more reps
    max_interval: int     # days; the readiness cap
    daily_cap: int        # most cards in one email before it becomes wallpaper
    problems: int         # timed DSA problems per day -- where the volume is
    timed_design: bool    # include a timed system design prompt


MODES: dict[str, Intensity] = {
    # Not interviewing. Let FSRS do what it is designed for.
    "sharp": Intensity(0.90, 180, 3, 0, False),
    # Interviews expected but not imminent.
    "ramping": Intensity(0.91, 60, 6, 1, False),
    # Actively recruiting: a callback can land any week, so nothing may go stale.
    # Volume comes from problems rather than cards -- with ~20 patterns and a
    # 21-day cap the deck alone settles at 1-2 reviews/day, which is the right
    # amount of *recognition* practice and nowhere near enough actual practice.
    "recruiting": Intensity(0.93, 21, 10, 3, True),
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


def leeches(conn: sqlite3.Connection, deck: str) -> list[sqlite3.Row]:
    """Cards failed so often that rescheduling them is not the answer."""
    return conn.execute(
        "SELECT * FROM review WHERE deck = ? AND fails >= ? ORDER BY fails DESC",
        (deck, LEECH_AT),
    ).fetchall()


def due_cards(conn: sqlite3.Connection, deck: str, limit: int) -> list[sqlite3.Row]:
    """Cards due now: some weak-area slots, the rest in due order.

    Pure `ORDER BY lapses DESC` looks like weak-area targeting but degenerates
    into a treadmill -- simulated over 90 days, three always-failed cards
    occupied three of six daily slots every single day and crowded out all
    breadth. So only a third of the budget goes to the weakest cards; the rest
    follows the schedule. Leeches are excluded entirely -- they are surfaced
    separately as material to study rather than review.
    """
    now = int(_now().timestamp())
    weak_slots = max(1, limit // 3)

    weak = conn.execute(
        """
        SELECT * FROM review
        WHERE deck = ? AND due_utc <= ? AND fails > 0 AND fails < ?
        ORDER BY fails DESC, due_utc ASC, RANDOM()
        LIMIT ?
        """,
        (deck, now, LEECH_AT, weak_slots),
    ).fetchall()

    picked = {r["card_id"] for r in weak}
    # RANDOM() is the final tie-break on purpose. Until a card has been graded
    # its due_utc is identical to every other ungraded card, and without a
    # random term SQLite falls back to primary-key order -- which served the
    # same alphabetical ten cards every day and never showed the other thirty.
    rest = conn.execute(
        """
        SELECT * FROM review
        WHERE deck = ? AND due_utc <= ? AND fails < ?
        ORDER BY due_utc ASC, last_utc ASC NULLS FIRST, RANDOM()
        LIMIT ?
        """,
        (deck, now, LEECH_AT, limit),
    ).fetchall()

    out = list(weak)
    for row in rest:
        if len(out) >= limit:
            break
        if row["card_id"] not in picked:
            out.append(row)
    return out


def grade(conn: sqlite3.Connection, deck: str, card_id: str, rating: Rating) -> bool:
    """Apply a review outcome and reschedule. Returns False if unknown."""
    row = conn.execute(
        "SELECT * FROM review WHERE deck = ? AND card_id = ?", (deck, card_id)
    ).fetchone()
    if row is None:
        return False

    try:
        card = Card.from_dict(json.loads(row["state"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # A corrupt blob must not lose the grade -- Phase 5 calls this from an
        # inbound webhook, where an unhandled exception drops the review.
        card = Card()

    before = card.state
    updated, _ = scheduler().review_card(card, rating, _now())

    # A lapse is `Again` on a card already in Review. Counting a first-day
    # fumble inflates the weak-area signal that drives ordering and both timed
    # slots, biasing everything toward whatever was seen first.
    lapsed = 1 if (rating == Rating.Again and before in (State.Review, State.Relearning)) else 0

    conn.execute(
        """
        UPDATE review
        SET state = ?, due_utc = ?, reps = reps + 1,
            lapses = lapses + ?, fails = fails + ?, last_utc = ?
        WHERE deck = ? AND card_id = ?
        """,
        (json.dumps(updated.to_dict()), int(updated.due.timestamp()),
         lapsed, 1 if rating == Rating.Again else 0,
         int(_now().timestamp()), deck, card_id),
    )
    conn.execute(
        "INSERT INTO review_log (deck, card_id, rating, state_before, review_utc) "
        "VALUES (?,?,?,?,?)",
        (deck, card_id, int(rating), before.name, int(_now().timestamp())),
    )
    return True


def pick_problems(conn: sqlite3.Connection, n: int) -> list[dict]:
    """Timed problems from the weakest patterns, one per pattern.

    This is where practice volume actually comes from -- the card deck is
    recognition training and settles at a couple of reviews a day no matter how
    the scheduler is tuned. Problems already given are avoided so the rotating
    set actually rotates instead of replaying the same question.
    """
    cards = {c["id"]: c for c in load_deck("dsa")}
    if not cards or n <= 0:
        return []

    given = {r["problem"] for r in conn.execute("SELECT problem FROM problem_log")}

    # Weighted draw, same reason as pick_design_prompt: sorting by lapses made
    # one pattern supply 76% of all problems over 90 simulated days.
    rows = conn.execute(
        "SELECT card_id FROM review WHERE deck = 'dsa' "
        "ORDER BY RANDOM() / (1.0 + fails)"
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        if len(out) >= n:
            break
        card = cards.get(row["card_id"])
        if not card:
            continue
        fresh = [p for p in card.get("problems", []) if p not in given]
        # Every problem in this pattern has been given -- fall back to the full
        # set rather than skipping the pattern entirely.
        pool = fresh or card.get("problems", [])
        if not pool:
            continue
        out.append({"pattern": card["name"], "problem": random.choice(pool),
                    "cue": card["cue"], "card_id": card["id"]})
    return out


def record_problems(conn: sqlite3.Connection, problems: list[dict]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO problem_log (card_id, problem, given_utc) VALUES (?,?,?)",
        [(p["card_id"], p["problem"], int(_now().timestamp())) for p in problems],
    )


def pick_design_prompt(conn: sqlite3.Connection) -> dict | None:
    """A timed system design prompt, weighted toward weak areas.

    Weighted, not sorted. `ORDER BY lapses DESC LIMIT 1` is a positive feedback
    loop -- shown, failed, lapses rise, shown again -- and simulated over 90
    days it served one card on 87 of them. Dividing a random draw by (1+lapses)
    keeps the bias without the monopoly. Iterating rather than taking LIMIT 1
    also survives an orphaned row whose card was renamed out of the deck.
    """
    cards = {c["id"]: c for c in load_deck("system_design")}
    if not cards:
        return None
    rows = conn.execute(
        "SELECT card_id FROM review WHERE deck='system_design' "
        "ORDER BY RANDOM() / (1.0 + fails) LIMIT 5"
    ).fetchall()
    for row in rows:
        if card := cards.get(row["card_id"]):
            return card
    return None


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
        "problems": pick_problems(conn, cfg.problems),
        "design_prompt": pick_design_prompt(conn) if cfg.timed_design else None,
        "leeches": [dsa_lookup.get(r["card_id"]) or sd_lookup.get(r["card_id"])
                    for deck in DECKS for r in leeches(conn, deck)],
    }
    out["leeches"] = [c for c in out["leeches"] if c]
    record_problems(conn, out["problems"])
    return out


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) total,
               SUM(CASE WHEN due_utc <= ? THEN 1 ELSE 0 END) due,
               SUM(reps) reps,
               SUM(lapses) lapses,
               SUM(fails) fails
        FROM review
        """,
        (int(_now().timestamp()),),
    ).fetchone()
    return {k: (row[k] or 0) for k in ("total", "due", "reps", "lapses", "fails")}


RATINGS = {"again": Rating.Again, "hard": Rating.Hard,
           "good": Rating.Good, "easy": Rating.Easy}


def orphans(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Review rows whose card no longer exists in the deck JSON.

    A renamed id silently strands its history and today() drops it without
    saying so, which makes a rename look like a reset.
    """
    known = {(d, c["id"]) for d, cards in all_cards().items() for c in cards}
    return [(r["deck"], r["card_id"])
            for r in conn.execute("SELECT deck, card_id FROM review")
            if (r["deck"], r["card_id"]) not in known]


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="pipeline.prep")
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("grade", help="record a review outcome")
    g.add_argument("deck", choices=DECKS)
    g.add_argument("card_id")
    g.add_argument("rating", choices=sorted(RATINGS))
    ap.add_argument("--quiet", action="store_true",
                    help="counts only -- omit card names and lapse ordering")
    args = ap.parse_args()

    with connect() as conn:
        if args.cmd == "grade":
            ok = grade(conn, args.deck, args.card_id, RATINGS[args.rating])
            if not ok:
                print(f"unknown card: {args.deck}/{args.card_id}", file=sys.stderr)
                return 1
            row = conn.execute(
                "SELECT due_utc, reps, lapses, fails FROM review WHERE deck=? AND card_id=?",
                (args.deck, args.card_id)).fetchone()
            days = (row["due_utc"] - int(_now().timestamp())) / 86400
            print(f"{args.card_id}: {args.rating} -> due in {days:.1f}d "
                  f"(reps {row['reps']}, lapses {row['lapses']}, fails {row['fails']})")
            return 0

        added = ensure_cards(conn)
        if added:
            print(f"registered {added} new cards")
        if stranded := orphans(conn):
            print(f"WARNING: {len(stranded)} review rows have no card in the deck "
                  f"(renamed or removed): {stranded[:3]}", file=sys.stderr)

        cfg = intensity()
        print(f"mode: {MODE} (retention {cfg.retention}, "
              f"cap {cfg.max_interval}d, up to {cfg.daily_cap} cards/day)")

        day = today(conn)
        # Card names printed in weak-first order ARE the ranking of what he
        # keeps failing. Harmless locally, but this is the natural thing to drop
        # into a CI step, and public-repo Actions logs are public.
        if args.quiet:
            print(f"due: {len(day['dsa'])} dsa, {len(day['system_design'])} design, "
                  f"{len(day['problems'])} problems, {len(day['leeches'])} to study")
            return 0

        print(f"stats: {stats(conn)}\n")
        for deck in ("dsa", "system_design"):
            for card in day[deck]:
                print(f"  [{deck}] {card['name']}")
        for p in day["problems"]:
            print(f"\n  timed problem ({p['pattern']}): {p['problem']}")
        if d := day["design_prompt"]:
            print(f"  timed design: {d['prompt']}")
        if day["leeches"]:
            print(f"\n  STUDY (failed {LEECH_AT}+ times, reviewing is not working):")
            for card in day["leeches"]:
                print(f"    {card['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
