# signal-forge

A self-notification system that emails me two things: **project ideas grounded in real
complaints scraped off the internet**, and **interview prep scheduled by FSRS**.

Ideas land Monday and Thursday. Prep lands daily. It runs on free infrastructure.

## Why it's built this way

The obvious version of this is `cron → ask an LLM for project ideas → email them`. That
produces slop, and there's research on exactly why. Three findings drove the architecture:

**Grounding beats prompting.** [HindSight](https://arxiv.org/abs/2603.15164) scored
LLM-generated ideas against what actually turned out to matter. Retrieval-grounded systems
produced **2.5× higher-scoring ideas** than ungrounded ones. So most of this repo is a
harvester, not a prompt.

**Don't let the model judge novelty.** The same paper found LLM-judged novelty is
*negatively* correlated with real impact. Ranking by "rate this 1-10 on novelty" actively
selects for worse ideas. So ranking keys off **evidence density** instead — how many
independent, recent, real complaints corroborate a given pain point. Novelty is demoted to
a dedup filter.

**Diversity has to be structural.** [Nova](https://arxiv.org/pdf/2410.14255) found LLM idea
*sets* have narrow taste even when each individual idea looks fresh. Asking for variety
doesn't fix it. Instead the corpus is partitioned and the generator walks a deterministic
rotation of domains.

In practice that rotation is doing real work — consecutive runs produced a deterministic
container-placement scheduler, a crash-point explorer for SQLite, and a type-error blame
localizer built on minimum-weight unsat cores. Same system, same week, no overlap.

And because [LLM ideas rate as more novel but less feasible, and score lower once actually
built](https://arxiv.org/pdf/2506.20803), every candidate goes through a feasibility gate
that forces a milestone breakdown and a first-weekend deliverable.

## Pipeline

```
[1 HARVEST]  free APIs -> raw signal            daily, no LLM
     |
[2 THEMES]   embed + cluster -> pain themes     sunday, local embeddings
     |       carrying recency-weighted evidence counts
     |
[3 IDEATE]   LLM on a rotated domain slice      mon + thu
     |
[4 GATE]     dedup ledger, prior-art search,    mon + thu
     |       feasibility pass, taste filter
     |
[5 PREP]     FSRS over two decks                daily, no LLM
     |
[6 DELIVER]  email + push
     ^
[7 FEEDBACK] replies -> theme weights, FSRS state, taste
```

Two independent runs, so a failure in one never blocks the other:

| Run | When | Does |
|---|---|---|
| daily | every morning | harvest, prep, deliver — no LLM in the critical path |
| ideas | Mon + Thu | theme rebuild if stale, ideate, gate |

### Sources

| Source | Access | Notes |
|---|---|---|
| HN (Algolia) | free, no key, 10k req/hr | phrase probes for complaint language |
| GitHub issues | 5k req/hr authenticated | reaction counts *are* evidence density |
| Lobste.rs | free | low volume, high systems density |

Reddit is deliberately absent — its `.json` endpoints now return 403 to unauthenticated
clients, and a source that silently returns nothing is worse than no source.

Every source runs through the same two gates, which live in `item.py` rather than in each
source so a new source cannot accidentally skip them: **domain** (is this about systems?)
and **pain** (did someone actually hit a wall?). Both are required, because they are
orthogonal — a resume in a hiring thread and a comment listing thirty technical nouns are
both domain-dense and problem-free. About 95% of raw hits are rejected, which is correct.

### Prep decks

Deck A is **DSA patterns, not problems** — cards are `monotonic stack`, `binary search on
answer`, `prefix sum + hashmap`, each with a rotating problem set behind it. Scheduling
individual problems trains recall of those problems; scheduling patterns generalizes.

Deck B is system design, weighted toward what interviews actually probe now — cost
reasoning, failure recovery, operational maturity — and toward the specific omissions that
sink candidates: Redis without an eviction policy, Kafka without ordering requirements,
sharding without rebalancing.

### The feedback loop

A push-only system gets ignored within a month. Replying to the digest with `boring`,
`exists`, `too easy`, `building this`, or `solved 24m` / `failed` feeds back into theme
weights, FSRS card state, and a learned taste file.

There's also a quality canary: if engagement drops off for two weeks, the system emails to
say it thinks it's become irrelevant and asks for recalibration.

## Stack

Everything is on a free tier.

| Concern | Choice |
|---|---|
| Compute | GitHub Actions |
| Scheduling | external cron → `workflow_dispatch` |
| LLM | Claude Opus 5 |
| State | SQLite + `sqlite-vec` |
| Embeddings | `sentence-transformers`, local |
| Email | Resend |
| Push | ntfy |

Two things worth knowing if you fork this:

**Don't use Actions `schedule`.** Free-tier cron drift now averages [several hours](https://github.com/orgs/community/discussions/196910)
and runs get silently dropped under load. A morning digest that arrives at noon is useless.
An external cron POSTing `workflow_dispatch` fires immediately.

**This repo contains no state.** All harvested data, the idea ledger, and prep history live
in a separate private repo checked out at runtime. That's a structural guarantee rather
than a gitignore rule — an unattended daily committer is exactly the situation where a
soft guard fails quietly.

## Running it

```sh
uv sync
cp .env.example .env        # fill in
git config core.hooksPath .githooks

uv run python -m pipeline.harvest      # pull signal
uv run python -m pipeline.themes       # cluster into pain themes
uv run python -m pipeline.ideate       # generate + gate one idea
uv run python -m pipeline.prep         # today's cards
uv run python -m pipeline.deliver      # send
```

See `samples/` for what the output actually looks like.
