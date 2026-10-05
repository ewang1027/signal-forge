# signal-forge

A personal pipeline that emails me project ideas built from real developer
complaints, plus interview prep cards scheduled with FSRS.

## Status

It ran every day from August 13 to September 12, 2026, and is not running now.
In that month it sent four Monday digests of three ideas each, and prep cards
every Monday, Wednesday and Saturday. The reply loop, where answering the email
grades cards and rates ideas, is built and tested but never saw a reply. The
harvest also stayed small: the corpus only grew from 188 to 216 posts, so later
ideas drew on mostly the same evidence.

## How it works

Two workflows, both started by an external cron through `workflow_dispatch`,
because the built-in Actions `schedule` can run hours late on the free tier.

- **daily** reads replies over IMAP, harvests new posts, and sends a digest if
  today is a send day. No LLM is involved.
- **ideas** runs on Mondays. It clusters the corpus into themes, then generates
  and filters ideas for the next digest.

The parts worth reading:

- **Grounding.** The harvester pulls posts from HN (Algolia), GitHub issues and
  Lobsters, and keeps only those about systems work where someone is clearly
  stuck. Most raw hits are dropped. Each idea is written from one theme's posts
  and has to cite at least two of them. Citations to posts that weren't supplied
  are stripped, and an idea left with fewer than two is rejected.
- **Ranking by evidence, not novelty.** Posts are embedded locally and clustered.
  A theme scores by how many independent people hit the problem (one vote per
  source and author, decayed by age), with a bonus when several sources agree.
  No LLM is asked to rate novelty.
- **Rotation for variety.** Themes fall into eight domains (distributed systems,
  storage, compilers, networking, ML infra, OS/kernel, observability, security).
  Each idea comes from the next domain in a fixed rotation, using the top theme
  whose evidence earlier ideas haven't mostly used, so variety comes from the
  structure instead of from asking the model for it.
- **Gates.** Each model call proposes three candidates. They go through a shape
  check (milestones, a plain-language explanation, a glossary), an embedding
  dedup against every past idea, and a model call that judges prior art and
  feasibility against a real GitHub search. If nothing passes, nothing ships.
- **Prep.** FSRS over 24 DSA patterns (patterns, not individual problems) and 21
  system design cards. Intervals are capped at 21 days by default so nothing
  goes stale.

Personal data (the corpus, idea history, review state) lives in a separate
private repo that the workflows check out and commit back to. This repo holds
only code and the card decks.

## Running it

Credentials and the cron setup are in [SETUP.md](SETUP.md).

```sh
uv sync --extra embed    # plain `uv sync` covers the daily path only
cp .env.example .env     # fill in
uv run pytest            # embedding tests are skipped without the extra

uv run python -m pipeline.harvest
uv run --extra embed python -m pipeline.themes
uv run python -m pipeline.ideate               # needs the `claude` CLI, signed in
uv run python -m pipeline.deliver --dry-run    # writes build/digest.html
```

State is read from a sibling `signal-forge-state` checkout, or from `STATE_DIR`.
It is never created automatically, so a wrong path fails instead of quietly
starting an empty corpus.
