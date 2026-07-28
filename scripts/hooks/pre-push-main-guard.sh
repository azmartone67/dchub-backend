#!/bin/bash
# pre-push-main-guard.sh — branch-independent block on direct pushes to main.
# Canonical source. Installed into $(git rev-parse --git-common-dir)/hooks/ by
# scripts/install-git-hooks.sh. PR-deploy migration, phase 2 (2026-07-28).
#
# WHY THIS IS INSTALLED INTO .git/hooks INSTEAD OF RUN FROM THE WORKTREE
# ----------------------------------------------------------------------
# The deploy throttle lives at scripts/pre-push-throttle.sh and the dispatcher
# resolves it as "$top/scripts/pre-push-throttle.sh" — i.e. from the WORKTREE
# BEING PUSHED FROM. That makes a guard only as current as whatever branch that
# worktree sits on. The primary checkout was 708 commits behind on a local-only
# branch, so its copy predated the guard entirely and pushes from it were never
# blocked. This repo has 74 worktrees; any one parked on an old branch is a hole.
#
# core.hooksPath is an absolute path to the SHARED hooks directory, so a copy
# installed there applies no matter what branch a worktree is on. Verified by
# reverting a worktree's scripts/ copy to its pre-guard version and confirming a
# main push still blocks.
#
# SCOPE IS DELIBERATELY NARROW: the main-push block and nothing else. The
# throttle, and anything else that evolves, stays in scripts/pre-push-throttle.sh
# where normal version-controlled edits take effect immediately. Only the one
# invariant that must not depend on branch position is pinned here — and this
# file is version-controlled so the pinned copy is still reviewable.
#
# Fails OPEN on any uncertainty. A guard that misfires and blocks every push is
# worse than one that occasionally misses. Same discipline as the throttle.

ZERO40="0000000000000000000000000000000000000000"

if [ -t 2 ]; then
  _red() { printf "\033[31m%s\033[0m\n" "$*" >&2; }
  _ylw() { printf "\033[33m%s\033[0m\n" "$*" >&2; }
else
  _red() { printf "%s\n" "$*" >&2; }
  _ylw() { printf "%s\n" "$*" >&2; }
fi

# --- bypasses (same contract as scripts/pre-push-throttle.sh) --------------
# DCHUB_PUSH_FORCE=1      -> skip every guard (real emergency)
# DCHUB_ALLOW_MAIN_PUSH=1 -> skip this block; the worktree's throttle still runs
[ "${DCHUB_PUSH_FORCE:-}" = "1" ] && exit 0
[ "${DCHUB_ALLOW_MAIN_PUSH:-}" = "1" ] && exit 0

# --- does this push target main? -------------------------------------------
pushes_main=0
saw_any_line=0
while read -r local_ref local_sha remote_ref remote_sha; do
  [ -z "$remote_ref" ] && continue
  saw_any_line=1
  case "$remote_ref" in
    refs/heads/main|main)
      # Deleting main is not a deploy — let git handle it.
      [ "$local_sha" = "$ZERO40" ] && continue
      pushes_main=1
      ;;
  esac
done

# git fed us no ref lines: we cannot tell what is being pushed -> allow.
[ "$saw_any_line" -eq 0 ] && exit 0
[ "$pushes_main" -eq 0 ] && exit 0

_red "push to main is BLOCKED — deploys go through a pull request."
_ylw ""
_ylw "  git switch -c fix/<slug>          # or feat/<slug>"
_ylw "  git push -u origin HEAD"
_ylw "  gh pr create --fill"
_ylw "  gh pr merge --auto --squash       # merges itself once checks pass"
_ylw ""
_ylw "Your commits are SAFE — nothing is lost, they are already local."
_ylw "A merged PR pushes to main exactly like you would, so Railway still"
_ylw "auto-deploys; the only change is that CI runs first."
_ylw ""
_ylw "Emergency override (skips every guard):  DCHUB_PUSH_FORCE=1 git push"
_ylw "Old throttle behaviour instead:          DCHUB_ALLOW_MAIN_PUSH=1 git push"
_ylw ""
_ylw "(branch-independent guard — reinstall: bash scripts/install-git-hooks.sh)"
exit 1
