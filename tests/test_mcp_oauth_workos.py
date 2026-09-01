"""
Tests for routes.mcp_oauth_2025_06_18 — the WorkOS-backed OAuth resource-server
layer (metadata advertisement, DCR handling, token→identity/tier bridge guards).

Design constraints (see tests/conftest.py + reference_dchub_green_main_0709):
  - NO import of main.py, NO DB, NO network. WorkOS JWKS/Management-API and
    Postgres are never touched here.
  - Pure helpers are tested directly. Route behaviour is tested against a
    minimal local Flask app that registers ONLY this blueprint (importlib.reload
    under a controlled env), so the env-gating security properties are proven
    without any live secret.
"""
import importlib
import json

import pytest
from flask import Flask

MODPATH = "routes.mcp_oauth_2025_06_18"


# ── module (re)loading under a controlled env ────────────────────────────────
_OAUTH_ENV_KEYS = (
    "WORKOS_AUTHKIT_DOMAIN", "MCP_OAUTH_ADVERTISE", "MCP_OAUTH_DCR_PROXY",
    "DCHUB_MCP_RESOURCE", "DCHUB_INTERNAL_KEY", "INTERNAL_AUTH_LEGACY_OK",
)


def _reload_with_env(monkeypatch, **env):
    """Reload the OAuth module with a clean, explicit env and return the module.
    Any oauth-relevant var not provided is removed so tests are hermetic."""
    for k in _OAUTH_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module(MODPATH)
    return importlib.reload(mod)


def _client(mod):
    app = Flask(__name__)
    app.register_blueprint(mod.mcp_oauth_2025_bp)
    return app.test_client()


# ══════════════════════════════════════════════════════════════════════════
# Pure helpers
# ══════════════════════════════════════════════════════════════════════════
def _mod():
    return importlib.import_module(MODPATH)


def test_clamp_tier_downgrades_unknown():
    m = _mod()
    assert m._clamp_tier("admin") == "free"          # never escalate
    assert m._clamp_tier("root") == "free"
    assert m._clamp_tier("") == "free"
    assert m._clamp_tier(None) == "free"
    assert m._clamp_tier("free") == "free"
    assert m._clamp_tier("paid") == "paid"
    assert m._clamp_tier("ENTERPRISE") == "enterprise"   # case-insensitive
    assert m._clamp_tier("  Paid  ") == "paid"


def test_derive_key_is_deterministic_and_secret_bound():
    m = _mod()
    a1 = m._derive_oauth_identity_key("user_01ABC", "secret-A")
    a2 = m._derive_oauth_identity_key("user_01ABC", "secret-A")
    assert a1 == a2                                    # stable => retention
    b = m._derive_oauth_identity_key("user_01ABC", "secret-B")
    assert a1 != b                                     # secret-bound
    c = m._derive_oauth_identity_key("user_02XYZ", "secret-A")
    assert a1 != c                                     # per-identity


def test_derive_key_format_and_non_guessable():
    m = _mod()
    api_key, dev_id = m._derive_oauth_identity_key("user_01ABC", "s3kr3t")
    assert api_key.startswith("dch_oauth_") and len(api_key) == 42
    assert dev_id.startswith("dev_oauth_")
    # the public sub must not leak into the key material
    assert "user_01ABC" not in api_key
    # accepts bytes secret too (server passes str; be robust)
    assert m._derive_oauth_identity_key("user_01ABC", b"s3kr3t")[0] == api_key


def test_derive_key_empty_sub_fails_closed():
    m = _mod()
    with pytest.raises(ValueError):
        m._derive_oauth_identity_key("", "secret")
    with pytest.raises(ValueError):
        m._derive_oauth_identity_key(None, "secret")


def test_derive_workos_endpoints():
    m = _mod()
    assert m._derive_workos_endpoints("") == {}
    assert m._derive_workos_endpoints(None) == {}
    ep = m._derive_workos_endpoints("https://x.authkit.app/")   # trailing slash
    assert ep["issuer"] == "https://x.authkit.app"
    assert ep["authorization_endpoint"] == "https://x.authkit.app/oauth2/authorize"
    assert ep["token_endpoint"] == "https://x.authkit.app/oauth2/token"
    assert ep["registration_endpoint"] == "https://x.authkit.app/oauth2/register"
    assert ep["jwks_uri"] == "https://x.authkit.app/oauth2/jwks"


