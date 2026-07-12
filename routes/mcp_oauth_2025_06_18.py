"""
mcp_oauth_2025_06_18.py — MCP 2025-06-18 OAuth: the THIN resource-server layer
around WorkOS AuthKit (the Authorization Server).

★ ARCHITECTURE (do NOT hand-roll an AS) ─────────────────────────────────────
DC Hub is the OAuth 2.0 **RESOURCE SERVER**. WorkOS AuthKit
(`WORKOS_AUTHKIT_DOMAIN`, e.g. https://beloved-stream-52.authkit.app) is the
hardened **AUTHORIZATION SERVER** — it owns authorize/token/register/jwks and
issues the access tokens. This module ONLY:

  1. advertises discovery metadata (RFC 9728 protected-resource + RFC 8414
     authorization-server) that POINTS AT WorkOS — never at DC Hub crypto;
  2. bridges a WorkOS-verified identity → a durable DC Hub key + tier
     (`/api/v1/oauth/identity`, gated by X-Internal-Key, called ONLY by the
     MCP gateway after it has cryptographically verified the JWT);
  3. offers an OPTIONAL, default-OFF same-origin DCR proxy to WorkOS's native
     RFC 7591 endpoint (for marketplace reviewers that require same-origin DCR).

The actual JWT crypto (signature + issuer + audience + exp, JWKS self-heal)
happens in the OUT-OF-REPO MCP gateway `dchub-mcp-server/server.mjs`
(`resolveWorkosBearer`). This module TRUSTS the {sub,email,iss} the gateway
forwards, but only from the gateway (X-Internal-Key). Public callers → 403.

★ FAIL-SOFT + ADDITIVE. Everything here is ENV-GATED and inert until WorkOS is
configured. The X-API-Key path (flask_mcp_endpoints /keys/validate) is the
default free/dev/pro path and is COMPLETELY UNTOUCHED. OAuth is ADDITIVE for
enterprise connectors (Gemini Enterprise, Copilot, Google Cloud Marketplace).

★ SECURITY INVARIANTS
  - An OAuth token can NEVER grant more than the mapped identity's real tier:
    fresh WorkOS subs seed `tier='free'`; upgrades happen ONLY via Stripe/admin
    on the mcp_dev_keys row. `_clamp_tier` is the last-line downgrade guard.
  - The durable key is HMAC(server_secret, 'workos:<sub>') — deterministic per
    identity (retention) but NOT guessable from the public `sub`.
  - The identity bridge is fail-CLOSED on a bad/absent X-Internal-Key (403).
  - Metadata `issuer` is the WorkOS domain (the real token issuer), never
    dchub.cloud — DC Hub does not sign tokens.

★ LIVE-EDGE NOTE: the live `dchub.cloud/.well-known/*` path is served by an
OUT-OF-REPO Cloudflare worker `dchub-oauth-meta`; the Flask routes here also
answer on the `api.dchub.cloud` origin and at `/api/v1/oauth-*`. The MCP 401
`WWW-Authenticate: resource_metadata` points clients at the Flask path so we
control the advertised scopes/AS from code. Any change to the advertised AS
MUST be mirrored into the CF worker in lockstep (see reference memory).

Routes:
  GET  /.well-known/oauth-protected-resource[/mcp|.json]  — RFC 9728 metadata
  GET  /api/v1/oauth-protected-resource[/mcp]             — same (edge-bypass)
  GET  /.well-known/oauth-authorization-server[.json]     — RFC 8414 → WorkOS
  POST /oauth/register                                     — DCR → WorkOS (opt)
  POST /oauth/token                                        — 400 → WorkOS token
  POST /oauth/introspect                                   — 400 → WorkOS
  POST /api/v1/oauth/identity                              — token→tier bridge
"""
import datetime
import os
from flask import Blueprint, jsonify, request

mcp_oauth_2025_bp = Blueprint("mcp_oauth_2025", __name__)


