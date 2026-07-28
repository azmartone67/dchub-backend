# Rollback runbook

**Rolling production back is a Railway operation, not a git operation.**

## Why

`main` is protected. `substance-gate`, `syntax-check` and `unit-tests` are
required checks, and `github-actions[bot]` is not a repo admin. Any bot push to
`main` is rejected:

```
remote: error: GH006: Protected branch update failed for refs/heads/main.
remote: - Required status check "substance-gate" is expected.
 ! [remote rejected] main -> main (protected branch hook declined)
```

That is deliberate and stays. The failure mode it created was not: a rollback
that has to pass CI deadlocks exactly when production is broken, because
"production is down" and "CI is red" are usually the same event.

So `auto-rollback` restores service by telling Railway to re-run the previous
image. Branch protection cannot veto it and it does not wait for a green run.
The git revert still happens — as a PR, at human pace, on the normal path.

Two paths, two jobs:

| | restores service | stops a re-ship |
|---|---|---|
| Railway rollback | ✅ seconds, no CI | ❌ |
| Revert PR | ❌ needs green CI | ✅ |

Do both. Never wait on the second to get the first.

## What CI is actually allowed to do

Measured 2026-07-28, with `GITHUB_TOKEN`:

| action | allowed? |
|---|---|
| push a branch | ✅ |
| push to `main` | ❌ `GH006` — protected, bot is not an admin |
| open a pull request | ❌ *"GitHub Actions is not permitted to create or approve pull requests"* |

So **the git side of a rollback cannot be automated end to end today.** The
workflows push the revert branch — which does work — and put a one-click
compare link in the alert. Opening the PR is a human click.

That is precisely why rollback is a Railway operation: it is the only
remediation CI can actually complete on its own.

To let the bot open the PR too, either:

- **Settings → Actions → General → "Allow GitHub Actions to create and approve
  pull requests"** (repo-wide; also lets any workflow open PRs), or
- add a `GH_PAT` secret — `brain-pr-post-merge-guard.yml` already prefers it.

Neither is required for a rollback to restore service.

## Prerequisite: the `RAILWAY_TOKEN` secret

**The automated rollback is inert until this is set.** Without it the gate still
detects the burn and opens an alerting issue, but it cannot restore service.

1. Railway dashboard → Account Settings → Tokens → create a token scoped to the
   `resourceful-essence` project.
2. Add it as a repository secret named `RAILWAY_TOKEN`
   (Settings → Secrets and variables → Actions → New repository secret).

`funnel-autotune.yml` already expects this same secret, and it is not set there
either — adding it fixes both.

To confirm it works once set, run `auto-rollback` from the Actions tab with
**drill = true**. That resolves the rollback target and opens a `🧪 drill` issue
without touching production.

## Who is watching, and how often

| detector | where | real cadence | can roll back |
|---|---|---|---|
| `slo_rollback_sentinel` | **dchub-worker** | every 60s | yes, when armed |
| `auto-rollback.yml` | GitHub cron | **~30 min median** (p90 73, max 84) | yes, if `RAILWAY_TOKEN` set |

The sentinel is the primary. The GitHub job declares `*/5` but GitHub throttles
schedules in this repo — measured over 100 runs, the median gap is 29.7 min and
it is never under 22. It is kept as a backstop because it is the only detector
in a **separate failure domain**: `dchub-worker` deploys from this same repo, so
a bad commit can take the sentinel down along with the backend.

The sentinel grades in-process via `slo_error_budget.compute_budget()` — a DB
read, no HTTP hop through the web service it is measuring, and the same function
the endpoint serves, so the two cannot drift on what `hard_burn` means.

> **Blind spot, unchanged by the move:** the verdict reads `brain_http_errors`,
> which the *web* role writes after each response. A **total** outage writes no
> rows and therefore grades as `within_budget`. Total-outage detection belongs
> to the uptime probes and the failover chain, not to this gate.

### Arming the sentinel

Detection always runs. Rolling production back needs BOTH, on **dchub-worker**:

