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

from pathlib import Path

from . import gate, ledger, taste
from .gate import Verdict
from .config import IDEAS_DIR, PROMPTS_DIR, REJECTS_DIR, ensure_state_dirs
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


# Fraction of a theme's evidence that may already have been written about
# before the theme is considered spent. Comparison is `<=` so that a 2-member
# theme with one cited row (exactly 0.5) stays eligible -- most themes are
# size 2, and `<` would retire them on a single shared row.
MAX_REUSED_EVIDENCE = 0.5


def top_theme(conn: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    """Highest evidence-density theme in this domain whose evidence is mostly unused.

    `evidence * weight` is the ranking signal -- corroborated independent voices,
    adjusted by feedback. Explicitly NOT an LLM novelty score, which has been
    shown to anti-correlate with what turns out to matter.

    Exclusion is by *evidence overlap*, not theme identity. Theme keys are hashes
    of member sets, and members join as the corpus grows, so a used theme quietly
    becomes a new theme and would be eligible again. Signal ids never change.
    """
    return conn.execute(
        """
        SELECT * FROM (
            SELECT t.*,
                   CAST((SELECT COUNT(*) FROM theme_member m
                         WHERE m.theme_id = t.id
                           AND m.signal_id IN (SELECT signal_id FROM idea_signal))
                        AS REAL) / MAX(t.size, 1) AS reused
            FROM theme t
            WHERE t.domain = ?
        )
        WHERE reused <= ?
        ORDER BY (evidence * weight) DESC
        LIMIT 1
        """,
        (domain, MAX_REUSED_EVIDENCE),
    ).fetchone()


def theme_evidence(conn: sqlite3.Connection, theme_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.id, s.url, s.title, s.text, s.pain, s.domain_hits, s.created_utc, s.source
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
    complaints. Weaker grounding -- uncorroborated -- but better than nothing.

    Excludes already-cited rows for the same reason `top_theme` does. Without
    that, this deterministic ordering hands back the identical 14 rows on every
    visit forever, and the fallback becomes a duplicate-idea generator.
    """
    return conn.execute(
        """
        SELECT id, url, title, text, pain, domain_hits, created_utc, source
        FROM signal
        WHERE domain = ?
          AND id NOT IN (SELECT signal_id FROM idea_signal)
        ORDER BY (pain * 2 + domain_hits) DESC, created_utc DESC
        LIMIT ?
        """,
        (domain, limit),
    ).fetchall()


def gather_evidence(conn: sqlite3.Connection, domain: str) -> tuple[list[sqlite3.Row], str | None, str]:
    """Returns (evidence rows, theme key or None, provenance description).

    The theme key is carried for provenance only -- exclusion keys off the
    signal ids in the returned rows.
    """
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


def generate(domain: str, rows: list[sqlite3.Row]) -> list[dict]:
    """Three candidates in one call. Cheaper than three calls, and the model can
    see all three at once and push them apart."""
    template = (PROMPTS_DIR / "ideate.md").read_text()
    prompt = (template
              .replace("{domain}", domain)
              .replace("{evidence}", format_evidence(rows))
              .replace("{taste}", taste.load()))

    result = complete_json(prompt)
    candidates = result if isinstance(result, list) else [result]
    return [c for c in candidates if isinstance(c, dict)]


def save(conn: sqlite3.Connection, idea: dict, domain: str,
         theme_key: str | None, rows: list[sqlite3.Row]) -> tuple[str, Path]:
    """DB writes only. The JSON file is written by the caller *after* commit --
    ideas/*.json is the dedup ledger's source of truth, so a file written for a
    transaction that later rolls back would permanently block regenerating an
    idea that was never recorded and can never be sent."""
    ensure_state_dirs()
    now = int(time.time())
    base = slugify(idea.get("title", ""))

    # Disambiguate rather than letting INSERT OR IGNORE swallow the collision --
    # a dropped row means deliver finds nothing and reports success.
    slug = base
    if conn.execute("SELECT 1 FROM idea WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{now}"

    cur = conn.execute(
        """
        INSERT INTO idea (slug, title, body, domain, theme_key, sent_utc)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (slug, idea.get("title", ""), json.dumps(idea, indent=2), domain, theme_key),
    )
    # Record only the evidence the idea actually *cited*, not everything it was
    # shown. gather_evidence pads a theme out to 14 rows with unrelated
    # domain-wide material, so recording `rows` marked ~14 signals consumed for
    # a 3-member theme -- enough to burn a whole domain's themes with one idea.
    # evidence_refs has already been validated against the corpus.
    cited = set(idea.get("evidence_refs") or [])
    used = [r["id"] for r in rows if r["id"] is not None and r["url"] in cited]
    conn.executemany(
        "INSERT OR IGNORE INTO idea_signal (idea_id, signal_id) VALUES (?, ?)",
        [(cur.lastrowid, sid) for sid in used],
    )
    return slug, IDEAS_DIR / f"{now}-{slug}.json"


def save_reject(candidate: dict, verdicts: list, domain: str, index: int = 0) -> None:
    ensure_state_dirs()
    now = int(time.time())
    # Index disambiguates: shape rejection is instant, so two untitled
    # candidates in the same second would otherwise write the same filename.
    slug = f"{slugify(candidate.get('title', ''))}-{index}"
    payload = {
        "domain": domain,
        "rejected_utc": now,
        "verdicts": [{"stage": v.stage, "ok": v.ok, "reason": v.reason} for v in verdicts],
        "candidate": candidate,
    }
    (REJECTS_DIR / f"{now}-{slug}.json").write_text(json.dumps(payload, indent=2))


def _embedder():
    """Deferred so `--help` and the daily prep run never import torch."""
    from .themes import embed
    return embed


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

        embed_fn = _embedder()
        added = ledger.sync(conn, embed_fn)
        if added:
            print(f"ledger: embedded {added} past idea(s)")

        candidates = generate(domain, rows)
        print(f"generated {len(candidates)} candidates\n")

        survivor = None
        for i, candidate in enumerate(candidates, 1):
            title = candidate.get("title", "(untitled)")
            print(f"[{i}] {title[:70]}")

            try:
                candidate["evidence_refs"] = validate_refs(candidate, rows)
            except UngroundedIdea as exc:
                print(f"    [REJECT] grounding: {exc}")
                # Fabricated-citation rejects are the highest-value calibration
                # data there is; they were the one class never being recorded.
                save_reject(candidate, [Verdict(False, "grounding", str(exc))],
                            domain, i)
                continue

            # A failure on one candidate must not discard the other two -- they
            # are already paid for, and with Mon/Thu delivery a network blip on
            # candidate 1 would otherwise cost a whole send.
            try:
                ok, revised, verdicts = gate.run(conn, candidate, embed_fn, domain)
            except Exception as exc:
                print(f"    [ERROR] gate: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue

            for v in verdicts:
                print(f"    {v}")
            if ok:
                survivor = revised
                break
            # Keep rejects. They are the only way to tell whether the gates are
            # calibrated or just strict, and they cost nothing to store.
            save_reject(candidate, verdicts, domain, i)

        if survivor is None:
            # Every candidate failed. Do NOT ship the least-bad one -- the gates
            # exist precisely to make "nothing" an acceptable outcome. Exit 2 so
            # a run that shipped nothing is distinguishable from one that did.
            print("\nall candidates rejected; nothing to ship")
            advance_domain(conn)
            return 2

        slug, path = save(conn, survivor, domain, theme_key, rows)
        advance_domain(conn)

    # Transaction is committed here. Only now is it safe to write the file that
    # the dedup ledger treats as proof the idea exists.
    path.write_text(json.dumps(survivor, indent=2))
    with connect() as conn:
        ledger.sync(conn, embed_fn)

    print(f"\n{survivor.get('title')}")
    print(f"  {survivor.get('one_liner')}")
    print(f"\nhard part: {survivor.get('why_hard')}")
    print(f"saved as {slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
