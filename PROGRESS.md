# Progress log

Running log so the work can be picked up cold. Newest at the bottom.

## Phase 0 — repo setup

- Two repos: this one public (code only), `signal-forge-state` private (all data).
- Two hooks, covering different surfaces. `pre-commit` blocks anything under `state/`,
  attribution lines in staged *content*, and staged credentials (gitleaks when present,
  coarse regex fallback when not). `commit-msg` blocks attribution in the *message* —
  pre-commit can't see the message, which is easy to miss and was caught by testing the
  guard rather than assuming it worked. Enable both with
  `git config core.hooksPath .githooks`.
- All three guards verified by deliberately trying to violate them.
- Git identity uses the GitHub noreply address so a public repo doesn't leak a personal
  email into every commit.

## Phase 1 — thin end-to-end slice (done)

`harvest -> ideate -> render` works. Three ideas generated from three different
rotation slices, and they came out genuinely different from each other
(deterministic container placement / SQLite crash-point explorer / MaxSMT type-error
blame localization) — which was the point of the rotation and the thing most worth
verifying.

Two things the first cut got wrong, both found by looking at output rather than by
reasoning about the code:

- **Lexicon matched substrings.** `ast` matched "past", `otel` matched "hotel", `abi`
  matched "ability". An article about advertising was classified as `compilers`. Now
  word-boundary anchored, stems marked with `*`, covered by tests.
- **Topic and complaint are orthogonal.** Filtering on domain alone kept a resume from
  a hiring thread and a comment listing thirty technical nouns — both domain-dense,
  neither a problem. Harvest now requires domain hits *and* pain markers. Rejection
  rate is ~95%, which is correct: the corpus should be small and real.

Corpus is 154 rows from HN alone over 540 days. Thin. Phase 2 adds the other sources.

## Phase 2 — grounding (done)

Three sources now: HN (Algolia), GitHub issues, Lobste.rs. **Reddit is deliberately
out** — its `.json` endpoints return 403 unauthenticated, and a source that silently
yields nothing is worse than no source.

Corpus: 200 rows. Sources are pluggable (`pipeline/sources/`), and the gates live in
`item.py` rather than in each source so a new source cannot accidentally skip them.

**Clustering threshold was swept against the real corpus, not guessed:**

| threshold | clusters n>=2 | largest | verdict |
|---|---|---|---|
| 0.35 | 10 | 4 | too tight, nothing corroborates |
| **0.45** | **26** | **7** | **one specific complaint per cluster** |
| 0.55 | 40 | 23 | drifting topical |
| 0.65 | 33 | 25 | "everything about Rust", useless |

At 0.65 the top theme was `rust, zig, compiler, code, checker` (n=41) — a field, not a
pain point. At 0.45 themes read like `sqlite, wal, merkle hashes` (n=7) and
`zig, rust, allocator, footgun` (n=7). The 125 singletons are honest: one person's
opinion is not a theme.

Ideation now grounds on the top-evidence **theme** in the rotation domain, falling back
to domain-wide rows when a domain has no cluster yet.

## Phase 2 review — fixed

An independent review of Phases 0-2 found several bugs that were live. Fixed:

- **Theme rowids are recycled.** `DELETE FROM theme` resets SQLite's rowid counter, so
  `idea.theme_id = 7` excluded whichever unrelated cluster later landed on id 7 — which
  was suppressing the *highest-scoring theme in the database*. Themes now carry a
  content-derived `key` (sha1 of sorted member ids); `idea.theme_key` matches on that.
- **The diversity multiplier was inverted.** It counted distinct sources over raw rows,
  so one maximally-stale row bought the full 1.5×. Decomposed on real data: a lobsters
  row the decay valued at **0.05 was contributing 35% of the top theme's score**. Now
  scaled by each source's weighted contribution.
- **Lobsters timestamps were all stubbed to one value**, which is what made every
  lobsters row stale in the first place. These two bugs were multiplying each other.
- **`evidence_refs` were never validated** — 3 of 21 cited URLs across the first four
  ideas were invented HN item IDs, rendered under a heading that says "Evidence".
  Fabricated citations are the worst possible output for a system premised on grounding.
  Now intersected with supplied evidence; an idea with <2 real refs is refused.
- **No migration path.** `CREATE TABLE IF NOT EXISTS` never adds columns. Indexes now run
  *after* an explicit `ALTER TABLE` pass — the first attempt at this fix failed
  immediately because an index on the new column ran before the column existed.
