"""
tests/test_upgrade_outreach.py — DRAFT-AND-APPROVE upgrade outreach engine.

NO NETWORK, NO DB. We mock:
  · the brain LLM        (upgrade_outreach._call_model)
  · the marketing sender (routes.marketing_send.send_marketing_email)
  · the transactional    (email_fallback.send_email_resilient)
  · suppression          (routes.email_suppression.is_suppressed)
  · the DB layer         (upgrade_outreach._conn -> a fake cursor)

Covers the safety contract:
  · generate is FLAG-GATED: OUTREACH_GENERATE_ENABLED off -> 0 drafts and the
    LLM is NEVER called (a boom-sentinel _call_model proves it);
  · a MARKETING-channel approve routes through send_marketing_email (NOT a
    direct Resend) and a TRANSACTIONAL approve through email_fallback;
  · approve with OUTREACH_SEND_ENABLED off -> approved_pending_send and NEITHER
    sender is ever called;
  · a suppressed recipient is NOT sent (marketing + transactional);
  · high_usage_free candidate SELECTION SQL filters to opted-in only;
  · endpoints are admin-gated (403 without the key);
  · main.py wires up register_upgrade_outreach (wiring guard).
"""
import json

import pytest

uo = pytest.importorskip("routes.upgrade_outreach")


# ─────────────────────────────────────────────────────────────────────
# A fake psycopg2 connection/cursor so the engine's DB calls never touch
# a real DB. Each test installs a scripted cursor via monkeypatch on _conn.
# ─────────────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, fetch_rows=None, on_execute=None):
        self._fetch_rows = list(fetch_rows or [])
        self._idx = 0
        self.executed = []          # [(sql, params), ...]
        self._on_execute = on_execute
        self.connection = self

    # context-manager protocol (cur used as `with conn.cursor() as cur:`)
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

    # connection-ish bits the engine calls on the cursor / conn
    def rollback(self): pass
    def commit(self): pass
    def close(self): pass
    def cursor(self): return self


class _FakeConn:
    def __init__(self, cur): self._cur = cur
    def cursor(self): return self._cur
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


def _install_conn(monkeypatch, cur):
    monkeypatch.setattr(uo, "_conn", lambda: _FakeConn(cur))


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    """Default both gates OFF for every test (ship dark); tests opt-in."""
    monkeypatch.delenv("OUTREACH_GENERATE_ENABLED", raising=False)
    monkeypatch.delenv("OUTREACH_SEND_ENABLED", raising=False)


# ── 1. generate is flag-gated: off -> 0 drafts, LLM NEVER called ─────
def test_generate_flag_off_never_calls_llm(monkeypatch):
    def _boom(*a, **k):  # boom-sentinel — must never be reached
        raise AssertionError("LLM called while OUTREACH_GENERATE_ENABLED is off")
    monkeypatch.setattr(uo, "_call_model", _boom)
    # _conn must also never be touched (no candidate selection when gated).
    monkeypatch.setattr(uo, "_conn",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("_conn touched while gated")))

    out = uo.generate_drafts("keyless_payer", max_n=5)
    assert out["enabled"] is False
    assert out["drafts"] == 0


# ── 2. generate ON: drafts the candidates, stores them ───────────────
def test_generate_on_drafts_and_stores(monkeypatch):
    monkeypatch.setenv("OUTREACH_GENERATE_ENABLED", "1")

    # One keyless_payer candidate.
    monkeypatch.setattr(uo, "select_candidates", lambda seg, limit=25: [{
        "email": "payer@acme.com", "segment": "keyless_payer",
        "channel": "transactional",
        "context": {"name": "Pat", "plan": "pro", "invoices_paid_count": 2,
                    "reason": "activation gap"},
    }])

    called = {"n": 0}
    def _fake_model(system, prompt, *, tier="routine", max_tokens=700):
        called["n"] += 1
        return json.dumps({"subject": "Activate your DC Hub key",
                           "body": "Hi Pat, you're paying for Pro — here's how "
                                   "to activate your MCP key."}), None, "m"
    monkeypatch.setattr(uo, "_call_model", _fake_model)

    inserts = []
    cur = _FakeCursor(on_execute=lambda sql, p: inserts.append((sql, p))
                      if "INSERT INTO outreach_drafts" in sql else None)
    _install_conn(monkeypatch, cur)

    out = uo.generate_drafts("keyless_payer", max_n=5)
    assert out["ok"] and out["enabled"] is True
    assert out["drafts"] == 1 and out["flagged"] == 0
    assert called["n"] == 1
    # exactly one INSERT, status 'drafted'
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert params[0] == "payer@acme.com"   # email
    assert params[1] == "keyless_payer"    # segment
    assert params[5] == "drafted"          # status