# ── ENV / config ─────────────────────────────────────────────────────────────
# WorkOS AuthKit domain = the Authorization Server. Empty => this whole module
# stays inert (metadata advertises no AS; today's anonymous/X-API-Key behaviour).
_AUTHKIT = (os.environ.get("WORKOS_AUTHKIT_DOMAIN") or "").strip().rstrip("/")

# r-onboarding-fix: advertising an authorization_server in the RFC 9728
# protected-resource doc makes Claude.ai/Cursor START an OAuth sign-in the
# moment a connector is added — but /mcp actually accepts anonymous + X-API-Key.
# So merely setting WORKOS_AUTHKIT_DOMAIN must NOT force every self-serve user
# into a WorkOS login. Gate the RFC 9728 `authorization_servers` advert behind
# an EXPLICIT flag; default OFF => authorization_servers=[] => today's working
# anon/X-API-Key behaviour. (The RFC 8414 AS-metadata doc is decoupled — see
# oauth_authorization_server below.)
_ADVERTISE_OAUTH = (os.environ.get("MCP_OAUTH_ADVERTISE", "").strip().lower()
                    in ("1", "true", "yes", "on"))

# OPTIONAL same-origin DCR proxy to WorkOS's native /oauth2/register. Default
# OFF. Only enable if a marketplace reviewer requires the registration_endpoint
# be same-origin as the resource; otherwise discovery clients use the WorkOS
# registration_endpoint advertised in the AS metadata directly (zero relay).
_DCR_PROXY = (os.environ.get("MCP_OAUTH_DCR_PROXY", "").strip().lower()
              in ("1", "true", "yes", "on"))

# The MCP resource indicator — MUST match the WorkOS "Resource Indicator" config
# and the token `aud` the gateway enforces. This is the URL clients connect to.
_RESOURCE = (os.environ.get("DCHUB_MCP_RESOURCE") or "https://dchub.cloud/mcp").strip()

# WorkOS AuthKit issues ONLY these standard OIDC scopes. Custom resource scopes
# (read:facilities…) make WorkOS reject authorization with invalid_scope.
# profile+email give the sub/email for identity binding; offline_access yields a
# refresh token for durable (multi-day) sessions.
_OIDC_SCOPES = ["openid", "profile", "email", "offline_access"]

_VALID_TIERS = ("free", "paid", "enterprise")


# ── Pure helpers (unit-testable — NO Flask app / DB / network) ────────────────
def _clamp_tier(tier):
    """Clamp any tier string to the mcp_dev_keys CHECK-constraint vocabulary
    (free/paid/enterprise). Unknown/empty => 'free'.

    SECURITY: last-line guard that a garbage/injected tier value can only ever
    DOWNGRADE — an OAuth identity must never resolve to a tier the row doesn't
    actually hold."""
    t = (tier or "").strip().lower()
    return t if t in _VALID_TIERS else "free"


def _derive_oauth_identity_key(sub, secret):
    """Deterministic, non-guessable durable key + developer_id for a WorkOS
    subject already verified by the gateway.

    HMAC(secret, 'workos:<sub>') so (a) the key is STABLE across sessions for the
    SAME identity (the retention lever) and (b) it is NOT derivable from the
    public `sub` alone without the server secret. Returns (api_key, developer_id).
    Raises ValueError on empty sub (fail-closed)."""
    import hmac
    import hashlib
    sub = (sub or "").strip()
    if not sub:
        raise ValueError("sub required")
    secret_b = secret if isinstance(secret, (bytes, bytearray)) else str(secret).encode()
    h = hmac.new(secret_b, f"workos:{sub}".encode(), hashlib.sha256).hexdigest()
    return f"dch_oauth_{h[:32]}", f"dev_oauth_{h[:12]}"


def _derive_workos_endpoints(domain):
    """Canonical AuthKit OAuth 2.0 endpoint map from a WorkOS domain. AuthKit's
    endpoint paths are fixed (/oauth2/authorize|token|register|jwks|
    introspection|userinfo|device_authorization). Empty domain => {}."""
    d = (domain or "").strip().rstrip("/")
    if not d:
        return {}
    return {
        "issuer":                          d,
        "authorization_endpoint":          f"{d}/oauth2/authorize",
        "token_endpoint":                  f"{d}/oauth2/token",
        "registration_endpoint":           f"{d}/oauth2/register",
        "jwks_uri":                        f"{d}/oauth2/jwks",
        "introspection_endpoint":          f"{d}/oauth2/introspection",
        "userinfo_endpoint":               f"{d}/oauth2/userinfo",
        "device_authorization_endpoint":   f"{d}/oauth2/device_authorization",
    }


