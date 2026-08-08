"""
tests/test_customer_portal.py — Admin CUSTOMER PORTAL (READ-ONLY).

The portal is the ONE browser-openable surface that reconciles billing
(users.plan) against the MCP paywall (mcp_dev_keys.tier) and classifies each
account into one of the four outreach segments. These tests STUB the DB roster
(monkeypatched — nothing touches Postgres) and cover:

  · _classify_segment applies the four verified predicates correctly;
  · _key_tail redacts a full api_key to a non-reversible short tail;
  · build_roster returns {ok, counts_by_segment, accounts} and never raises;
  · /api/v1/portal/accounts is ADMIN-GATED (403 without the key) and never
    builds the roster (no PII) for a non-admin;
  · /api/v1/portal/accounts returns segmented data with a stubbed roster;
  · the page returns 200 HTML for an admin (+ Set-Cookie remembers the key) and
    403 HTML for anon;
  · the admin gate accepts the ?admin_key= query param (browser-openable);
  · the register helper actually wires both routes onto a real app (the
    false-DONE guard — module + tests can pass while the blueprint never
    registers and every endpoint 404s in prod).
"""
import pytest

cp = pytest.importorskip("routes.customer_portal")


# ════════════════════════════════════════════════════════════════════
#  _key_tail — redact the api_key to a short tail (full key never leaves DB)
# ════════════════════════════════════════════════════════════════════
def test_key_tail_redacts_to_tail():
    assert cp._key_tail("dchub_live_ABCD1234") == "…1234"
    assert cp._key_tail("") == ""
    assert cp._key_tail(None) == ""
    # short keys still get masked (never echo the whole thing)
    assert cp._key_tail("ab") == "…ab"
    # the full key is NEVER present in the tail
    full = "sk-supersecret-0000-9999"  # secretscan:allow — synthetic fixture
    assert full not in cp._key_tail(full)


# ════════════════════════════════════════════════════════════════════
#  _classify_segment — the four verified predicates
# ════════════════════════════════════════════════════════════════════
def test_classify_keyless_payer():
    """Real invoice payer on a paid plan with NO paid key = the activation gap."""
    seg = cp._classify_segment({
        "plan": "pro", "real_payer": True, "has_paid_key": False,
        "opted_in": False, "calls_30d": 0, "calls_7d": 0})
    assert seg == "keyless_payer"


def test_classify_at_risk_paid():
    """Real payer WITH a paid key but zero 7d usage = customer-success check-in."""
    seg = cp._classify_segment({
        "plan": "founding", "real_payer": True, "has_paid_key": True,
        "opted_in": False, "calls_30d": 12, "calls_7d": 0})
    assert seg == "at_risk_paid"
    # …and a healthy real payer (recent calls) is NOT at_risk.
    healthy = cp._classify_segment({
        "plan": "founding", "real_payer": True, "has_paid_key": True,
        "opted_in": False, "calls_30d": 40, "calls_7d": 9})
    assert healthy == ""


def test_classify_comped_paid():
    """Paid plan with ZERO real invoices = founding/dev comp (marketing)."""
    seg = cp._classify_segment({
        "plan": "enterprise", "real_payer": False, "has_paid_key": True,
        "opted_in": False, "calls_30d": 5, "calls_7d": 1})
    assert seg == "comped_paid"


def test_classify_high_usage_free_requires_optin():
    """Free + heavy usage qualifies ONLY when marketing opt-in is true."""
    base = {"plan": "free", "real_payer": False, "has_paid_key": False,
            "calls_30d": cp._HIGH_USAGE_30D_THRESHOLD + 1, "calls_7d": 3}
    assert cp._classify_segment({**base, "opted_in": True}) == "high_usage_free"
    # NOT opted in -> NOT in the marketing segment (consent gate).
    assert cp._classify_segment({**base, "opted_in": False}) == ""
    # Opted in but below the usage threshold -> not high-usage.
    low = {**base, "opted_in": True, "calls_30d": 1}
    assert cp._classify_segment(low) == ""


def test_classify_unsegmented_returns_empty():
    """A plain free account with no usage / no opt-in matches no segment."""
    assert cp._classify_segment({
        "plan": "free", "real_payer": False, "has_paid_key": False,
        "opted_in": False, "calls_30d": 0, "calls_7d": 0}) == ""


# ════════════════════════════════════════════════════════════════════
#  build_roster — best-effort payload shape
# ════════════════════════════════════════════════════════════════════
_STUB_ROSTER = {
    "accounts": [
        {"email": "payer@acme.com", "name": "Acme", "plan": "pro",
         "real_payer": True, "has_paid_key": False, "opted_in": False,
         "key_tier": None, "key_tail": "", "calls_30d": 20, "calls_7d": 4,
         "last_active": "2026-06-18T00:00:00", "subscription_status": "active",
         "created_at": "2026-01-01", "last_login": "", "mrr_usd": 49.0,
         "segment": "keyless_payer"},
        {"email": "comp@lab.io", "name": "Lab", "plan": "founding",
         "real_payer": False, "has_paid_key": True, "opted_in": False,
         "key_tier": "paid", "key_tail": "…9f2a", "calls_30d": 3, "calls_7d": 0,
         "last_active": None, "subscription_status": "active",
         "created_at": "2026-02-02", "last_login": "", "mrr_usd": 0.0,
         "segment": "comped_paid"},
    ],
    "counts_by_segment": {"keyless_payer": 1, "comped_paid": 1},
}