- **`deliver` could send the same email twice** — an ntfy failure raised before the
  commit, so the email went out while the DB still said unsent. Email → update → commit,
  push after, in a try/except. Also `ORDER BY id DESC` stranded older ideas forever.
- **Security.** The fallback secret scanner's `sk-ant-[a-z0-9-]` class had no underscore,
  so it let `CLAUDE_CODE_OAUTH_TOKEN` (base64url) straight through — the one credential
  the system exists to protect. An unquoted path list also let a filename containing a
  space bypass the attribution scan entirely. Both fixed and tested.
- **The hooks weren't installed where they mattered.** `core.hooksPath` is local config
  that no clone inherits, and the *state* repo — the one the pipeline commits to
  unattended — had no hooks at all. `scripts/install-hooks.sh` installs both; the state
  repo declares `signalforge.role=state` so it keeps the credential guards but may hold
  data.
- **`ensure_state_dirs()` silently created a missing `STATE_DIR`**, turning a wrong path
  into a fresh empty corpus that a run would then commit over the real one. Now fatal.

29 tests, covering each of these as a regression.

**Correction to an earlier note:** O(n²) clustering is *not* a looming problem. Measured:
n=5,000 → 2.9s / 200 MB; n=10,000 → 11.8s / 800 MB. At the observed intake rate the
corpus reaches 1,000 rows in years. The real cost is re-embedding every row on every
build (cache vectors per `signal.id`), and the real constraint on corpus size is the
pain lexicon, not any API limit.

## Phase 3 — anti-slop gates (done)

Ideation generates 3 candidates in one call; gates run cheapest-first (shape →
embedding dedup → one model call for prior art + feasibility); the first survivor
ships and **nothing ships if all three fail**. Rejects are kept in `rejects/` — they
are the only way to tell whether the gates are calibrated or merely strict.

**Verification step 4 passes.** Fed the plan's test case — "build a distributed
tracing system" — the gate killed it with: *the stated hard part (context propagation)
is a solved one-liner via W3C `traceparent`, and the first weekend is scaffolding that
proves nothing.* That is the right rejection for the right reason.

**Two bugs caught while writing, both silent-failure class:**

- `hash()` is salted per process, so the ledger key would have differed every run and
  the dedup would have matched nothing, forever. Uses sha1 now.
- The prior-art query builder sliced the one-liner into consecutive 3-grams, producing
  queries like `daemon host independently` that matched zero repositories — a gate that
  always returns "no prior art found" is worse than no gate. Rebuilt on the domain
  lexicon; URLs in the prose were also leaking `https` and `news` into queries.

**Dedup threshold measured, not guessed.** The plan specified 0.85. Against real
generations, distinct ideas topped out at **0.605** and an actual duplicate pair scored
**0.830** — so 0.85 let a true duplicate through, which it did. Set to 0.75, in the
middle of the empty band.

**Theme keys turned out to be unstable, so exclusion moved to evidence.** The Phase 2
fix replaced recycled rowids with a content hash, which solved *aliasing* but not
*stability*: a theme's key is a hash of its members, and members join as the corpus
grows. Measured — **adding 3 rows invalidated 2 of 25 theme keys**. Since harvest runs
daily, within weeks every used theme quietly becomes a "new" theme and eligible again.
Confirmed in the live DB: one of the two existing ideas already pointed at a theme key
that no longer existed.

Exclusion now keys off `idea_signal` — which harvested rows an idea was built from.
Signal ids never change. A theme is skipped when >50% of its evidence has already been
written about, and that holds through arbitrary re-clustering. Two regression tests
cover it, including one that renames a theme and confirms it stays excluded.

So the layers are: **used-evidence overlap** (exact, primary) and **embedding
similarity** (fuzzy, backstop for two themes converging on the same project). The fuzzy
layer genuinely can miss — a hand-written paraphrase of a known duplicate scored 0.698,
below threshold. Worth knowing rather than pretending otherwise.

## Phase 3 review — fixed

A review found three ways the gates were failing open. All were invisible without
looking at real output.