def _workos_as_metadata(domain, resource):
    """RFC 8414 Authorization Server Metadata pointing at WorkOS AuthKit.

    `issuer` == the WorkOS domain (the REAL token issuer), NOT dchub.cloud — DC
    Hub is only the resource server and never signs tokens. Mirrors WorkOS's own
    published metadata (verified live) so a client that fetches this courtesy
    copy from dchub.cloud gets the same truth it would from WorkOS directly.
    Returns None when domain is unset (=> caller 404s: "we are not an AS")."""
    ep = _derive_workos_endpoints(domain)
    if not ep:
        return None
    return {
        "issuer":                                   ep["issuer"],
        "authorization_endpoint":                   ep["authorization_endpoint"],
        "token_endpoint":                           ep["token_endpoint"],
        "registration_endpoint":                    ep["registration_endpoint"],
        "introspection_endpoint":                   ep["introspection_endpoint"],
        "jwks_uri":                                 ep["jwks_uri"],
        "userinfo_endpoint":                        ep["userinfo_endpoint"],
        "device_authorization_endpoint":            ep["device_authorization_endpoint"],
        "scopes_supported":                         list(_OIDC_SCOPES),
        "response_types_supported":                 ["code"],
        "response_modes_supported":                 ["query"],
        "grant_types_supported":                    ["authorization_code", "refresh_token"],
        # WorkOS supports public (PKCE) AND confidential clients — Gemini
        # Enterprise wants a client_id + client_secret (confidential); Claude.ai
        # uses public (none) + PKCE.
        "token_endpoint_auth_methods_supported":    ["none", "client_secret_post",
                                                     "client_secret_basic"],
        "code_challenge_methods_supported":         ["S256"],
        "subject_types_supported":                  ["public"],
        # RFC 7591 DCR is served by WorkOS (open registration) at the
        # registration_endpoint above.
        "registration_endpoint_auth_methods_supported": ["none"],
        "resource":                                 resource,
        "service_documentation":                    "https://dchub.cloud/integrations/mcp",
        "mcp_protocol_version":                     "2025-06-18",
        "_note": ("DC Hub is the RESOURCE server; WorkOS AuthKit is the "
                  "Authorization Server. Register clients (RFC 7591 DCR), run "
                  "the authorization-code+PKCE flow, and obtain tokens at the "
                  "WorkOS endpoints above. Present the resulting access token as "
                  f"'Authorization: Bearer' to the MCP resource ({resource})."),
    }


# RFC 7591 client-metadata fields allowed to pass through to WorkOS DCR.
_DCR_ALLOWED_FIELDS = frozenset((
    "redirect_uris", "token_endpoint_auth_method", "grant_types",
    "response_types", "client_name", "client_uri", "logo_uri", "scope",
    "contacts", "tos_uri", "policy_uri", "jwks_uri", "jwks",
    "software_id", "software_version", "software_statement", "subject_type",
))


def _is_safe_redirect_uri(u):
    """Exact-form redirect-URI check: absolute https:// OR http://localhost |
    127.0.0.1 (dev only). No wildcards, no fragments (OAuth forbids them), no
    javascript:/data:/file: schemes. Defends the DCR proxy against registering a
    client with an open-redirect target."""
    if not isinstance(u, str) or "#" in u:
        return False
    try:
        from urllib.parse import urlparse
        p = urlparse(u)
    except Exception:
        return False
    if p.scheme == "https":
        return bool(p.netloc)
    if p.scheme == "http":
        return (p.hostname or "").lower() in ("localhost", "127.0.0.1")
    return False


