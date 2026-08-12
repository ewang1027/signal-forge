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
import sys
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


def top_theme(conn: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    """Highest evidence-density theme in this domain that hasn't been used yet.

    `evidence * weight` is the ranking signal -- corroborated independent voices,
    adjusted by feedback. Explicitly NOT an LLM novelty score, which has been
    shown to anti-correlate with what turns out to matter.
    """
    return conn.execute(
        """
        SELECT t.* FROM theme t
        WHERE t.domain = ?
          AND (t.key IS NULL OR t.key NOT IN
               (SELECT theme_key FROM idea WHERE theme_key IS NOT NULL))
        ORDER BY (t.evidence * t.weight) DESC
        LIMIT 1
        """,
        (domain,),
    ).fetchone()


def theme_evidence(conn: sqlite3.Connection, theme_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.url, s.title, s.text, s.pain, s.domain_hits, s.created_utc, s.source
        FROM signal s
        JOIN theme_member m ON m.signal_id = s.id
        WHERE m.theme_id = ?
        ORDER BY (s.pain * 2 + s.domain_hits) DESC
        """,
        (theme_id,),
    ).fetchall()


def domain_evidence(conn: sqlite3.Connection, domain: str,
                    limit: int = EVIDENCE_PER_IDEA) -> list[sqlite3.Row]:
    """Fallback when a domain has no clustered theme yet: strongest individual
    complaints. Weaker grounding -- uncorroborated -- but better than nothing."""
    return conn.execute(
        """
        SELECT url, title, text, pain, domain_hits, created_utc, source
        FROM signal
        WHERE domain = ?
        ORDER BY (pain * 2 + domain_hits) DESC, created_utc DESC
        LIMIT ?
        """,
        (domain, limit),
    ).fetchall()


def gather_evidence(conn: sqlite3.Connection, domain: str) -> tuple[list[sqlite3.Row], str | None, str]:
    """Returns (evidence rows, theme key or None, provenance description)."""
    theme = top_theme(conn, domain)
    if theme is not None:
        rows = list(theme_evidence(conn, theme["id"]))[:EVIDENCE_PER_IDEA]
        if len(rows) >= 2:
            # top up with domain evidence so a 2-item theme still gets context
            if len(rows) < EVIDENCE_PER_IDEA:
                seen = {r["url"] for r in rows}
                extra = [r for r in domain_evidence(conn, domain, EVIDENCE_PER_IDEA)
                         if r["url"] not in seen]
                rows = rows + extra[: EVIDENCE_PER_IDEA - len(rows)]
            return rows, theme["key"], (
                f"theme {theme['key']} (evidence {theme['evidence']:.2f}, "
                f"n={theme['size']}): {theme['label']}"
            )
    return domain_evidence(conn, domain), None, "no clustered theme; using domain-wide evidence"


class UngroundedIdea(RuntimeError):
    """Raised when an idea cites evidence that was never in the corpus."""


def validate_refs(idea: dict, rows: list[sqlite3.Row], *, minimum: int = 2) -> list[str]:
    """Drop cited URLs that were not in the evidence we supplied.

    Models invent plausible-looking HN item IDs. Measured on the first four
    generations, 3 of 21 cited URLs pointed at items that were never in the
    corpus -- and the digest renders them under a heading that says "Evidence".
    For a system whose whole premise is grounding, shipping a fabricated
    citation is the single worst output it can produce.
    """
    supplied = {r["url"] for r in rows}
    cited = idea.get("evidence_refs") or []
    kept = [u for u in cited if u in supplied]

    dropped = len(cited) - len(kept)
    if dropped:
        print(f"  dropped {dropped}/{len(cited)} fabricated evidence refs", file=sys.stderr)
    if len(kept) < minimum:
        raise UngroundedIdea(
            f"only {len(kept)} of {len(cited)} cited URLs were real; "
            "refusing to ship an ungrounded idea"
        )
    return kept


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


def save(conn: sqlite3.Connection, idea: dict, domain: str, theme_key: str | None) -> str:
    ensure_state_dirs()
    now = int(time.time())
    base = slugify(idea.get("title", ""))

    # Disambiguate rather than letting INSERT OR IGNORE swallow the collision --
    # a dropped row means deliver finds nothing and reports success.
    slug = base
    if conn.execute("SELECT 1 FROM idea WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{now}"

    conn.execute(
        """
        INSERT INTO idea (slug, title, body, domain, theme_key, sent_utc)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (slug, idea.get("title", ""), json.dumps(idea, indent=2), domain, theme_key),
    )
    (IDEAS_DIR / f"{now}-{slug}.json").write_text(json.dumps(idea, indent=2))
    return slug


def main() -> int:
    with connect() as conn:
        domain = peek_domain(conn)
        rows, theme_key, provenance = gather_evidence(conn, domain)

        if len(rows) < 4:
            # Rotating onto a starved slice is normal early on. Skipping beats
            # generating from three comments and pretending it is grounded.
            # Consume the slot so the next run tries a different domain.
            print(f"{domain}: only {len(rows)} pieces of evidence, skipping")
            advance_domain(conn)
            return 0

        print(f"{domain}: {provenance}")
        print(f"{domain}: {len(rows)} pieces of evidence")
        idea = generate(domain, rows)
        idea["evidence_refs"] = validate_refs(idea, rows)
        slug = save(conn, idea, domain, theme_key)
        advance_domain(conn)

    print(f"\n{idea.get('title')}")
    print(f"  {idea.get('one_liner')}")
    print(f"\nhard part: {idea.get('why_hard')}")
    print(f"saved as {slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