def test_as_metadata_points_at_workos_not_dchub():
    m = _mod()
    assert m._workos_as_metadata("", "https://dchub.cloud/mcp") is None
    meta = m._workos_as_metadata("https://x.authkit.app", "https://dchub.cloud/mcp")
    # issuer is the REAL token issuer (WorkOS) — DC Hub never signs tokens
    assert meta["issuer"] == "https://x.authkit.app"
    assert "dchub" not in meta["issuer"]
    # RFC 7591 DCR endpoint advertised (Google Marketplace requirement)
    assert meta["registration_endpoint"] == "https://x.authkit.app/oauth2/register"
    # confidential-client auth methods present (Gemini Enterprise needs a secret)
    assert "client_secret_post" in meta["token_endpoint_auth_methods_supported"]
    assert "client_secret_basic" in meta["token_endpoint_auth_methods_supported"]
    # PKCE + OIDC scopes only (custom read:* would trigger invalid_scope)
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["scopes_supported"] == ["openid", "profile", "email", "offline_access"]
    assert meta["resource"] == "https://dchub.cloud/mcp"


def test_dcr_sanitizer_whitelists_and_validates():
    m = _mod()
    clean, err = m._sanitize_dcr_request({
        "redirect_uris": ["https://app.example/cb"],
        "client_name": "Example",
        "token_endpoint_auth_method": "client_secret_post",
        "evil": "DROP TABLE users",           # junk field dropped
        "__proto__": {"x": 1},                # junk field dropped
    })
    assert err is None
    assert "evil" not in clean and "__proto__" not in clean
    assert clean["client_name"] == "Example"
    # redirect_uris required
    assert m._sanitize_dcr_request({"client_name": "x"})[1] == "redirect_uris_required"
    assert m._sanitize_dcr_request({"redirect_uris": []})[1] == "redirect_uris_required"
    # unsafe redirect URIs rejected
    assert m._sanitize_dcr_request(
        {"redirect_uris": ["javascript:alert(1)"]})[1] == "invalid_redirect_uri"
    assert m._sanitize_dcr_request(
        {"redirect_uris": ["http://evil.example/cb"]})[1] == "invalid_redirect_uri"
    # cap
    assert m._sanitize_dcr_request(
        {"redirect_uris": ["https://a/%d" % i for i in range(20)]})[1] == "too_many_redirect_uris"
    # scope narrowed to OIDC set (custom scopes dropped)
    clean2, err2 = m._sanitize_dcr_request({
        "redirect_uris": ["https://a.example/cb"],
        "scope": "openid email read:facilities admin:tenant",
    })
    assert err2 is None
    assert set(clean2["scope"].split()) == {"openid", "email"}
    # non-dict body
    assert m._sanitize_dcr_request("nope")[1] == "invalid_client_metadata"


def test_redirect_uri_safety():
    m = _mod()
    assert m._is_safe_redirect_uri("https://app.example/callback")
    assert m._is_safe_redirect_uri("http://localhost:8080/cb")
    assert m._is_safe_redirect_uri("http://127.0.0.1/cb")
    assert not m._is_safe_redirect_uri("http://evil.example/cb")
    assert not m._is_safe_redirect_uri("javascript:alert(1)")
    assert not m._is_safe_redirect_uri("data:text/html,x")
    assert not m._is_safe_redirect_uri("https://app.example/cb#frag")   # no fragment
    assert not m._is_safe_redirect_uri("https:///no-host")
    assert not m._is_safe_redirect_uri(123)


# ══════════════════════════════════════════════════════════════════════════
# Route behaviour — UNCONFIGURED (no WorkOS): today's anon/X-API-Key posture
# ══════════════════════════════════════════════════════════════════════════
def test_unconfigured_protected_resource_no_auth(monkeypatch):
    mod = _reload_with_env(monkeypatch)   # no WORKOS domain
    c = _client(mod)
    r = c.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.get_json()
    # empty AS list == "no auth required" — X-API-Key path unchanged
    assert body["authorization_servers"] == []
    assert body["resource"] == "https://dchub.cloud/mcp"


def test_unconfigured_authorization_server_404(monkeypatch):
    mod = _reload_with_env(monkeypatch)
    c = _client(mod)
    r = c.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 404
    assert r.get_json()["error"] == "no_authorization_server"


def test_unconfigured_register_501_points_to_contact(monkeypatch):
    mod = _reload_with_env(monkeypatch)
    c = _client(mod)
    r = c.post("/oauth/register", json={"redirect_uris": ["https://a/cb"]})
    assert r.status_code == 501
    body = r.get_json()
    assert body["error"] == "registration_not_proxied"
    assert "registration_endpoint" not in body     # no workos configured


def test_token_and_introspect_are_not_here(monkeypatch):
    mod = _reload_with_env(monkeypatch)
    c = _client(mod)
    assert c.post("/oauth/token", json={}).status_code == 400
    assert c.post("/oauth/introspect", json={}).status_code == 400