def _sanitize_dcr_request(body, max_redirect_uris=10):
    """Validate + whitelist an RFC 7591 registration request before forwarding
    to WorkOS. Returns (clean_dict, error_code_or_None).

    SECURITY:
      - only whitelisted RFC 7591 fields survive (drops junk / injection);
      - redirect_uris MUST be a non-empty, capped list of safe absolute URIs
        (prevents open-redirect registration + unbounded payloads);
      - a requested `scope` is narrowed to the OIDC set WorkOS actually issues
        (custom read:* would trigger invalid_scope downstream)."""
    if not isinstance(body, dict):
        return None, "invalid_client_metadata"
    clean = {k: v for k, v in body.items() if k in _DCR_ALLOWED_FIELDS}
    ru = clean.get("redirect_uris")
    if not isinstance(ru, list) or not ru:
        return None, "redirect_uris_required"
    if len(ru) > max_redirect_uris:
        return None, "too_many_redirect_uris"
    for u in ru:
        if not _is_safe_redirect_uri(u):
            return None, "invalid_redirect_uri"
    if "scope" in clean and isinstance(clean["scope"], str):
        req = [s for s in clean["scope"].split() if s in _OIDC_SCOPES]
        clean["scope"] = " ".join(req) if req else " ".join(_OIDC_SCOPES)
    return clean, None


_CORS = {"Access-Control-Allow-Origin": "*"}
_META_HEADERS = {"Cache-Control": "public, max-age=3600",
                 "Access-Control-Allow-Origin": "*"}


# ── RFC 9728 — Protected Resource Metadata ───────────────────────────────────
@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource", methods=["GET"])
@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
@mcp_oauth_2025_bp.route("/.well-known/oauth-protected-resource.json", methods=["GET"])
@mcp_oauth_2025_bp.route("/api/v1/oauth-protected-resource", methods=["GET"])
@mcp_oauth_2025_bp.route("/api/v1/oauth-protected-resource/mcp", methods=["GET"])
def oauth_protected_resource():
    """RFC 9728 — tells clients which AS can issue tokens this resource accepts.

    Empty `authorization_servers` => "no auth required" (today's free-tier
    anon/X-API-Key behaviour). Populated (WorkOS) => enterprise OAuth path. The
    populate is gated behind BOTH a configured WorkOS domain AND the explicit
    MCP_OAUTH_ADVERTISE flag, so this stays inert until OAuth is genuinely the
    intended path (avoids forcing anon self-serve users into a login)."""
    return jsonify({
        "resource":                       _RESOURCE,
        "resource_documentation":         "https://dchub.cloud/integrations/mcp",
        "resource_name":                  "DC Hub Intelligence MCP Server",
        "resource_policy_uri":            "https://dchub.cloud/terms",
        "resource_tos_uri":               "https://dchub.cloud/terms",
        "authorization_servers":          ([_AUTHKIT] if (_AUTHKIT and _ADVERTISE_OAUTH) else []),
        "bearer_methods_supported":       ["header"],
        "resource_signing_alg_values_supported": ["RS256", "ES256"],
        "scopes_supported":               list(_OIDC_SCOPES),
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
    }), 200, dict(_META_HEADERS)


