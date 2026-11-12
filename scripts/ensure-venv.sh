#!/usr/bin/env bash
# Ensure the repo-local toolchain: .venv/ with the pins of requirements.txt
# and, where there is one, requirements-dev.txt.
# All of it is gitignored and lives inside the checkout, so deleting the repo
# directory removes the whole toolchain: nothing is installed system-wide.
#
# Called by scripts/check.sh and any other script that needs the toolchain;
# safe to run directly.
# Managed by repo-template: changes here are overwritten by its next update.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
VENV=.venv

# venv needs ensurepip; on Debian, Ubuntu and Mint that is the python3-venv package.
"$PYTHON" -c 'import ensurepip' 2>/dev/null || {
  echo "ERROR: $PYTHON cannot create a venv (no ensurepip)." >&2
  echo "On Debian, Ubuntu or Mint: sudo apt install python3-venv" >&2
  exit 1
}

# A venv breaks silently when the OS python it symlinks gets upgraded away, or
# loses its pip — probe it instead of trusting that the files exist.
if [ -d "$VENV" ] && ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  echo "existing .venv is broken (OS python changed, or no pip) — rebuilding"
  rm -rf "$VENV"
fi

if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
fi

# Fast no-op when already satisfied; picks up Renovate bumps of the pins. A
# requirements-dev.txt, where there is one, holds the tools beside the
# project's own dependencies.
reqs=(-r requirements.txt)
if [ -f requirements-dev.txt ]; then reqs+=(-r requirements-dev.txt); fi
"$VENV/bin/pip" install --quiet "${reqs[@]}"
