"""GitHub issues on major infrastructure projects.

The best source in the set, because reaction counts *are* evidence density.
An open issue with 200 thumbs-up is a pain point several hundred people
independently confirmed, which is exactly the ranking signal this system is
built around -- no inference required.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from ..config import GITHUB_TOKEN
from ..item import Item, gate

SEARCH = "https://api.github.com/search/issues"

# Curated rather than discovered: these are the projects whose pain is worth
# building against, spread across the rotation domains.
REPOS = [
    "kubernetes/kubernetes", "etcd-io/etcd", "istio/istio", "envoyproxy/envoy",
    "grpc/grpc-go", "hashicorp/terraform", "moby/moby", "containerd/containerd",
    "postgres/postgres", "duckdb/duckdb", "apache/arrow", "apache/iceberg",
    "redis/redis", "valkey-io/valkey", "clickhouse/clickhouse",
    "rust-lang/rust", "golang/go", "ziglang/zig", "llvm/llvm-project",
    "microsoft/TypeScript", "swiftlang/swift", "webpack/webpack",
    "open-telemetry/opentelemetry-collector", "prometheus/prometheus",
    "grafana/grafana", "grafana/loki", "jaegertracing/jaeger",
    "vllm-project/vllm", "ggml-org/llama.cpp", "pytorch/pytorch",
    "ray-project/ray", "triton-lang/triton", "huggingface/transformers",
    "torvalds/linux", "cilium/cilium", "iovisor/bcc",
]

MIN_REACTIONS = 15
PER_PAGE = 40

# The search endpoint allows 30 requests/minute authenticated. Spacing is
# measured from the start of the previous request rather than slept for after it
# finishes, so a slow response counts toward the interval instead of being added
# to it -- the same ceiling, about a minute less wall clock across the repo list.
MIN_INTERVAL = 2.2


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _ts(raw: str) -> int | None:
    """GitHub's `created_at` as a real UTC epoch, or None if unparseable.

    `strptime("%Y-%m-%dT%H:%M:%SZ")` returned a *naive* datetime, whose
    `.timestamp()` is interpreted as local time -- so every issue's age was off
    by the machine's UTC offset, and that age feeds the recency decay in
    `evidence_score`.
    """
    try:
        when = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:  # GitHub always sends 'Z'; assume UTC if it stops
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


def harvest(client: httpx.Client, since: int) -> tuple[list[Item], int]:
    items: list[Item] = []
    dropped = 0

    started = 0.0
    for repo in REPOS:
        query = (
            f"repo:{repo} is:issue is:open "
            f"reactions:>={MIN_REACTIONS} created:>={_iso(since)}"
        )
        if (wait := MIN_INTERVAL - (time.monotonic() - started)) > 0:
            time.sleep(wait)
        started = time.monotonic()
        try:
            resp = client.get(
                SEARCH,
                params={"q": query, "sort": "reactions", "order": "desc",
                        "per_page": PER_PAGE},
                headers=_headers(),
                timeout=30,
            )
            if resp.status_code == 403:
                # secondary rate limit -- back off rather than hammering
                time.sleep(60)
                continue
            resp.raise_for_status()
        except httpx.HTTPError:
            continue

        for issue in resp.json().get("items", []):
            body = issue.get("body") or ""
            created_utc = _ts(issue.get("created_at", ""))
            if created_utc is None:
                created_utc = since

            item = gate(Item(
                source="github",
                external_id=str(issue.get("id")),
                url=issue.get("html_url", ""),
                title=f"{repo}: {issue.get('title', '')}",
                text=body,
                author=(issue.get("user") or {}).get("login", ""),
                created_utc=created_utc,
                engagement=int((issue.get("reactions") or {}).get("total_count", 0)),
                query=f"repo:{repo}",
            ))
            if item:
                items.append(item)
            else:
                dropped += 1

    return items, dropped
