"""Prior-art lookup against the real GitHub index.

Deliberately not "ask the model what already exists". A model's recall of
obscure tooling is unreliable in both directions -- it invents projects and it
forgets real ones -- and the whole system is built on the finding that grounding
beats asking. So we query the actual repository index and hand the results over
as evidence, exactly as with harvested complaints.
"""

from __future__ import annotations

import re
from collections import Counter

import httpx

from .config import GITHUB_TOKEN, USER_AGENT
from .domains import _DOMAIN_RE

SEARCH = "https://api.github.com/search/repositories"
MAX_QUERIES = 3
PER_QUERY = 5

_URL_RE = re.compile(r"https?://\S+|\b\w+\.(?:com|org|io|net|dev|sh)\b")

_STOP = {
    "https", "http", "www", "com", "org", "news", "item", "html", "github",
    "thread", "comment", "post", "article", "blog", "repo", "issue",
    "a", "an", "the", "for", "with", "that", "this", "your", "you", "and", "or",
    "of", "to", "in", "on", "at", "by", "from", "into", "under", "over",
    "build", "building", "builds", "built", "make", "makes", "using", "use",
    "tool", "toolkit", "system", "service", "platform", "framework", "library",
    "based", "new", "own", "when", "where", "which", "what", "how", "why",
    "plus", "without", "across", "every", "each", "per", "via",
}


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def queries_for(idea: dict) -> list[str]:
    """Search terms describing what the project *is*, topically.

    A project's name is useless here (nobody else called theirs "crashgov"), and
    so are consecutive words from the prose -- slicing the one-liner into
    3-grams produced queries like "daemon host independently", which match
    nothing. What works is the technical vocabulary: pull the domain-lexicon
    terms the idea actually uses and pair them with its most frequent content
    nouns.
    """
    text = " ".join([
        idea.get("title", ""), idea.get("one_liner", ""),
        idea.get("problem", ""), idea.get("why_hard", ""),
    ]).lower()
    # URLs in the prose leak their own vocabulary -- "https", "news", "com" --
    # straight into the queries. Strip them before tokenising.
    text = _URL_RE.sub(" ", text)

    # Domain terms are the reliable part -- they are curated and topical.
    jargon: list[str] = []
    for rx in _DOMAIN_RE.values():
        for m in rx.finditer(text):
            term = m.group(0).lower()
            if term not in jargon and len(term) > 2:
                jargon.append(term)

    # Frequent content words fill in project-specific vocabulary the lexicon
    # does not cover ("placement", "blame", "crash").
    counts = Counter(
        w for w in re.findall(r"[a-z][a-z0-9-]{3,}", text)
        if w not in _STOP and w not in jargon
    )
    common = [w for w, _ in counts.most_common(6)]

    # Queries go broad -> narrow. GitHub's repo search ANDs the terms, so a
    # long query returns nothing; two terms is usually the sweet spot.
    queries: list[str] = []
    if len(jargon) >= 2:
        queries.append(" ".join(jargon[:2]))
    if jargon and common:
        queries.append(f"{jargon[0]} {common[0]}")
    if len(jargon) >= 3:
        queries.append(f"{jargon[0]} {jargon[2]}")
    elif len(common) >= 2:
        queries.append(" ".join(common[:2]))

    return list(dict.fromkeys(queries))[:MAX_QUERIES]


def search(idea: dict) -> list[dict]:
    """Existing repositories that might already do this."""
    found: dict[str, dict] = {}
    with httpx.Client(timeout=30, headers=_headers()) as client:
        for query in queries_for(idea):
            try:
                resp = client.get(SEARCH, params={
                    "q": f"{query} stars:>50", "sort": "stars",
                    "order": "desc", "per_page": PER_QUERY,
                })
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            for repo in resp.json().get("items", []):
                name = repo.get("full_name")
                if name and name not in found:
                    found[name] = {
                        "name": name,
                        "stars": repo.get("stargazers_count", 0),
                        "description": (repo.get("description") or "")[:200],
                        "url": repo.get("html_url", ""),
                        "archived": bool(repo.get("archived")),
                        "pushed_at": (repo.get("pushed_at") or "")[:10],
                    }

    return sorted(found.values(), key=lambda r: r["stars"], reverse=True)[:12]


def format_for_prompt(repos: list[dict]) -> str:
    if not repos:
        return "(no repositories with >50 stars matched the search terms)"
    return "\n".join(
        f"- {r['name']} ({r['stars']}★, last push {r['pushed_at']}"
        f"{', ARCHIVED' if r['archived'] else ''}): {r['description']}"
        for r in repos
    )
