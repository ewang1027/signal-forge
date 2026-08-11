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