# ── RFC 8414 — Authorization Server Metadata (points at WorkOS) ───────────────
@mcp_oauth_2025_bp.route("/.well-known/oauth-authorization-server", methods=["GET"])
@mcp_oauth_2025_bp.route("/.well-known/oauth-authorization-server.json", methods=["GET"])
@mcp_oauth_2025_bp.route("/api/v1/oauth-authorization-server", methods=["GET"])
def oauth_authorization_server():
    """RFC 8414 — Authorization Server Metadata.

    We are the RESOURCE server, so we do NOT run an AS. When WorkOS is
    configured we serve a courtesy copy of WorkOS's metadata (issuer == WorkOS,
    all endpoints == WorkOS) so Gemini Enterprise / Copilot / Google Cloud
    Marketplace can discover the real authorize/token/register/jwks. When WorkOS
    is unset we return 404 — the RFC-correct "there is no AS here" (previously
    this advertised dead DC Hub /oauth/* 501 stubs; that was misleading).

    DECOUPLED from MCP_OAUTH_ADVERTISE on purpose: publishing AS metadata is
    passive discovery data pointing at the real WorkOS AS; it does not itself
    trigger any login (that's the RFC 9728 authorization_servers + 401 challenge,
    which stays gated). Enterprise connectors need this discoverable even while
    the anon 401-challenge stays off."""
    meta = _workos_as_metadata(_AUTHKIT, _RESOURCE)
    if meta is None:
        return jsonify({
            "error": "no_authorization_server",
            "error_description": ("This MCP resource does not operate its own "
                                  "OAuth Authorization Server. Use the X-API-Key "
                                  "header (free/developer/pro), or contact "
                                  "api@dchub.cloud for enterprise OAuth."),
        }), 404, dict(_CORS)
    return jsonify(meta), 200, dict(_META_HEADERS)


# ── RFC 7591 — Dynamic Client Registration ───────────────────────────────────
@mcp_oauth_2025_bp.route("/oauth/register", methods=["POST", "OPTIONS"])
def oauth_register():
    """RFC 7591 DCR.

    Default posture (MCP_OAUTH_DCR_PROXY unset): DC Hub does NOT relay client
    registration. Discovery clients register at WorkOS's registration_endpoint
    directly (advertised in the AS metadata). We return a 501 that POINTS THERE
    — informative, not opaque.

    Optional posture (MCP_OAUTH_DCR_PROXY=1 AND WorkOS configured): a thin
    same-origin proxy that VALIDATES (whitelist RFC 7591 fields + safe redirect
    URIs + narrowed scopes) then forwards to WorkOS's /oauth2/register and
    returns WorkOS's response verbatim. WorkOS remains the store of record; DC
    Hub holds no client secrets. Enable ONLY if a marketplace reviewer requires
    same-origin DCR (see the residual-risk note in the module docstring)."""
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Methods": "POST, OPTIONS"})

    workos_reg = _derive_workos_endpoints(_AUTHKIT).get("registration_endpoint")

    if _DCR_PROXY and workos_reg:
        body = request.get_json(silent=True) or {}
        clean, err = _sanitize_dcr_request(body)
        if err:
            return jsonify({
                "error": "invalid_client_metadata",
                "error_description": f"DCR request rejected: {err}.",
            }), 400, dict(_CORS)
        try:
            import json as _json
            import urllib.request as _ur
            payload = _json.dumps(clean).encode("utf-8")
            _req = _ur.Request(
                workos_reg, data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "User-Agent": "dchub-oauth-dcr-proxy/1.0"})
            with _ur.urlopen(_req, timeout=8) as _resp:
                status = getattr(_resp, "status", 201)
                raw = _resp.read(65536).decode("utf-8") or "{}"
                out = _json.loads(raw)
            return jsonify(out), (status if status in (200, 201) else 502), dict(_CORS)
        except Exception as e:
            return jsonify({
                "error": "registration_proxy_failed",
                "error_description": f"{type(e).__name__}",
                "registration_endpoint": workos_reg,
            }), 502, dict(_CORS)

    # Default: point at WorkOS's native DCR (or the enterprise contact).
    body = {
        "error":             "registration_not_proxied",
        "error_description": ("Register OAuth clients directly at the WorkOS "
                              "Authorization Server's RFC 7591 endpoint."
                              if workos_reg else
                              "Dynamic Client Registration is enterprise-tier "
                              "and not self-serve yet."),
        "self_serve_alternative": {
            "method": "X-API-Key header (no OAuth needed)",
            "signup_url": "https://dchub.cloud/signup",
            "free_tier": "10 calls/day, no card",
            "developer_tier": "$49/mo, 1000 calls/day",
        },
        "enterprise_path": {"contact": "api@dchub.cloud",
                            "subject": "Enterprise OAuth DCR provisioning"},
    }
    if workos_reg:
        body["registration_endpoint"] = workos_reg
    return jsonify(body), 501, dict(_CORS)