# ══════════════════════════════════════════════════════════════════════════
# Route behaviour — CONFIGURED (WorkOS domain + advertise on)
# ══════════════════════════════════════════════════════════════════════════
# Deliberately NOT the production domain. Feeding the real value here would
# let this test pass even if the module ignored WORKOS_AUTHKIT_DOMAIN and
# fell back to its default -- the assertion would match either way. A
# synthetic value makes "the env was honoured" the only way to pass.
DOMAIN = "https://authkit-fixture.example.com"


def test_configured_advertises_workos_as(monkeypatch):
    mod = _reload_with_env(monkeypatch,
                           WORKOS_AUTHKIT_DOMAIN=DOMAIN,
                           MCP_OAUTH_ADVERTISE="1")
    c = _client(mod)
    # protected-resource now lists WorkOS as the AS
    pr = c.get("/.well-known/oauth-protected-resource").get_json()
    assert pr["authorization_servers"] == [DOMAIN]
    # authorization-server metadata now served, issuer == WorkOS
    as_ = c.get("/.well-known/oauth-authorization-server")
    assert as_.status_code == 200
    meta = as_.get_json()
    assert meta["issuer"] == DOMAIN
    assert meta["registration_endpoint"] == f"{DOMAIN}/oauth2/register"
    assert meta["token_endpoint"] == f"{DOMAIN}/oauth2/token"


def test_advertise_flag_decoupled_from_as_metadata(monkeypatch):
    """Domain set but MCP_OAUTH_ADVERTISE OFF: the RFC 9728 authorization_servers
    stays EMPTY (anon self-serve unaffected) BUT the RFC 8414 AS metadata is
    still served for enterprise/marketplace discovery. Proves the deliberate
    decoupling decision."""
    mod = _reload_with_env(monkeypatch, WORKOS_AUTHKIT_DOMAIN=DOMAIN)  # advertise off
    c = _client(mod)
    pr = c.get("/.well-known/oauth-protected-resource").get_json()
    assert pr["authorization_servers"] == []          # anon path preserved
    assert c.get("/.well-known/oauth-authorization-server").status_code == 200


def test_configured_register_points_to_workos_dcr(monkeypatch):
    """Proxy OFF (default): /oauth/register 501s but hands back the real WorkOS
    RFC 7591 registration_endpoint so discovery clients go straight there."""
    mod = _reload_with_env(monkeypatch, WORKOS_AUTHKIT_DOMAIN=DOMAIN)
    c = _client(mod)
    r = c.post("/oauth/register", json={"redirect_uris": ["https://a/cb"]})
    assert r.status_code == 501
    assert r.get_json()["registration_endpoint"] == f"{DOMAIN}/oauth2/register"


def test_configured_token_points_to_workos(monkeypatch):
    mod = _reload_with_env(monkeypatch, WORKOS_AUTHKIT_DOMAIN=DOMAIN)
    c = _client(mod)
    r = c.post("/oauth/token", json={})
    assert r.status_code == 400
    assert r.get_json()["token_endpoint"] == f"{DOMAIN}/oauth2/token"


# ══════════════════════════════════════════════════════════════════════════
# Identity bridge — fail-closed guards (no DB required: both return early)
# ══════════════════════════════════════════════════════════════════════════
def test_identity_requires_internal_key(monkeypatch):
    mod = _reload_with_env(monkeypatch, DCHUB_INTERNAL_KEY="unit-test-secret")
    c = _client(mod)
    # no X-Internal-Key => 403, public callers can never reach the bridge
    r = c.post("/api/v1/oauth/identity", json={"sub": "user_1"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "internal_only"
    # wrong key => 403
    r2 = c.post("/api/v1/oauth/identity",
                json={"sub": "user_1"},
                headers={"X-Internal-Key": "wrong"})
    assert r2.status_code == 403


def test_identity_valid_key_missing_sub_400(monkeypatch):
    """With a valid internal key but no sub, returns 400 BEFORE any DB touch —
    exercises the auth guard + input validation without a database."""
    mod = _reload_with_env(monkeypatch, DCHUB_INTERNAL_KEY="unit-test-secret")
    c = _client(mod)
    r = c.post("/api/v1/oauth/identity",
               json={"iss": DOMAIN},                    # sub omitted
               headers={"X-Internal-Key": "unit-test-secret"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "sub_required"


def test_metadata_json_is_serializable(monkeypatch):
    """Every metadata doc must be strict-JSON serializable (no datetime leak)."""
    mod = _reload_with_env(monkeypatch, WORKOS_AUTHKIT_DOMAIN=DOMAIN,
                           MCP_OAUTH_ADVERTISE="1")
    c = _client(mod)
    for path in ("/.well-known/oauth-protected-resource",
                 "/.well-known/oauth-authorization-server"):
        json.dumps(c.get(path).get_json())   # raises if not serializable
