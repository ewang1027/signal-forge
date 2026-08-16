# signal-forge

Emails me two things: project ideas grounded in real complaints scraped off the
internet, and interview prep scheduled by FSRS.

Ideas land Monday, two or three at a time, each with a plain-language pass so an
idea above my level is still one I can choose. Prep lands Monday, Wednesday and
Saturday. Runs entirely on free infrastructure.

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
sets have narrow taste even when each individual idea looks fresh, and asking for variety
doesn't fix it. Instead the corpus is partitioned and the generator walks a deterministic
rotation of domains.

That rotation does real work: consecutive runs produced a deterministic container-placement
scheduler, a crash-point explorer for SQLite, and a type-error blame localizer built on
minimum-weight unsat cores. Same system, same week, no overlap.

[LLM ideas also rate as more novel but less feasible, and score lower once actually
built](https://arxiv.org/pdf/2506.20803), so every candidate goes through a feasibility gate
that forces a milestone breakdown and a first-weekend deliverable.

## Pipeline

```
[1 HARVEST]  free APIs -> raw signal            daily, no LLM
     |
[2 THEMES]   embed + cluster -> pain themes     sunday, local embeddings
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

Themes are rebuilt wholesale each run, but the embeddings underneath them are
cached per signal, so a rebuild costs the week's harvest rather than the whole
corpus.

Two independent runs, so a failure in one never blocks the other:

| Run | When | Does |
|---|---|---|
| daily | fires every morning; sends Mon/Wed/Sat | harvest, replies, prep, deliver — no LLM in the critical path |
| ideas | Mon | theme rebuild if stale, then 2-3 ideations, each gated |

The `daily` run fires every day but only sends on Mon/Wed/Sat. Harvesting and
reply-fetching need to run daily regardless, and keeping the send cadence in
`pipeline/config.py` rather than in the cron means a duplicate or hand-fired
dispatch can't produce an off-day email. It also makes the cadence testable.

### Sources

| Source | Access | Notes |
|---|---|---|
| HN (Algolia) | free, no key, 10k req/hr | phrase probes for complaint language |
| GitHub issues | 5k req/hr authenticated | reaction counts *are* evidence density |
| Lobste.rs | free | low volume, high systems density |

Reddit is deliberately absent: its `.json` endpoints now return 403 to unauthenticated
clients, and a source that silently returns nothing is worse than no source.

Every source runs through the same two gates, which live in `item.py` rather than in each
source so a new source can't accidentally skip them: domain (is this about systems?) and
pain (did someone actually hit a wall?). Both are required — a resume in a hiring thread
and a comment listing thirty technical nouns are both domain-dense and problem-free. About
95% of raw hits get rejected.

### Prep decks

Deck A is DSA patterns, not problems: cards are `monotonic stack`, `binary search on
answer`, `prefix sum + hashmap`, each with a rotating problem set behind it. Scheduling
individual problems trains recall of those problems; scheduling patterns generalizes.

Deck B is system design, weighted toward what interviews actually probe now (cost
reasoning, failure recovery, operational maturity) and toward the specific omissions that
sink candidates: Redis without an eviction policy, Kafka without ordering requirements,
sharding without rebalancing.

### Written to be readable

The ideas are meant to sit above my current level — that's the point of the system.
But an idea I can't follow is an idea I can't choose, however good it is, and the
first drafts were unreadable: *"PSI is a stall-time integral, so 'warning' is not
an event but a threshold crossing you choose"* in the second paragraph.

The fix is a ramp, not a simpler idea. Every idea carries a plain-language pass
before the precise one (what goes wrong today and why it's hard, in words an
intern knows), then the dense version underneath it untouched, then a glossary of
every term it used and a couple of things to go read. The shape gate requires all
of it, so an idea that arrives without its ramp doesn't ship.

### The gates

Each ideation produces three candidates; the gates decide whether any of them
ships. They run cheapest-first, and a run that rejects all three sends nothing —
the point of a gate is that "nothing" is an acceptable outcome. A weekly digest
wants two or three ideas, so it walks that many domain slices, with a ceiling on
how many generations one run may spend.

| Gate | Cost | What it does |
|---|---|---|
| shape | free | required fields present, milestones real, plain-language pass and glossary present |
| dedup | local embeddings | cosine against every idea ever generated |
| judged | one model call | prior art + feasibility, against a real repo search |

Prior art comes from searching GitHub, not from asking the model what exists.
Model recall of obscure tooling is unreliable in both directions (it invents
projects and forgets real ones), so the same grounding principle that governs the
harvester governs this. The search results go into the prompt as evidence.

A mature tool existing is not automatically fatal: a project attacking a
*specific documented failure* of an existing tool is often better than a
greenfield one, because the problem is already proven real. What is fatal is
rebuilding something that works fine with no articulated gap.

The gate returns `ship`, `reframe`, or `kill`. A reframe gets revised, not
annotated. Its critiques are consistently of the form "the problem is real but
this framing is wrong," so shipping unchanged would put claims in the digest the
pipeline has already concluded are false, while rejecting would discard a problem
the gate itself called real and unserved. Instead the critique goes back to the
model once, the revision is judged again, and only a clean verdict ships. A
candidate that still doesn't converge is dropped for the next one.

In practice the revisions concede rather than argue. One rewrote its own core as:
*"probe success is a one-sided certificate... RFC 8899 DPLPMTUD already ships
exactly that. Finding a working size is closed."* It then relocated the hard part
to the attribution problem that actually is open, and cut the scope by half.

Nothing ships carrying an objection the pipeline never addressed.

### The feedback loop

A push-only system gets ignored within a month. This is what makes it compound instead,
and it's also what makes the prep track self-driving, since nothing else ever grades a card.

Replies arrive over **IMAP, not a webhook**: a webhook needs a public endpoint and
something running to receive it, and this is a cron job with no server. `imaplib` is
stdlib.

One reply can do several things at once:

```
two-pointers good
dijkstra again
monoblame boring
```

That grades two cards, records a verdict on one idea, and down-weights the evidence behind
it. A verdict has to name its idea (the digest prints a short handle next to each one)
because a bare `boring` against three ideas can't be attributed, and guessing would move
the wrong idea's evidence weights and write the wrong line into `TASTE.md`. Anything
unattributable or unrecognized is kept as a note instead of applied. The parser is
deliberately loose: this gets typed on a phone, one-handed, and a format that demands
precision gets used twice and then never.

The subtler case: the digest quoted back. It lists every card id and the words
`more`/`boring` in its own footer, so parsing the quote would grade the whole deck off a
one-word reply. Quoted text is cut before anything else happens.

Feedback weight lives on signals, not themes, since themes are rebuilt with fresh
identities every run and anything stored on them is erased. A theme's weight is the mean
of its members', which also means a `boring` suppresses a pain point without killing it:
as fresh harvest joins the cluster the mean dilutes back toward neutral.

The quality canary watches for the failure that has no other symptom. A system that has
become irrelevant looks exactly like one that is working, from the inside — the only
observable difference is that nobody replies. After sustained silence it says so, with
numbers, and asks for one word back. It fires once per silent stretch rather than nagging.

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
and runs get silently dropped under load. A morning digest that arrives at noon is useless.
An external cron POSTing `workflow_dispatch` fires immediately.

**This repo contains no state.** All harvested data, the idea ledger, and prep history live
in a separate private repo checked out at runtime. That's a structural guarantee rather
than a gitignore rule: an unattended daily committer is exactly the situation where a soft
guard fails quietly.

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
points. It's never created automatically: a missing state directory is a fatal
error, because silently starting a fresh empty corpus and then committing it over
the real one is data loss with a green checkmark.

The prep track (`pipeline.prep`) is Phase 4 and not built yet.
