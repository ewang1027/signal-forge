#!/usr/bin/env python3
"""Add the plain-language pass to ideas queued before it was required.

Ideas generated before 2026-08-14 have no `in_plain_terms`, `glossary` or
`starting_points`, so they render without the ramp that makes a deliberately
over-my-level idea choosable. They are already past the gates and otherwise
fine, so they get the missing fields rather than being dropped.

Only **unsent** ideas are touched. A sent one is history; rewriting it would
change what a reply is replying to.

Usage:
    uv run python scripts/backfill-plainly.py [--dry-run]

Local runs authenticate from the Claude Code keychain. If `.env` carries a
stale `CLAUDE_CODE_OAUTH_TOKEN` it will shadow that and fail with a 401 --
blank it rather than deleting it, so the CI value stays documented.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import IDEAS_DIR, PROMPTS_DIR          # noqa: E402
from pipeline.db import connect                             # noqa: E402
from pipeline.gate import check_shape                       # noqa: E402
from pipeline.llm import complete_json                      # noqa: E402

FIELDS = ("in_plain_terms", "why_it_is_hard_plainly", "glossary", "starting_points")


def needs_backfill(idea: dict) -> bool:
    return not (idea.get("in_plain_terms") and idea.get("glossary"))


def plainly(idea: dict) -> dict:
    """Ask for the missing fields only. The idea itself is not regenerated."""
    template = (PROMPTS_DIR / "plainly.md").read_text()
    # Strip the fields being generated so a partial previous attempt cannot be
    # echoed back instead of a fresh one.
    source = {k: v for k, v in idea.items() if k not in FIELDS}
    result = complete_json(template.replace("{idea}", json.dumps(source, indent=2)))
    if isinstance(result, list):
        result = result[0] if result else {}
    if not isinstance(result, dict):
        raise ValueError(f"expected an object, got {type(result).__name__}")
    return result


def idea_file(slug: str) -> Path | None:
    """The JSON file for a slug. It is the dedup ledger's source of truth, so it
    has to be updated alongside the DB or the two disagree."""
    matches = sorted(IDEAS_DIR.glob(f"*-{slug}.json"))
    return matches[-1] if matches else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change without writing")
    args = ap.parse_args()

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM idea WHERE sent_utc IS NULL ORDER BY id ASC").fetchall()

        pending = [(r, json.loads(r["body"])) for r in rows]
        pending = [(r, i) for r, i in pending if needs_backfill(i)]

        if not pending:
            print("nothing to backfill; every queued idea has its plain pass")
            return 0

        print(f"{len(pending)} queued idea(s) missing the plain-language pass\n")
        failed = 0

        for row, idea in pending:
            print(f"  {row['slug'][:55]}")
            try:
                added = plainly(idea)
            except Exception as exc:
                print(f"    FAILED ({type(exc).__name__}: {exc})", file=sys.stderr)
                failed += 1
                continue

            merged = dict(idea)
            merged.update({k: v for k, v in added.items() if k in FIELDS})

            # Same bar a fresh idea has to clear. A backfill that produces a
            # one-line glossary is the failure this is meant to fix.
            verdict = check_shape(merged)
            if not verdict.ok:
                print(f"    REJECTED: {verdict.reason}", file=sys.stderr)
                failed += 1
                continue

            terms = ", ".join(t["term"] for t in merged["glossary"][:6])
            print(f"    +{len(merged['glossary'])} terms: {terms}")
            print(f"    {merged['in_plain_terms'][:100]}...")

            if args.dry_run:
                continue

            conn.execute("UPDATE idea SET body = ? WHERE id = ?",
                         (json.dumps(merged, indent=2), row["id"]))
            if path := idea_file(row["slug"]):
                path.write_text(json.dumps(merged, indent=2))
            else:
                print(f"    WARNING: no ideas/*.json for {row['slug']}",
                      file=sys.stderr)

        if args.dry_run:
            print("\n(dry run, nothing written)")
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
