#!/usr/bin/env bash
# Install the guards into BOTH repos.
#
# `core.hooksPath` is local git config -- it is not committed and does not
# travel with a clone. Without this script a fresh checkout has zero protection,
# and more importantly the *state* repo had none at all: that is the repo the
# pipeline commits to unattended every day, which is precisely the surface the
# hooks were written to protect.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state="${STATE_DIR:-$(cd "$here/.." && pwd)/signal-forge-state}"

git -C "$here" config core.hooksPath .githooks
chmod +x "$here/.githooks/"*
echo "hooks installed: $here"

if [ -d "$state/.git" ]; then
  mkdir -p "$state/.githooks"
  # Verbatim copies. The state repo is supposed to hold data, so it declares its
  # role and the hook skips the "no state/" rule -- rather than shipping a
  # hand-trimmed variant that can drift or break.
  cp "$here/.githooks/pre-commit" "$here/.githooks/commit-msg" "$state/.githooks/"
  chmod +x "$state/.githooks/"*
  git -C "$state" config core.hooksPath .githooks
  git -C "$state" config signalforge.role state
  echo "hooks installed: $state (role=state)"
else
  echo "state repo not found at $state -- skipping" >&2
fi
