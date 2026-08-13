"""Hacker News via the Algolia search API. Free, no key, 10k req/hr."""

from __future__ import annotations

import os
import time

import httpx

from ..config import env
from ..item import Item, clean_html, gate

ALGOLIA = "https://hn.algolia.com/api/v1/search"

# Phrases people write when they have personally hit a wall. We are not after
# popular stories -- popular stories are announcements.
PAIN_PROBES = [
    "biggest pain point", "we had to build our own", "there is no good solution",
    "wish there was a tool", "surprisingly hard", "the hard part is",
    "spent weeks debugging", "does not scale", "footgun", "why is there no",
    "ended up writing our own", "nobody has solved", "turned out to be much harder",
    "still an unsolved problem", "the tooling is terrible", "hardest bug",
    "took us months", "no one talks about", "the real problem is",
    "everyone runs into", "bit us in production", "silently fails",
    "hard to debug", "does not compose", "leaky abstraction", "worst part about",
]

# Named territory. Trades recall for precision against the broad phrases above.
DOMAIN_PROBES = [
    "kubernetes operational complexity", "observability cost cardinality",
    "distributed tracing overhead", "postgres scaling problems",
    "compiler incremental rebuild slow", "gpu inference latency",
    "kernel scheduler regression", "secret rotation painful",
    "vector database limitations", "consensus implementation bug",
    "network partition data loss", "container cold start",
    "prometheus cardinality explosion", "database migration downtime",
    "connection pool exhausted", "kafka consumer lag rebalance",
    "etcd performance cluster", "grpc streaming backpressure",
    "llm inference batching throughput", "kv cache memory eviction",
    "wasm toolchain debugging", "build system incremental cache",
    "ebpf kernel tracing overhead", "log volume ingestion cost",
    "flaky integration tests distributed", "oauth token refresh race",
    "sqlite concurrency wal", "rust compile time slow",
    "terraform state drift", "memory leak production debugging",
]

HITS_PER_PAGE = 100
MAX_PAGES = int(env("HARVEST_PAGES", "3"))


def _search(client: httpx.Client, query: str, since: int, *, phrase: bool, page: int) -> dict:
    resp = client.get(
        ALGOLIA,
        params={
            "query": f'"{query}"' if phrase else query,
            "tags": "comment",
            "advancedSyntax": "true",
            "numericFilters": f"created_at_i>{since}",
            "hitsPerPage": HITS_PER_PAGE,
            "page": page,
        },
    )
    resp.raise_for_status()
    return resp.json()


def _probe(client: httpx.Client, query: str, since: int, *, phrase: bool) -> tuple[list[Item], int]:
    hits: list[dict] = []
    for page in range(MAX_PAGES):
        payload = _search(client, query, since, phrase=phrase, page=page)
        page_hits = payload.get("hits", [])
        hits.extend(page_hits)
        if page >= payload.get("nbPages", 1) - 1 or not page_hits:
            break
        time.sleep(0.2)

    kept, dropped = [], 0
    for hit in hits:
        oid = hit.get("objectID")
        if not oid:
            continue
        item = gate(Item(
            source="hn",
            external_id=str(oid),
            url=f"https://news.ycombinator.com/item?id={oid}",
            title=hit.get("story_title") or "",
            text=clean_html(hit.get("comment_text") or ""),
            author=hit.get("author") or "",
            created_utc=int(hit.get("created_at_i") or 0),
            engagement=int(hit.get("num_comments") or 0),
            query=query,
        ))
        if item:
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


def harvest(client: httpx.Client, since: int) -> tuple[list[Item], int]:
    plan = [(p, True) for p in PAIN_PROBES] + [(p, False) for p in DOMAIN_PROBES]
    items, dropped = [], 0
    for query, phrase in plan:
        try:
            kept, d = _probe(client, query, since, phrase=phrase)
        except httpx.HTTPError:
            continue
        items.extend(kept)
        dropped += d
        time.sleep(0.3)
    return items, dropped
