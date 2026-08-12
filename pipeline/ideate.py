"""Stage 3 -- generate one idea from a rotated slice of the corpus.

The rotation is the important part. LLM idea *sets* are narrow even when each
individual idea looks fresh, and asking a model for variety does not fix it.
Walking a deterministic cursor over domain slices does, because each run sees a
structurally different corpus.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from .config import IDEAS_DIR, PROMPTS_DIR, ensure_state_dirs
from .db import connect, get_kv, set_kv
from .domains import ROTATION
from .llm import complete_json

EVIDENCE_PER_IDEA = 14


def peek_domain(conn: sqlite3.Connection) -> str:
    """Current slice, without consuming it."""
    cursor = int(get_kv(conn, "rotation_cursor", "0"))
    return ROTATION[cursor % len(ROTATION)]


def advance_domain(conn: sqlite3.Connection) -> None:
    """Consume the slice. Called only after a successful generation -- otherwise
    a transient model failure silently burns a slot, and with ideas going out
    twice a week that means a whole delivery with nothing in it."""
    cursor = int(get_kv(conn, "rotation_cursor", "0"))
    set_kv(conn, "rotation_cursor", str(cursor + 1))


def gather_evidence(conn: sqlite3.Connection, domain: str, limit: int = EVIDENCE_PER_IDEA) -> list[sqlite3.Row]:
    """Strongest complaints in this domain.

    Ranked by pain and domain density, NOT by engagement. Popular threads are
    announcements; we want the comment where somebody describes hitting a wall.
    """
    return conn.execute(
        """
        SELECT url, title, text, pain, domain_hits, created_utc
        FROM signal
        WHERE domain = ?
        ORDER BY (pain * 2 + domain_hits) DESC, created_utc DESC
        LIMIT ?
        """,
        (domain, limit),
    ).fetchall()


def format_evidence(rows: list[sqlite3.Row]) -> str:
    chunks = []
    for i, r in enumerate(rows, 1):
        chunks.append(
            f"--- [{i}] {r['url']}\n"
            f"thread: {r['title']}\n\n"
            f"{r['text'][:1600]}"
        )
    return "\n\n".join(chunks)


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:60] or "idea"


def generate(domain: str, rows: list[sqlite3.Row]) -> dict:
    template = (PROMPTS_DIR / "ideate.md").read_text()
    prompt = template.replace("{domain}", domain).replace("{evidence}", format_evidence(rows))
    return complete_json(prompt)


def save(conn: sqlite3.Connection, idea: dict, domain: str) -> str:
    ensure_state_dirs()
    slug = slugify(idea.get("title", ""))
    now = int(time.time())

    conn.execute(
        """
        INSERT OR IGNORE INTO idea (slug, title, body, domain, sent_utc)
        VALUES (?, ?, ?, ?, NULL)
        """,
        (slug, idea.get("title", ""), json.dumps(idea, indent=2), domain),
    )
    (IDEAS_DIR / f"{now}-{slug}.json").write_text(json.dumps(idea, indent=2))
    return slug


def main() -> int:
    with connect() as conn:
        domain = peek_domain(conn)
        rows = gather_evidence(conn, domain)

        if len(rows) < 4:
            # Rotating onto a starved slice is normal early on. Skipping beats
            # generating from three comments and pretending it is grounded.
            # Consume the slot so the next run tries a different domain.
            print(f"{domain}: only {len(rows)} pieces of evidence, skipping")
            advance_domain(conn)
            return 0

        print(f"{domain}: {len(rows)} pieces of evidence")
        idea = generate(domain, rows)
        slug = save(conn, idea, domain)
        advance_domain(conn)

    print(f"\n{idea.get('title')}")
    print(f"  {idea.get('one_liner')}")
    print(f"\nhard part: {idea.get('why_hard')}")
    print(f"saved as {slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
