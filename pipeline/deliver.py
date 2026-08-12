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
    return f"""
<span class="tag">{_esc(domain)}</span>
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
<p>{_esc(idea.get('prior_art'))}</p>

<h2>Kill criteria</h2>
<p>{_esc(idea.get('kill_criteria'))}</p>

<h2>Evidence</h2>
<div class="ev">{evidence}</div>
"""


def render(idea: dict | None, domain: str, prep: str = "") -> str:
    body = render_idea(idea, domain) if idea else "<h1>Today's prep</h1>"
    prep_block = f"<h2>Prep</h2>{prep}" if prep else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style></head>
<body><div class="wrap">
{body}
{prep_block}
<div class="foot">
Reply <code>more</code>, <code>boring</code>, <code>exists</code>, or
<code>too easy</code> to tune what gets sent.
</div>
</div></body></html>"""


def latest_unsent(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM idea WHERE sent_utc IS NULL ORDER BY id DESC LIMIT 1"
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
    args = ap.parse_args()

    with connect() as conn:
        row = latest_unsent(conn)
        if row is None:
            print("nothing to send")
            return 0

        idea = json.loads(row["body"])
        body_html = render(idea, row["domain"] or "")
        subject = idea.get("title", "signal-forge")

        if args.dry_run:
            out = Path("build/digest.html")
            out.parent.mkdir(exist_ok=True)
            out.write_text(body_html)
            print(f"rendered {len(body_html)} bytes -> {out}")
            return 0

        send_email(subject, body_html)
        send_push(f"{subject} — {idea.get('one_liner', '')[:120]}")
        conn.execute("UPDATE idea SET sent_utc = ? WHERE id = ?",
                     (int(time.time()), row["id"]))

    print(f"sent: {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