# ── 2b. honest-numbers fence flags a draft that cites a banned figure ─
def test_generate_fences_banned_figure(monkeypatch):
    monkeypatch.setenv("OUTREACH_GENERATE_ENABLED", "1")
    monkeypatch.setattr(uo, "select_candidates", lambda seg, limit=25: [{
        "email": "x@acme.com", "segment": "keyless_payer",
        "channel": "transactional", "context": {"name": "X", "plan": "pro"},
    }])
    monkeypatch.setattr(uo, "_call_model",
        lambda s, p, *, tier="routine", max_tokens=700: (
            json.dumps({"subject": "Hi", "body": "We track 50,000 facilities!"}),
            None, "m"))
    captured = {}
    def _on_exec(sql, params):
        if "INSERT INTO outreach_drafts" in sql:
            captured["status"] = params[5]
            captured["context"] = params[6]
    _install_conn(monkeypatch, _FakeCursor(on_execute=_on_exec))
    out = uo.generate_drafts("keyless_payer", max_n=1)
    assert out["flagged"] == 1 and out["drafts"] == 0
    assert captured["status"] == "flagged"
    assert "fence_hits" in (captured["context"] or "")


# ── 3. MARKETING approve routes through send_marketing_email ─────────
def test_marketing_approve_uses_chokepoint(monkeypatch):
    monkeypatch.setenv("OUTREACH_SEND_ENABLED", "1")
    draft = {"id": 7, "email": "comp@acme.com", "segment": "comped_paid",
             "channel": "marketing", "subject": "Convert your comp",
             "body": "Hi", "status": "drafted", "context": {"name": "Comp"}}
    monkeypatch.setattr(uo, "_get_draft", lambda cur, i: dict(draft))

    import routes.marketing_send as ms
    marketing_calls = []
    monkeypatch.setattr(ms, "send_marketing_email",
        lambda **kw: (marketing_calls.append(kw) or
                      {"sent": True, "suppressed": False, "reason": None,
                       "source": kw.get("source")}))
    # transactional sender must NOT be touched for a marketing draft
    import email_fallback as ef
    monkeypatch.setattr(ef, "send_email_resilient",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("transactional sender used for marketing draft")))

    _install_conn(monkeypatch, _FakeCursor())
    res = uo._send_draft(dict(draft))
    assert res["sent"] is True and res["status"] == "sent"
    assert len(marketing_calls) == 1
    # the choke-point got the right source tag + recipient
    assert marketing_calls[0]["to_email"] == "comp@acme.com"
    assert marketing_calls[0]["source"].startswith("upgrade_outreach:comped_paid")


# ── 4. TRANSACTIONAL approve routes through email_fallback ───────────
def test_transactional_approve_uses_email_fallback(monkeypatch):
    draft = {"id": 8, "email": "payer@acme.com", "segment": "keyless_payer",
             "channel": "transactional", "subject": "Activate",
             "body": "Hi", "status": "drafted", "context": {"name": "P"}}
    # not suppressed
    import routes.email_suppression as es
    monkeypatch.setattr(es, "is_suppressed", lambda cur, e: False)
    # marketing choke-point must NOT be used for a transactional draft
    import routes.marketing_send as ms
    monkeypatch.setattr(ms, "send_marketing_email",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("marketing choke-point used for transactional draft")))
    import email_fallback as ef
    sent = []
    monkeypatch.setattr(ef, "send_email_resilient",
        lambda **kw: (sent.append(kw) or True))
    _install_conn(monkeypatch, _FakeCursor())

    res = uo._send_draft(dict(draft))
    assert res["sent"] is True and res["status"] == "sent"
    assert len(sent) == 1 and sent[0]["to_email"] == "payer@acme.com"


# ── 5. approve with SEND flag OFF -> parked, NEITHER sender called ───
def test_approve_send_off_parks_pending(monkeypatch):
    # OUTREACH_SEND_ENABLED is unset (off) via the autouse fixture.
    draft = {"id": 9, "email": "x@acme.com", "segment": "comped_paid",
             "channel": "marketing", "subject": "S", "body": "B",
             "status": "drafted", "context": {}}
    states = {"after": dict(draft)}

    def _on_exec(sql, params):
        if "approved_pending_send" in sql:
            states["after"]["status"] = "approved_pending_send"
    cur = _FakeCursor(on_execute=_on_exec)
    _install_conn(monkeypatch, cur)
    # First _get_draft returns the drafted row; the final one returns parked.
    seq = [dict(draft), {**draft, "status": "approved_pending_send"}]
    monkeypatch.setattr(uo, "_get_draft", lambda c, i: seq.pop(0) if seq else
                        {**draft, "status": "approved_pending_send"})

    # BOTH senders are boom-sentinels — must never be called when send is off.
    import routes.marketing_send as ms
    import email_fallback as ef
    monkeypatch.setattr(ms, "send_marketing_email",
        lambda **k: (_ for _ in ()).throw(AssertionError("marketing sent while flag off")))
    monkeypatch.setattr(ef, "send_email_resilient",
        lambda **k: (_ for _ in ()).throw(AssertionError("transactional sent while flag off")))

    out = uo.approve_draft(9)
    assert out is not None
    assert out["status"] == "approved_pending_send"


