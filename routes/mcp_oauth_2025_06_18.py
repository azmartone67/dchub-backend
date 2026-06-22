"""
mcp_oauth_2025_06_18.py — MCP 2025-06-18 OAuth Protected Resource Metadata.

Phase ZZZZZ-round37 (2026-05-24). The MCP spec 2025-06-18 introduces
formal OAuth Protected Resource discovery via RFC 9728. Enterprise
customers using Claude Enterprise, Cursor Enterprise, or self-hosted
MCP relays need this metadata to onboard self-serve.

Pre-r37: worker v4.9.0+ shipped a minimal handler that returned an
empty authorization_servers array (interpreted by Claude.ai as "no
auth required"). That worked for ANONYMOUS connections but kept
enterprise tier locked out — agents that REQUIRE OAuth saw nothing
to call.

r37: Adds the full metadata + Dynamic Client Registration (DCR) stub
+ token introspection endpoint. The token endpoint itself returns
501 with clear "contact api@dchub.cloud for enterprise OAuth" so
the failure mode is informative, not opaque.

Routes:
  GET /.well-known/oauth-protected-resource           — RFC 9728 metadata
  GET /.well-known/oauth-authorization-server         — RFC 8414 metadata
  POST /oauth/register                                 — DCR stub
  POST /oauth/token                                    — token stub
  GET /oauth/introspect                                — introspection stub
"""
import datetime
import os
from flask import Blueprint, jsonify, request

mcp_oauth_2025_bp = Blueprint("mcp_oauth_2025", __name__)


ROOT = "https://api.dchub.cloud"

# r-workos (2026-06-21): wire WorkOS AuthKit as the Authorization Server. DC Hub
# is the RESOURCE SERVER — clients discover WorkOS via authorization_servers and
# do DCR/consent/token directly with WorkOS; DC Hub just validates the JWT
# (Phase B). ENV-GATED: empty WORKOS_AUTHKIT_DOMAIN => authorization_servers stays
# [] (today's anonymous behaviour, unchanged) so this is inert until the domain
# is set AFTER the WorkOS dashboard is configured (CIMD/DCR + Resource Indicator).
_AUTHKIT = (os.environ.get("WORKOS_AUTHKIT_DOMAIN") or "").strip().rstrip("/")
# The MCP resource indicator — MUST match the WorkOS "Resource Indicator" config
# and the token `aud`. This is the URL clients actually connect to.
_RESOURCE = (os.environ.get("DCHUB_MCP_RESOURCE") or "https://dchub.cloud/mcp").strip()


@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource", methods=["GET"])
@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource.json", methods=["GET"])
# r-workos-scopes (2026-06-21): ALSO serve this metadata at a non-.well-known
# path. The live .well-known/* path is served by the out-of-repo dchub-oauth-meta
# CF worker (which advertises stale custom scopes → Claude requests them → WorkOS
# rejects with invalid_scope). The MCP 401 `WWW-Authenticate: resource_metadata`
# points clients HERE instead, so we control the advertised scopes from Flask.
@mcp_oauth_2025_bp.route("/api/v1/oauth-protected-resource", methods=["GET"])
@mcp_oauth_2025_bp.route("/api/v1/oauth-protected-resource/mcp", methods=["GET"])
def oauth_protected_resource():
    """RFC 9728 — OAuth 2.0 Protected Resource Metadata.

    Tells clients which authorization servers can issue tokens accepted
    by this resource. Empty array = no auth required (free tier).
    Populated array = enterprise tier; tokens accepted from listed AS.
    """
    return jsonify({
        "resource":                       _RESOURCE,
        "resource_documentation":         "https://dchub.cloud/integrations/mcp",
        "resource_name":                  "DC Hub Intelligence MCP Server",
        "resource_policy_uri":            "https://dchub.cloud/terms",
        "resource_tos_uri":               "https://dchub.cloud/terms",
        # WorkOS AuthKit as the AS when WORKOS_AUTHKIT_DOMAIN is set; empty = today's
        # anonymous/X-API-Key behaviour (clients read [] as "no auth required").
        "authorization_servers":          ([_AUTHKIT] if _AUTHKIT else []),
        "bearer_methods_supported":       ["header"],
        "resource_signing_alg_values_supported": ["RS256", "ES256"],
        # r-workos-scopes: advertise ONLY the standard OIDC scopes WorkOS AuthKit
        # actually issues (openid/profile/email/offline_access). Custom resource
        # scopes (read:facilities…) make WorkOS reject authorization with
        # invalid_scope. profile+email give us the sub/email for identity binding;
        # offline_access yields a refresh token for durable (multi-day) sessions.
        "scopes_supported":               [
            "openid",
            "profile",
            "email",
            "offline_access",
        ],
        # r37: explicit MCP 2025-06-18 declaration
        "mcp_protocol_version":           "2025-06-18",
        "mcp_protocol_versions_supported": ["2024-11-05", "2025-06-18"],
        "mcp_capabilities": {
            "tools":     {"list_changed": True},
            "resources": {"list_changed": False, "subscribe": False},
            "prompts":   {"list_changed": False},
        },
        "enterprise_contact":             "api@dchub.cloud",
        "tier_with_oauth":                "enterprise",
        "self_serve_alternative":         "Use X-API-Key header instead (free + dev tier).",
        "computed_at":                    datetime.datetime.utcnow().isoformat() + "Z",
    }), 200, {"Cache-Control": "public, max-age=3600",
              "Access-Control-Allow-Origin": "*"}


