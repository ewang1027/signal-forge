# signal-forge

Emails me two things: project ideas grounded in real complaints scraped off the
internet, and interview prep scheduled by FSRS. Ideas land Monday, two or three
at a time; prep lands Monday, Wednesday and Saturday. Runs entirely on free
infrastructure.

## Why it's built this way

`cron → ask an LLM for project ideas → email them` produces slop, and there's
research on exactly why. Three findings drove the architecture:

- **Grounding beats prompting.** [HindSight](https://arxiv.org/abs/2603.15164)
  scored LLM ideas against what turned out to matter: retrieval-grounded systems
  produced **2.5× higher-scoring ideas**. So most of this repo is a harvester,
  not a prompt.
- **Don't let the model judge novelty.** The same paper found LLM-judged novelty
  is *negatively* correlated with real impact. Ranking keys off **evidence
  density** instead — how many independent, recent complaints corroborate a pain
  point — and novelty is demoted to a dedup filter.
- **Diversity has to be structural.** [Nova](https://arxiv.org/pdf/2410.14255)
  found LLM idea sets have narrow taste even when each idea looks fresh, and
  asking for variety doesn't fix it. So the corpus is partitioned and the
  generator walks a deterministic rotation of domains.

[Built LLM ideas also score lower than they read](https://arxiv.org/pdf/2506.20803),
so every candidate must survive a feasibility gate that forces a milestone
breakdown and a first-weekend deliverable.

## Pipeline

```
[1 HARVEST]  free APIs -> raw signal            daily, no LLM
     |
[2 THEMES]   embed + cluster -> pain themes     monday, local embeddings
     |       carrying recency-weighted evidence counts
     |
[3 IDEATE]   LLM over 2-3 rotated domain        monday
     |       slices, one idea from each
     |
[4 GATE]     dedup ledger, prior-art search,    monday
     |       feasibility, taste, plain-language
     |
[5 PREP]     FSRS over two decks                mon/wed/sat, no LLM
     |
[6 DELIVER]  email + push                       mon/wed/sat
     ^
[7 FEEDBACK] replies -> theme weights, FSRS state, taste
```

Two independent runs, so a failure in one never blocks the other:

| Run | When | Does |
|---|---|---|
| daily | fires every morning; sends Mon/Wed/Sat | harvest, replies, prep, deliver — no LLM in the critical path |
| ideas | Mon | rebuild themes, then 2-3 ideations, each gated |

The send cadence lives in `pipeline/config.py`, not in the cron: harvesting and
reply-fetching run daily regardless, and keeping it in code means a duplicate
dispatch can't produce an off-day email. Themes are rebuilt wholesale, but their
embeddings are cached per signal, so a rebuild costs the week's harvest rather
than the whole corpus.

### Sources

| Source | Access | Notes |
|---|---|---|
| HN (Algolia) | free, no key, 10k req/hr | phrase probes for complaint language |
| GitHub issues | 5k req/hr authenticated | reaction counts *are* evidence density |
| Lobste.rs | free | low volume, high systems density |

Reddit is deliberately absent: its `.json` endpoints now return 403 to
unauthenticated clients, and a source that silently returns nothing is worse than
no source.

Both filters live in `item.py` rather than in each source, so a new source can't
skip them: domain (is this about systems?) and pain (did someone actually hit a
wall?). Both are required — a resume in a hiring thread and a comment listing
thirty technical nouns are domain-dense and problem-free. About 95% of raw hits
get rejected.

### Prep decks

Deck A is DSA *patterns*, not problems — `monotonic stack`, `binary search on
answer` — each with a rotating problem set behind it. Scheduling problems trains
recall of those problems; scheduling patterns generalizes. Deck B is system
design, weighted toward the omissions that sink candidates: Redis without an
eviction policy, Kafka without ordering requirements, sharding without
rebalancing.

### The gates

Three candidates per ideation, cheapest gate first. A run that rejects all three
sends nothing — the point of a gate is that "nothing" is an acceptable outcome.

| Gate | Cost | What it does |
|---|---|---|
| shape | free | required fields, real milestones, plain-language pass and glossary |
| dedup | local embeddings | cosine against every idea ever generated |
| judged | one model call | prior art + feasibility, against a real repo search |

Prior art comes from searching GitHub, not from asking the model what exists —
its recall of obscure tooling invents projects and forgets real ones. A mature
tool existing is not automatically fatal: attacking a *specific documented
failure* of one is often better than greenfield, because the problem is already
proven real. Rebuilding something that works fine with no articulated gap is.

A `reframe` verdict is revised and re-judged once rather than shipped with a
warning attached, so nothing ships carrying an objection the pipeline never
addressed. A candidate that still doesn't converge is dropped for the next one.

Ideas are meant to sit above my current level, so each one carries a
plain-language pass before the precise version, then a glossary and a couple of
things to go read. An idea I can't follow is one I can't choose; the shape gate
requires the ramp.

### The feedback loop

A push-only system gets ignored within a month, and nothing else ever grades a
prep card. Replies arrive over **IMAP, not a webhook** — a webhook needs a public
endpoint and something running to receive it, and this is a cron job with no
server. One reply can do several things at once:

```
two-pointers good
dijkstra again
monoblame boring
```

Two cards graded, a verdict recorded, and the evidence behind that idea
down-weighted. A verdict has to name its idea — the digest prints a short handle
next to each — because a bare `boring` against three of them would move the wrong
idea's evidence weights; anything
unattributable is kept as a note instead. Quoted text is cut first — the digest
lists every card id in its own footer, so parsing the quote back would grade the
whole deck off a one-word reply.

Weight lives on signals, not themes, since themes are rebuilt with fresh
identities every run. A quality canary watches for the failure with no other
symptom: a system that has become irrelevant looks exactly like one that is
working, except that nobody replies.

## Stack

Everything is on a free tier.

| Concern | Choice |
|---|---|
| Compute | GitHub Actions |
| Scheduling | external cron → `workflow_dispatch` |
| LLM | Claude Opus 5 |
| State | SQLite |
| Embeddings | `sentence-transformers`, local |
| Email | Resend |
| Push | ntfy |

Two things worth knowing if you fork this:

**Don't use Actions `schedule`.** Free-tier cron drift now averages [several hours](https://github.com/orgs/community/discussions/196910)
and runs get silently dropped under load. An external cron POSTing
`workflow_dispatch` fires immediately.

**This repo contains no state.** Harvested data, the idea ledger and prep history
live in a separate private repo checked out at runtime — a structural guarantee
rather than a gitignore rule, because an unattended daily committer is exactly
where a soft guard fails quietly.

## Setup

See **[SETUP.md](SETUP.md)** — every credential, where to get it, what it costs
(nothing), and what breaks if you skip it.

## Running it

```sh
uv sync --extra embed       # plain `uv sync` REMOVES the embedding deps
cp .env.example .env        # fill in
./scripts/install-hooks.sh  # installs guards into this repo AND the state repo

uv run python -m pipeline.harvest               # pull signal from all sources
uv run --extra embed python -m pipeline.themes  # cluster into pain themes
uv run python -m pipeline.ideate                # generate one grounded idea
uv run python -m pipeline.deliver --dry-run     # render to build/digest.html
```

State lives in a sibling `signal-forge-state` checkout, or wherever `STATE_DIR`
points. It's never created automatically: silently starting a fresh empty corpus
and then committing it over the real one is data loss with a green checkmark.

Pull it before running anything locally. The scheduled runs commit to it every
day, and `signal.db` is a binary, so a stale checkout diverges rather than merges.