@mcp_oauth_2025_bp.route("/oauth/token", methods=["POST", "OPTIONS"])
def oauth_token():
    """DC Hub is not a token endpoint — WorkOS AuthKit issues tokens. Point the
    caller at the real WorkOS token endpoint (or the enterprise contact)."""
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Methods": "POST, OPTIONS"})
    tok = _derive_workos_endpoints(_AUTHKIT).get("token_endpoint")
    resp = {
        "error":             "unsupported_grant_type",
        "error_description": ("DC Hub is the resource server, not the token "
                              "endpoint. Obtain tokens from the WorkOS "
                              "Authorization Server, or use X-API-Key for "
                              "free/developer/pro tiers."),
        "enterprise_contact": "api@dchub.cloud",
    }
    if tok:
        resp["token_endpoint"] = tok
    return jsonify(resp), 400, dict(_CORS)


@mcp_oauth_2025_bp.route("/oauth/introspect", methods=["POST", "OPTIONS"])
def oauth_introspect():
    """WorkOS runs introspection; DC Hub does not. Point the caller there."""
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*"})
    intr = _derive_workos_endpoints(_AUTHKIT).get("introspection_endpoint")
    resp = {"active": False, "error": "introspection_not_supported_here"}
    if intr:
        resp["introspection_endpoint"] = intr
    return jsonify(resp), 400, dict(_CORS)


# ── Phase B — token→identity/tier bridge (verified WorkOS sub → DC Hub key) ───
# The RESOURCE-SERVER half of the WorkOS OAuth connector. The MCP gateway
# (server.mjs) does the JWT crypto: it validates the WorkOS access token against
# the WorkOS JWKS (signature + issuer + audience + exp) BEFORE calling this. We
# therefore TRUST the {sub,email,iss} it sends, but ONLY from the gateway —
# gated by the same X-Internal-Key used for every other server-to-server call.
# Public callers get 403 (fail-closed).
#
# Job: map a verified OAuth identity (`sub`) → a durable mcp_dev_keys row
# (get-or-create). The api_key is DETERMINISTIC per sub (HMAC over the server
# secret) so (a) concurrent first-calls converge on one row via
# ON CONFLICT(api_key) DO NOTHING, and (b) the SAME agent identity re-resolves
# to the SAME key across sessions — the retention lever.
#
# DORMANT until the gateway flag DCHUB_WORKOS_OAUTH_ENABLED is on — nothing
# calls this otherwise. Anonymous + X-API-Key flows are completely untouched.
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

    import json as _json
    import psycopg2 as _pg
    # HMAC so the durable key is NOT guessable from the public `sub` alone.
    _secret = (os.environ.get("JWT_SECRET")
               or os.environ.get("DCHUB_SESSION_SECRET")
               or "dchub-oauth-fallback")
    api_key, developer_id = _derive_oauth_identity_key(sub, _secret)
    # tier CHECK on mcp_dev_keys = free/paid/enterprise ONLY — seed 'free'.
    metadata = {"source": "workos_oauth", "oauth_sub": sub, "oauth_iss": iss}

    # CONTACTABILITY: the gateway can't supply an email — WorkOS access tokens
    # omit the email claim and /oauth2/userinfo 401s our aud-bound token (RFC
    # 8707). Resolve it server-side via the WorkOS Management API by user_id (the
    # verified `sub`). The API key lives HERE (backend secrets), NOT on the
    # internet-facing gateway. DORMANT unless WORKOS_API_KEY is set. Any miss =>
    # email stays None (durable identity still works; row just not yet
    # contactable). Backfills the existing row via COALESCE below.
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
        # Stamp last_used_at on EVERY resolve (create OR return) so OAuth
        # retention is measurable (created_at vs last_used_at).
        cur.execute("UPDATE mcp_dev_keys SET last_used_at = NOW() WHERE api_key = %s", (api_key,))
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
        # SECURITY: clamp to the enumerated tiers — an OAuth identity can NEVER
        # resolve to more than the row actually holds (fresh subs = 'free').
        tier = _clamp_tier(row[0] if row else "free")
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