@mcp_oauth_2025_bp.route("/.well-known/oauth-authorization-server", methods=["GET"])
@mcp_oauth_2025_bp.route("/.well-known/oauth-authorization-server.json", methods=["GET"])
def oauth_authorization_server():
    """RFC 8414 — OAuth 2.0 Authorization Server Metadata."""
    return jsonify({
        "issuer":                                   ROOT,
        "authorization_endpoint":                   f"{ROOT}/oauth/authorize",
        "token_endpoint":                           f"{ROOT}/oauth/token",
        "registration_endpoint":                    f"{ROOT}/oauth/register",
        "introspection_endpoint":                   f"{ROOT}/oauth/introspect",
        "revocation_endpoint":                      f"{ROOT}/oauth/revoke",
        "jwks_uri":                                 f"{ROOT}/.well-known/jwks.json",
        "scopes_supported":                         [
            "read:facilities", "read:deals", "read:grid", "read:fiber",
            "read:pipeline", "write:reports", "admin:tenant",
        ],
        "response_types_supported":                 ["code", "token"],
        "grant_types_supported":                    ["authorization_code", "client_credentials",
                                                      "refresh_token"],
        "token_endpoint_auth_methods_supported":    ["client_secret_basic", "client_secret_post"],
        "code_challenge_methods_supported":         ["S256"],
        "response_modes_supported":                 ["query", "fragment"],
        "subject_types_supported":                  ["public"],
        # r37: explicit DCR support advertised (stub for now)
        "registration_endpoint_auth_methods_supported": ["none"],
        "service_documentation":                    "https://dchub.cloud/integrations/mcp",
        "mcp_protocol_version":                     "2025-06-18",
        "_note": ("Enterprise tier only — anonymous + X-API-Key flows "
                   "remain the recommended path for free/developer/pro."),
    }), 200, {"Cache-Control": "public, max-age=3600",
              "Access-Control-Allow-Origin": "*"}


@mcp_oauth_2025_bp.route("/oauth/register", methods=["POST", "OPTIONS"])
def oauth_register():
    """DCR stub. Returns 501 with an actionable error message and the
    enterprise contact. A real DCR implementation lives behind a
    feature flag — coming Q3 2026 with the enterprise launch."""
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Methods": "POST, OPTIONS"})
    return jsonify({
        "error":             "registration_not_implemented",
        "error_description": ("Dynamic Client Registration is reserved for "
                              "enterprise tier and is not self-serve yet."),
        "self_serve_alternative": {
            "method": "X-API-Key header (no OAuth needed)",
            "signup_url": "https://dchub.cloud/signup",
            "free_tier": "10 calls/day, no card",
            "developer_tier": "$49/mo, 1000 calls/day",
        },
        "enterprise_path": {
            "contact": "api@dchub.cloud",
            "subject": "Enterprise OAuth DCR provisioning",
        },
    }), 501, {"Access-Control-Allow-Origin": "*"}


@mcp_oauth_2025_bp.route("/oauth/token", methods=["POST", "OPTIONS"])
def oauth_token():
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Methods": "POST, OPTIONS"})
    return jsonify({
        "error":             "unsupported_grant_type",
        "error_description": ("Token endpoint is enterprise-only. Use X-API-Key "
                              "for free/developer/pro tiers."),
        "enterprise_contact": "api@dchub.cloud",
    }), 501, {"Access-Control-Allow-Origin": "*"}


@mcp_oauth_2025_bp.route("/oauth/introspect", methods=["POST", "OPTIONS"])
def oauth_introspect():
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*"})
    return jsonify({"active": False,
                    "error": "introspection_not_implemented"}), 501