- **Prior art was searching the wrong topic.** `queries_for` never received the
  domain, so `DOMAINS` insertion order made `distributed` terms anchor every query:
  the SQLite crash-explorer was searched as `sharded sharding` and returned connection
  poolers. This is *worse* than the empty results it replaced — the gate reads five
  plausible repos and concludes the field is clear. Also dropped `sort=stars` (returns
  the biggest repo matching a term rather than the closest — a Springboot tutorial got
  cited as prior art) and lowered the floor to 5 stars, since the real competitor to a
  niche tool is itself niche. Now finds `criticalstack/e2d` (31★, gossip-based etcd
  manager) for the restart-budget idea.
- **The judged gate rejected only the literal string `"kill"`.** `{}`, a null verdict,
  a truncated response, and the word `"reject"` all shipped. A gate whose default on
  malformed output is *pass* is not a gate. Allow-list now.
- **`idea_signal` recorded all 14 supplied rows, not the cited ones.** `gather_evidence`
  pads a theme to 14 rows with unrelated material, so one idea marked 14 signals spent
  on the strength of a 3-member theme — measured at **100% of themes burned in
  `distributed` and `networking` after a single idea**. And the `domain_evidence`
  fallback had no exclusion at all, so once a domain burned out it served the identical
  14 rows forever. Now records cited evidence only (3 per idea, verified) and the
  fallback excludes used rows.

Also fixed: prompts were shipping `{{ }}` to the model because the code uses
`replace()` not `format()`; GitHub repo descriptions are attacker-controlled and went
into the gate prompt unsanitised (a newline forges a bullet); a failed search was
indistinguishable from a clean one; one candidate's exception killed the other two; the
idea JSON was written *before* commit, so a rollback orphaned it into the dedup ledger
permanently; and the pre-commit hook blocked its own `.env.example`, which trains the
`--no-verify` habit that disarms every check.

46 tests.

## Known gaps

- Remaining harvest noise is **argumentative, not off-topic** — k8s-vs-docker flamewars
  and critiques of blog posts pass both gates. Lexical filters can't separate "complaining
  about a system" from "complaining about an article about a system".
- `deliver.py` is only exercised via `--dry-run`; no Resend key yet, so a real send is
  unverified.
- **`reframe` verdicts currently ship with a note attached** rather than being rewritten
  or rejected. Open question whether that is right: one observed run shipped an idea the
  gate described as having an "inflated" hard core with a weekend 1 that is "mostly
  plumbing". The critique is genuinely valuable content, but shipping a known-weak idea
  with a warning is not obviously better than trying the next candidate.
- **Lobsters items get `created_utc = since`**, not a real timestamp — the per-comment
  dates are strings that weren't worth parsing during Phase 2. This feeds the recency
  decay in `evidence_score()`, so lobsters evidence is mis-weighted. Fix before the
  corpus grows.
- **GitHub source skews to popular feature proposals**, not bug reports — sorting by
  reactions surfaces "add SIMD intrinsics" (353 👍) over failure reports. Arguably still
  valid demand evidence, but consider a parallel `label:bug` query.
- Corpus is small (~200 rows), so evidence scores are all low. Needs weeks of intake
  before the ranking really discriminates.

### Open findings from the review, roughly by leverage

1. **The pain lexicon misses the voice the best signal is written in.** Four realistic,
   on-topic systems complaints written in flat incident-report register were all
   rejected. Phrases doing the work — *"fell over", "took eleven hours", "we lost every
   alert", "blocks the whole cluster", "OOMs", "throughput collapses"* — match nothing in
   `domains.py`. The list is heavy on explicit whining and light on incident prose.
   65% of the corpus sits at exactly `pain=2`, one marker from being dropped, so **corpus
   size is a function of lexicon coverage, not of what's out there.** Highest-leverage
   single change in the codebase. Also `\boom\b` never matches "OOMs", and "spent weeks"
   never matches "spent three weeks".
2. **`engagement` is written by every source and read by nothing.** GitHub reaction counts
   — literal quantified corroboration — are ignored by ranking. A theme carrying 83
   reactions ranked below one built from anonymous comments. Fix *after* #3, since raw
   reactions currently measure proposal popularity. Note HN rows all have
   `engagement=0`: Algolia comment hits carry no `num_comments` field, so that source's
   value is structurally dead.
3. **GitHub queries surface proposals, not bugs.** `is:issue is:open reactions:>=15
   sort:reactions` on these repos is a language-design-proposal detector — roughly 2 of
   the top 20 are defect reports, and `is:open` compounds it since proposals stay open
   for years while bugs get closed. Add a parallel `label:bug` query and drop `is:open`
   for it (a *closed* high-reaction bug is stronger evidence the pain was real).
