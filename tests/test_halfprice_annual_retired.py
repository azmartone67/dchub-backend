"""r-sku-wall (2026-09-24): the half-price Pro Annual campaign cannot send.

Its email says "50% off your current $199/mo rate — $1,188 = effectively
$99/mo, through end of June". Pro has been $99/mo since 2026-09-05, so $1,188
is exactly 12 x $99: no discount at all, and Pro Annual is withdrawn. Before
this change /fire sent it given a fresh fire_key or the ADMIN_OVERRIDE key,
stopped only if CAMPAIGN_DISABLE happened to be set.
"""
from __future__ import annotations

import hashlib

import pytest


@pytest.fixture()
def camp(monkeypatch):
    import routes.campaign_halfprice_annual as m

    def _no_send(*a, **k):
        raise AssertionError("a retired campaign tried to send an email")

    monkeypatch.setattr(m, "_send_via_resend", _no_send)
    monkeypatch.delenv("CAMPAIGN_DISABLE", raising=False)
    monkeypatch.delenv("DCHUB_HALFPRICE_DRY_RUN", raising=False)

    class _Conn:
        def cursor(self):
            class _Cur:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def execute(s, *a, **k): pass
                def fetchall(s): return []
                def fetchone(s): return None
            return _Cur()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(m, "_db_conn", lambda: _Conn())
    monkeypatch.setattr(m, "_ensure_schema", lambda c: None)
    monkeypatch.setattr(m, "_find_eligible", lambda cur: [
        {"user_id": 1, "email": "sub@acme-energy.co", "plan": "pro",
         "joined_at": "2026-05-01"}])
    return m


def test_fire_refuses_with_a_fresh_fire_key(camp):
    out = camp.run_fire(camp._mint_fire_key())
    assert out["errors"] == ["campaign_retired"] and out["sent"] == []
    assert "retired" in out


def test_fire_refuses_the_admin_override(camp, monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    override = "ADMIN_OVERRIDE_" + hashlib.sha256(b"k").hexdigest()[:16]
    out = camp.run_fire(override)
    assert out["errors"] == ["campaign_retired"] and out["sent"] == []


def test_preview_lists_candidates_but_mints_no_fire_key(camp):
    out = camp.run_preview()
    assert out["eligible_count"] == 1          # the preview really ran
    assert out["fire_key"] is None
    assert out["retired"].startswith("campaign_retired")
