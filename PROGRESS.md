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

## Known gaps

- Remaining harvest noise is **argumentative, not off-topic** — k8s-vs-docker flamewars
  and critiques of blog posts pass both gates. Lexical filters can't separate "complaining
  about a system" from "complaining about an article about a system". Theme clustering
  and the evidence review are the intended fix.
- `deliver.py` is only exercised via `--dry-run`; no Resend key yet, so a real send is
  unverified.
- No gates yet (dedup, prior-art, feasibility) — ideas ship unfiltered until Phase 3.

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
