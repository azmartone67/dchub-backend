"""Digest wave (week of 2026-07-27) — pins for relay, entitlements, recidivism gate.

The three top strategic recs, built for real (the merged brain PRs #1773-75
were unregistered _proposed_ scaffolds by design):
  1. agent→human relay: /upgrade/h/<token> + relay_opens — the bridge behind
     '2,155 claims consumed by agents, ZERO opened by a human';
  2. entitlement self-check + admin repair — the open founder-tier ask;
  3. autopilot recidivism gate — >=3 actions/30d with zero verified
     successes suppresses the tactical patch and escalates once.

CI-SAFETY: no DATABASE_URL/JWT_SECRET; direct module imports (never main);
DB paths only via fail-soft contracts.
"""
import os
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def relay():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import human_relay as m
    return m


# ── wiring ────────────────────────────────────────────────────────────

def test_wave_blueprints_registered():
    src = _read("main.py")
    assert "register_blueprint(human_relay_bp)" in src
    assert "register_blueprint(account_entitlements_bp)" in src


# ── relay token contract ─────────────────────────────────────────────

def test_relay_token_roundtrip(relay, monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "test-secret")
    tok = relay.make_relay_token("sess-1", "get_grid_intelligence", "free")
    info = relay.parse_relay_token(tok)
    assert info and info["sid"] == "sess-1"
    assert info["tool"] == "get_grid_intelligence" and info["tier"] == "free"


def test_relay_token_rejects_tamper_and_age(relay, monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "test-secret")
    tok = relay.make_relay_token("s", "t", "free")
    assert relay.parse_relay_token(tok[:-2] + "zz") is None, "bad sig must fail"
    old = relay.make_relay_token("s", "t", "free",
                                 ts=int(time.time()) - 15 * 86400)
    assert relay.parse_relay_token(old) is None, "15d-old token must expire"
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "DIFFERENT")
    assert relay.parse_relay_token(tok) is None, "wrong secret must fail"


def test_relay_page_never_dead_ends(relay):
    # An expired/garbage token still renders a working upgrade page (a
    # paying-curious human must never hit a wall) — pinned as source.
    src = _read(os.path.join("routes", "human_relay.py"))
    assert "link expired" in src
    assert "no-store" in src


def test_relay_token_contract_matches_server_side():
    # The mcp-server mints with the identical contract — pin the shape here
    # so a backend refactor can't silently strand the JS minter.
    src = _read(os.path.join("routes", "human_relay.py"))
    assert 'hashlib.sha256).hexdigest()[:32]' in src
    assert '"%s|%s|%s|%d"' in src


# ── entitlements ─────────────────────────────────────────────────────

def test_entitlements_requires_key_and_uses_tier_name():
    src = _read(os.path.join("routes", "account_entitlements.py"))
    assert '"pass your API key as X-API-Key"' in src
    assert '.name' in src and "resolve_tier()" in src, \
        "IntEnum .name contract (never .value)"
    assert "mcp_dev_keys" in src, "claim-flow keys must be first-class"
    assert "stripe_customer_id" in src


def test_entitlements_repair_is_admin_gated_and_audited():
    src = _read(os.path.join("routes", "account_entitlements.py"))
    i = src.index("def repair")
    body = src[i:i + 2200]
    assert "X-Admin-Key" in body
    assert "entitlement_repairs" in body, "repairs must leave an audit trail"


# ── recidivism gate ──────────────────────────────────────────────────

def test_recidivism_gate_wired_into_rate_limit_check():
    src = _read(os.path.join("routes", "brain_autopilot.py"))
    assert "_recidivism_check" in src
    i = src.index("def _rate_limit_check")
    assert "_recidivism_check" in src[i:i + 1200], \
        "gate must run inside the central should-act choke point"
    assert "DCHUB_RECIDIVISM_GATE" in src, "killable"
    assert "autopilot_recidivism_escalations" in src, "escalate-once ledger"


def test_recidivism_gate_never_gates_on_a_failed_read():
    # ★flattering-zero (shell #34): a failed count is UNKNOWN, never 0 —
    # the gate may only suppress on an affirmative zero-successes read.
    src = _read(os.path.join("routes", "brain_autopilot.py"))
    i = src.index("def _recidivism_check")
    body = src[i:src.index("def _escalate_recidivism_once")]
    assert "row is None or row[0] is None" in body
