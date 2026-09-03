#!/bin/bash
# install-git-hooks.sh — install the branch-independent pre-push guard and
# the commit-boundary pre-commit guards.
#
#   bash scripts/install-git-hooks.sh            install (idempotent)
#   bash scripts/install-git-hooks.sh --check    verify only, change nothing
#
# WHAT AND WHY
# ------------
# `main` is push-to-deploy. Branch protection requires substance-gate,
# syntax-check and unit-tests, and GitHub does enforce those on direct pushes —
# a bot push is rejected with "GH006: Protected branch update failed". But
# enforce_admins is false, so a push from the repo ADMIN bypasses all of it. In
# the 7 days to 2026-07-28, 137 of 223 commits (61%) reached main that way,
# entirely ungated. This hook closes that gap for the admin.
#
# ★ 2026-08-05: THE REASON THIS HOOK EXISTS INSTEAD OF enforce_admins IS GONE.
# This header used to say enforce_admins:true "would also block
# auto-rollback.yml, which must write to main precisely when CI is red". That
# has not been true since 2026-07-28. Rollback is now a Railway operation
# (scripts/railway_rollback.py, docs/ROLLBACK-RUNBOOK.md), and NOTHING in CI
# writes to main — verified across all four workflows that still mention it
# (weekly-shadow-audit, link-check, auto-rollback, brain-pr-post-merge-guard);
# every one pushes to a $BRANCH, and the `git push origin main` lines that turn
# up on a grep are COMMENTS describing the behaviour that was removed.
#
# So the choice is now purely: keep relying on a local hook that every fresh
# clone lacks until someone runs this script, or set enforce_admins:true and
# have GitHub enforce it for everyone, everywhere, with no install step. The
# ruleset-bypass-actor limitation (org-only, HTTP 422 on a user-owned repo) is
# irrelevant to that decision — no bypass actor is needed if nothing in CI
# needs to bypass.
#
# ★ AND THIS HOOK IS STRUCTURALLY UNRELIABLE, by construction. It lives in
# .git/hooks, which is NOT version-controlled, so a fresh clone has NO guard
# until this script runs. That is not hypothetical: the Claude Code remote
# container — a session that can and does push — came up with the hook absent
# (verified 2026-08-05). .claude/hooks/session-start.sh now installs it for
# that case, but the general point stands: a guard with an install step is a
# guard someone will be missing.
#
# The guard is installed into the SHARED hooks directory rather than run from
# the worktree, because a worktree parked on an old branch would otherwise carry
# a copy that predates the guard. See scripts/hooks/pre-push-main-guard.sh.
#
# Safe to re-run. Backs up any pre-existing hook it did not write.

set -u

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if [ -t 1 ]; then
  _red()  { printf "\033[31m%s\033[0m\n" "$*"; }
  _grn()  { printf "\033[32m%s\033[0m\n" "$*"; }
  _ylw()  { printf "\033[33m%s\033[0m\n" "$*"; }
else
  _red()  { printf "%s\n" "$*"; }
  _grn()  { printf "%s\n" "$*"; }
  _ylw()  { printf "%s\n" "$*"; }
fi

TOP="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$TOP" ]; then
  _red "not inside a git repository"
  exit 1
fi

