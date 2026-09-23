"""r-metered-wall-sku (2026-09-23) — ship item #2, one surface of it.

free_tier_gate._metered_402 fronts every METERED_MAP_PREFIXES route. Before
this, only the Land & Power / site-score / site-planner branch got a signed
checkout (util.plan_tease.lp_ladder); every other prefix (power-plants,
fiber routes/intel/sources, grid intelligence, capacity-headroom,
energy-discovery, competitive-intel, competitor) fell straight through to a
bare 'https://dchub.cloud/pricing?utm_source=map_session_cap' — unattributable,
and worse UX than the signed /go/c link every other wall in this codebase uses.

util.plan_tease.metered_wall_ladder() fills that gap. It deliberately does
NOT offer the $10 pack: free_tier_gate._resolve_caller bypasses THIS specific
402 only when the caller's `plan` is in tier_registry.paid_plan_names(), and
grant_credit_pack (routes/mcp_conversion_plays.py) never touches `plan` — it
only ever writes mcp_topups credits. Selling the pack here would be exactly
the false-promise rung rest_wall_ladder's own docstring already refuses.
Developer is the cheapest plan actually in paid_plan_names(), so it leads.
"""
from __future__ import annotations

import base64
import hashlib
import os

import flask
import pytest

SECRET = "metered-wall-test-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.delenv("DCHUB_GO_LINKS", raising=False)


def _decode(url: str):
    assert url.startswith("https://dchub.cloud/go/c/"), url
    token = url[len("https://dchub.cloud/go/c/"):]
    payload = token.rsplit(".", 1)[0]
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    return raw.split("|")


# ── util.plan_tease.metered_wall_ladder ─────────────────────────────────────
def test_no_key_offers_developer_then_pro_both_signed():
    from util.plan_tease import metered_wall_ladder
    out = metered_wall_ladder()
    assert out["key_bound"] is False
    plans = [r["plan"] for r in out["upgrade_options"]]
    assert plans == ["developer", "pro"]
    for r in out["upgrade_options"]:
        parts = _decode(r["url"])
        assert parts[0] == r["plan"]
    assert out["upgrade_url"] == out["upgrade_options"][0]["url"]


def test_the_pack_is_never_offered_here():
    """The single most important guard in this file: this gate cannot be
    resolved by mcp_topups credits, only by `plan`, so offering the pack
    would be a checkout that doesn't open what it's shown on."""
    from util.plan_tease import metered_wall_ladder
    out = metered_wall_ladder()
    plans = [r["plan"] for r in out["upgrade_options"]]
    assert "pack" not in plans
    for r in out["upgrade_options"]:
        assert "pack" not in _decode(r["url"])[0]


def test_a_key_binds_both_rungs_to_it():
    from util.plan_tease import metered_wall_ladder
    key = "dch_live_test_key_for_metered_wall"
    app = flask.Flask(__name__)
    with app.test_request_context("/api/v1/power-plants"):
        flask.request.api_key_info = {"ok": True}  # require_plan already validated it
        out = metered_wall_ladder(key)
    assert out["key_bound"] is True
    h = "k-" + hashlib.sha256(key.encode()).hexdigest()
    for r in out["upgrade_options"]:
        assert _decode(r["url"])[1] == h


def test_no_signing_secret_fails_open_to_nothing_not_a_wall(monkeypatch):
    """checkout_url() returns the pricing page with no secret to sign with;
    metered_wall_ladder must not surface that as upgrade_url (the caller in
    free_tier_gate.py only applies rungs when upgrade_url is truthy)."""
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    from util.plan_tease import metered_wall_ladder
    out = metered_wall_ladder()
    for r in (out.get("upgrade_options") or []):
        assert not r["url"].startswith("https://dchub.cloud/go/c/")


# ── free_tier_gate._metered_402 wiring ──────────────────────────────────────
def _metered_402_payload(path: str, monkeypatch):
    import free_tier_gate as fg
    app = flask.Flask(__name__)
    with app.test_request_context(path):
        resp, status = fg._metered_402()
        assert status == 402
        return resp.get_json()


def test_a_non_land_power_prefix_gets_the_signed_ladder_not_bare_pricing(monkeypatch):
    body = _metered_402_payload("/api/v1/power-plants", monkeypatch)
    assert body["upgrade_url"].startswith("https://dchub.cloud/go/c/")
    assert "upgrade_options" in body
    plans = [r["plan"] for r in body["upgrade_options"]]
    assert plans == ["developer", "pro"]


def test_land_power_still_gets_its_own_pro_only_ladder_unchanged(monkeypatch):
    body = _metered_402_payload("/api/v1/land-power/data", monkeypatch)
    assert body["upgrade_url"].startswith("https://dchub.cloud/go/c/")
    assert _decode(body["upgrade_url"])[0] == "pro"
    assert body["tier_required"] == "pro"


def test_no_signing_secret_still_falls_back_to_the_utm_pricing_url(monkeypatch):
    """The pre-existing fail-open contract test_conversion_visibility.py pins:
    a walled agent must never get a broken link, so this stays the bare,
    utm-tagged pricing URL when nothing can be signed — never a 500, never
    an empty upgrade_url."""
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    body = _metered_402_payload("/api/v1/power-plants", monkeypatch)
    assert body["upgrade_url"] == "https://dchub.cloud/pricing?utm_source=map_session_cap"
