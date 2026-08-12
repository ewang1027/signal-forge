"""Prior-art lookup against the real GitHub index.

Deliberately not "ask the model what already exists". A model's recall of
obscure tooling is unreliable in both directions -- it invents projects and it
forgets real ones -- and the whole system is built on the finding that grounding
beats asking. So we query the actual repository index and hand the results over
as evidence, exactly as with harvested complaints.
"""

from __future__ import annotations

import json
import re
from collections import Counter

import httpx

from .config import GITHUB_TOKEN, USER_AGENT
from .domains import matched_terms
from .item import _DELIM_RE

SEARCH = "https://api.github.com/search/repositories"
MAX_QUERIES = 3
PER_QUERY = 5

_URL_RE = re.compile(r"https?://\S+|\b\w+\.(?:com|org|io|net|dev|sh)\b")


def _sanitize(text: str, limit: int) -> str:
    """Flatten untrusted text before it enters a prompt.

    Newlines are the important part: the prior-art block is a bullet list, so a
    description containing a newline can forge an extra bullet or a fake section
    header inside the gate's own prompt.
    """
    flat = " ".join(text.split())
    flat = _DELIM_RE.sub("", flat)
    return flat[:limit]

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


def queries_for(idea: dict, domain: str | None = None) -> list[str]:
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

    # Domain terms are the reliable part -- curated and topical. The idea's own
    # domain goes first: without it, DOMAINS insertion order made `distributed`
    # terms anchor every query, so a SQLite crash-explorer was searched for as
    # "sharded sharding" and came back with connection poolers. Irrelevant
    # results are worse than none -- the gate reads five plausible repos and
    # concludes the space is clear.
    jargon = [t for t in matched_terms(text, domain) if len(t) > 2]

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


def search(idea: dict, domain: str | None = None) -> tuple[list[dict], bool]:
    """Existing repositories that might already do this.

    Returns (repos, searched_ok). The flag matters: an empty list from a failed
    search looks identical to a genuinely clear field, and the gate would read
    that as permission to ship.
    """
    found: dict[str, dict] = {}
    attempted = failed = 0

    with httpx.Client(timeout=30, headers=_headers()) as client:
        for query in queries_for(idea, domain):
            attempted += 1
            try:
                resp = client.get(SEARCH, params={
                    # No `sort` -- GitHub's default is relevance. Sorting by
                    # stars returns the *biggest* repo matching a term rather
                    # than the closest, which is how a Springboot tutorial got
                    # cited as prior art. The star floor is low because the real
                    # competitors to a niche tool are themselves niche.
                    "q": f"{query} stars:>5",
                    "per_page": PER_QUERY,
                })
                resp.raise_for_status()
            except httpx.HTTPError:
                failed += 1
                continue
            except (ValueError, json.JSONDecodeError):
                failed += 1
                continue
            for repo in resp.json().get("items", []):
                name = repo.get("full_name")
                if name and name not in found:
                    found[name] = {
                        # Both fields are attacker-controlled: anyone can create
                        # a repo with any name and description, and a star count
                        # is not a trust boundary. This text goes into the gate's
                        # prompt, so it is sanitised like any harvested text.
                        "name": _sanitize(name, 80),
                        "stars": repo.get("stargazers_count", 0),
                        "description": _sanitize(repo.get("description") or "", 200),
                        "url": repo.get("html_url", ""),
                        "archived": bool(repo.get("archived")),
                        "pushed_at": (repo.get("pushed_at") or "")[:10],
                    }

    searched_ok = attempted > 0 and failed < attempted
    ranked = sorted(found.values(), key=lambda r: r["stars"], reverse=True)[:12]
    return ranked, searched_ok


def format_for_prompt(repos: list[dict], searched_ok: bool = True) -> str:
    if not searched_ok:
        return ("SEARCH UNAVAILABLE -- the repository search failed. Do NOT "
                "conclude the field is clear; treat prior art as unknown and "
                "say so.")
    if not repos:
        return "(no repositories matched the search terms)"
    return "\n".join(
        f"- {r['name']} ({r['stars']} stars, last push {r['pushed_at']}"
        f"{', ARCHIVED' if r['archived'] else ''}): {r['description']}"
        for r in repos
    )
