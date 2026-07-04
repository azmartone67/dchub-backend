/**
 * dchub-oauth-meta — CF Worker BEHAVIORAL CAPTURE (2026-07-04)
 * ===========================================================================
 * STATUS: SOURCE NOT MIRRORED. This file is a BEHAVIORAL capture, not the
 * deployed JS module. The live worker's bundle could NOT be pulled this run —
 * see "WHY THE SOURCE IS MISSING" below. It documents exactly what the live
 * worker EMITS (verbatim HTTP captures) plus the exact command to pull the
 * real module the moment a valid Cloudflare credential is available.
 *
 * Read-only infra archaeology (OAUTH STAGE-0). NO deploys, NO edits, doc-only.
 *
 * ---------------------------------------------------------------------------
 * WHY THE SOURCE IS MISSING (credential blocker, 2026-07-04)
 * ---------------------------------------------------------------------------
 * The canonical pull path is:
 *   railway variables --service dchub-backend --json  -> CLOUDFLARE_API_TOKEN
 *   GET https://api.cloudflare.com/client/v4/accounts/
 *       4bb33ec40ef02f9f4b41dc97668d5a52/workers/scripts/dchub-oauth-meta
 *       (multipart; extract the JS module)
 *
 * Both credential sources were dead on the capturing host:
 *   - Railway CLI  : `railway variables` -> "invalid_grant" on OAuth refresh,
 *                    then "Unauthorized. Please run `railway login` again."
 *   - Railway MCP  : list_projects/list_variables -> "Unauthorized".
 *   - Stored token : ~/.railway/config.json accessToken -> GraphQL "Not
 *                    Authorized" (refresh token also revoked).
 *   - wrangler OAuth (~/.wrangler/config/default.toml, valid to 2026-07-04
 *     17:49Z) authenticates, BUT its user is NOT a member of CF account
 *     4bb33ec4...: GET /accounts -> [] ; GET /accounts/4bb33ec4... ->
 *     {"code":9109,"message":"Unauthorized to access requested resource"} ;
 *     GET .../workers/scripts/dchub-oauth-meta -> HTTP 403
 *     {"code":10000,"message":"Authentication error"}.
 *
 * => No credential on this host can read account 4bb33ec4...'s workers.
 *    The module pull is deferred; the behavior below is fully captured.
 *
 * TO COMPLETE THE SOURCE PULL (run when a live CLOUDFLARE_API_TOKEN exists):
 *   TOKEN=$(railway variables --service dchub-backend --json \
 *           | python3 -c 'import json,sys;print(json.load(sys.stdin)["CLOUDFLARE_API_TOKEN"])')
 *   curl -sS -H "Authorization: Bearer $TOKEN" \
 *     "https://api.cloudflare.com/client/v4/accounts/4bb33ec40ef02f9f4b41dc97668d5a52/workers/scripts/dchub-oauth-meta" \
 *     -o dchub-oauth-meta.multipart
 *   # then split the multipart on its boundary and extract the JS module part,
 *   # exactly as documented in the dchubapiproxy worker runbook.
 *
 * ===========================================================================
 * LIVE BEHAVIOR — verbatim captures (curl, 2026-07-04 ~15:25 UTC)
 * ===========================================================================
 *
 * The dchub-oauth-meta worker fronts the .well-known/* OAuth discovery paths
 * at the dchub.cloud edge. Identified as the serving component because these
 * responses carry CF-only headers (server: cloudflare, speculation-rules) and
 * NONE of the Railway/Flask markers (no x-dc-hub-served-by, no
 * x-dc-worker-version, no x-railway-*). Contrast: /api/v1/oauth-protected-
 * resource is Railway/Flask (x-dc-hub-served-by: railway-primary).
 *
 * ---------------------------------------------------------------------------
 * (1) GET https://dchub.cloud/.well-known/oauth-protected-resource  -> 200
 *     content-type: application/json; charset=utf-8   cache-control: no-store
 *     (served by worker — no railway headers)
 * ---------------------------------------------------------------------------
 * BODY (verbatim):
 */
const LIVE__well_known__oauth_protected_resource = {
  "resource": "https://dchub.cloud/mcp",
  "resource_name": "DC Hub Intelligence MCP Server",
  "resource_documentation": "https://dchub.cloud/integrations/mcp",
  "authorization_servers": [],          // <-- EMPTY. no AS advertised.
  "bearer_methods_supported": ["header"],
  "scopes_supported": ["openid", "profile", "email", "offline_access"],
  "mcp_protocol_version": "2025-06-18"
};
/*
 * NOTE — DRIFT vs git source. The git-tracked worker
 * `dchub-frontend/_worker.js` (repo azmartone67/dchub-frontend) emits this
 * SAME 7-field shape in the SAME field order (its handler block, r-workos-
 * oauth 2026-06-21), EXCEPT it sets:
 *     authorization_servers: ['https://beloved-stream-52.authkit.app']
 * The LIVE worker returns []  ==>  the deployed dchub-oauth-meta worker is
 * BEHIND / diverged from that git source: the WorkOS-AuthKit advertisement
 * that exists in git is NOT live. Live serves the pre-/rolled-back empty
 * array. This is the "not mirrored in git" state the ADR finding flagged.
 *
 * ---------------------------------------------------------------------------
 * (2) GET https://dchub.cloud/.well-known/oauth-authorization-server  -> 404
 *     content-type: text/plain; charset=utf-8   body: "Not Found"
 *     (served by worker — no railway headers)
 * ---------------------------------------------------------------------------
 * The Flask blueprint routes/mcp_oauth_2025_06_18.py DEFINES an RFC 8414
 * handler for this path, but the worker intercepts .well-known/* and returns
 * a hard 404 — so the RFC 8414 authorization-server metadata is NOT reachable
 * at the well-known path in production. (git _worker.js confirms: intentional
 * "clean 404" — "We are NOT the authorization server (WorkOS AuthKit is).")
 *
 * ===========================================================================
 * END behavioral capture. Replace this file with the real extracted module
 * when a credential for CF account 4bb33ec4... becomes available.
 * ===========================================================================
 */
