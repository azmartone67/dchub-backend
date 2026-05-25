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
from flask import Blueprint, jsonify, request

mcp_oauth_2025_bp = Blueprint("mcp_oauth_2025", __name__)


ROOT = "https://api.dchub.cloud"


@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource", methods=["GET"])
@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource.json", methods=["GET"])
def oauth_protected_resource():
    """RFC 9728 — OAuth 2.0 Protected Resource Metadata.

    Tells clients which authorization servers can issue tokens accepted
    by this resource. Empty array = no auth required (free tier).
    Populated array = enterprise tier; tokens accepted from listed AS.
    """
    return jsonify({
        "resource":                       f"{ROOT}/mcp",
        "resource_documentation":         "https://dchub.cloud/integrations/mcp",
        "resource_name":                  "DC Hub Intelligence MCP Server",
        "resource_policy_uri":            "https://dchub.cloud/terms",
        "resource_tos_uri":               "https://dchub.cloud/terms",
        # Empty array = free tier (anonymous OK). Enterprise customers
        # get a custom AS configured per-tenant; contact api@dchub.cloud.
        "authorization_servers":          [],
        "bearer_methods_supported":       ["header"],
        "resource_signing_alg_values_supported": ["RS256", "ES256"],
        "scopes_supported":               [
            "read:facilities",
            "read:deals",
            "read:grid",
            "read:fiber",
            "read:pipeline",
            "write:reports",
            "admin:tenant",
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
