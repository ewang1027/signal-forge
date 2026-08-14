"""Paths and env. State lives outside this repo on purpose -- see README."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


def env(name: str, default: str = "") -> str:
    """Environment lookup that treats empty as absent.

    `os.environ.get(k, default)` returns "" when the key exists but is blank,
    and GitHub Actions sets every referenced secret -- so an unconfigured
    `${{ secrets.IMAP_HOST }}` becomes an empty string and the default never
    applies. That silently dialled localhost instead of imap.gmail.com.
    """
    return os.environ.get(name) or default

REPO_ROOT = Path(__file__).resolve().parent.parent

# All mutable/personal state lives in the private state repo. Defaults to a
# sibling checkout locally; CI points STATE_DIR at its own checkout path.
STATE_DIR = Path(os.environ.get("STATE_DIR", REPO_ROOT.parent / "signal-forge-state"))
DB_PATH = STATE_DIR / "signal.db"
IDEAS_DIR = STATE_DIR / "ideas"
REJECTS_DIR = STATE_DIR / "rejects"
DECKS_DIR = STATE_DIR / "decks"
TASTE_PATH = STATE_DIR / "TASTE.md"

PROMPTS_DIR = REPO_ROOT / "prompts"

RESEND_API_KEY = env("RESEND_API_KEY", "")
DIGEST_TO = env("DIGEST_TO", "")
DIGEST_FROM = env("DIGEST_FROM", "")
NTFY_TOPIC = env("NTFY_TOPIC", "")
GITHUB_TOKEN = env("GITHUB_TOKEN", "")

MODEL = env("SIGNAL_FORGE_MODEL", "claude-opus-5")

USER_AGENT = "signal-forge/0.1 (+https://github.com/ewang1027/signal-forge)"


# --- delivery cadence ------------------------------------------------------
#
# Which days a digest goes out, and in whose timezone.
#
# The cadence lives here rather than in the cron schedule. The external cron
# fires `daily` every day -- harvesting and reply-fetching want to run daily
# regardless of whether there is an email -- and `deliver` decides whether
# today is a send day. Two things fall out of that:
#
#   * a duplicate or hand-fired dispatch cannot produce an off-day email, and
#   * the cadence is testable without a scheduler.
#
# Three digests were sent within 31 minutes on 2026-08-13 because nothing in
# the code had an opinion about how often it should send. Now it does.

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def weekdays(name: str, default: str) -> frozenset[int]:
    """Parse `"mon wed sat"` into weekday numbers matching `datetime.weekday()`."""
    out = set()
    for part in env(name, default).replace(",", " ").split():
        key = part.strip().lower()[:3]
        if key not in _DAYS:
            raise ValueError(f"{name}: {part!r} is not a weekday")
        out.add(_DAYS[key])
    return frozenset(out)


def _tz(name: str) -> ZoneInfo:
    """A bad zone name must not take down the run -- a typo would cost a whole
    digest, unattended, with the traceback buried in an Actions log."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"unknown DIGEST_TZ {name!r}, falling back to UTC", file=sys.stderr)
        return ZoneInfo("UTC")


# Keep this in step with the cron job's timezone, or "Monday" means two
# different things at either end and the idea run misses its own digest.
DIGEST_TZ = _tz(env("DIGEST_TZ", "America/New_York"))

# Prep goes out three times a week; ideas ride along on Monday's.
PREP_DAYS = weekdays("PREP_DAYS", "mon wed sat")
IDEA_DAYS = weekdays("IDEA_DAYS", "mon")

# Ideas per weekly digest. Each one comes from a different rotation slice, so
# this is also how many domain slices a single ideation run consumes.
IDEAS_PER_DIGEST = max(1, int(env("IDEAS_PER_DIGEST", "3")))


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
    for d in (IDEAS_DIR, DECKS_DIR, REJECTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
