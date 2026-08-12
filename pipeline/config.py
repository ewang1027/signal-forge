"""Paths and env. State lives outside this repo on purpose -- see README."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

# All mutable/personal state lives in the private state repo. Defaults to a
# sibling checkout locally; CI points STATE_DIR at its own checkout path.
STATE_DIR = Path(os.environ.get("STATE_DIR", REPO_ROOT.parent / "signal-forge-state"))
DB_PATH = STATE_DIR / "signal.db"
IDEAS_DIR = STATE_DIR / "ideas"
DECKS_DIR = STATE_DIR / "decks"
TASTE_PATH = STATE_DIR / "TASTE.md"

PROMPTS_DIR = REPO_ROOT / "prompts"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
DIGEST_TO = os.environ.get("DIGEST_TO", "")
DIGEST_FROM = os.environ.get("DIGEST_FROM", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

MODEL = os.environ.get("SIGNAL_FORGE_MODEL", "claude-opus-5")

USER_AGENT = "signal-forge/0.1 (+https://github.com/ewang1027/signal-forge)"


class StateMissing(RuntimeError):
    pass


def ensure_state_dirs() -> None:
    """Create the subdirectories, but never STATE_DIR itself.

    Creating it silently turns a wrong or unset path into a brand-new empty
    database that the run then reports as a success -- and since the pipeline
    commits unattended, it would commit that empty DB over the real corpus with
    a green checkmark. A missing state checkout is fatal, not something to
    paper over.
    """
    if not STATE_DIR.is_dir():
        raise StateMissing(
            f"state directory does not exist: {STATE_DIR}\n"
            "Clone signal-forge-state next to this repo, or set STATE_DIR. "
            "Refusing to create it -- that would silently start an empty corpus."
        )
    for d in (IDEAS_DIR, DECKS_DIR):
        d.mkdir(parents=True, exist_ok=True)
