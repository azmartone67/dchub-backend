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

A `pre-push` hook rejects pushes to main and prints this recipe. Install or
verify it — safe to re-run, and it self-tests:

```bash
bash scripts/install-git-hooks.sh            # install
bash scripts/install-git-hooks.sh --check    # verify only, exits 1 on drift
```

The guard is installed into the **shared** hooks directory
(`git rev-parse --git-common-dir`), not run from the worktree, so it applies no
matter which branch a worktree sits on — this repo has ~74 worktrees and one
parked on an old branch was carrying a copy that predated the guard. Canonical
sources are version-controlled in `scripts/hooks/`.

Overrides, in increasing order of bluntness:

| | effect |
|---|---|
| `DCHUB_ALLOW_MAIN_PUSH=1 git push` | skip the block, apply the old 20-min deploy throttle |
| `DCHUB_PUSH_FORCE=1 git push` | skip every guard in the hook |

Use `DCHUB_PUSH_FORCE=1` for a genuine production emergency, not to save a
minute. **Never disable a guard to make your own change land.**

### Why the hook exists when branch protection already does this

`main` requires **six** checks — `substance-gate`, `syntax-check`, `unit-tests`,
`regression-lint`, `db-parity` and `app-contract-gate` — and GitHub enforces
them on direct pushes too: the push is rejected with
`GH006: Protected branch update failed`.

★ **`enforce_admins` is `true`**, so that applies to the repo admin as well.
This section used to say `false`, and recorded **137 of 223 commits (61%)
reaching main as ungated admin pushes in the 7 days to 2026-07-28** — that is
the regime the hook was written for, and it is over. Re-verify both facts in
one call:

```bash
gh api repos/azmartone67/dchub-backend/branches/main/protection \
  --jq '{admins: .enforce_admins.enabled, checks: .required_status_checks.contexts}'
```

Measured 2026-09-01: of **1089 commits on `main` since 2026-08-01, exactly one**
— `f5df4f668`, 2026-08-05 — has no associated pull request. (Method: every
commit whose subject does not end in `(#N)`, then
`gh api repos/OWNER/REPO/commits/SHA/pulls` on each candidate. It would miss a
direct push that hand-wrote a `(#N)` suffix.)

**So the hook is no longer the only thing between an admin and `main`** — do
not cite `enforce_admins: false` as its justification. It still earns its
place: it fails in under a second, locally, printing the `gh pr create` recipe,
instead of after a push round-trip; and it keeps working if protection is ever
relaxed. It is the fast path now, not the last line.

A GitHub ruleset with a bypass actor for the Actions app is still impossible
here — Integration bypass actors are org-only and this repo is user-owned
(GitHub returns HTTP 422). This repo has no rulesets at all
(`gh api repos/azmartone67/dchub-backend/rulesets` returns `[]`), so branch
protection is the whole story.

**Nothing in CI writes to `main` any more.** `auto-rollback.yml` used to, and it
never worked: the push was rejected with GH006 every time, and because the step
then failed, the alert step after it was skipped — so a real 5xx burn produced
no rollback *and* no alert. Rollback is now a Railway operation
(`scripts/railway_rollback.py`), which branch protection cannot veto and which
does not need CI to be green — the property that actually matters, since a
rollback gated on green checks deadlocks exactly when production is broken. The
git revert follows as an ordinary PR. See `docs/ROLLBACK-RUNBOOK.md`.

That removed the last argument for keeping `enforce_admins: false`, and it has
since been flipped to `true` — the open question recorded here is closed.

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
python3 -m pytest tests/ -q          # ~9,000 tests, ~8min
```

No test **imports `main.py` in-process** — it opens DB pools, starts keepalive
threads and registers ~200 blueprints. Tests that need shipped code pull the
real function out of the source with `ast` and execute it against stubs.

★ **But the suite does boot the real app, in a subprocess.**
`tests/test_app_contract_gate.py` runs `scripts/app_contract_gate.py` with
`cwd=` the repo root, and that child imports `main.py` for real. So every
import- and registration-time side effect — threads, schedulers, and any
module that writes a file on a **relative** path — runs during
`pytest tests/`, against the working tree.

That is not theoretical. `register_ambassador_routes()` ran two cycles that
raced on `data/ambassador_state.json`, a TRACKED 2.2 MB file, and shredded it
to 4% of its size (#3014, #3018). It went unnoticed for a long time because
the obvious diagnostic lies: an `open()` or audit hook installed inside the
pytest process sees **nothing**, since the writer is a child process. Diff the
working tree instead — `git status --porcelain` after a run.

New state files get a `DCHUB_*_STATE_FILE` env override so a test can redirect
them; `test_app_contract_gate.py` sha256s everything under `data/` across the
boot and fails by name if any of it moves.

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
