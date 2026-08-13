"""Stage 1 -- pull raw complaint signal from free APIs.

No LLM here on purpose. Harvest runs daily and needs to be cheap and boring;
all the judgement happens downstream.

Reddit is deliberately absent: its .json endpoints now return 403 to
unauthenticated clients, and a source that silently yields nothing is worse
than no source at all.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from .config import USER_AGENT, env
from .db import connect
from .item import Item
from .sources import ALL

# Daily runs only need a short window; the first run wants a real backfill so
# there is a corpus to cluster before the first Monday.
LOOKBACK_DAYS = int(env("HARVEST_LOOKBACK_DAYS", "45"))


def store(items: list[Item]) -> int:
    """Insert, ignoring anything already harvested. Returns new-row count."""
    if not items:
        return 0
    now = int(time.time())
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS c FROM signal").fetchone()["c"]
        conn.executemany(
            """
            INSERT OR IGNORE INTO signal
                (source, external_id, url, title, text, author,
                 created_utc, harvested_utc, engagement, query, domain, domain_hits, pain)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (i.source, i.external_id, i.url, i.title, i.text, i.author,
                 i.created_utc, now, i.engagement, i.query, i.domain, i.domain_hits, i.pain)
                for i in items
            ],
        )
        after = conn.execute("SELECT COUNT(*) AS c FROM signal").fetchone()["c"]
    return after - before


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated source names")
    args = ap.parse_args()

    wanted = set(args.only.split(",")) if args.only else set(ALL)
    since = int(time.time()) - LOOKBACK_DAYS * 86400
    all_items: list[Item] = []

    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT},
                      follow_redirects=True) as client:
        for name, fn in ALL.items():
            if name not in wanted:
                continue
            started = time.time()
            try:
                items, dropped = fn(client, since)
            except Exception as exc:  # a broken source must not kill the run
                print(f"{name}: FAILED ({type(exc).__name__}: {exc})", file=sys.stderr)
                continue
            print(f"{name}: kept {len(items)}, dropped {dropped} "
                  f"({time.time() - started:.0f}s)")
            all_items.extend(items)

    new = store(all_items)
    print(f"\n{len(all_items)} kept, {new} new rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
