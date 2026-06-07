# Security: remove public hardcoded internal key — deploy runbook

**Branch:** `security/remove-hardcoded-internal-key` (staged, NOT pushed)
**Date:** 2026-06-07

> ⚠️ **The hole stays OPEN until BOTH happen: (a) this branch deploys, AND
> (b) `INTERNAL_AUTH_LEGACY_OK` is set to `0` on every host.** Railway
> currently has it **=1** (verified 2026-06-07), which overrides the new code
> default. Code changes alone do NOT fully close it.

## What & why
`azmartone67/dchub-backend` is a **public** repo. The string
`dchub-internal-sync-2026` was a hardcoded admin-bypass accepted on 50+
internal/admin gates. Proven exposure: an unauthenticated `curl` with only
that public string read paying customers' emails + **Stripe customer IDs**
from `/api/v1/admin/founding-customers`, and could hit write/outreach
endpoints (`tag`, `schema_repair`, `facility_admin`, outreach senders).

## What this branch changes (code)
- `internal_auth.py`: `LEGACY_OK` default flipped **ON→OFF**; last-resort
  sender returns `""` (fail closed); added `accepted_internal_keys()`
  (env-sourced, filters empties).
- 28 route modules + `main.py` (6 sites) + 2 oddballs (`surveillance_sweep`,
  `brain_autoaction_helpers`): inline `{"dchub-internal-sync-2026"}` accept
  sets replaced with **env-sourced** keys. These no longer reference the
  literal AT ALL, so they are closed independent of `LEGACY_OK`.
- Senders fail closed instead of falling back to the public string:
  `.github/workflows/data-sync.yml`, `.github/workflows/dchub-osm-refresh.yml`,
  `server.mjs`.
- All changed Python compiles (`py_compile` clean); `server.mjs` passes
  `node --check`. No live accept site still references the literal (remaining
  hits are comments/docstrings + the intentionally-gated `_LEGACY_KEYS`).

## Deploy order (DO NOT skip / reorder)
1. **Add the GitHub Actions secret** (workflows now FAIL without it):
   ```bash
   # value = the 64-char DCHUB_INTERNAL_KEY already on Railway
   gh secret set DCHUB_INTERNAL_KEY --repo azmartone67/dchub-backend
   ```
2. **Confirm `DCHUB_INTERNAL_KEY` is set** on every host that calls or serves
   internal endpoints: Railway backend (✅ 64ch), **Render** (verify),
   **dchub-mcp-server** service (verify). Missing on any host → internal auth
   there breaks after step 3.
3. **Set `INTERNAL_AUTH_LEGACY_OK=0`** on Railway, Render, AND the MCP server.
   **Railway currently has it =1 — this is the single most important step.**
   ```bash
   railway variables --set INTERNAL_AUTH_LEGACY_OK=0     # repeat on Render + MCP server
   ```
4. **Sibling repo:** apply the same `|| ''` fix to
   `~/dchub-mcp-server/server.mjs` (line ~202) and redeploy that service.
   (The in-repo `dchub-mcp-v2.1/` copy is stale/undeployed — optional.)
5. **Deploy** this branch (Render is healthy; Railway was degraded
   2026-06-07 — deploy when its storage incident clears or rely on Render).

## Verify after deploy + env flip
```bash
# Literal must now be REJECTED (expect 403):
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "X-Internal-Key: dchub-internal-sync-2026" \
  https://dchub.cloud/api/v1/admin/founding-customers      # → 403

# Real key must still work (expect 200):
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "X-Internal-Key: $DCHUB_INTERNAL_KEY" \
  https://dchub.cloud/api/v1/admin/founding-customers      # → 200
```

## Notes
- The literal stays in git history forever (public) — that's fine; it is now
  **dead** (rejected). The 64-char `DCHUB_INTERNAL_KEY` env value was never in
  code, so **no rotation of that value is required**.
- Optional later cleanup: delete `_LEGACY_KEYS` from `internal_auth.py` once
  logs show zero `legacy hardcoded key accepted` warnings.
