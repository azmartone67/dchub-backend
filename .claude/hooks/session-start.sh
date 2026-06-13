#!/bin/bash
# SessionStart hook for Claude Code on the web.
# Installs Python dependencies + pytest so tests, linters, and
# `python -m py_compile` work in remote sessions without manual setup.
set -euo pipefail

# Only run in remote (web) sessions; locally the user manages their own venv.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"

PIP_FLAGS=(--quiet --disable-pip-version-check --root-user-action=ignore)

# The base image ships several Debian-managed Python packages (PyJWT, wheel,
# etc.) that lack RECORD files, so pip can't uninstall them. --ignore-installed
# tells pip to install fresh side-by-side copies, which the Python interpreter
# then prefers via site-packages ordering.
echo "[session-start] upgrading build tooling"
python3 -m pip install "${PIP_FLAGS[@]}" --ignore-installed --upgrade pip setuptools wheel

echo "[session-start] installing requirements.txt"
python3 -m pip install "${PIP_FLAGS[@]}" --ignore-installed -r requirements.txt

echo "[session-start] installing pytest (used by tests/ and CI)"
python3 -m pip install "${PIP_FLAGS[@]}" --ignore-installed pytest

echo "[session-start] done"