def test_build_roster_shape(monkeypatch):
    monkeypatch.setattr(cp, "_gather_accounts", lambda limit=500: _STUB_ROSTER)
    d = cp.build_roster()
    assert d["ok"] is True
    assert "generated_at" in d
    assert d["counts_by_segment"] == {"keyless_payer": 1, "comped_paid": 1}
    assert len(d["accounts"]) == 2
    assert d["accounts"][0]["segment"] == "keyless_payer"
    # the threshold is surfaced so the operator knows the high_usage_free cutoff
    assert d["high_usage_30d_threshold"] == cp._HIGH_USAGE_30D_THRESHOLD


def test_gather_accounts_empty_when_no_db(monkeypatch):
    """With no DB connection the roster is empty, never a crash."""
    monkeypatch.setattr(cp, "_conn", lambda: None)
    out = cp._gather_accounts()
    assert out == {"accounts": [], "counts_by_segment": {}}
    # …and build_roster still produces a clean, zero-count payload.
    d = cp.build_roster()
    assert d["ok"] is True
    assert d["accounts"] == []
    assert d["counts_by_segment"] == {}


# ════════════════════════════════════════════════════════════════════
#  Endpoints (Flask test client) — admin-gated, READ-ONLY
# ════════════════════════════════════════════════════════════════════
@pytest.fixture()
def client():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(cp.customer_portal_bp)
    return app.test_client()


def test_accounts_returns_segmented_data_for_admin(client, monkeypatch):
    monkeypatch.setattr(cp, "_admin_ok", lambda: True)
    monkeypatch.setattr(cp, "_gather_accounts", lambda limit=500: _STUB_ROSTER)
    resp = client.get("/api/v1/portal/accounts")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["counts_by_segment"]["keyless_payer"] == 1
    assert {a["segment"] for a in data["accounts"]} == {"keyless_payer", "comped_paid"}
    # PII is present (admin context) but the full api_key is NOT in the payload.
    assert "_api_key" not in data["accounts"][0]


def test_accounts_is_admin_gated_403_anon(client, monkeypatch):
    """403 for a non-admin — and the roster must NEVER be built (no PII leak)."""
    monkeypatch.setattr(cp, "_admin_ok", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("roster must not be built for a non-admin")
    monkeypatch.setattr(cp, "build_roster", _boom)
    resp = client.get("/api/v1/portal/accounts")
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_page_returns_200_html_for_admin_with_cookie(client, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("INTERNAL_KEY", raising=False)
    resp = client.get("/api/v1/portal?admin_key=sekret")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    # The self-contained page renders the table + the four segment filter chips
    # and fetches the segmented JSON endpoint.
    assert "/api/v1/portal/accounts" in body
    assert "keyless_payer" in body
    assert "comped_paid" in body
    assert "high_usage_free" in body
    assert "at_risk_paid" in body
    assert 'name="robots" content="noindex,nofollow"' in body
    # …and it Set-Cookie's the admin key so the operator pastes it once.
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "dchub_innov_key=sekret" in set_cookie


def test_page_is_admin_gated_403_anon(client, monkeypatch):
    monkeypatch.setattr(cp, "_admin_ok", lambda: False)
    resp = client.get("/api/v1/portal")
    assert resp.status_code == 403
    assert resp.mimetype == "text/html"
    # …and no admin key is leaked into a Set-Cookie on the rejection.
    assert "dchub_innov_key" not in resp.headers.get("Set-Cookie", "")


def test_admin_gate_accepts_query_param_key(client, monkeypatch):
    """The browser-openable auth: ?admin_key= must satisfy the gate (mirrors the
    brain-dashboard pattern)."""
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("INTERNAL_KEY", raising=False)
    monkeypatch.setattr(cp, "build_roster", lambda limit=500: {"ok": True, "accounts": []})
    ok = client.get("/api/v1/portal/accounts?admin_key=sekret")
    assert ok.status_code == 200
    bad = client.get("/api/v1/portal/accounts?admin_key=wrong")
    assert bad.status_code == 403
    none = client.get("/api/v1/portal/accounts")
    assert none.status_code == 403


def test_responses_are_no_store(client, monkeypatch):
    """Account PII must never be edge-cached: every response is no-store/private."""
    monkeypatch.setattr(cp, "_admin_ok", lambda: True)
    monkeypatch.setattr(cp, "build_roster", lambda limit=500: {"ok": True, "accounts": []})
    resp = client.get("/api/v1/portal/accounts")
    assert "no-store" in resp.headers.get("Cache-Control", "")


# ════════════════════════════════════════════════════════════════════
#  Wiring guard — the false-DONE check
# ════════════════════════════════════════════════════════════════════
def test_register_helper_wires_routes_onto_a_real_app():
    """Guard against the false-DONE where the module + its tests pass but the app
    never registers the blueprint (every endpoint 404s in prod). The register
    helper must put BOTH the JSON + page routes in a real app's URL map."""
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    cp.register_customer_portal(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/portal/accounts" in rules
    assert "/api/v1/portal" in rules
    # Served under /api/ so the CF worker forwards it (bare subpaths hit CF Error-1000).
    assert all(r.startswith("/api/") for r in rules
               if r in ("/api/v1/portal", "/api/v1/portal/accounts"))
