# Setup

Everything here is free. Budget about 30 minutes, most of it waiting on DNS.

Work top to bottom — each section says what breaks if you skip it, so you can stop
early and still have a working subset.

---

## 1. Claude — the model (5 min)

The one credential the whole design protects. It runs Opus on your **Max
subscription**, which is why the system costs nothing to run.

```sh
claude setup-token
```

Copy the token it prints. It starts `sk-ant-oat01-…`.

> **Never set `ANTHROPIC_API_KEY` anywhere in this project.** If both are present the
> API key wins and every run bills your API account instead of the subscription.
> People have run up four figures this way. The workflows deliberately don't
> reference it, and the pre-commit hook blocks it by name.

**Without this:** no ideas. Prep still works.

---

## 2. Resend — outbound email (10 min)

Free tier is 3,000 emails/month; this system sends ~30.

1. Sign up at [resend.com](https://resend.com).
2. **API Keys → Create**, sending permission. Copy it (`re_…`).
3. Decide your sender:

   **Option A — no DNS, works immediately.** Use `onboarding@resend.dev` as
   `DIGEST_FROM`. Resend only allows this to send to *the address you signed up
   with*, so `DIGEST_TO` must be that same address. Fine for this system, since
   you're the only recipient.

   **Option B — your own domain.** Domains → Add, then add the DKIM/SPF records
   Resend gives you. Verification usually lands in under an hour. Then
   `DIGEST_FROM` can be anything `@yourdomain`.

Start with A. You can switch later by changing one secret.

**Without this:** nothing is delivered at all.

---

## 3. Gmail App Password — inbound replies (5 min)

This is what closes the feedback loop, and it's also the **only** thing that ever
grades a prep card. Without it the scheduler never advances and you get the same
cards forever.

1. Google Account → Security → **2-Step Verification** must be on (App Passwords
   don't exist without it).
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Create one named `signal-forge`. You get 16 lowercase letters. **Remove the
   spaces.**

That is *not* your Google password, and it can be revoked independently.

### Verify this on day one

Reply to the first digest with `boring` and run the daily workflow again. If the
log doesn't say `1 reply`, the likely cause is that **Gmail marks mail you send to
yourself as already read**, which defeats the `UNSEEN` filter. Fix:

- Gmail → Settings → Filters → Create: `to: <your address>` and `subject: Re:` →
  Apply label `signal-forge`, and **do not** mark as read.
- Set the `IMAP_FOLDER` secret to `signal-forge`.

**Without this:** ideas still arrive, prep cards never advance, and the canary
eventually complains that you've gone quiet.

---

## 4. GitHub PAT — writing state (2 min)

The workflows check out the private state repo and commit the corpus back to it.

[github.com/settings/tokens](https://github.com/settings/tokens) → **Tokens
(classic)** → Generate → scope **`repo`** → no expiry (or set a reminder).

**Without this:** every run starts from an empty corpus and nothing accumulates.

---

## 5. ntfy — phone push (2 min, optional)

Free, no account. Pick an unguessable topic name — **the topic string is the only
auth**, so treat it as a secret:

```sh
python3 -c "import secrets; print('signalforge-' + secrets.token_urlsafe(12))"
```

Install the ntfy app, subscribe to that topic. Test it:

```sh
curl -d "hello" ntfy.sh/YOUR-TOPIC
```

**Without this:** email still works; you just don't get a lock-screen nudge.

---

## 6. Add the secrets

`gh secret set NAME --repo ewang1027/signal-forge`, or paste them at
Settings → Secrets and variables → Actions.

| Secret | Value | Required |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | from step 1 | for ideas |
| `RESEND_API_KEY` | `re_…` from step 2 | **yes** |
| `DIGEST_TO` | the address you read | **yes** |
| `DIGEST_FROM` | `onboarding@resend.dev`, or your domain | **yes** |
| `STATE_REPO_PAT` | from step 4 | **yes** |
| `IMAP_PASSWORD` | app password, no spaces | for feedback |
| `IMAP_USER` | your Gmail address | if it differs from `DIGEST_TO` |
| `IMAP_FOLDER` | `signal-forge` | only if you made the filter |
| `NTFY_TOPIC` | from step 5 | optional |

Optional repo **variables** (not secrets):

| Variable | Default | What it does |
|---|---|---|
| `PREP_INTENSITY` | `recruiting` | `ramping` / `sharp` relax the 21-day freshness cap and cut volume |
| `DIGEST_TZ` | `America/New_York` | whose Monday it is — must match the cron job's timezone |
| `PREP_DAYS` | `mon wed sat` | days the prep email goes out |
| `IDEA_DAYS` | `mon` | days ideas ride along |
| `IDEAS_PER_DIGEST` | `3` | ideas per weekly digest, and slices one ideation run walks |

```sh
gh secret set RESEND_API_KEY --repo ewang1027/signal-forge
gh variable set PREP_INTENSITY --body recruiting --repo ewang1027/signal-forge
```

---

## 7. Trigger it by hand first

```sh
gh workflow run daily.yml  --repo ewang1027/signal-forge
gh workflow run ideas.yml  --repo ewang1027/signal-forge
gh run watch --repo ewang1027/signal-forge
```

Check: the email arrived, the state repo got a commit, and the Actions log shows
no errors. `ideas.yml` exiting 0 with "all candidates rejected" is a **normal**
outcome — the gates are allowed to ship nothing.

---

## 8. The external cron (5 min)

**Do not add `schedule:` to the workflows.** Free-tier Actions cron drift now
averages several hours and runs get dropped silently under load — a 7am digest
arriving at noon is useless. `workflow_dispatch` fires immediately, so an external
cron calls it.

Create a fine-grained PAT with **Actions: read and write** on this repo only, then
at [cron-job.org](https://cron-job.org) (free, and it shows failure history) add
two jobs:

Or just run `uv run python scripts/setup-cron.py` with `CRON_KEY`, `GH_PAT` and
`TZ_NAME` set — it creates both jobs, and re-running updates rather than
duplicating them. Note that `TZ_NAME` must match `DIGEST_TZ` (below), or "Monday"
means two different things at either end.

**Daily digest** — every day at your preferred time:
```
POST https://api.github.com/repos/ewang1027/signal-forge/actions/workflows/daily.yml/dispatches
Headers:  Authorization: Bearer <PAT>
          Accept: application/vnd.github+json
Body:     {"ref":"main"}
```

The daily job fires **every day** even though the digest only goes out Mon/Wed/Sat.
Harvesting and reply-fetching want to run daily regardless; the send cadence lives
in `pipeline/config.py` so that a duplicate dispatch can't produce an off-day email.

**Ideas** — Monday, 20 minutes earlier, so the week's ideas are queued when the
digest assembles:
```
POST https://api.github.com/repos/ewang1027/signal-forge/actions/workflows/ideas.yml/dispatches
(same headers, same body)
```

A 204 means accepted.

---

## What it costs

| | |
|---|---|
| GitHub Actions | $0 — public repos get unlimited minutes |
| Claude Opus | $0 — your Max subscription, via the OAuth token |
| Resend | $0 — 3,000/mo free, this sends ~30 |
| ntfy | $0 |
| cron-job.org | $0 |
| Gmail IMAP | $0 |

**After a week, check [console.anthropic.com](https://console.anthropic.com) shows
$0 API usage.** Anything above zero means `ANTHROPIC_API_KEY` leaked into the
environment somewhere and you're paying per token.

---

## Running it locally

```sh
uv sync --extra embed
cp .env.example .env          # same values as the secrets above
./scripts/install-hooks.sh    # installs guards into BOTH repos

uv run python -m pipeline.harvest
uv run --extra embed python -m pipeline.themes
uv run python -m pipeline.ideate
uv run python -m pipeline.deliver --dry-run     # renders build/digest.html
uv run python -m pipeline.deliver --force       # sends off-cadence, or twice in a day
uv run python -m pipeline.feedback
```

Grade a card without email:

```sh
uv run python -m pipeline.prep grade dsa two-pointers good
uv run python -m pipeline.prep --quiet          # counts only, safe for logs
```

State lives in a sibling `signal-forge-state` checkout, or wherever `STATE_DIR`
points. It is never created automatically — a missing state directory is a fatal
error, because silently starting a fresh corpus and then committing it over the
real one is data loss with a green checkmark.