# ★ --git-common-dir, NOT --git-dir. In a worktree, --git-dir is that worktree's
#   private directory; hooks live in the SHARED one. Getting this wrong installs
#   a hook that only ever fires for one worktree.
COMMON="$(git rev-parse --git-common-dir 2>/dev/null)"
case "$COMMON" in
  /*) : ;;                                  # already absolute
  *)  COMMON="$(cd "$COMMON" 2>/dev/null && pwd)" || COMMON="$TOP/.git" ;;
esac
HOOKS="$COMMON/hooks"
SRC="$TOP/scripts/hooks"

for f in pre-push pre-push-main-guard.sh pre-commit; do
  if [ ! -f "$SRC/$f" ]; then
    _red "missing canonical source: scripts/hooks/$f"
    exit 1
  fi
done

# ★ pre-commit destination. install-memory-precommit.sh (user-level, outside
#   this repo) owns .git/hooks/pre-commit and chains whatever it found as
#   pre-commit.chained-1. Overwriting its dispatcher would silently drop the
#   memory check, so when one is present we take the chained-1 slot it already
#   calls instead. Our hook invokes the conflict-marker script from the
#   worktree, so nothing in the old chain is lost.
PC_DST="pre-commit"
if [ -f "$HOOKS/pre-commit" ] \
   && grep -q 'pre-commit\.chained-1' "$HOOKS/pre-commit" 2>/dev/null \
   && ! cmp -s "$SRC/pre-commit" "$HOOKS/pre-commit"; then
  PC_DST="pre-commit.chained-1"
fi

# --- check mode -------------------------------------------------------------
if [ "$CHECK_ONLY" = "1" ]; then
  rc=0
  for f in pre-push pre-push-main-guard.sh; do
    if [ ! -f "$HOOKS/$f" ]; then
      _red "  MISSING   $HOOKS/$f"; rc=1
    elif ! cmp -s "$SRC/$f" "$HOOKS/$f"; then
      _ylw "  DRIFTED   $HOOKS/$f differs from scripts/hooks/$f"; rc=1
    else
      _grn "  ok        $f"
    fi
  done
  if [ ! -f "$HOOKS/$PC_DST" ]; then
    _red "  MISSING   $HOOKS/$PC_DST"; rc=1
  elif ! cmp -s "$SRC/pre-commit" "$HOOKS/$PC_DST"; then
    _ylw "  DRIFTED   $HOOKS/$PC_DST differs from scripts/hooks/pre-commit"; rc=1
  else
    _grn "  ok        $PC_DST"
  fi
  want="$HOOKS"
  got="$(git config --get core.hooksPath || true)"
  if [ "$got" != "$want" ]; then
    _red "  core.hooksPath = '${got:-<unset>}' (want '$want')"; rc=1
  else
    _grn "  ok        core.hooksPath"
  fi
  [ "$rc" = "0" ] && _grn "hooks are installed and current" \
                  || _ylw "run: bash scripts/install-git-hooks.sh"
  exit $rc
fi

# --- install ----------------------------------------------------------------
mkdir -p "$HOOKS" || { _red "cannot create $HOOKS"; exit 1; }

for f in pre-push pre-push-main-guard.sh; do
  dst="$HOOKS/$f"
  if [ -f "$dst" ] && cmp -s "$SRC/$f" "$dst"; then
    _grn "unchanged  $f"
    continue
  fi
  # Never silently discard a hook we did not write.
  if [ -f "$dst" ]; then
    bak="$dst.bak-$(date +%Y%m%d%H%M%S)"
    cp -P "$dst" "$bak" && _ylw "backed up  $f -> $(basename "$bak")"
  fi
  # ★ rm FIRST. An existing hook may be a SYMLINK into scripts/ (the install
  #   line documented in precommit-no-conflict-markers.sh is `ln -sf`), and
  #   `cp src dst` writes THROUGH a symlink — clobbering the tracked source
  #   instead of replacing the hook. That produced a hook that called itself
  #   and hung every commit (2026-09-03). Replace the link, never its target.
  rm -f "$dst"
  cp "$SRC/$f" "$dst" && chmod +x "$dst" && _grn "installed  $f"
done

pc_dst="$HOOKS/$PC_DST"
if [ -f "$pc_dst" ] && cmp -s "$SRC/pre-commit" "$pc_dst"; then
  _grn "unchanged  $PC_DST"
else
  if [ -f "$pc_dst" ]; then
    bak="$pc_dst.bak-$(date +%Y%m%d%H%M%S)"
    cp -P "$pc_dst" "$bak" && _ylw "backed up  $PC_DST -> $(basename "$bak")"
  fi
  rm -f "$pc_dst"                      # see the symlink note above
  cp "$SRC/pre-commit" "$pc_dst" && chmod +x "$pc_dst" \
    && _grn "installed  $PC_DST"
fi

if [ "$(git config --get core.hooksPath || true)" != "$HOOKS" ]; then
  git config core.hooksPath "$HOOKS" && _grn "set        core.hooksPath = $HOOKS"
else
  _grn "unchanged  core.hooksPath"
fi

# --- self-test --------------------------------------------------------------
# ★ A guard that cannot fail is not a guard. Assert both directions against the
#   INSTALLED hook, not the source, so a bad install is caught here.
_probe() {
  printf '%s\n' "$2" \
    | env -u DCHUB_PUSH_FORCE -u DCHUB_ALLOW_MAIN_PUSH $3 "$HOOKS/pre-push" origin https://example.invalid \
      >/dev/null 2>&1
  rc=$?
  if [ "$rc" = "$4" ]; then _grn "  ok        $1"; else _red "  FAILED    $1 (rc=$rc want $4)"; return 1; fi
}

echo
echo "self-test:"
fail=0
_probe "push to main is blocked"      "refs/heads/t a refs/heads/main b"  ""                        1 || fail=1
_probe "feature branch allowed"       "refs/heads/t a refs/heads/fix/x b" ""                        0 || fail=1
_probe "DCHUB_PUSH_FORCE bypasses"    "refs/heads/t a refs/heads/main b"  "DCHUB_PUSH_FORCE=1"      0 || fail=1
_probe "deleting main is not a deploy" \
  "refs/heads/t 0000000000000000000000000000000000000000 refs/heads/main b" ""                      0 || fail=1
_probe "empty stdin fails open"       ""                                  ""                        0 || fail=1

# ★ The credential gate, both directions, against the INSTALLED hook. Runs in a
#   throwaway repo carrying a copy of the scanner, because the hook resolves it
#   from the worktree being committed to.
_probe_precommit() {
  tmp="$(mktemp -d)" || return 1
  ( set -e
    cd "$tmp"
    git init -q .
    mkdir -p scripts
    cp "$TOP/scripts/check_no_leaked_credentials.py" scripts/
    # split + %s ON PURPOSE: the assembled URL must never appear literally
    # on one line, or this installer becomes a finding in its own scan.
    filler="Xk7dQ2vRm9pLzT4w"
    printf 'DSN = "postgresql://u:%s@db.example.com/x"\n' "$filler" > cfg.py
    git add cfg.py scripts/
    if "$HOOKS/$PC_DST" >/dev/null 2>&1; then exit 10; fi   # must BLOCK
    printf 'DSN = "postgresql://u:pw@db.example.com/x"\n' > cfg.py
    git add cfg.py
    if ! "$HOOKS/$PC_DST" >/dev/null 2>&1; then exit 11; fi  # must ALLOW
  )
  rc=$?
  rm -rf "$tmp"
  case "$rc" in
    0)  _grn "  ok        credential gate blocks a real password, allows a placeholder"; return 0 ;;
    10) _red "  FAILED    credential gate did NOT block a real password"; return 1 ;;
    11) _red "  FAILED    credential gate blocked a placeholder (false positive)"; return 1 ;;
    *)  _red "  FAILED    credential gate probe errored (rc=$rc)"; return 1 ;;
  esac
}
_probe_precommit || fail=1

echo
if [ "$fail" = "0" ]; then
  _grn "pre-push + pre-commit guards installed and verified"
else
  _red "hooks installed but SELF-TEST FAILED — do not rely on them"
  exit 1
fi
