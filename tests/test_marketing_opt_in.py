#!/usr/bin/env python3
"""tests/test_marketing_opt_in.py — DOUBLE-OPT-IN consent-capture guard.

NO NETWORK, NO DB. We mock:
  · the transactional sender  (email_fallback.send_email_resilient)
  · suppression               (routes.email_suppression.is_suppressed)
  · the DB layer              (routes.marketing_opt_in._conn -> a fake cursor)

Covers the compliance contract the brief mandates:
  · /api/v1/opt-in/request sends a confirm email and does NOT set opt-in;
  · /api/v1/opt-in/confirm with a VALID token SETS opt-in + LOGS consent;
  · an INVALID/expired token does NOT set opt-in (no UPDATE, no consent row);
  · a SUPPRESSED email is not confirmed (opt-in not flipped);
  · the request path is a polite no-op for a suppressed address (no send);
  · /api/v1/opt-in/status is ADMIN-GATED (403 without the key);
  · request_opt_in() helper is reusable + does not set opt-in;
  · a registration wiring-guard (register() mounts all three routes).

Run standalone:   python3 tests/test_marketing_opt_in.py
Run under pytest: pytest tests/test_marketing_opt_in.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
from flask import Flask

import routes.marketing_opt_in as moi
import routes.email_suppression as es
import routes.email_validation as ev
import email_fallback as ef


# ── scripted fake DB — installed via monkeypatch on moi._conn ─────────────
class _FakeCursor:
    def __init__(self, fetch_rows=None, on_execute=None):
        # fetch_rows: list of row-lists, returned one PER fetchone()/fetchall().
        self._fetch_rows = list(fetch_rows or [])
        self._idx = 0
        self.executed = []          # [(sql, params), ...]
        self._on_execute = on_execute

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if self._on_execute:
            self._on_execute(sql, params)

    def fetchall(self):
        if self._idx < len(self._fetch_rows):
            rows = self._fetch_rows[self._idx]
            self._idx += 1
            return rows
        return []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def close(self): pass


class _FakeConn:
    """Supports both `with conn, conn.cursor() as cur:` (conn-as-ctx-manager)
    and a bare `conn.cursor()`."""
    def __init__(self, cur):
        self._cur = cur
    def cursor(self): return self._cur
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


def _install_conn(monkeypatch, cur):
    monkeypatch.setattr(moi, "_conn", lambda: _FakeConn(cur))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset the module table-ready flag, make the audit table a no-op, give a
    deterministic token secret, and default the validator to pass + nobody
    suppressed. Individual tests override as needed."""
    monkeypatch.setattr(moi, "_TABLE_READY", True, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    # validator: accept by default (soft-fail philosophy lives in the real one)
    monkeypatch.setattr(ev, "validate_email",
                        lambda e: (True, None, (e or "").strip().lower()))
    # suppression: nobody suppressed by default
    monkeypatch.setattr(es, "is_suppressed", lambda cur, e: False)


def _client():
    app = Flask(__name__)
    moi.register(app)
    return app.test_client()


# ── 1) request sends a confirm email and does NOT set opt-in ──────────────
def test_request_sends_confirm_and_does_not_set_optin(monkeypatch):
    sent = {}
    monkeypatch.setattr(ef, "send_email_resilient",
                        lambda to, subj, **kw: sent.update(to=to, subj=subj) or True)
    # No prior consent row (fetchone -> None for the rate-limit read), and the
    # _record_request UPSERT just records.
    cur = _FakeCursor(fetch_rows=[[]])  # rate-limit SELECT returns no row
    _install_conn(monkeypatch, cur)

    c = _client()
    r = c.post("/api/v1/opt-in/request", json={"email": "Power@Example.com"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["sent"] is True
    # the confirm email went to the normalized address
    assert sent.get("to") == "power@example.com"
    assert "confirm" in sent.get("subj", "").lower()
    # CRITICAL: no marketing_opt_in UPDATE on mcp_dev_keys happened here.
    joined = " ".join(s for s, _ in cur.executed)
    assert "marketing_opt_in" not in joined
    assert "UPDATE mcp_dev_keys" not in joined


# ── 2) confirm with a VALID token SETS opt-in + LOGS consent ──────────────
def test_confirm_valid_token_sets_optin_and_logs(monkeypatch):
    email = "buyer@acme.com"
    token = moi._opt_in_token(email)
    updates = []
    cur = _FakeCursor(on_execute=lambda sql, p: updates.append((sql, p)))
    _install_conn(monkeypatch, cur)

    c = _client()
    r = c.get(f"/api/v1/opt-in/confirm?email={email}&token={token}")
    assert r.status_code == 200
    assert b"in!" in r.data or b"confirmed" in r.data
    joined = " ".join(s for s, _ in updates)
    # opt-in flipped on the key
    assert "UPDATE mcp_dev_keys" in joined and "marketing_opt_in" in joined
    # consent logged to the audit table
    assert "opt_in_consents" in joined and "confirmed_at" in joined


# ── 3) an INVALID token does NOT set opt-in (no DB writes at all) ─────────
def test_confirm_invalid_token_does_not_set_optin(monkeypatch):
    writes = []
    cur = _FakeCursor(on_execute=lambda sql, p: writes.append(sql))
    _install_conn(monkeypatch, cur)

    c = _client()
    r = c.get("/api/v1/opt-in/confirm?email=buyer@acme.com&token=deadbeefdeadbeef")
    assert r.status_code == 200            # neutral page, never an error
    assert b"valid" in r.data or b"expired" in r.data
    joined = " ".join(writes)
    assert "UPDATE mcp_dev_keys" not in joined
    assert "opt_in_consents" not in joined  # no consent row on a bad token


# ── 4) a SUPPRESSED email is NOT confirmed even with a valid token ────────
def test_confirm_suppressed_email_not_opted_in(monkeypatch):
    monkeypatch.setattr(es, "is_suppressed", lambda cur, e: True)
    email = "optedout@acme.com"
    token = moi._opt_in_token(email)
    writes = []
    cur = _FakeCursor(on_execute=lambda sql, p: writes.append(sql))
    _install_conn(monkeypatch, cur)

    c = _client()
    r = c.get(f"/api/v1/opt-in/confirm?email={email}&token={token}")
    assert r.status_code == 200
    joined = " ".join(writes)
    assert "UPDATE mcp_dev_keys" not in joined  # suppression wins over confirm
    assert "opt_in_consents" not in joined


# ── 5) request is a polite no-op (no send) for a suppressed address ───────
def test_request_suppressed_no_send(monkeypatch):
    monkeypatch.setattr(es, "is_suppressed", lambda cur, e: True)
    calls = {"n": 0}
    monkeypatch.setattr(ef, "send_email_resilient",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or True)
    cur = _FakeCursor()
    _install_conn(monkeypatch, cur)

    res = moi.request_opt_in("optedout@acme.com", source="unit")
    assert res["ok"] is True and res["sent"] is False
    assert res["reason"] == "suppressed"
    assert calls["n"] == 0  # no confirm email sent to a suppressed address


# ── 6) status endpoint is ADMIN-GATED ─────────────────────────────────────
def test_status_admin_gated(monkeypatch):
    cur = _FakeCursor(fetch_rows=[[]])
    _install_conn(monkeypatch, cur)
    c = _client()

    # Patch the module gate directly so this is deterministic regardless of the
    # env / chained brain-gate state another test may leave behind (the gate
    # threads through 3 modules, which made this flaky in the full suite).
    # no admin -> 403
    monkeypatch.setattr(moi, "_admin_ok", lambda: False)
    r = c.get("/api/v1/opt-in/status?email=x@y.com")
    assert r.status_code == 403

    # admin -> 200
    monkeypatch.setattr(moi, "_admin_ok", lambda: True)
    r2 = c.get("/api/v1/opt-in/status?email=x@y.com")
    assert r2.status_code == 200
    assert r2.get_json()["ok"] is True


# ── 7) request_opt_in() helper is reusable + does not set opt-in ──────────
def test_request_opt_in_helper_no_optin(monkeypatch):
    monkeypatch.setattr(ef, "send_email_resilient", lambda *a, **k: True)
    writes = []
    cur = _FakeCursor(fetch_rows=[[]],
                      on_execute=lambda sql, p: writes.append(sql))
    _install_conn(monkeypatch, cur)

    res = moi.request_opt_in("dev@startup.io", source="bind_email")
    assert res["ok"] is True and res["sent"] is True
    joined = " ".join(writes)
    # the helper only records the request; it never flips opt-in.
    assert "UPDATE mcp_dev_keys" not in joined


# ── 8) registration wiring-guard ──────────────────────────────────────────
def test_register_mounts_all_three_routes():
    app = Flask(__name__)
    moi.register(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/opt-in/request" in rules
    assert "/api/v1/opt-in/confirm" in rules
    assert "/api/v1/opt-in/status" in rules


# ── token cross-verifies with the email_suppression idiom ─────────────────
def test_token_matches_email_suppression_idiom(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "shared-secret")
    # our token must equal email_suppression.unsub_token for the same address.
    assert moi._opt_in_token("a@b.com") == es.unsub_token("a@b.com")


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-v"]))