```
SLO_SENTINEL_ROLLBACK=1
RAILWAY_TOKEN=<project-scoped token>
```

Disarmed it still detects, logs, and files a `brain_findings` row
(`detector='slo_rollback_sentinel'`) — it just does not actuate.

Check it: `GET /api/v1/slo/sentinel/status`. If `serving_process_runs_loop` is
false you reached a *web* replica; the loop runs on the worker, so read
dchub-worker logs for the real state.

Other knobs: `SLO_SENTINEL_ENABLED=0` (kill switch),
`SLO_SENTINEL_INTERVAL_S` (default 60), `SLO_SENTINEL_COOLDOWN_S` (default
1800 — after acting it stays quiet, because `brain_http_errors` still holds the
pre-rollback 5xx for its 5-minute window and would otherwise re-trigger on
damage already fixed).

### ⚠️ Arm only one

If `RAILWAY_TOKEN` is set as a **GitHub secret** *and* the sentinel is armed,
both can roll back the same incident. The second actor runs after the first
already restored the good image, sees it as "same commit as current", skips it,
and **rolls back too far**. Recommended: arm the sentinel, leave `RAILWAY_TOKEN`
unset in GitHub so `auto-rollback.yml` stays alert-only.

## Automatic path

`auto-rollback.yml` samples `/api/v1/slo/error-budget` five times. On 3+/5
`hard_burn` it:

1. rolls Railway back to the last good deployment (`scripts/railway_rollback.py`),
2. opens a revert PR for the offending commit,
3. **always** opens an issue saying what each step actually did, and
4. fails the run if the burn was not remediated, so it cannot look green.

Step 3 is the part that was broken: the old workflow died on the rejected push
and skipped its issue step, so a real burn produced no rollback *and* no alert.

## Manual path

When the automation cannot (no token, API down, no eligible image):

```bash
export RAILWAY_TOKEN=...            # project-scoped token
python3 scripts/railway_rollback.py --dry-run    # what would it roll back to?
python3 scripts/railway_rollback.py              # do it, and wait for recovery
```

Exit codes: `0` done · `2` no eligible target · `3` API/auth failure ·
`4` rollback issued but the service never came back.

Or: Railway dashboard → **dchub-backend** → Deployments → ⋯ → **Rollback**.

Then open the revert PR by hand so the next deploy does not re-ship the bad
commit:

```bash
git checkout -b revert-<sha> && git revert --no-edit <sha> && git push origin HEAD
gh pr create --base main --fill
```

## Gotchas

- **Rolling back does not change `main`.** The bad commit is still there, so the
  next deploy re-ships it. The revert PR is what actually closes the incident.
- **`dchub-worker` deploys from this same repo.** `railway_rollback.py` targets
  `dchub-backend` (what serves the 5xx). If the regression is in worker code,
  roll that back too:
  `python3 scripts/railway_rollback.py --service-id <dchub-worker id>`.
- **Image retention.** Railway can only roll back to deployments it still has an
  image for; the script skips anything with `canRollback: false` and tells you
  when nothing is eligible.
- **Same-commit targets are skipped.** Rolling back to a redeploy of the *same*
  bad SHA would report success and fix nothing.
- **The published Railway docs are wrong about the mutation.** They show
  `deploymentRollback(id:) { id status }`; the live schema returns `Boolean!`,
  so a sub-selection fails GraphQL validation. `tests/test_railway_rollback.py`
  guards this.

## What not to do

- **Do not weaken branch protection** to let the bot push. The required checks
  are deliberate (added 2026-07-28).
- **A GitHub ruleset with an Integration bypass actor is not available here.**
  This is a user-owned repo; GitHub returns 422 *"Actor GitHub Actions
  integration must be part of the ruleset source or owner organization"*.
  `evaluate` (dry-run) enforcement is Enterprise-only. The only bypass actor a
  user repo accepts is `RepositoryRole`, which the owner already has.
- **Do not make the rollback depend on a green CI run.** That is the deadlock
  this design exists to avoid.
