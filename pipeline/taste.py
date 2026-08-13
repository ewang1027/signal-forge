"""TASTE.md -- what has landed well and what hasn't.

Written by the feedback loop (Phase 5), read by ideation. Kept as prose rather
than structured preferences because it goes straight into a prompt, and because
the useful signal ("too infrastructure-flavoured, prefers algorithmic cores")
does not decompose into fields.
"""

from __future__ import annotations

from .config import TASTE_PATH
from .item import _DELIM_RE

DEFAULT = """\
# Taste

No feedback recorded yet. Until there is, follow the prompt's constraints as written.
"""


def load() -> str:
    if TASTE_PATH.is_file():
        text = TASTE_PATH.read_text().strip()
        if text:
            return text
    return DEFAULT


def _flatten(text: str, limit: int = 200) -> str:
    """Reply text goes into TASTE.md, which is interpolated verbatim into the
    ideation prompt. Left raw, a reply could forge extra bullets with its own
    newlines or close the evidence tag. Same sanitiser the harvester already
    applies to comment text."""
    return _DELIM_RE.sub("", " ".join(text.split()))[:limit]


def record(verdict: str, title: str, note: str = "") -> None:
    """Append an observation. Phase 5 calls this from inbound replies."""
    TASTE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TASTE_PATH.is_file():
        TASTE_PATH.write_text(DEFAULT)
    line = f"- `{verdict}` — {_flatten(title, 120)}"
    if note:
        line += f" · {_flatten(note)}"
    with TASTE_PATH.open("a") as fh:
        fh.write(f"{line}\n")