4. **Rotation starvation is structural.** 3 of 8 slices have no clustered theme and fall
   back to `domain_evidence()`, which has no recency filter at all. `observability` is a
   permanently dead slot — every time the cursor lands there the run produces nothing and
   burns the delivery. Make rotation evidence-aware (skip to the next domain with enough
   evidence) and balance the probe list per domain.
5. **A deterministic generation failure deadlocks the rotation forever.** The cursor only
   advances on success — right for transient failures, wrong for a prompt that always
   fails. Add a per-domain consecutive-failure counter and advance after 2.
6. **Nothing signals failure.** All sources down still exits 0 with a green check. Add a
   heartbeat (`last_harvest_utc` / `last_deliver_utc` in `kv`) and ntfy if either goes
   >48h stale.
7. **Prompt injection is unguarded.** Raw harvested text is interpolated into the prompt
   inside `<evidence>` tags; any commenter can write `</evidence>` and then instructions.
   Low stakes now, real in Phase 3 (prior-art search may use tools) and Phase 5 (inbound
   email). Strip the tags in `clean_html`.
8. **`themes.build()` resets feedback weights.** It inserts without `weight`, so every
   rebuild restores `DEFAULT 1.0` and `ORDER BY (evidence * weight)` is always just
   `evidence`. Latent until Phase 5 writes weights, but it will silently erase them.
9. **Prompt injection is mitigated, not solved.** Delimiter stripping handles the
   obvious case; unicode lookalikes (`‹evidence›`, fullwidth `＜`) still pass, and
   nothing defends against instructions that need no tags at all. Two untrusted
   channels now reach prompts — harvested comments and GitHub repo descriptions — and
   the gate is the component an attacker would most want to influence.
10. **Smaller:** GitHub timestamps are parsed naive and read as local time (`.replace(tzinfo=utc)`);
   a bad `GITHUB_TOKEN` 403 is treated as a rate limit and burns 36 minutes sleeping;
   the prompt goes through `argv` (131 KB cap, and evidence text is visible in `ps`) and
   should use stdin; domain ties break by dict order so `distributed` wins every tie
   (7% of rows); embeddings are recomputed from scratch every build; the committed
   `signal.db` is ~1 MB of poorly-delta-compressing binary per commit — consider
   committing a `.dump` instead; `USER_AGENT` hardcodes the GitHub handle.

---

## Picking this up cold

```sh
cd ~/signal-forge && uv sync --extra embed
export GITHUB_TOKEN=$(gh auth token)

uv run python -m pipeline.harvest                    # ~7 min, all sources
uv run --extra embed python -m pipeline.themes       # cluster + score
uv run python -m pipeline.ideate                     # costs one Opus call
uv run python -m pipeline.deliver --dry-run          # renders build/digest.html
```

State lives in `../signal-forge-state`. Delete `signal.db` to start the corpus over.

**Next up is Phase 4** (interview prep): FSRS scheduler over two decks — DSA *patterns*
rather than individual problems, and system design weighted toward cost reasoning,
failure recovery, and operational maturity. Needs an interview target date to ramp
backward from. Then Phase 5 (feedback loop + quality canary). Nothing is wired to
GitHub Actions yet — no workflow files, no external cron.

**Before the first real send** you need: `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`,
a Resend key + verified sender, an ntfy topic, and an interview target date for the
Phase 4 ramp. Never set `ANTHROPIC_API_KEY` — it takes precedence and bills the API
account instead of the subscription.

## Open decisions

- **Interview target date** not set yet. Phase 4's intensity ramp schedules backward from
  it; until it's set the prep track runs at the low "staying sharp" volume.
- **Idea cadence** is Mon/Thu. If the harvester turns out to accumulate new signal slower
  than that, drop to weekly rather than letting it re-rank a stale corpus.
- **ntfy vs Pushover** — starting on ntfy since it's free and needs no account. Pushover
  ($5 once) is the fallback if ntfy delivery proves unreliable.
- **SMS** deliberately deferred. Carrier email-to-SMS gateways are dead and fail silently;
  10DLC is a 2-4 week carrier review. Toll-free verification is the fast lane if real SMS
  becomes worth it.
