"""Stage 6 -- render and send the digest.

Email carries the content; push is a nudge. `--dry-run` writes the rendered HTML
to disk instead of sending, which is how this gets tested without burning a send
or needing credentials.

## Cadence

Prep goes out on `PREP_DAYS` (Mon/Wed/Sat), ideas ride along on `IDEA_DAYS`
(Mon). The external cron still fires this every day -- harvest and reply-fetching
want to run daily -- so the decision about whether there is an email to send is
made here, in code, where it can be tested and where a duplicate dispatch cannot
route around it.
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import feedback, prep
from .replies import SENTINEL
from .config import (DIGEST_FROM, DIGEST_TO, DIGEST_TZ, IDEA_DAYS,
                     IDEAS_PER_DIGEST, NTFY_TOPIC, PREP_DAYS, RESEND_API_KEY,
                     USER_AGENT)
from .db import connect, get_kv, set_kv

CSS = """
body{font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     color:#1a1a1a;background:#fff;margin:0;padding:24px}
.wrap{max-width:620px;margin:0 auto}
h1{font-size:21px;line-height:1.3;margin:0 0 4px}
.sub{color:#666;font-size:15px;margin:0 0 22px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#888;
   margin:26px 0 6px;font-weight:600}
p{margin:0 0 12px}
.tag{display:inline-block;background:#f0f0f0;color:#555;font-size:11px;
     padding:2px 8px;border-radius:3px;letter-spacing:.04em;text-transform:uppercase}
.hard{background:#fafafa;border-left:3px solid #d0d0d0;padding:12px 16px;margin:8px 0}
ol{margin:0;padding-left:20px}
li{margin-bottom:6px}
a{color:#0645ad}
.ev a{display:block;font-size:13px;color:#666;margin-bottom:3px}
.foot{margin-top:34px;padding-top:14px;border-top:1px solid #eee;
      color:#888;font-size:13px}
code{background:#f4f4f4;padding:1px 5px;border-radius:3px;font-size:14px}
.idea+.idea{margin-top:40px;padding-top:30px;border-top:2px solid #eee}
.count{color:#999;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
       margin:0 0 10px}
.plain{background:#f4f8ff;border-left:3px solid #9dbdf0;padding:12px 16px;
       margin:8px 0}
.gloss li{margin-bottom:8px}
.gloss b{font-weight:600}
"""


def _esc(v) -> str:
    return html.escape(str(v or ""))


def render_idea(idea: dict, domain: str, reply_id: str = "",
                position: str = "") -> str:
    milestones = "".join(f"<li>{_esc(m)}</li>" for m in idea.get("milestones", []))
    evidence = "".join(
        f'<a href="{_esc(u)}">{_esc(u)}</a>' for u in idea.get("evidence_refs", [])
    )

    closest = ""
    if repo := idea.get("closest_existing"):
        closest = (f' Closest existing: <a href="https://github.com/{_esc(repo)}">'
                   f'{_esc(repo)}</a>.')

    # Ideas no longer ship with an unaddressed objection -- a reframe is revised
    # and re-judged first. What is worth showing is that it happened, so the
    # scope claims can be read with the right amount of trust.
    reframe = ""
    if note := idea.get("revision_note"):
        reframe = (f'<h2>Sharpened after review</h2>'
                   f'<div class="hard">{_esc(note)}</div>')

    feas = ""
    if f := idea.get("feasibility"):
        feas = f' <span class="tag">scope: {_esc(f)}</span>'

    # The handle is what gets typed back to judge THIS idea. With two or three
    # ideas in one digest a bare `boring` is ambiguous, and guessing which one
    # it meant would put the verdict -- and the evidence weight change it drives
    # -- on the wrong idea.
    handle = (f'<p class="count">reply <code>{_esc(reply_id)}</code> + a verdict</p>'
              if reply_id else "")
    counter = f'<p class="count">{_esc(position)}</p>' if position else ""

    # The plain-language pass comes first, and the precise one stays underneath
    # it untouched. The point is a ramp into an idea that is deliberately above
    # the reader's current level -- not a simpler idea. An idea that cannot be
    # understood cannot be chosen, however good it is.
    plain = ""
    if p := idea.get("in_plain_terms"):
        plain = f'<h2>In plain terms</h2><div class="plain">{_esc(p)}</div>'
    if p := idea.get("why_it_is_hard_plainly"):
        plain += f'<h2>Why it\'s hard, plainly</h2><div class="plain">{_esc(p)}</div>'

    # Terms are defined at the bottom rather than inline so the dense sections
    # stay readable straight through, with somewhere to look when they don't.
    glossary = ""
    if terms := idea.get("glossary"):
        items = "".join(
            f'<li><b>{_esc(t.get("term"))}</b> — {_esc(t.get("means"))}</li>'
            for t in terms if isinstance(t, dict) and t.get("term")
        )
        if items:
            glossary = f'<h2>Words used above</h2><ul class="gloss">{items}</ul>'

    starts = ""
    if refs := idea.get("starting_points"):
        items = "".join(f"<li>{_esc(r)}</li>" for r in refs if r)
        if items:
            starts = f"<h2>Read these first</h2><ul>{items}</ul>"

    return f"""<div class="idea">
{counter}<span class="tag">{_esc(domain)}</span>{feas}
<h1>{_esc(idea.get('title'))}</h1>
<p class="sub">{_esc(idea.get('one_liner'))}</p>
{handle}
{plain}
<h2>The problem</h2>
<p>{_esc(idea.get('problem'))}</p>

<h2>Why it's hard — the precise version</h2>
<div class="hard">{_esc(idea.get('why_hard'))}</div>

<h2>What you'd learn</h2>
<p>{_esc(idea.get('what_you_learn'))}</p>

<h2>First weekend</h2>
<p>{_esc(idea.get('first_weekend'))}</p>

<h2>Then</h2>
<ol>{milestones}</ol>

<h2>Prior art</h2>
<p>{_esc(idea.get('prior_art'))}{closest}</p>
{reframe}
<h2>Kill criteria</h2>
<p>{_esc(idea.get('kill_criteria'))}</p>
{glossary}{starts}
<h2>Evidence</h2>
<div class="ev">{evidence}</div>
</div>
"""


def render_prep(day: dict) -> str:
    """The daily prep block. On idea days this sits below the fold."""
    if not day:
        return ""

    def card_list(cards: list[dict], key: str) -> str:
        out = []
        for c in cards:
            detail = _esc(c.get(key, ""))
            extra = ""
            if trap := c.get("trap"):
                extra = f'<div class="hard">trap: {_esc(trap)}</div>'
            # The id is what you type back to grade it. Without it in the email
            # there is no way to reply, and the scheduler never advances.
            out.append(
                f"<li><b>{_esc(c.get('name'))}</b> "
                f'<code>{_esc(c.get("id"))}</code><br>{detail}{extra}</li>'
            )
        return "".join(out)

    blocks = []

    if problems := day.get("problems"):
        items = "".join(
            f'<li><b>{_esc(p["problem"])}</b> '
            f'<span class="tag">{_esc(p["pattern"])}</span>'
            f'<div class="hard">cue: {_esc(p["cue"])}</div></li>'
            for p in problems
        )
        blocks.append(f'<h2>Timed problems — 25 min each</h2><ol>{items}</ol>')

    if stuck := day.get("leeches"):
        items = "".join(
            f'<li><b>{_esc(c.get("name"))}</b> — {_esc(c.get("cue") or c.get("prompt"))}</li>'
            for c in stuck
        )
        blocks.append(
            '<h2>Sit down and learn these</h2>'
            '<p>Failed repeatedly. Reviewing them again tomorrow has been tried '
            'and did not work — read the source material instead.</p>'
            f'<ul>{items}</ul>'
        )

    if d := day.get("design_prompt"):
        covers = "".join(f"<li>{_esc(m)}</li>" for m in d.get("must_cover", []))
        blocks.append(
            f'<h2>Timed design — 20 min, out loud</h2>'
            f'<p><b>{_esc(d["prompt"])}</b></p>'
            f'<p>Answer first. Then check you covered:</p><ol>{covers}</ol>'
            f'<div class="hard">what sinks candidates: {_esc(d.get("trap"))}</div>'
        )

    if cards := day.get("dsa"):
        blocks.append(f"<h2>Patterns due</h2><ul>{card_list(cards, 'cue')}</ul>")

    if cards := day.get("system_design"):
        blocks.append(f"<h2>Design concepts due</h2><ul>{card_list(cards, 'prompt')}</ul>")

    return "".join(blocks)


def render(ideas: list[dict], prep: str = "", alert: str = "") -> str:
    """`ideas` is a list of `{"idea", "domain", "reply_id"}`, oldest first."""
    if ideas:
        n = len(ideas)
        body = "".join(
            render_idea(e["idea"], e.get("domain", ""), e.get("reply_id", ""),
                        f"idea {i} of {n}" if n > 1 else "")
            for i, e in enumerate(ideas, 1)
        )
    else:
        body = "<h1>Today's prep</h1>"
    prep_block = prep or ""
    if alert:
        body = (f'<div class="hard"><b>This system thinks it has stopped being '
                f'useful.</b><br>{_esc(alert)}</div>' + body)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style></head>
<body><div class="wrap">
{body}
{prep_block}
<div class="foot">
{SENTINEL}<br>
<b>Just reply to this email.</b><br>
On the idea, name it first, one per line: {_idea_example(ideas)}
(also <code>exists</code>, <code>too easy</code>, <code>building</code>)<br>
On a card, one per line: <code>two-pointers good</code> ·
<code>dijkstra again</code> (also <code>hard</code>, <code>easy</code>,
<code>skip</code>)<br>
Anything it does not recognise is kept as a note, so plain English is fine.
</div>
</div></body></html>"""


def _idea_example(ideas: list[dict]) -> str:
    """The example reply line in the footer, using this digest's real handles."""
    names = [e.get("reply_id") for e in ideas if e.get("reply_id")]
    if not names:
        return "<code>more</code> · <code>boring</code> "
    if len(names) == 1:
        return f"<code>{_esc(names[0])} more</code> · <code>{_esc(names[0])} boring</code> "
    return (f"<code>{_esc(names[0])} more</code> · "
            f"<code>{_esc(names[1])} boring</code> ")


def next_unsent(conn: sqlite3.Connection, limit: int = 1) -> list[sqlite3.Row]:
    """Oldest unsent first, not newest. Ordering by id DESC strands anything
    older than the newest idea permanently -- it can never reach the front of a
    finite-width queue."""
    return conn.execute(
        "SELECT * FROM idea WHERE sent_utc IS NULL ORDER BY id ASC LIMIT ?",
        (limit,),
    ).fetchall()


def reply_id_for(slug: str, taken: set[str]) -> str:
    """A short handle for one idea, unique among `taken`.

    The project name -- the slug's first token -- is what a human would type,
    and the whole slug is 60 characters of hyphens that nobody types on a phone.
    `taken` must include live card ids as well as other ideas' handles: the
    reply parser looks for both in the same line, and a handle that collides
    with a card id makes every reply mentioning it ambiguous.
    """
    parts = [p for p in (slug or "").split("-") if p]
    if not parts:
        return ""
    # A two-letter first token ("go", "k8"-ish) is too weak to match on without
    # colliding with ordinary prose, so borrow the next word.
    base = parts[0] if len(parts[0]) >= 3 else "-".join(parts[:2])
    if base not in taken:
        return base
    for n in range(2, 100):
        if (candidate := f"{base}{n}") not in taken:
            return candidate
    return slug


def send_email(subject: str, body_html: str) -> None:
    missing = [n for n, v in
               (("RESEND_API_KEY", RESEND_API_KEY), ("DIGEST_TO", DIGEST_TO),
                ("DIGEST_FROM", DIGEST_FROM)) if not v]
    if missing:
        raise RuntimeError(f"cannot send, unset: {', '.join(missing)}")

    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        # reply_to is load-bearing. Without it Reply addresses DIGEST_FROM --
        # a Resend sending domain, which has no inbound MX -- so every reply
        # bounces into nothing and the feedback loop is silently dead while
        # looking fine from the outside.
        json={"from": DIGEST_FROM, "to": [DIGEST_TO], "reply_to": DIGEST_TO,
              "subject": subject, "html": body_html},
        timeout=30,
    )
    resp.raise_for_status()


def send_push(message: str) -> None:
    if not NTFY_TOPIC:
        print("  (no NTFY_TOPIC, skipping push)", file=sys.stderr)
        return
    httpx.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": "signal-forge", "User-Agent": USER_AGENT},
        timeout=20,
    ).raise_for_status()


def _day_names(days) -> str:
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return "/".join(names[d] for d in sorted(days))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="render to build/digest.html instead of sending "
                         "(ignores the cadence, since it sends nothing)")
    ap.add_argument("--prep-only", action="store_true",
                    help="skip the ideas even if some are waiting")
    ap.add_argument("--force", action="store_true",
                    help="send even on an off day, or twice in one day")
    args = ap.parse_args()

    now = datetime.now(DIGEST_TZ)
    today = now.date().isoformat()
    # A dry run has no outbound side effect, so it renders whatever today would
    # carry regardless of cadence -- otherwise previewing the digest would only
    # work on three days of the week.
    unrestricted = args.force or args.dry_run

    want_prep = unrestricted or now.weekday() in PREP_DAYS
    want_ideas = (unrestricted or now.weekday() in IDEA_DAYS) and not args.prep_only

    if not (want_prep or want_ideas):
        print(f"{now:%a %Y-%m-%d} is not a send day "
              f"(prep {_day_names(PREP_DAYS)}, ideas {_day_names(IDEA_DAYS)})")
        return 0

    with connect() as conn:
        # Idempotency, checked before anything is consumed. Three digests went
        # out within 31 minutes on 2026-08-13 -- each one drained a different
        # queued idea, so it read as three legitimate emails rather than as a
        # repeat. Nothing in the code had an opinion about how often it should
        # send; a retrying cron, a double-clicked test run, or a second manual
        # dispatch all cost a real email and a queued idea.
        if not unrestricted and get_kv(conn, "last_digest_date", "") == today:
            print(f"already sent a digest on {today}; use --force to send again")
            return 0

        # Ordering matters: prep must not depend on there being an idea, so a
        # failed ideation run never costs a prep day.
        # A hand-edited cards/*.json with a syntax error must not also kill the
        # Monday ideas. The comment below claimed protection in one direction;
        # this makes it true in both.
        #
        # `prep.today()` records the problems it hands out, so it must not run
        # on a day whose prep is not going out -- that silently burns problems
        # from the rotation for an email nobody receives.
        day, prep_html = {}, ""
        if want_prep:
            try:
                day = prep.today(conn)
                prep_html = render_prep(day)
            except Exception as exc:
                print(f"prep failed ({type(exc).__name__}: {exc})", file=sys.stderr)
                day, prep_html = {}, ""

        paused = get_kv(conn, "ideas_paused", "0") == "1"
        rows = (next_unsent(conn, IDEAS_PER_DIGEST)
                if want_ideas and not paused else [])

        # Handles must be unique against live card ids too -- the reply parser
        # scans one line for both, so an idea called `trie` would make every
        # grade of the `trie` card ambiguous.
        taken = {c["id"] for cards in prep.all_cards().values() for c in cards}
        ideas: list[dict] = []
        for row in rows:
            handle = reply_id_for(row["slug"], taken)
            taken.add(handle)
            ideas.append({"row": row, "idea": json.loads(row["body"]),
                          "domain": row["domain"] or "", "reply_id": handle})

        if not ideas and not prep_html:
            print("nothing to send")
            return 0

        try:
            # Only a real send consumes the canary.
            alert = feedback.canary(conn, mark=not args.dry_run) or ""
        except Exception as exc:
            print(f"canary failed ({exc})", file=sys.stderr)
            alert = ""

        body_html = render(ideas, prep_html, alert)
        # The subject is how a reply finds its way home: `feedback._is_ours`
        # matches it to decide whether a message is feedback at all. Both forms
        # here are recognised there -- change one and change the other.
        if ideas:
            subject = "ideas — " + ", ".join(e["reply_id"] for e in ideas)
        else:
            subject = f"prep — {len(day['dsa']) + len(day['system_design'])} due"

        if args.dry_run:
            out = Path("build/digest.html")
            out.parent.mkdir(exist_ok=True)
            out.write_text(body_html)
            print(f"rendered {len(body_html)} bytes -> {out}")
            return 0

        # Order matters. The email is the irreversible step, so it must be
        # recorded before anything that can fail afterwards -- otherwise an ntfy
        # outage raises, the commit never happens, and the next run sends the
        # same email again.
        send_email(subject, body_html)
        sent_utc = int(time.time())
        # One timestamp for the whole digest, so "the ideas that went out
        # together" is a query rather than a guess.
        for e in ideas:
            conn.execute(
                "UPDATE idea SET sent_utc = ?, reply_id = ? WHERE id = ?",
                (sent_utc, e["reply_id"], e["row"]["id"]),
            )
        set_kv(conn, "last_digest_date", today)
        conn.commit()

        # Push is explicitly a nudge; it must never be able to undo the send.
        first = (day.get("problems") or [{}])[0].get("problem", "")
        nudge = (f"{subject} — {ideas[0]['idea'].get('one_liner', '')[:120]}"
                 if ideas else f"{subject}. {first}")
        try:
            send_push(nudge)
        except Exception as exc:
            print(f"  push failed ({exc}), email already sent", file=sys.stderr)

    print(f"sent: {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