# ── 6. suppressed recipient is NOT sent (both channels) ──────────────
def test_marketing_suppressed_not_sent(monkeypatch):
    monkeypatch.setenv("OUTREACH_SEND_ENABLED", "1")
    draft = {"email": "sup@acme.com", "segment": "comped_paid",
             "channel": "marketing", "subject": "S", "body": "B", "context": {}}
    import routes.marketing_send as ms
    # choke-point itself reports suppressed (its internal suppression check)
    monkeypatch.setattr(ms, "send_marketing_email",
        lambda **k: {"sent": False, "suppressed": True, "reason": "suppressed",
                     "source": k.get("source")})
    res = uo._send_draft(dict(draft))
    assert res["sent"] is False and res["status"] == "suppressed"


def test_transactional_suppressed_not_sent(monkeypatch):
    monkeypatch.setenv("OUTREACH_SEND_ENABLED", "1")
    draft = {"email": "sup@acme.com", "segment": "at_risk_paid",
             "channel": "transactional", "subject": "S", "body": "B", "context": {}}
    import routes.email_suppression as es
    monkeypatch.setattr(es, "is_suppressed", lambda cur, e: True)   # suppressed
    import email_fallback as ef
    monkeypatch.setattr(ef, "send_email_resilient",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("transactional sent to a suppressed recipient")))
    _install_conn(monkeypatch, _FakeCursor())
    res = uo._send_draft(dict(draft))
    assert res["sent"] is False and res["status"] == "suppressed"


# ── 7. high_usage_free SELECTION excludes non-opted-in (SQL guard) ───
def test_high_usage_free_selection_filters_opted_in(monkeypatch):
    captured = {"sql": None}

    class _Cur(_FakeCursor):
        def execute(self, sql, params=()):
            captured["sql"] = sql
            super().execute(sql, params)
            # no rows -> [] candidates; we only care about the SQL shape

    _install_conn(monkeypatch, _Cur(fetch_rows=[[]]))
    uo.select_candidates("high_usage_free", limit=10)
    sql = captured["sql"] or ""
    # The marketing-consent gate MUST be in the candidate-selection SQL itself.
    assert "marketing_opt_in' = 'true'" in sql
    # And suppression excluded at selection time.
    assert "email_suppression" in sql


def test_comped_paid_selection_filters_opted_in(monkeypatch):
    captured = {"sql": None}

    class _Cur(_FakeCursor):
        def execute(self, sql, params=()):
            captured["sql"] = sql
            super().execute(sql, params)

    _install_conn(monkeypatch, _Cur(fetch_rows=[[]]))
    uo.select_candidates("comped_paid", limit=10)
    sql = captured["sql"] or ""
    assert "marketing_opt_in' = 'true'" in sql
    assert "email_suppression" in sql
    assert "invoices_paid_count,0) = 0" in sql


# ── 8. endpoints are admin-gated (403 without key) ───────────────────
@pytest.fixture()
def client(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(uo.upgrade_outreach_bp)
    return app.test_client()


def test_endpoints_admin_gated(monkeypatch, client):
    monkeypatch.setattr(uo, "_admin_ok", lambda: False)
    assert client.get("/api/v1/portal/outreach").status_code == 403
    assert client.post("/api/v1/portal/outreach/generate",
                       json={"segment": "comped_paid"}).status_code == 403
    assert client.post("/api/v1/portal/outreach/1/approve").status_code == 403
    assert client.post("/api/v1/portal/outreach/1/skip").status_code == 403
    assert client.post("/api/v1/portal/outreach/1/edit",
                       json={"subject": "x"}).status_code == 403


def test_generate_endpoint_rejects_unknown_segment(monkeypatch, client):
    monkeypatch.setattr(uo, "_admin_ok", lambda: True)
    r = client.post("/api/v1/portal/outreach/generate", json={"segment": "bogus"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "unknown_segment"


def test_queue_endpoint_surfaces_flags(monkeypatch, client):
    monkeypatch.setattr(uo, "_admin_ok", lambda: True)
    monkeypatch.setattr(uo, "list_drafts", lambda **k: [])
    r = client.get("/api/v1/portal/outreach")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert "generate_enabled" in body["flags"] and "send_enabled" in body["flags"]


# ── 9. wiring guard: main.py registers the engine ────────────────────
def test_main_registers_upgrade_outreach():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "from routes.upgrade_outreach import register_upgrade_outreach" in src
    assert "register_upgrade_outreach(app)" in src


def test_register_helper_exists():
    assert hasattr(uo, "register_upgrade_outreach")
    assert hasattr(uo, "upgrade_outreach_bp")
