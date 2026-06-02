#!/bin/bash
# pre-push-throttle.sh — min-interval push throttle for main (Railway deploy gate)
# ---------------------------------------------------------------------------
# WHY: the autopilot loop pushes to main in sub-5-minute bursts. Every push to
# main is a Railway deploy/restart, and the backend is ONE replica — stacked
# restarts cause site-wide flapping. This hook coalesces rapid main pushes by
# REJECTING a push to main if origin/main was last updated < THRESHOLD minutes
# ago. Rejected commits are NOT lost: they stay local and roll into the next
# allowed push.
#
# This is a STANDARD git pre-push hook. git invokes it as:
#     pre-push <remote-name> <remote-url>
# and feeds, on stdin, one line per ref being pushed:
#     <local ref> <local sha> <remote ref> <remote sha>
#
# Behavior:
#   * Only throttles pushes whose REMOTE ref is main (refs/heads/main or main).
#     All other branches/tags push freely.
#   * Threshold: DCHUB_PUSH_THROTTLE_MIN minutes (default 20).
#   * Bypass:    DCHUB_PUSH_FORCE=1 git push   -> always allowed.
#   * Fails OPEN: any internal/git introspection error -> ALLOW the push.
#     (A missed throttle costs one extra deploy; failing closed would block
#      every push — far worse. So we never hard-block on our own bugs.)
#
# Install (run by the main loop, last):
#     cp scripts/pre-push-throttle.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
#   (or symlink:  ln -sf ../../scripts/pre-push-throttle.sh .git/hooks/pre-push)
# ---------------------------------------------------------------------------

# NOTE: set -e is intentionally OFF. A failing git subcommand must not crash
# the hook into a non-zero exit that blocks the push. We fail OPEN everywhere.

THRESHOLD_MIN="${DCHUB_PUSH_THROTTLE_MIN:-20}"
REMOTE_NAME="${1:-origin}"
ZERO40="0000000000000000000000000000000000000000"

# --- color helpers (degrade to plain text if not a tty) --------------------
if [ -t 2 ]; then
  _red()  { printf "\033[31m%s\033[0m\n" "$*" >&2; }
  _ylw()  { printf "\033[33m%s\033[0m\n" "$*" >&2; }
  _grn()  { printf "\033[32m%s\033[0m\n" "$*" >&2; }
else
  _red()  { printf "%s\n" "$*" >&2; }
  _ylw()  { printf "%s\n" "$*" >&2; }
  _grn()  { printf "%s\n" "$*" >&2; }
fi

# --- bypass ---------------------------------------------------------------
if [ "${DCHUB_PUSH_FORCE:-}" = "1" ]; then
  _ylw "push-throttle: DCHUB_PUSH_FORCE=1 set — bypassing throttle."
  exit 0
fi

# --- validate threshold is a positive integer; else fail OPEN -------------
case "$THRESHOLD_MIN" in
  ''|*[!0-9]*)
    _ylw "push-throttle: DCHUB_PUSH_THROTTLE_MIN='$THRESHOLD_MIN' is not a non-negative integer — allowing push (fail-open)."
    exit 0
    ;;
esac
# A threshold of 0 disables throttling entirely.
if [ "$THRESHOLD_MIN" -eq 0 ]; then
  exit 0
fi

# --- does this push target main? -------------------------------------------
# Read the stdin ref lines git provides. A push to main is detected when the
# REMOTE ref (3rd field) is refs/heads/main or bare "main", AND it is not a
# branch deletion (local sha is the all-zero sha).
pushes_main=0
saw_any_line=0
while read -r local_ref local_sha remote_ref remote_sha; do
  # Blank trailing line guard.
  [ -z "$remote_ref" ] && continue
  saw_any_line=1
  case "$remote_ref" in
    refs/heads/main|main)
      # Deletion of main (local sha all zeros) — not a deploy; let git handle it.
      if [ "$local_sha" = "$ZERO40" ]; then
        continue
      fi
      pushes_main=1
      ;;
  esac
done

# If git fed us no ref lines (some git versions/flows), we cannot tell what is
# being pushed. Fail OPEN — never block on uncertainty.
if [ "$saw_any_line" -eq 0 ]; then
  exit 0
fi

# Not pushing main -> allow unconditionally (other branches deploy nothing).
if [ "$pushes_main" -eq 0 ]; then
  exit 0
fi

# --- determine age of the latest commit currently on origin/main -----------
# Try, in order: the tracking ref origin/main, then <remote-name>/main, then a
# direct ls-remote against the remote. Any success wins; total failure -> open.
last_ct=""
for ref in "${REMOTE_NAME}/main" "origin/main" "refs/remotes/${REMOTE_NAME}/main"; do
  ct=$(git log -1 --format=%ct "$ref" 2>/dev/null)
  case "$ct" in
    ''|*[!0-9]*) : ;;            # empty or non-numeric -> try next source
    *) last_ct="$ct"; break ;;
  esac
done

# Fallback: ask the remote directly for the sha, then read its commit time
# (works only if we have that object locally; harmless if not).
if [ -z "$last_ct" ]; then
  remote_main_sha=$(git ls-remote --heads "$REMOTE_NAME" main 2>/dev/null | awk '{print $1}' | head -1)
  case "$remote_main_sha" in
    [0-9a-fA-F]*)
      ct=$(git log -1 --format=%ct "$remote_main_sha" 2>/dev/null)
      case "$ct" in
        ''|*[!0-9]*) : ;;
        *) last_ct="$ct" ;;
      esac
      ;;
  esac
fi

# Could not establish a baseline timestamp (fresh clone, no tracking ref,
# offline, empty remote, first-ever push, etc.) -> ALLOW (fail open).
if [ -z "$last_ct" ]; then
  _ylw "push-throttle: could not read origin/main commit time — allowing push (fail-open)."
  exit 0
fi

# --- compute age in minutes ------------------------------------------------
now_ct=$(date +%s 2>/dev/null)
case "$now_ct" in
  ''|*[!0-9]*)
    _ylw "push-throttle: could not read current time — allowing push (fail-open)."
    exit 0
    ;;
esac

age_sec=$(( now_ct - last_ct ))
# Clock skew or a future-dated commit -> non-positive age. Treat as "old
# enough" is unsafe (could be a real recent deploy with skew); but blocking on
# skew is worse. Fail OPEN.
if [ "$age_sec" -lt 0 ]; then
  _ylw "push-throttle: origin/main commit is future-dated (clock skew?) — allowing push (fail-open)."
  exit 0
fi
age_min=$(( age_sec / 60 ))

# --- decide -----------------------------------------------------------------
if [ "$age_min" -lt "$THRESHOLD_MIN" ]; then
  wait_min=$(( THRESHOLD_MIN - age_min ))
  [ "$wait_min" -lt 1 ] && wait_min=1
  _red  "push throttled: last main deploy was ${age_min}m ago (<${THRESHOLD_MIN}m)."
  _ylw  "Your commits are SAFE locally — nothing is lost. They will coalesce into"
  _ylw  "the next allowed push. Retry in ~${wait_min}m, or override right now with:"
  _ylw  "    DCHUB_PUSH_FORCE=1 git push"
  _ylw  "(tune the window via DCHUB_PUSH_THROTTLE_MIN, e.g. DCHUB_PUSH_THROTTLE_MIN=10 git push)"
  exit 1
fi

_grn "push-throttle: last main deploy was ${age_min}m ago (>=${THRESHOLD_MIN}m) — allowed."
exit 0