# ── Phase B (r-workos 2026-06-21): durable-identity binding ────────────────
# The RESOURCE-SERVER half of the WorkOS OAuth connector. The MCP gateway
# (server.mjs) does the JWT crypto: it validates the WorkOS access token
# against the WorkOS JWKS (signature + issuer + audience + exp) BEFORE calling
# this endpoint. We therefore TRUST the {sub,email,iss} it sends, but only from
# the gateway — gated by the same X-Internal-Key the gateway uses for every
# other server-to-server call. Public callers get 403.
#
# Job: map a verified OAuth identity (`sub`) → a durable mcp_dev_keys row
# (get-or-create). The api_key is DETERMINISTIC per sub (HMAC over the server
# secret) so (a) concurrent first-calls converge on one row via
# ON CONFLICT(api_key) DO NOTHING, and (b) the SAME agent identity re-resolves
# to the SAME key across sessions — the retention lever (usage accrues to one
# identity → multi-day return; baseline 4.5%).
#
# DORMANT until the gateway flag DCHUB_WORKOS_OAUTH_ENABLED is on — nothing
# calls this otherwise. Anonymous + X-API-Key flows are completely untouched:
# this only ever runs for a gateway that has ALREADY verified a WorkOS JWT.
@mcp_oauth_2025_bp.route("/api/v1/oauth/identity", methods=["POST"])
def oauth_identity_resolve():
    try:
        from internal_auth import is_valid_internal_key
    except Exception:
        is_valid_internal_key = lambda _v: False  # noqa: E731 — fail-closed
    if not is_valid_internal_key(request.headers.get("X-Internal-Key", "")):
        return jsonify({"error": "internal_only"}), 403

    body = request.get_json(silent=True) or {}
    sub = (body.get("sub") or "").strip()
    iss = (body.get("iss") or "").strip()
    email = (body.get("email") or "").strip() or None
    if not sub:
        return jsonify({"error": "sub_required"}), 400

    import hmac as _hmac
    import hashlib as _hashlib
    import json as _json
    import psycopg2 as _pg
    # HMAC so the durable key is NOT guessable from the public `sub` alone.
    _secret = (os.environ.get("JWT_SECRET")
               or os.environ.get("DCHUB_SESSION_SECRET")
               or "dchub-oauth-fallback").encode()
    _h = _hmac.new(_secret, f"workos:{sub}".encode(), _hashlib.sha256).hexdigest()
    api_key = f"dch_oauth_{_h[:32]}"
    developer_id = f"dev_oauth_{_h[:12]}"
    # tier CHECK on mcp_dev_keys = free/paid/enterprise ONLY — seed 'free'.
    metadata = {"source": "workos_oauth", "oauth_sub": sub, "oauth_iss": iss}

    # CONTACTABILITY (2026-06-22): the gateway can't supply an email — WorkOS access
    # tokens omit the email claim, and /oauth2/userinfo 401s our aud-bound token
    # (RFC 8707, valid for us not for WorkOS). Resolve it server-side via the WorkOS
    # Management API by user_id (the verified `sub`). The API key lives HERE (backend
    # secrets), NOT on the internet-facing gateway. DORMANT unless WORKOS_API_KEY is
    # set. Fail-safe: any miss → email stays None (durable identity still works; the
    # row is just not yet contactable). Backfills the existing row via COALESCE below.
    if not email and sub.startswith("user_"):
        _wk = (os.environ.get("WORKOS_API_KEY") or "").strip()
        if _wk:
            try:
                import urllib.request as _ur
                _req = _ur.Request(
                    f"https://api.workos.com/user_management/users/{sub}",
                    headers={"Authorization": f"Bearer {_wk}",
                             "Accept": "application/json",
                             "User-Agent": "dchub-oauth-identity/1.0"})
                with _ur.urlopen(_req, timeout=5) as _resp:
                    if getattr(_resp, "status", 200) == 200:
                        _u = _json.loads(_resp.read().decode("utf-8") or "{}")
                        _e = ((_u.get("email") or "").strip().lower()) or None
                        if _e and "@" in _e:
                            email = _e
            except Exception as _we:
                print(f"[oauth-identity] workos email lookup failed: {_we!r}", flush=True)

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return jsonify({"error": "no_db"}), 500
    conn = None
    try:
        conn = _pg.connect(dsn, sslmode="require", connect_timeout=5)
        cur = conn.cursor()
        # Get-or-create. Deterministic key => concurrent first-calls collide on
        # the SAME api_key and DO NOTHING resolves the race to one row.
        cur.execute(
            "INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, status, metadata) "
            "VALUES (%s, %s, %s, 'free', 'active', %s::jsonb) "
            "ON CONFLICT (api_key) DO NOTHING",
            (api_key, developer_id, email, _json.dumps(metadata)),
        )
        # Backfill an email we learned later (row created before bind).
        if email:
            cur.execute(
                "UPDATE mcp_dev_keys SET email = COALESCE(NULLIF(email, ''), %s) "
                "WHERE api_key = %s",
                (email, api_key),
            )
        # Read back the CURRENT tier so a later upgrade reflects immediately.
        cur.execute("SELECT tier FROM mcp_dev_keys WHERE api_key = %s", (api_key,))
        row = cur.fetchone()
        conn.commit()
        tier = (row[0] if row else "free") or "free"
        return jsonify({
            "api_key": api_key,
            "tier": tier,
            "developer_id": developer_id,
            "source": "workos_oauth",
        }), 200
    except Exception as e:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "identity_resolve_failed",
                        "detail": f"{type(e).__name__}: {e}"}), 500
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
