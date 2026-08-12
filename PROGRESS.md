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

## Known gaps

- Remaining harvest noise is **argumentative, not off-topic** — k8s-vs-docker flamewars
  and critiques of blog posts pass both gates. Lexical filters can't separate "complaining
  about a system" from "complaining about an article about a system".
- `deliver.py` is only exercised via `--dry-run`; no Resend key yet, so a real send is
  unverified.
- No gates yet (dedup, prior-art, feasibility) — ideas ship unfiltered until Phase 3.
- **Lobsters items get `created_utc = since`**, not a real timestamp — the per-comment
  dates are strings that weren't worth parsing during Phase 2. This feeds the recency
  decay in `evidence_score()`, so lobsters evidence is mis-weighted. Fix before the
  corpus grows.
- **GitHub source skews to popular feature proposals**, not bug reports — sorting by
  reactions surfaces "add SIMD intrinsics" (353 👍) over failure reports. Arguably still
  valid demand evidence, but consider a parallel `label:bug` query.
- Corpus is small (200 rows), so evidence scores are all low (max 3.24). Needs to
  accumulate over weeks before the ranking really discriminates.
- Clustering is O(n²) and rebuilt wholesale each run. Fine at 200 rows; will need
  attention somewhere in the low thousands.

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

**Next up is Phase 3** (anti-slop gates): dedup ledger via embedding similarity against
every idea ever sent, prior-art search, feasibility pass, `TASTE.md`. Phases 4-5 after
that. Nothing is wired to GitHub Actions yet — no workflow files, no external cron.

**Phase 3 must treat `ideas/*.json` as the source of truth, not the `idea` table.**
Deleting `signal.db` during a corpus rebuild drops the table but leaves the JSON files
intact — that already happened once, leaving 4 files against 1 table row. Since the whole
point of the dedup ledger is that it never forgets an idea, it has to rebuild itself from
the files rather than trusting the DB.

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
