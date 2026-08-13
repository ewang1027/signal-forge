"""Stage 6 -- render and send the digest.

Email carries the content; push is a nudge. `--dry-run` writes the rendered HTML
to disk instead of sending, which is how this gets tested without burning a send
or needing credentials.
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

from . import feedback, prep
from .config import DIGEST_FROM, DIGEST_TO, NTFY_TOPIC, RESEND_API_KEY, USER_AGENT
from .db import connect

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
"""


def _esc(v) -> str:
    return html.escape(str(v or ""))


def render_idea(idea: dict, domain: str) -> str:
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

    return f"""
<span class="tag">{_esc(domain)}</span>{feas}
<h1>{_esc(idea.get('title'))}</h1>
<p class="sub">{_esc(idea.get('one_liner'))}</p>

<h2>The problem</h2>
<p>{_esc(idea.get('problem'))}</p>

<h2>Why it's hard</h2>
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

<h2>Evidence</h2>
<div class="ev">{evidence}</div>
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


def render(idea: dict | None, domain: str, prep: str = "", alert: str = "") -> str:
    body = render_idea(idea, domain) if idea else "<h1>Today's prep</h1>"
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
<b>Just reply to this email.</b><br>
On the idea: <code>more</code> · <code>boring</code> · <code>exists</code> ·
<code>too easy</code> · <code>building</code><br>
On a card, one per line: <code>two-pointers good</code> ·
<code>dijkstra again</code> (also <code>hard</code>, <code>easy</code>,
<code>skip</code>)<br>
Anything it does not recognise is kept as a note, so plain English is fine.
</div>
</div></body></html>"""


def next_unsent(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Oldest unsent, not newest. Ordering by id DESC strands anything older
    than the newest idea permanently -- it can never reach the front of a
    one-at-a-time queue."""
    return conn.execute(
        "SELECT * FROM idea WHERE sent_utc IS NULL ORDER BY id ASC LIMIT 1"
    ).fetchone()


def send_email(subject: str, body_html: str) -> None:
    missing = [n for n, v in
               (("RESEND_API_KEY", RESEND_API_KEY), ("DIGEST_TO", DIGEST_TO),
                ("DIGEST_FROM", DIGEST_FROM)) if not v]
    if missing:
        raise RuntimeError(f"cannot send, unset: {', '.join(missing)}")

    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": DIGEST_FROM, "to": [DIGEST_TO],
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="render to build/digest.html instead of sending")
    ap.add_argument("--prep-only", action="store_true",
                    help="skip the idea even if one is waiting")
    args = ap.parse_args()

    with connect() as conn:
        # Prep goes out every day; an idea only on the days one is waiting.
        # Ordering matters: prep must not depend on there being an idea, so a
        # failed ideation run never costs a prep day.
        # A hand-edited cards/*.json with a syntax error must not also kill the
        # Monday/Thursday idea. The comment below claimed protection in one
        # direction; this makes it true in both.
        try:
            day = prep.today(conn)
            prep_html = render_prep(day)
        except Exception as exc:
            print(f"prep failed ({type(exc).__name__}: {exc})", file=sys.stderr)
            day, prep_html = {}, ""

        row = next_unsent(conn) if not args.prep_only else None
        idea = json.loads(row["body"]) if row else None
        domain = row["domain"] if row else ""

        if idea is None and not prep_html:
            print("nothing to send")
            return 0

        try:
            alert = feedback.canary(conn) or ""
        except Exception as exc:
            print(f"canary failed ({exc})", file=sys.stderr)
            alert = ""

        body_html = render(idea, domain or "", prep_html, alert)
        subject = (idea.get("title") if idea
                   else f"prep — {len(day['dsa']) + len(day['system_design'])} due")

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
        if row is not None:
            conn.execute("UPDATE idea SET sent_utc = ? WHERE id = ?",
                         (int(time.time()), row["id"]))
        conn.commit()

        # Push is explicitly a nudge; it must never be able to undo the send.
        first = (day.get("problems") or [{}])[0].get("problem", "")
        nudge = (f"{subject} — {idea.get('one_liner', '')[:120]}" if idea
                 else f"{subject}. {first}")
        try:
            send_push(nudge)
        except Exception as exc:
            print(f"  push failed ({exc}), email already sent", file=sys.stderr)

    print(f"sent: {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
