#!/bin/bash
# install-git-hooks.sh — install the branch-independent pre-push guard.
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
# entirely ungated. This hook is what closes that gap for the admin, because
# GitHub cannot: enforce_admins:true would also block auto-rollback.yml, which
# must write to main precisely when CI is red, and a ruleset bypass actor is
# org-only (this repo is user-owned — GitHub returns HTTP 422).
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

for f in pre-push pre-push-main-guard.sh; do
  if [ ! -f "$SRC/$f" ]; then
    _red "missing canonical source: scripts/hooks/$f"
    exit 1
  fi
done

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
    cp "$dst" "$bak" && _ylw "backed up  $f -> $(basename "$bak")"
  fi
  cp "$SRC/$f" "$dst" && chmod +x "$dst" && _grn "installed  $f"
done

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

echo
if [ "$fail" = "0" ]; then
  _grn "pre-push guard installed and verified"
else
  _red "pre-push guard installed but SELF-TEST FAILED — do not rely on it"
  exit 1
fi
