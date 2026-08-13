#!/usr/bin/env python3
"""Create the two cron-job.org jobs that trigger the workflows.

Exists because hand-writing this as curl means JSON inside single quotes inside
a shell command, with a token interpolated through both -- which silently
produced malformed bodies twice. Python builds the JSON, so there is no quoting
to get wrong.

Usage:
    CRON_KEY=... GH_PAT=... TZ_NAME=Europe/Brussels python3 scripts/setup-cron.py

Re-running is safe: jobs are matched by title and updated rather than duplicated.
"""

from __future__ import annotations

import json
import os
import sys
import time

# httpx rather than urllib: the python.org build on macOS does not use the
# system certificate store, so urllib fails SSL verification against
# api.cron-job.org. httpx bundles certifi and the project already depends on it.
# Run with `uv run python scripts/setup-cron.py`.
import httpx

API = "https://api.cron-job.org"
REPO = os.environ.get("REPO", "ewang1027/signal-forge")

CRON_KEY = os.environ.get("CRON_KEY", "")
GH_PAT = os.environ.get("GH_PAT", "")
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Brussels")


def call(method: str, path: str, body: dict | None = None) -> dict:
    # cron-job.org rate-limits job creation, so creating two in a row 429s on
    # the second. Backing off is the whole fix; the first job succeeded.
    for attempt in range(5):
        resp = httpx.request(
            method,
            f"{API}{path}",
            json=body,
            headers={"Authorization": f"Bearer {CRON_KEY}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)
            print(f"  rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            raise SystemExit(f"cron-job.org {method} {path} -> HTTP "
                             f"{resp.status_code}\n  {resp.text[:400]}")
        return resp.json() if resp.text.strip() else {}
    raise SystemExit("cron-job.org kept rate limiting; try again in a minute")


def job_spec(title: str, workflow: str, hour: int, minute: int,
             wdays: list[int]) -> dict:
    return {
        "job": {
            "title": title,
            "url": (f"https://api.github.com/repos/{REPO}"
                    f"/actions/workflows/{workflow}/dispatches"),
            "enabled": True,
            "saveResponses": True,
            "requestMethod": 1,          # 1 = POST. The default 0 (GET) does nothing.
            "schedule": {
                "timezone": TZ_NAME,
                "hours": [hour],
                "minutes": [minute],
                "mdays": [-1],           # -1 means "any"; omitting these never matches
                "months": [-1],
                "wdays": wdays,
            },
            "extendedData": {
                "headers": {
                    "Authorization": f"Bearer {GH_PAT}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                },
                "body": json.dumps({"ref": "main"}),
            },
        }
    }


def main() -> int:
    missing = [n for n, v in (("CRON_KEY", CRON_KEY), ("GH_PAT", GH_PAT)) if not v]
    if missing:
        raise SystemExit(f"set these first: {', '.join(missing)}")

    existing = {j.get("title"): j.get("jobId")
                for j in call("GET", "/jobs").get("jobs", [])}

    # Off-round minutes on purpose: everyone schedules on the hour, so :03 and
    # :41 dodge the worst contention on both cron-job.org and GitHub's dispatch
    # queue. The 22-minute gap keeps ideas ahead of the digest that carries them.
    plan = [
        ("signal-forge daily", "daily.yml", 7, 3, [-1]),
        ("signal-forge ideas", "ideas.yml", 6, 41, [1, 4]),   # Mon, Thu
    ]

    for title, workflow, hour, minute, wdays in plan:
        spec = job_spec(title, workflow, hour, minute, wdays)
        if jid := existing.get(title):
            call("PATCH", f"/jobs/{jid}", spec)
            print(f"updated  {title:22} id={jid}  {hour:02d}:{minute:02d} {TZ_NAME}")
        else:
            jid = call("PUT", "/jobs", spec).get("jobId")
            print(f"created  {title:22} id={jid}  {hour:02d}:{minute:02d} {TZ_NAME}")

    print("\nverifying...")
    for j in call("GET", "/jobs").get("jobs", []):
        s = j.get("schedule", {})
        method = "POST" if j.get("requestMethod") == 1 else "GET (WRONG)"
        days = s.get("wdays", [])
        when = "daily" if days == [-1] else f"wdays={days}"
        print(f"  {j.get('title'):22} {method:12} "
              f"{s.get('hours')}:{s.get('minutes')} {s.get('timezone')} {when} "
              f"enabled={j.get('enabled')}")

    print("\nto test before a scheduled firing, temporarily PATCH the schedule")
    print("  (cron-job.org has no run-now REST endpoint; use TEST RUN in the web UI)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
