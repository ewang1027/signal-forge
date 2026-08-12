"""Domain and pain lexicons -- the two filters that decide what enters the corpus.

Two orthogonal axes, both required:

1. **Domain** -- is this about systems? Pain phrases like "the hard part is" are
   domain-agnostic, so probing HN for them alone returns MRI machines and MIDI
   recorders. Requiring domain vocabulary is what makes the corpus about systems.

2. **Pain** -- did someone actually hit a wall? A resume in a hiring thread and a
   comment listing thirty technical nouns both score high on domain terms while
   containing no problem at all.

Matching is word-boundary anchored, not substring. Naive substring matching is
badly wrong here: "ast" matches "past", "abi" matches "ability", "otel" matches
"hotel", "slo" matches "slow". A term ending in `*` is a stem and matches
suffixes ("quantiz*" -> quantize/quantization/quantized); everything else must
match as a whole word.
"""

from __future__ import annotations

import re

DOMAINS: dict[str, list[str]] = {
    "distributed": [
        "distributed", "consensus", "raft", "paxos", "quorum", "replicat*",
        "shard*", "partition tolerance", "eventual consistency",
        "linearizab*", "split brain", "leader election", "gossip", "crdt",
        "idempoten*", "exactly-once", "at-least-once", "backpressure",
        "kubernetes", "k8s", "etcd", "service mesh", "istio", "envoy",
        "microservice*", "rpc", "grpc", "load balanc*", "failover",
    ],
    "storage": [
        "database", "postgres", "mysql", "sqlite", "rocksdb", "lsm tree",
        "b-tree", "btree", "write-ahead", "mvcc", "vacuum", "wal",
        "index scan", "query planner", "columnar", "parquet", "iceberg",
        "object storage", "blob store", "compaction", "durability",
        "fsync", "transaction*", "isolation level", "deadlock", "migration*",
        "connection pool", "query plan", "full table scan", "backup*",
    ],
    "compilers": [
        "compiler*", "parser", "lexer", "type system", "type checker",
        "type infer*", "borrow checker", "llvm", "codegen", "bytecode",
        "interpreter", "garbage collect*", "gc pause", "monomorphi*",
        "macro expansion", "language server", "incremental compil*",
        "linker", "abi", "ffi", "ast", "lsp", "jit", "build system",
        "static analysis", "lint*", "transpil*", "wasm", "toolchain",
    ],
    "networking": [
        "tcp", "udp", "quic", "http/2", "http/3", "tls", "dns", "bgp",
        "packet loss", "round trip", "rtt", "congestion", "latency",
        "throughput", "bandwidth", "firewall", "vpn", "ipv6",
        "nat travers*", "websocket*", "reverse proxy", "cdn", "load balancer",
        "network partition", "mtu", "socket", "epoll", "io_uring", "timeout*",
    ],
    "ml-infra": [
        "inference", "training run", "gpu", "cuda", "tensor", "embedding*",
        "vector database", "vector search", "quantiz*", "kv cache",
        "batch inference", "model serving", "fine-tun*", "checkpoint*",
        "distributed training", "feature store", "data pipeline",
        "llm", "context window", "rag", "retrieval", "token limit",
        "hallucinat*", "prompt", "gpu memory", "vram", "batching",
    ],
    "os-kernel": [
        "kernel", "syscall", "scheduler", "cgroup*", "namespace", "ebpf",
        "page fault", "mmap", "virtual memory", "numa", "cache line",
        "false sharing", "memory barrier", "lock-free", "mutex", "atomics",
        "context switch", "interrupt", "filesystem", "inode", "device driver",
        "container runtime", "hypervisor", "virtualiz*", "oom", "swap",
    ],
    "observability": [
        "observability", "tracing", "opentelemetry", "otel", "prometheus",
        "cardinality", "log aggregation", "structured log*", "metrics",
        "profiling", "flamegraph", "apm", "datadog", "grafana", "jaeger",
        "alert fatigue", "on-call", "postmortem", "slo", "sli", "pagerduty",
        "root cause", "incident", "dashboards", "log volume", "sampling",
    ],
    "security": [
        "authentication", "authoriz*", "oauth", "jwt", "mtls", "certificate",
        "key rotation", "secret management", "sandbox*", "isolation",
        "supply chain", "sbom", "cve", "vulnerab*", "fuzzing", "memory safety",
        "side channel", "zero trust", "rbac", "capability-based",
    ],
}

ROTATION = list(DOMAINS.keys())


def _compile(terms: list[str]) -> re.Pattern[str]:
    parts = []
    for t in terms:
        if t.endswith("*"):
            parts.append(rf"\b{re.escape(t[:-1])}\w*")
        else:
            parts.append(rf"\b{re.escape(t)}\b")
    return re.compile("|".join(parts), re.IGNORECASE)


_DOMAIN_RE = {d: _compile(terms) for d, terms in DOMAINS.items()}


def score(text: str) -> dict[str, int]:
    """Distinct domain terms matched, per domain."""
    return {d: len(set(m.group(0).lower() for m in rx.finditer(text)))
            for d, rx in _DOMAIN_RE.items()}


def best_domain(text: str) -> tuple[str | None, int]:
    scores = score(text)
    domain = max(scores, key=lambda d: scores[d])
    return (domain, scores[domain]) if scores[domain] else (None, 0)


def is_technical(text: str, min_hits: int = 2) -> bool:
    """min_hits=2 rather than 1 because one generic term ('scheduler', 'socket')
    shows up in plenty of writing that is not a systems complaint."""
    return sum(score(text).values()) >= min_hits


PAIN_MARKERS = [
    "painful", "pain point", "frustrat*", "annoying", "nightmare",
    "broke", "broken", "fails", "failed", "failure", "crash*", "corrupt*",
    "workaround", "kludge", "band-aid",
    "no way to", "there is no", "impossible to", "cannot",
    "doesn't work", "does not work", "gave up", "had to",
    "wasted", "took us", "cost us", "spent days", "spent weeks", "spent months",
    "hard to", "difficult to", "tricky", "gotcha", "footgun",
    "limitation*", "bottleneck", "regression", "edge case",
    "struggl*", "unusable", "brittle", "fragile", "flaky",
    "why is there no", "wish there was", "sorely missing",
]

_PAIN_RE = _compile(PAIN_MARKERS)

# Story titles that reliably produce domain-dense, complaint-free text.
NOISE_TITLES = [
    "who wants to be hired", "who is hiring", "freelancer", "seeking work",
    "ask hn: who", "hiring thread",
]


def pain_score(text: str) -> int:
    return len(set(m.group(0).lower() for m in _PAIN_RE.finditer(text)))


def is_noise_thread(title: str) -> bool:
    low = (title or "").lower()
    return any(n in low for n in NOISE_TITLES)
