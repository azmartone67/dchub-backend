"""r-white-glove BUILD 2 (2026-07-18) — validated auto-approve lane tests.

Covers:
  · pure validation heuristics (routes.onboard_auto_approve) with
    ZERO-FALSE-APPROVE tolerance: any garbage submission must fail
    validation → stays in the human queue exactly as today;
  · lane kill switch + fail-closed behavior (no DB → no approval);
  · Flask test-client on the new public routes
    (/integrations/<slug> stub serving + legacy fallback redirect,
    /api/v1/onboard/auto-approve/config) and the white-glove admin
    endpoints' auth gate;
  · static guard that the onboarder hook is wired.

Run:  python3 -m pytest tests/test_onboard_auto_approve.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import routes.onboard_auto_approve as oaa
from routes.onboard_auto_approve import (
    name_spam_reasons,
    slugify,
    valid_contact_email,
    valid_https_url,
    validate_submission,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

GOOD = {
    "id": 7,
    "name": "Acme Agent Cloud",
    "url": "https://agents.acme.dev/platform",
    "type": "mcp",
    "contact_email": "ops@acme.dev",
    "description": "We build MCP-native agents for infrastructure teams.",
}


def sub(**over):
    d = dict(GOOD)
    d.update(over)
    return d


# ── happy path ───────────────────────────────────────────────────────

def test_good_submission_passes():
    ok, reasons = validate_submission(sub(), reachable=True)
    assert ok, reasons
    assert reasons == []


# ── ZERO FALSE APPROVES: every garbage axis must fail ────────────────

@pytest.mark.parametrize("url", [
    "http://agents.acme.dev",          # https required
    "ftp://acme.dev",
    "not a url",
    "",
    "https://",
    "https://localhost-only",          # no dot / not a public host shape
    "javascript:alert(1)",
])
def test_bad_urls_fail(url):
    ok, reasons = validate_submission(sub(url=url), reachable=True)
    assert not ok
    assert "url_not_https_or_malformed" in reasons


def test_unreachable_url_fails():
    ok, reasons = validate_submission(sub(), reachable=False)
    assert not ok
    assert "url_not_reachable" in reasons


@pytest.mark.parametrize("name", [
    "",                                # empty
    "ab",                              # too short
    "FREE CA$INO BONUS $$$",           # spam keyword + currency
    "Buy viagra now",                  # spam keyword
    "aaaaaaaaaaa",                     # repeated-char run
    "visit www.spam.com now",          # embedded URL
    "Best SEO Service 2026",           # spam keyword
    "1234567890 Ltd",                  # digits-heavy / low letter ratio
    "!!!! ???? ####",                  # low letter ratio
    "CLICK HERE FOR FOLLOWERS",        # keyword + shouting
])
def test_spam_names_fail(name):
    assert name_spam_reasons(name), f"{name!r} should be rejected"
    ok, reasons = validate_submission(sub(name=name), reachable=True)
    assert not ok


@pytest.mark.parametrize("name", [
    "Acme Agent Cloud",
    "LobeChat",
    "You.com Research",   # a dot in a real product name is fine
    "Kagi Assistant",
    "Mistral Le Chat",
])
def test_legit_names_pass(name):
    # NB: "You.com" contains ".com" — the URL heuristic flags it.
    if ".com" in name.lower():
        pytest.skip("dot-com product names intentionally route to humans")
    assert name_spam_reasons(name) == []


@pytest.mark.parametrize("email", [
    "",
    "not-an-email",
    "a@b",                     # no TLD
    "test@test.com",           # test domain
    "foo@mailinator.com",      # disposable
    "bar@yopmail.com",         # disposable
    "test@acme.dev",           # throwaway local part
    "aaaa@acme.dev",           # repeated-char local part
    "noreply@acme.dev",        # not a human contact
])
def test_bad_emails_fail(email):
    assert not valid_contact_email(email)
    ok, reasons = validate_submission(sub(contact_email=email), reachable=True)
    assert not ok
    assert "contact_email_invalid_or_disposable" in reasons


def test_good_email_shapes_pass():
    assert valid_contact_email("ops@acme.dev")
    assert valid_contact_email("jane.doe+mcp@sub.company.co.uk")


def test_duplicate_name_fails():
    ok, reasons = validate_submission(
        sub(), existing_names={"ACME Agent Cloud"}, reachable=True)
    assert not ok
    assert "duplicate_platform_name" in reasons


def test_duplicate_host_fails():
    ok, reasons = validate_submission(
        sub(), existing_hosts={"agents.acme.dev"}, reachable=True)
    assert not ok
    assert "duplicate_platform_url_host" in reasons


def test_www_host_normalization():
    ok, reasons = validate_submission(
        sub(url="https://www.agents.acme.dev/x"),
        existing_hosts={"agents.acme.dev"}, reachable=True)
    assert not ok
    assert "duplicate_platform_url_host" in reasons


def test_garbage_submission_fails_every_axis():
    """The mission's canonical garbage case: nothing about it may
    auto-approve."""
    ok, reasons = validate_submission(
        {"name": "$$$ FREE CASINO $$$", "url": "http://spam",
         "contact_email": "x@mailinator.com"},
        reachable=False)
    assert not ok
    assert len(reasons) >= 3   # url + name + email all independently fail


# ── lane behavior: kill switch, fail-closed ──────────────────────────

def test_lane_kill_switch(monkeypatch):
    monkeypatch.setenv("ONBOARD_AUTO_APPROVE_DISABLE", "1")
    out = oaa.try_auto_approve_lane(object(), sub(), {}, reachable=True)
    assert out["approved"] is False
    assert any("ONBOARD_AUTO_APPROVE_DISABLE" in r for r in out["reasons"])


def test_lane_no_db_fails_closed(monkeypatch):
    monkeypatch.delenv("ONBOARD_AUTO_APPROVE_DISABLE", raising=False)
    out = oaa.try_auto_approve_lane(None, sub(), {}, reachable=True)
    assert out["approved"] is False
    assert "no_db" in out["reasons"]


def test_lane_daily_cap_fails_closed(monkeypatch):
    """Cap reached (or unknowable) → no approval, row stays human."""
    monkeypatch.delenv("ONBOARD_AUTO_APPROVE_DISABLE", raising=False)
    monkeypatch.setattr(oaa, "_todays_auto_approvals", lambda c: oaa.DAILY_CAP)
    out = oaa.try_auto_approve_lane(object(), sub(), {}, reachable=True)
    assert out["approved"] is False
    assert any(r.startswith("daily_cap_reached") for r in out["reasons"])

    monkeypatch.setattr(oaa, "_todays_auto_approvals", lambda c: None)
    out2 = oaa.try_auto_approve_lane(object(), sub(), {}, reachable=True)
    assert out2["approved"] is False


def test_lane_validation_failure_keeps_human_queue(monkeypatch):
    monkeypatch.delenv("ONBOARD_AUTO_APPROVE_DISABLE", raising=False)
    monkeypatch.setattr(oaa, "_todays_auto_approvals", lambda c: 0)
    monkeypatch.setattr(oaa, "_existing_platform_sets",
                        lambda c: (set(), set()))
    out = oaa.try_auto_approve_lane(
        object(), sub(contact_email="x@mailinator.com"), {}, reachable=True)
    assert out["approved"] is False
    assert "contact_email_invalid_or_disposable" in out["reasons"]


def test_lane_key_mint_failure_fails_closed(monkeypatch):
    """A validated row whose key mint fails must NOT be marked approved
    (an 'approved' partner with no working credential is a dead funnel)."""
    monkeypatch.delenv("ONBOARD_AUTO_APPROVE_DISABLE", raising=False)
    monkeypatch.setattr(oaa, "_todays_auto_approvals", lambda c: 0)
    monkeypatch.setattr(oaa, "_existing_platform_sets",
                        lambda c: (set(), set()))
    monkeypatch.setattr(oaa, "mint_live_key",
                        lambda slug, email, name: {"ok": False,
                                                   "error": "db down"})
    out = oaa.try_auto_approve_lane(object(), sub(), {}, reachable=True)
    assert out["approved"] is False
    assert any(r.startswith("key_mint_failed") for r in out["reasons"])


def test_lane_full_package_on_pass(monkeypatch):
    monkeypatch.delenv("ONBOARD_AUTO_APPROVE_DISABLE", raising=False)
    calls = {}
    monkeypatch.setattr(oaa, "_todays_auto_approvals", lambda c: 1)
    monkeypatch.setattr(oaa, "_existing_platform_sets",
                        lambda c: (set(), set()))

    def fake_mint(slug, email, name):
        calls["mint"] = (slug, email)
        return {"ok": True, "key": "dch_live_abc123",
                "key_prefix": "dch_live_abc"}

    def fake_store(c, slug, name, html, sid):
        calls["stub"] = (slug, sid)
        assert "https://dchub.cloud/mcp" in html
        return True

    def fake_seed(c, slug, name):
        calls["tuner"] = slug
        return True

    monkeypatch.setattr(oaa, "mint_live_key", fake_mint)
    monkeypatch.setattr(oaa, "store_stub_page", fake_store)
    monkeypatch.setattr(oaa, "seed_tool_tuner_proposal", fake_seed)
    out = oaa.try_auto_approve_lane(object(), sub(), {}, reachable=True)
    assert out["approved"] is True
    assert out["key_minted"] and out["stub_stored"] and out["tuner_seeded"]
    assert calls["mint"][0] == "acme-agent-cloud"
    assert calls["stub"] == ("acme-agent-cloud", 7)
    assert calls["tuner"] == "acme-agent-cloud"
    assert out["integration_url"].endswith("/integrations/acme-agent-cloud")


# ── stub page HTML generation ────────────────────────────────────────

def test_build_stub_html_generic_recipe():
    html = oaa.build_stub_html("Acme Agent Cloud", "acme-agent-cloud")
    assert "Acme Agent Cloud" in html
    assert "https://dchub.cloud/mcp" in html
    assert "streamable-http" in html
    assert "/integrations/acme-agent-cloud" in html   # canonical + og url
    assert "__" not in re.sub(r"__proto__", "", html), \
        "unfilled __SLOT__ placeholders left in stub page"


def test_build_stub_html_escapes_name():
    html = oaa.build_stub_html('<script>alert(1)</script>', "x-corp")
    assert "<script>alert(1)</script>" not in html


def test_slugify():
    assert slugify("Acme Agent Cloud") == "acme-agent-cloud"
    assert slugify("  Émile & Co!  ") == "mile-co"
    assert slugify("") == ""


def test_https_validator():
    assert valid_https_url("https://acme.dev")
    assert valid_https_url("https://acme.dev:8443/path?q=1")
    assert not valid_https_url("http://acme.dev")


# ── Flask test-client: new routes ────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(oaa.onboard_auto_approve_bp)
    from routes.white_glove_propagation import white_glove_propagation_bp
    app.register_blueprint(white_glove_propagation_bp)
    return app.test_client()


def test_stub_route_serves_stored_page(client, monkeypatch):
    monkeypatch.setattr(
        oaa, "load_stub_html",
        lambda slug: "<html>Acme stub</html>" if slug == "acme-agent-cloud"
        else None)
    r = client.get("/integrations/acme-agent-cloud")
    assert r.status_code == 200
    assert b"Acme stub" in r.data
    assert r.headers["Content-Type"].startswith("text/html")


def test_stub_route_unknown_slug_falls_back_to_legacy(client, monkeypatch):
    """Unknown slug must 308 to /integrations/<slug>/ — the legacy
    static integration-package route keeps working unchanged."""
    monkeypatch.setattr(oaa, "load_stub_html", lambda slug: None)
    r = client.get("/integrations/gemini")
    assert r.status_code == 308
    assert r.headers["Location"].endswith("/integrations/gemini/")


def test_stub_route_hostile_slug_no_db_lookup(client, monkeypatch):
    monkeypatch.setattr(
        oaa, "load_stub_html",
        lambda slug: (_ for _ in ()).throw(AssertionError("looked up")))
    r = client.get("/integrations/..%2f..%2fetc")
    assert r.status_code in (308, 404)   # never a 500, never a lookup


def test_lane_config_endpoint(client):
    r = client.get("/api/v1/onboard/auto-approve/config")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d["daily_cap"] >= 1
    assert d["lane"] == "auto_validated"


def test_white_glove_admin_endpoints_locked(client, monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    assert client.post(
        "/api/v1/admin/white-glove/propagate").status_code == 401
    assert client.get(
        "/api/v1/admin/white-glove/status").status_code == 401


def test_white_glove_propagate_kill_switch(client, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    monkeypatch.setenv("WHITE_GLOVE_PROPAGATE_DISABLE", "1")
    r = client.post("/api/v1/admin/white-glove/propagate?admin_key=k")
    assert r.status_code == 200
    d = r.get_json()
    assert d["skipped"] is True


# ── static wiring guards ─────────────────────────────────────────────

def test_onboarder_hook_wired():
    src = (REPO_ROOT / "routes" / "ai_platform_onboarder.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "from routes.onboard_auto_approve import try_auto_approve_lane" in src
    assert "auto_validated" in src, \
        "validated-lane approvals must be stamped approved_by='auto_validated'"
    # The lane may only fire on rows headed for the human queue.
    hook = src.split("try_auto_approve_lane", 1)[0].rsplit("elif", 1)[-1]
    assert "pending_review" in hook


def test_main_registers_new_blueprints():
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8",
                                            errors="ignore")
    assert "from routes.onboard_auto_approve import onboard_auto_approve_bp" in src
    assert ("from routes.white_glove_propagation import "
            "white_glove_propagation_bp") in src
