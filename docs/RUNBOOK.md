# DC Hub Operations Runbook

## Architecture
- MCP surface: https://dchub.cloud/mcp (fronted by dchubapiproxy.azmartone.workers.dev)
- Backend chain (failover order): railway-a -> railway-b -> replit
- Worker version pin: v4.5.11
- Canary gate: X-Dchub-Canary header + CANARY_SECRET env binding

## Secrets & Where They Live
| Secret | Location | Rotation cadence |
|---|---|---|
| CANARY_SECRET | GitHub repo secret AND Cloudflare Worker env | Quarterly (see scripts/rotate-canary-secret.sh) |
| ANTHROPIC_API_KEY | GitHub repo secret | Anthropic dashboard |
| DCHUB_API_KEY | GitHub repo secret | - |
| DCHUB_MCP_URL | GitHub repo secret | Static = https://dchub.cloud/mcp |

## Automated Probes
- .github/workflows/failover-canary.yml - every 6h. Drills dchub.cloud's real
  Railway -> Render failover and asserts on `x-dc-hub-served-by`. This is the
  live failover probe.
- ~~canary-weekly.yml~~ RETIRED 2026-08-20. It asserted `x-backend-used: replit`,
  and every part of that was stale: the deployed worker emits `x-dc-hub-backend`
  (never `x-backend-used`), `/api/health` short-circuits on `x-fast-path:
  wsgi-health` before any backend is selected, its target host
  dchubapiproxy.azmartone.workers.dev still runs worker 4.9.45 while prod runs
  4.70.0, and worker.js:439 says outright "This is the v4.6.1 fork (single
  Railway backend, no canary)" - the replit routing and canary header live in a
  separate unmerged v4.5.16 branch. It had failed every week since at least
  2026-07-13, filing a workflow-failure issue each time. failover-canary.yml
  covers the chain that actually exists.
- .github/workflows/eval-monthly.yml - 1st of month 13:00 UTC. 45-golden MCP eval (see issue #4 for timeout hardening).

## Common Ops

### "Canary is red"
1. curl -sS -D - https://dchub.cloud/api/health | grep -i x-dc-hub-served-by
2. If returns railway-a: CANARY_SECRET mismatch between GitHub and Cloudflare. Rotate via scripts/rotate-canary-secret.sh.
3. If returns nothing: Worker isn't proxying that route - check Worker code proxyToRailway function.

### "Git rejects my workflow push"
gh token or git credential lacks workflow scope. Run:

    unset GH_TOKEN GITHUB_TOKEN
    gh auth status        # confirm gho_ entry is Active: true, has workflow scope
    gh auth setup-git

### "New shell, auth broken again"
Replit injects GITHUB_TOKEN on shell start. Permanent mitigation already in:
- ~/.profile (login shells)
- ~/.replit_shell_rc (Replit-specific)
- gh alias in ~/.profile

For existing subshells: unset GH_TOKEN GITHUB_TOKEN

### "Worker deploy rejected multipart paste"
Use scripts/strip-multipart.py to extract the JS body from a multipart-form capture.

### "Mass-deletion committed in error"
Pre-commit hook blocks >20 file deletions. Override with GUARD_MASS_DELETE_OK=1 git commit ... if intentional. Recovery via merge from the last-good commit.

## Branch Protection (GitHub Settings -> Rules)
Recommended on main:
- Require PR before merge
- Require status checks: see the live list on main (6 today: app-contract-gate,
  db-parity, regression-lint, substance-gate, syntax-check, unit-tests)
- Prevent force-pushes
- Restrict deletions
