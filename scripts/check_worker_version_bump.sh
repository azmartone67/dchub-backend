#!/usr/bin/env bash
# Fail a change that modifies worker.js without bumping WORKER_VERSION.
#
# worker.js is the Cloudflare ZONE worker (dchubapiproxy). It deploys only by
# manual dashboard paste, so live-vs-repo drift detection hangs entirely on
# the X-DC-Worker-Version response header differing when the content differs.
# On 2026-07-30 two PRs (#1902, #1978) changed worker.js without a bump: live
# and repo reported the identical version string with different content, and
# the drift had to be proven by content fingerprinting instead.
#
# Policy: ANY edit to worker.js requires a bump — comment-only edits included.
# A bump is one line, and the changelog header at the top of the file expects
# an entry per change anyway.
#
# The check compares the extracted WORKER_VERSION VALUE at base vs head rather
# than grepping the diff: the file has ~20 other lines mentioning
# WORKER_VERSION (header stamping), and a PR touching one of those — or
# touching the const line without changing it — must not pass.
#
# Usage: check_worker_version_bump.sh [<base-rev>] [<head-rev>]
#   CI (pre-merge syntax-check job, PR merge ref): defaults HEAD^1 HEAD
#   Local, before pushing:  bash scripts/check_worker_version_bump.sh origin/main HEAD
set -euo pipefail

BASE_REV="${1:-HEAD^1}"
HEAD_REV="${2:-HEAD}"
FILE="worker.js"

if git diff --quiet "$BASE_REV" "$HEAD_REV" -- "$FILE"; then
  echo "OK: $FILE unchanged ($BASE_REV..$HEAD_REV) — WORKER_VERSION bump not required."
  exit 0
fi

if ! git cat-file -e "$HEAD_REV:$FILE" 2>/dev/null; then
  echo "OK: $FILE deleted in this change — no version to check."
  exit 0
fi

# Strict single-quote form on purpose: the drift tooling greps the same shape.
# Reformatting the line (double quotes, indentation) fails loudly rather than
# silently passing.
extract_version() {
  git show "$1:$FILE" 2>/dev/null \
    | sed -n "s/^const WORKER_VERSION = '\([^']*\)'.*/\1/p" \
    | head -n1
}

base_version="$(extract_version "$BASE_REV" || true)"
head_version="$(extract_version "$HEAD_REV" || true)"

if [ -z "$head_version" ]; then
  echo "FAIL: $FILE changed but no line matching \"const WORKER_VERSION = '...'\" found at $HEAD_REV."
  echo "The X-DC-Worker-Version drift header depends on that exact const — restore it and bump it."
  exit 1
fi

if [ "$base_version" = "$head_version" ]; then
  cat <<EOF
FAIL: $FILE changed but WORKER_VERSION did not (still '$head_version').

worker.js is the Cloudflare zone worker (dchubapiproxy). It deploys only by
manual dashboard paste, so detecting live-vs-repo drift depends on the
X-DC-Worker-Version header changing whenever the content changes.

Fix: bump the const (near line 415)

    const WORKER_VERSION = '$head_version';   <- pick a NEW value

and add a matching entry to the changelog header at the top of worker.js.

No exemptions — comment-only edits bump too. (2026-07-30: PRs #1902 and #1978
shipped without bumps; live and repo reported the same version with different
content, and drift had to be proven by content fingerprinting.)
EOF
  exit 1
fi

echo "OK: WORKER_VERSION bumped '$base_version' -> '$head_version'."
