# dchub-backend

## Deploys — open a PR, never push to main

`main` is push-to-deploy: anything that lands there goes to Railway. Deploys go
through a pull request.

```bash
git switch -c fix/<slug>            # or feat/<slug>
git push -u origin HEAD
gh pr create --fill
gh pr merge --auto --squash         # merges itself once checks pass
```

`gh pr merge --auto` is the point — the PR merges without a second visit, so
this is about as fast as a direct push, with CI actually run against the change.

A `pre-push` hook rejects pushes to main and prints this recipe. Overrides, in
increasing order of bluntness:

| | effect |
|---|---|
| `DCHUB_ALLOW_MAIN_PUSH=1 git push` | skip the block, apply the old 20-min deploy throttle |
| `DCHUB_PUSH_FORCE=1 git push` | skip every guard in the hook |

Use `DCHUB_PUSH_FORCE=1` for a genuine production emergency, not to save a
minute. **Never disable a guard to make your own change land.**

### Why the hook exists when branch protection already does this

`main` requires `substance-gate`, `syntax-check` and `unit-tests`, and GitHub
enforces those on direct pushes too — a bot push is rejected with
`GH006: Protected branch update failed`. But `enforce_admins` is **false**, so a
push from the repo admin bypasses all of it. In the 7 days to 2026-07-28, **137
of 223 commits (61%) reached main as ungated direct pushes.**

`enforce_admins: true` is not the fix: it would also block `auto-rollback.yml`,
which must be able to write to main precisely when CI is red. A GitHub ruleset
with a bypass actor for the Actions app would solve it, but Integration bypass
actors are org-only and this repo is user-owned (GitHub returns HTTP 422). So
the hook carries the admin case until the repo moves to an org.

## Working tree

`~/dchub-backend` is churned continuously by background automation: it switches
branches mid-session and unrelated uncommitted edits appear and revert under
you. **Never `git add -A`** — it sweeps in that churn.

Work from a clean worktree off `origin/main`:

```bash
git worktree add --detach /tmp/wt-<name> origin/main
```

Scope every `git add` to the files you actually changed. Re-fetch `origin/main`
and rebase immediately before pushing — main moves several times an hour.

## Tests

```bash
python3 -m pytest tests/ -q          # ~2,300 tests, ~60s
```

Tests **never import `main.py`** — it opens DB pools, starts keepalive threads
and registers ~200 blueprints. Tests that need shipped code pull the real
function out of the source with `ast` and execute it against stubs.

★ **Nothing under `tests/` may run at module scope.** A module-scope `sys.exit()`
aborts *collection*, which kills the whole session — exit 3, zero tests run,
rendered as an ordinary red job. That shipped twice on 2026-07-28 and left the
backend with no test gate at all for hours, invisible because main failed
identically. `scripts/check_collection_safety.py` now blocks it in
`syntax-check`. Keep every statement inside a function.

★ A red CI job that is red on main too is **not** automatically "the baseline" —
read the `passed/failed` counts before assuming it.

## Verifying

★ A green workflow run is **not** proof a push landed. `weekly-shadow-audit`
reported success for two weeks while its push was rejected and swallowed by
`|| true`. Check that the intended commit or artifact actually exists.
