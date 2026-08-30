"""The platform's own prewarm is not a free-demo user (2026-08-30).

/api/v1/dcpi/ask is served by routes/dcpi.py::dcpi_ask (NOT routes/dcpi_ask.py,
which registers the same rule on a blueprint main.py registers later and is
therefore shadowed dead code). The live handler delegates to routes.demo's
per-IP daily quota, PER_IP_DAILY, default 5.

cron_heartbeat._DISPATCH fires six dcpi_chat_prewarm_* jobs at minute%30 in
18..23 — 288 calls/day from ONE IP against a quota of 5. Worse, the CACHED
branch bumped the counter too, so a warm hit spent quota while re-warming a
cold entry needs the MISS path the exhausted quota blocks: a cache-warmer that
could not warm a cold cache.

These tests pin (a) that authenticated internal automation is exempt and does
not bump, (b) that the cached path does not bump for internal callers, and
★ (c) that an ANONYMOUS caller is still limited exactly as before — the
exemption must not become a hole in a keyless public endpoint.

No network, no DB.
Run with:  python3 -m pytest tests/test_dcpi_ask_prewarm_quota.py -v
"""
import pytest

import routes.dcpi as dcpi


@pytest.fixture
def app():
    from flask import Flask
    a = Flask(__name__)
    a.register_blueprint(dcpi.dcpi_bp)
    a.config["TESTING"] = True
    return a


@pytest.fixture(autouse=True)
def _stub_demo(monkeypatch):
    """Stub routes.demo so the handler's `from routes.demo import ...` gets
    fakes. Records every rate-limit bump so a test can assert on zero."""
    import sys, types
    bumps = []
    mod = types.ModuleType("routes.demo")
    mod._ensure_schema = lambda: None
    mod._is_dc_question = lambda q: True
    mod._hash_q = lambda q: "qh"
    mod._cached = lambda qh: None
    mod._cache_set = lambda *a, **k: None
    mod._client_ip = lambda: "127.0.0.1"
    mod.PER_IP_DAILY = 5
    mod._call_claude_with_tools = lambda q: ("an answer", [])

    def _bump(ip):
        bumps.append(ip)
        return (99, False)          # already over quota
    mod._check_and_bump_rate = _bump
    monkeypatch.setitem(sys.modules, "routes.demo", mod)
    mod.bumps = bumps
    return mod


def _get(app, headers=None):
    with app.test_client() as c:
        return c.get("/api/v1/dcpi/ask?q=what+is+the+DCPI+for+Cheyenne",
                     headers=headers or {})


# ── (c) the public gate must NOT regress ──────────────────────────────
def test_an_anonymous_caller_is_still_rate_limited(app, _stub_demo, monkeypatch):
    """The exemption must not open a hole in a keyless public endpoint."""
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    r = _get(app)
    assert r.status_code == 429
    assert r.get_json()["error"] == "rate_limited"
    assert _stub_demo.bumps == ["127.0.0.1"]


def test_a_bare_header_does_not_buy_an_exemption(app, _stub_demo, monkeypatch):
    """Fail-CLOSED: the header is validated against an env secret, so an
    attacker who merely SENDS X-Internal-Key is still a demo user."""
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "the-real-secret")
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    r = _get(app, {"X-Internal-Key": "not-the-secret"})
    assert r.status_code == 429
    assert _stub_demo.bumps == ["127.0.0.1"]


def test_no_configured_secret_means_no_exemption(app, _stub_demo, monkeypatch):
    """With nothing configured the gate fails closed and the quota applies
    exactly as it did before this change."""
    for v in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
              "INTERNAL_WORKER_SECRET", "DCHUB_ADMIN_KEY"):
        monkeypatch.delenv(v, raising=False)
    r = _get(app, {"X-Internal-Key": "anything"})
    assert r.status_code == 429


# ── (a)(b) the prewarm is exempt, and does not spend quota ────────────
@pytest.mark.parametrize("hdr", ["X-Internal-Key", "X-Admin-Key"])
def test_authenticated_internal_automation_is_exempt(app, _stub_demo, monkeypatch, hdr):
    """cron_heartbeat._hit sends BOTH headers; either must exempt."""
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "s3cret-value-here")
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "s3cret-value-here")
    r = _get(app, {hdr: "s3cret-value-here"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    body = r.get_json()
    assert body["ok"] is True
    assert body["rate_limit"] == {"exempt": "internal"}
    assert _stub_demo.bumps == [], "internal automation must not spend the public quota"


def test_a_cached_hit_does_not_spend_quota_for_internal(app, _stub_demo, monkeypatch):
    """★ The self-defeating half. The cached branch bumped unconditionally, so
    a warm hit burned quota and the cache could never be re-warmed once the
    day's 5 were gone."""
    _stub_demo._cached = lambda qh: {"answer": "warm", "tool_calls": []}
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "s3cret-value-here")
    r = _get(app, {"X-Internal-Key": "s3cret-value-here"})
    assert r.status_code == 200
    assert r.get_json()["cached"] is True
    assert _stub_demo.bumps == []


def test_a_cached_hit_still_spends_quota_for_anonymous(app, _stub_demo, monkeypatch):
    """Do not over-correct: the public cached path must keep counting."""
    _stub_demo._cached = lambda qh: {"answer": "warm", "tool_calls": []}
    for v in ("DCHUB_INTERNAL_KEY", "DCHUB_SYNC_KEY",
              "INTERNAL_WORKER_SECRET", "DCHUB_ADMIN_KEY"):
        monkeypatch.delenv(v, raising=False)
    r = _get(app)
    assert r.status_code == 200
    assert _stub_demo.bumps == ["127.0.0.1"]
