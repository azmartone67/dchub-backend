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

# ★ The pre-push main guard. It lives in .git/hooks, which is NOT
# version-controlled, so a fresh clone has NO guard until someone runs the
# installer — and this container IS a fresh clone every time. Verified
# 2026-08-05: the guard was absent here, in a session that can push.
#
# That is the wrong way round. `main` is push-to-deploy and enforce_admins is
# false, so the hook is the only thing standing between an ungated push and a
# production deploy — and the sessions most likely to push were the ones
# without it.
#
# Never fatal: `set -e` is on, and a broken guard install must not stop a
# session from starting. A FAILURE here is printed, not swallowed — a guard
# that quietly did not install is the same silent-success failure this repo
# keeps paying for.
echo "[session-start] installing pre-push main guard"
if bash scripts/install-git-hooks.sh >/tmp/hookinstall.log 2>&1; then
  echo "[session-start] pre-push guard installed"
else
  echo "[session-start] WARNING: pre-push guard NOT installed — pushes to main"
  echo "[session-start] are unguarded in this session. Tail of the installer:"
  tail -5 /tmp/hookinstall.log || true
fi

echo "[session-start] done"
