"""GitHub issues on major infrastructure projects.

The best source in the set, because reaction counts *are* evidence density.
An open issue with 200 thumbs-up is a pain point several hundred people
independently confirmed, which is exactly the ranking signal this system is
built around -- no inference required.
"""

from __future__ import annotations

import time
from datetime import datetime

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


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _iso(ts: int) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def harvest(client: httpx.Client, since: int) -> tuple[list[Item], int]:
    items: list[Item] = []
    dropped = 0

    for repo in REPOS:
        query = (
            f"repo:{repo} is:issue is:open "
            f"reactions:>={MIN_REACTIONS} created:>={_iso(since)}"
        )
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
            created = issue.get("created_at", "")
            try:
                created_utc = int(
                    datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").timestamp()
                )
            except ValueError:
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

        # search endpoint allows 30/min authenticated
        time.sleep(2.2)

    return items, dropped
