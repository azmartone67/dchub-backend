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
- .github/workflows/canary-weekly.yml - Mondays 14:00 UTC. Probes /api/health with canary header; asserts backend==replit.
- .github/workflows/eval-monthly.yml - 1st of month 13:00 UTC. 45-golden MCP eval (see issue #4 for timeout hardening).

## Common Ops

### "Canary is red"
1. curl -sS -D - -H "X-Dchub-Canary: <secret>" https://dchubapiproxy.azmartone.workers.dev/api/health | grep backend
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
- Require status checks: canary-weekly (optional)
- Prevent force-pushes
- Restrict deletions
