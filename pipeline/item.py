"""The unit of harvested signal, and the gates every source runs through.

Gates live here rather than in each source so that adding a source cannot
accidentally bypass them -- the filtering is the reason the corpus is worth
anything, and it should not be a thing each source remembers to do.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from .domains import best_domain, is_noise_thread, pain_score

MIN_DOMAIN_HITS = 2  # one generic term is not evidence of a systems topic
MIN_PAIN = 2         # topic and complaint are orthogonal; require both
MIN_CHARS = 220      # below this it is a quip, not a report

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


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


def clean_html(raw: str) -> str:
    text = raw.replace("<p>", "\n\n").replace("</p>", "\n")
    text = _TAG_RE.sub("", text)
    return _WS_RE.sub("\n\n", html.unescape(text)).strip()


def gate(item: Item, *, require_pain: bool = True) -> Item | None:
    """Apply the shared filters, annotating the item on success.

    require_pain=False exists for sources that are not complaint-shaped by
    nature -- arXiv abstracts describe a problem without whining about it -- but
    it is off by default because most sources need it.
    """
    if len(item.text) < MIN_CHARS:
        return None
    if is_noise_thread(item.title):
        return None

    domain, hits = best_domain(f"{item.title}\n{item.text}")
    if hits < MIN_DOMAIN_HITS:
        return None

    pain = pain_score(item.text)
    if require_pain and pain < MIN_PAIN:
        return None

    item.domain = domain
    item.domain_hits = hits
    item.pain = pain
    return item
