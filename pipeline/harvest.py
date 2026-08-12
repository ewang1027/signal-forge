"""Stage 1 -- pull raw complaint signal from free APIs.

Deliberately has no LLM in it. Harvest runs daily and needs to be cheap and
boring; all the judgement happens downstream in themes/ideate.

The probes below are the actual quality lever. We are not looking for popular
stories -- popular stories are announcements. We are looking for the sentences
people write when they have personally hit a wall, because a wall somebody
bothered to complain about is evidence that a real problem exists.
"""

from __future__ import annotations

import html
import os
import re
import sys
import time
from dataclasses import dataclass

import httpx

from .config import USER_AGENT
from .db import connect
from .domains import best_domain, is_noise_thread, pain_score

MIN_DOMAIN_HITS = 2  # see domains.is_technical for why 2 and not 1
MIN_PAIN = 2         # topic and complaint are orthogonal; require both

ALGOLIA = "https://hn.algolia.com/api/v1/search"

# Phrases that correlate with a genuine, specific complaint rather than
# generic negativity. Tuned to catch "I hit this and it cost me time".
PAIN_PROBES = [
    "biggest pain point",
    "we had to build our own",
    "there is no good solution",
    "wish there was a tool",
    "surprisingly hard",
    "the hard part is",
    "spent weeks debugging",
    "does not scale",
    "footgun",
    "why is there no",
    "ended up writing our own",
    "nobody has solved",
    "turned out to be much harder",
    "still an unsolved problem",
    "the tooling is terrible",
    "hardest bug",
    "took us months",
    "no one talks about",
    "the real problem is",
    "everyone runs into",
    "bit us in production",
    "silently fails",
    "hard to debug",
    "does not compose",
    "leaky abstraction",
    "worst part about",
]

# Domain-anchored probes. The broad phrases above have good recall but pull in
# all of HN; these trade recall for precision by naming the territory outright.
DOMAIN_PROBES = [
    "kubernetes operational complexity",
    "observability cost cardinality",
    "distributed tracing overhead",
    "postgres scaling problems",
    "compiler incremental rebuild slow",
    "gpu inference latency",
    "kernel scheduler regression",
    "secret rotation painful",
    "vector database limitations",
    "consensus implementation bug",
    "network partition data loss",
    "container cold start",
    "prometheus cardinality explosion",
    "database migration downtime",
    "connection pool exhausted",
    "kafka consumer lag rebalance",
    "etcd performance cluster",
    "grpc streaming backpressure",
    "llm inference batching throughput",
    "kv cache memory eviction",
    "wasm toolchain debugging",
    "build system incremental cache",
    "ebpf kernel tracing overhead",
    "s3 consistency latency",
    "log volume ingestion cost",
    "flaky integration tests distributed",
    "oauth token refresh race",
    "sqlite concurrency wal",
    "rust compile time slow",
    "terraform state drift",
    "cold start serverless latency",
    "memory leak production debugging",
]

MIN_COMMENT_CHARS = 220  # below this it is almost always a quip, not a report
HITS_PER_PROBE = 100
# The gates reject ~95% of raw hits, so one page per probe starves the corpus.
MAX_PAGES = int(os.environ.get("HARVEST_PAGES", "3"))

# Daily runs only need a short window; the first run wants a real backfill so
# there is a corpus to cluster before the first Monday.
LOOKBACK_DAYS = int(os.environ.get("HARVEST_LOOKBACK_DAYS", "45"))

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Item:
    source: str
    external_id: str
    url: str
    title: str
    text: str
    author: str
    created_utc: int
    engagement: int
    query: str
    domain: str | None = None
    domain_hits: int = 0
    pain: int = 0


def _clean(raw: str) -> str:
    """HN comment_text is HTML. Strip it to plain text."""
    text = raw.replace("<p>", "\n\n")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _fetch(
    client: httpx.Client, query: str, since_utc: int, *, phrase: bool, page: int
) -> dict:
    resp = client.get(
        ALGOLIA,
        params={
            # Phrase-match the pain probes so "the hard part is" stays intact;
            # domain probes are bags of terms and match better unquoted.
            "query": f'"{query}"' if phrase else query,
            "tags": "comment",
            "advancedSyntax": "true",
            "numericFilters": f"created_at_i>{since_utc}",
            "hitsPerPage": HITS_PER_PROBE,
            "page": page,
        },
    )
    resp.raise_for_status()
    return resp.json()


def probe_hn(
    client: httpx.Client, query: str, since_utc: int, *, phrase: bool
) -> tuple[list[Item], int]:
    """Returns (kept items, count dropped by the domain/pain gates)."""
    hits: list[dict] = []
    for page in range(MAX_PAGES):
        payload = _fetch(client, query, since_utc, phrase=phrase, page=page)
        page_hits = payload.get("hits", [])
        hits.extend(page_hits)
        if page >= payload.get("nbPages", 1) - 1 or not page_hits:
            break
        time.sleep(0.2)

    items: list[Item] = []
    dropped = 0
    for hit in hits:
        text = _clean(hit.get("comment_text") or "")
        if len(text) < MIN_COMMENT_CHARS:
            continue
        object_id = hit.get("objectID")
        if not object_id:
            continue

        title = hit.get("story_title") or ""
        if is_noise_thread(title):
            dropped += 1
            continue

        # Two independent gates. Domain says "is this about systems", pain says
        # "did this person actually hit a wall". Keyword dumps pass the first
        # and fail the second, which is exactly the point.
        domain, hits = best_domain(f"{title}\n{text}")
        pain = pain_score(text)
        if hits < MIN_DOMAIN_HITS or pain < MIN_PAIN:
            dropped += 1
            continue

        items.append(
            Item(
                source="hn",
                external_id=str(object_id),
                url=f"https://news.ycombinator.com/item?id={object_id}",
                title=title,
                text=text,
                author=hit.get("author") or "",
                created_utc=int(hit.get("created_at_i") or 0),
                # comments carry no points; story engagement is the best proxy
                # available for "did anyone else care about this thread"
                engagement=int(hit.get("story_id") is not None and hit.get("num_comments") or 0),
                query=query,
                domain=domain,
                domain_hits=hits,
                pain=pain,
            )
        )
    return items, dropped


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
                (
                    i.source, i.external_id, i.url, i.title, i.text, i.author,
                    i.created_utc, now, i.engagement, i.query, i.domain, i.domain_hits, i.pain,
                )
                for i in items
            ],
        )
        after = conn.execute("SELECT COUNT(*) AS c FROM signal").fetchone()["c"]
    return after - before


def main() -> int:
    since = int(time.time()) - LOOKBACK_DAYS * 86400
    all_items: list[Item] = []
    total_dropped = 0

    plan = [(p, True) for p in PAIN_PROBES] + [(p, False) for p in DOMAIN_PROBES]

    with httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        for probe, phrase in plan:
            try:
                found, dropped = probe_hn(client, probe, since, phrase=phrase)
            except httpx.HTTPError as exc:
                print(f"  {probe!r}: FAILED ({exc})", file=sys.stderr)
                continue
            total_dropped += dropped
            print(f"  {probe!r}: kept {len(found)}, dropped {dropped} off-domain")
            all_items.extend(found)
            time.sleep(0.3)  # well under Algolia's 10k/hr, just being polite

    new = store(all_items)
    print(f"\nkept {len(all_items)}, dropped {total_dropped} off-domain, {new} new rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
