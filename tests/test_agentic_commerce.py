"""Guards for Stripe Agentic-Commerce (ACP) fulfillment by FEED SKU (2026-06-20).

Locks the money-critical contract: fulfill the right product by
price.external_reference, never double-grant/double-email on a webhook replay,
skip non-feed orders, and never raise out of the webhook.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

sm = pytest.importorskip("routes.stripe_metered")
mcp = pytest.importorskip("routes.mcp_conversion_plays")


def _arm(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_x")
    monkeypatch.setattr(sm, "_agentic_key_for_email", lambda email: "dch_live_testkey0001")


def test_skips_when_no_feed_sku(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(sm, "_stripe_get", lambda p, k: {"data": [{"price": {}}]})
    out = sm.handle_agentic_commerce_order({"id": "cs_1"})
    assert out.get("skipped") == "no_feed_sku"


def test_skips_without_stripe_key(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    out = sm.handle_agentic_commerce_order({"id": "cs_2"})
    assert out.get("skipped") == "no_stripe_key"


def test_fulfills_pack_by_external_reference(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(sm, "_stripe_get", lambda p, k: {"data": [
        {"price": {"external_reference": "dchub_pack_5_1000"}, "quantity": 1}]})
    monkeypatch.setattr(sm, "_agentic_email_key", lambda email, key: True)
    monkeypatch.setattr(mcp, "grant_credit_pack",
                        lambda *a, **k: {"ok": True, "idempotent": False})
    out = sm.handle_agentic_commerce_order(
        {"id": "cs_3", "customer_details": {"email": "Buyer@Example.com"}})
    assert out["ok"]
    r = out["results"][0]
    assert r["sku"] == "dchub_pack_5_1000"
    assert r["granted"] is True and r["emailed"] is True


def test_idempotent_replay_does_not_reemail(monkeypatch):
    """A re-delivered webhook (same session) must NOT grant again or re-email."""
    _arm(monkeypatch)
    monkeypatch.setattr(sm, "_stripe_get", lambda p, k: {"data": [
        {"price": {"external_reference": "dchub_pack_5_1000"}}]})
    sent = {"email": False}

    def _spy(email, key):
        sent["email"] = True
        return True
    monkeypatch.setattr(sm, "_agentic_email_key", _spy)
    # grant returns idempotent=True (already granted on this session)
    monkeypatch.setattr(mcp, "grant_credit_pack",
                        lambda *a, **k: {"ok": True, "idempotent": True})
    out = sm.handle_agentic_commerce_order(
        {"id": "cs_4", "customer_email": "b@e.com"})
    assert out["ok"]
    assert sent["email"] is False, "must not re-email on an idempotent replay"


def test_passes_session_id_for_dedup(monkeypatch):
    """grant_credit_pack must receive the Stripe session id so it can dedupe
    against the amount-based pack5 path + webhook replays."""
    _arm(monkeypatch)
    monkeypatch.setattr(sm, "_stripe_get", lambda p, k: {"data": [
        {"price": {"external_reference": "dchub_pack_5_1000"}}]})
    monkeypatch.setattr(sm, "_agentic_email_key", lambda e, k: True)
    seen = {}
    monkeypatch.setattr(mcp, "grant_credit_pack",
                        lambda *a, **k: seen.update(k) or {"ok": True, "idempotent": False})
    sm.handle_agentic_commerce_order({"id": "cs_SESSION_123", "customer_email": "b@e.com"})
    assert seen.get("stripe_session_id") == "cs_SESSION_123"
    assert seen.get("source") == "agentic_pack5"


def test_never_raises_on_stripe_error(monkeypatch):
    _arm(monkeypatch)

    def _boom(p, k):
        raise RuntimeError("stripe unreachable")
    monkeypatch.setattr(sm, "_stripe_get", _boom)
    out = sm.handle_agentic_commerce_order({"id": "cs_5"})
    assert out.get("ok") is False  # captured, not raised
