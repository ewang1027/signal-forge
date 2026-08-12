"""Model calls, via the Claude Code CLI in headless mode.

Deliberately shells out to `claude -p` rather than hitting the HTTP API: that
path bills to the Max subscription instead of API credits, which is what keeps
this system free to run.

The env scrubbing below is not paranoia. If ANTHROPIC_API_KEY is present it
takes precedence over the OAuth token and usage silently bills to the API
account -- a documented way to run up a four-figure bill without noticing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from .config import MODEL

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class LLMError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # The whole point: never let an API key shadow the subscription token.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # Locally the CLI authenticates from the keychain, so a token is optional.
    # In CI there is no keychain and a missing token means the run would either
    # fail confusingly or fall back to billing an API key -- so require it.
    if env.get("CI") or env.get("GITHUB_ACTIONS"):
        if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            raise LLMError(
                "CLAUDE_CODE_OAUTH_TOKEN is not set. Generate one with `claude setup-token`. "
                "Do not substitute ANTHROPIC_API_KEY -- that bills the API account."
            )
    return env


def complete(prompt: str, *, timeout: int = 600) -> str:
    """Run a single headless turn and return the text response."""
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", MODEL],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(),
    )
    if proc.returncode != 0:
        raise LLMError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def _parse_json(raw: str) -> dict:
    match = _FENCE.search(raw)
    candidate = match.group(1) if match else raw

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to the outermost brace pair -- models sometimes emit a
        # sentence before the fence despite instructions.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"no JSON object in response: {raw[:300]}") from None
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"unparseable JSON: {exc}: {raw[:300]}") from exc


def complete_json(prompt: str, *, timeout: int = 600, attempts: int = 3) -> dict:
    """Run a turn and parse the single JSON object out of the response.

    Retries because this runs unattended twice a week: a transient subprocess or
    formatting failure that nobody is watching means a delivery with nothing in
    it, and the cost of retrying is a couple of minutes.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _parse_json(complete(prompt, timeout=timeout))
        except (LLMError, subprocess.SubprocessError, OSError) as exc:
            last = exc
            if attempt < attempts:
                print(f"  attempt {attempt} failed ({exc}), retrying", file=sys.stderr)
                time.sleep(5 * attempt)
    raise LLMError(f"all {attempts} attempts failed: {last}")
