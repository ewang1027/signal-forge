"""TASTE.md -- what has landed well and what hasn't.

Written by the feedback loop (Phase 5), read by ideation. Kept as prose rather
than structured preferences because it goes straight into a prompt, and because
the useful signal ("too infrastructure-flavoured, prefers algorithmic cores")
does not decompose into fields.
"""

from __future__ import annotations

from .config import TASTE_PATH

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


def record(verdict: str, title: str, note: str = "") -> None:
    """Append an observation. Phase 5 calls this from inbound replies."""
    TASTE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TASTE_PATH.is_file():
        TASTE_PATH.write_text(DEFAULT)
    line = f"- `{verdict}` — {title}" + (f" · {note}" if note else "")
    with TASTE_PATH.open("a") as fh:
        fh.write(f"{line}\n")
