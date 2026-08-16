"""Lobste.rs. Smaller than HN and much denser in systems content.

No search API, so this pulls recent stories by tag and keeps the comment
threads. Volume is low; signal-to-noise is high.
"""

from __future__ import annotations

import time
from datetime import datetime

import httpx

from ..item import Item, clean_html, gate


def _parse_ts(raw: str | None) -> int:
    """Real comment timestamps matter: they feed the recency decay in
    evidence_score. Stubbing them all to one value made every lobsters row
    maximally stale (weight 0.05) while still granting the full source-diversity
    bonus, which inflated cross-source themes by a third."""
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw).timestamp())
    except (ValueError, TypeError):
        return 0

BASE = "https://lobste.rs"
TAGS = ["programming", "distributed", "databases", "compilers", "performance",
        "devops", "networking", "security", "osdev", "rust", "go", "ml"]
PAGES_PER_TAG = 2


def harvest(client: httpx.Client, since: int) -> tuple[list[Item], int]:
    items: list[Item] = []
    dropped = 0
    seen: set[str] = set()

    for tag in TAGS:
        for page in range(1, PAGES_PER_TAG + 1):
            try:
                resp = client.get(f"{BASE}/t/{tag}.json", params={"page": page}, timeout=30)
                resp.raise_for_status()
                stories = resp.json()
            except (httpx.HTTPError, ValueError):
                break

            if not stories:
                break

            for story in stories:
                short_id = story.get("short_id")
                if not short_id or short_id in seen:
                    continue
                seen.add(short_id)

                # Nothing to harvest from a story nobody commented on, and the
                # detail fetch is the expensive part of this source -- one
                # request plus a courtesy pause per story, several hundred of
                # them per run, and about a third have no comments at all.
                # `.get` defaults to None so a missing field still fetches.
                if story.get("comment_count") == 0:
                    continue

                # The story itself is an announcement; its comments are where
                # people report what actually went wrong.
                try:
                    detail = client.get(f"{BASE}/s/{short_id}.json", timeout=30)
                    detail.raise_for_status()
                    payload = detail.json()
                except (httpx.HTTPError, ValueError):
                    continue

                title = payload.get("title", "")
                for comment in payload.get("comments", []):
                    item = gate(Item(
                        source="lobsters",
                        external_id=str(comment.get("short_id", "")),
                        url=comment.get("url", "") or payload.get("url", ""),
                        title=title,
                        text=clean_html(comment.get("comment") or ""),
                        author=(comment.get("commenting_user") or ""),
                        created_utc=_parse_ts(comment.get("created_at")),
                        engagement=int(comment.get("score") or 0),
                        query=f"tag:{tag}",
                    ))
                    if item:
                        items.append(item)
                    else:
                        dropped += 1
                time.sleep(0.4)
            time.sleep(0.3)

    return items, dropped
