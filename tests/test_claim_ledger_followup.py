"""tests/test_claim_ledger_followup.py — claim ledger follow-ups on #3045.

  (1) THE RESOLVER CARRIES THE ADMIN KEY. `get:` metrics are fetched by
      claim_ledger._default_fetch through util.internal_fetch.probe on the
      loopback. With no header, an admin-gated endpoint (step 2 registers
      claims against /api/v1/admin/facility-dedup/analyze) 401s, reads as
      'not observed', and the claim judges `unobserved` forever. The fetch now
      forwards X-Admin-Key from DCHUB_ADMIN_KEY, read per call, merged over
      the probe's own User-Agent. CONTROL: a public path still resolves with
      no env set — the header is additive, not a precondition.
  (2) Guards ported from the parallel step-1 build that still apply to the
      merged module and were missing here: the pre-existing L16 LLM path
      still writes was_correct=True (must-stay-green control); no producer
      stamps an outcome and the verifier is the only outcome writer (AST);
      main.py registers the ledger inside its OWN try/except; the squasher's
      claim wrapper is fail-soft; list_claims is newest-first and JSON-safe.

DB-free. Never imports main.py (AST on the source). Nothing at module scope
beyond imports and helpers.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

cl = pytest.importorskip("routes.claim_ledger")  # noqa: E402
l16 = pytest.importorskip("routes.brain_layer16_self_critique")  # noqa: E402
uif = pytest.importorskip("util.internal_fetch")  # noqa: E402

_UTC = dt.timezone.utc
_ADMIN_PATH = "/api/v1/admin/facility-dedup/analyze?country=FR"


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _envelope(path, data, ok=True):
    return {"path": path, "ok": ok, "data": data if ok else {},
            "status": 200 if ok else 401, "reason": None if ok else "HTTP 401",
            "empty": ok and not data}


# ── (1) the resolver carries the admin key ───────────────────────────

def test_default_fetch_forwards_the_admin_key_when_set(monkeypatch):
    seen = {}

    def fake_probe(path, timeout=8, headers=None):
        seen.update(path=path, timeout=timeout, headers=headers)
        return _envelope(path, {"ok": True, "count": 3})
    monkeypatch.setattr(uif, "probe", fake_probe)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm-live-key")
    assert cl._default_fetch(_ADMIN_PATH) == {"ok": True, "count": 3}
    assert seen["path"] == _ADMIN_PATH and seen["timeout"] == 8
    assert seen["headers"] == {"X-Admin-Key": "adm-live-key"}
    assert "User-Agent" not in seen["headers"], "the probe's own UA is kept, not overridden"
    # read PER CALL: a rotated key is picked up without a restart
    monkeypatch.setenv("DCHUB_ADMIN_KEY", " rotated-key \n")
    cl._default_fetch("/api/v1/admin/x")
    assert seen["headers"] == {"X-Admin-Key": "rotated-key"}
    # end to end: an admin-gated get: metric resolves through this fetch
    actual, ev = cl.resolve_metric(f"get:{_ADMIN_PATH} count")
    assert actual == 3 and ev["endpoint"] == _ADMIN_PATH


def test_default_fetch_keeps_the_probe_user_agent_on_the_wire(monkeypatch):
    """Through the REAL probe: the admin key rides next to the probe's UA on a
    127.0.0.1 GET — nothing else changes about the request."""
    import requests
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"count": 7}

    def fake_get(url, timeout=None, headers=None, **kw):
        seen.update(url=url, timeout=timeout, headers=headers)
        return _Resp()
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm-live-key")
    monkeypatch.delenv("PORT", raising=False)
    assert cl._default_fetch(_ADMIN_PATH) == {"count": 7}
    assert seen["url"] == "http://127.0.0.1:8080" + _ADMIN_PATH
    assert seen["timeout"] == 8
    assert seen["headers"] == {"User-Agent": "dchub-internal-probe/1.0",
                               "X-Admin-Key": "adm-live-key"}


def test_control_public_path_resolves_with_no_admin_key_in_the_env(monkeypatch):
    """CONTROL (must stay green when the header is dropped): with no
    DCHUB_ADMIN_KEY the fetch sends no key header and a public `get:` metric
    still resolves to its value."""
    seen = {}

    def fake_probe(path, timeout=8, headers=None):
        seen.update(path=path, headers=headers)
        return _envelope(path, {"facilities": 18406})
    monkeypatch.setattr(uif, "probe", fake_probe)
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    actual, ev = cl.resolve_metric("get:/api/v1/stats facilities")
    assert actual == 18406 and ev["endpoint"] == "/api/v1/stats"
    assert seen["path"] == "/api/v1/stats" and not seen["headers"]
    # an empty env value is the same as unset
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "   ")
    cl._default_fetch("/api/v1/stats")
    assert not seen["headers"]


def test_default_fetch_failure_is_not_observed_never_a_value(monkeypatch):
    monkeypatch.setattr(uif, "probe",
                        lambda path, timeout=8, headers=None: _envelope(path, {}, ok=False))
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm-live-key")
    assert cl._default_fetch(_ADMIN_PATH) == {}
    actual, ev = cl.resolve_metric(f"get:{_ADMIN_PATH} count")
    assert actual is None and ev["status"] == "empty_or_failed"
    assert cl.judge(actual, "< 5") == "unobserved"


# ── (2) ported guards ────────────────────────────────────────────────

class _Cur:
    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.script.pop(0) if self.script else None

    def fetchall(self):
        return list((self.script.pop(0) if self.script else None) or [])

    def close(self):
        pass


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.committed = 0
        self.closed = False
        self.autocommit = False

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


def test_control_l16_llm_verification_still_writes_was_correct_true(monkeypatch):
    """The pre-existing L16 path — pending L14 prediction -> Claude verdict ->
    UPDATE was_correct/verified_at — is untouched by the ledger. Stubbed
    Claude, stubbed `main.get_db` (the suite never imports main)."""
    row = (7, "chain", "high", "GET /api/v1/stats facilities > 0", "root cause",
           dt.datetime(2026, 8, 1, tzinfo=_UTC))
    cur = _Cur(script=[[row]])
    stub = types.ModuleType("main")
    stub.get_db = lambda *a, **k: _Conn(cur)
    prev = sys.modules.get("main")
    sys.modules["main"] = stub
    try:
        monkeypatch.setattr(l16, "_ANTHROPIC_KEY", "test-key")
        monkeypatch.setattr(l16, "_internal", lambda path, timeout=8: {})
        monkeypatch.delenv("BRAIN_VERIFY_RESOLVE_ENABLE", raising=False)

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"content": [{"type": "text", "text": json.dumps([
                    {"id": 7, "was_correct": True,
                     "actual_outcome": "facilities 18406 > 0",
                     "calibration_bucket": "well-calibrated"}])}]}
        monkeypatch.setattr(l16, "_llm_post", lambda *a, **k: _Resp())
        res = l16._verify_pending(max_to_verify=10)
    finally:
        if prev is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = prev
    assert res.get("verified") == 1, res
    upd = [(s, p) for s, p in cur.calls
           if s.startswith("UPDATE brain_predictions_log SET") and "was_correct = %s" in s
           and "verified_at = NOW()" in s]
    assert len(upd) == 1
    assert upd[0][1][1] is True and upd[0][1][3] == 7


@pytest.mark.parametrize("rel", [
    "content_publisher.py", "ai_surface_canon.py", "routes/squasher_queue.py",
])
def test_producers_never_stamp_outcomes(rel):
    """OUTCOME WRITER != AUTHOR — executable call sites, not comments."""
    tree = ast.parse(_src(rel))
    offenders = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Name) and n.func.id == "stamp_outcome")
        or (isinstance(n.func, ast.Attribute) and n.func.attr == "stamp_outcome"))]
    assert not offenders, f"{rel} stamps its own claim outcomes"


def test_the_verifier_is_the_only_outcome_writer():
    tree = ast.parse(_src("routes/claim_ledger.py"))
    writers = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "_stamp_outcome_sql" for c in ast.walk(n))}
    assert writers == {"stamp_outcome", "verify_due_claims"}


def _register_calls(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id.startswith("register_")]


def test_main_registers_the_ledger_in_its_own_try():
    """register_claim_ledger(app) sits directly inside one try/except that
    holds no sibling registration — a neighbour's import failure cannot take
    the ledger down, nor the reverse."""
    tree = ast.parse(_src("main.py"))
    inner = [t for t in ast.walk(tree) if isinstance(t, ast.Try) and any(
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
        and isinstance(s.value.func, ast.Name)
        and s.value.func.id == "register_claim_ledger" for s in t.body)]
    assert len(inner) == 1, "register_claim_ledger(app) must sit directly inside one try"
    t = inner[0]
    assert [c.func.id for c in _register_calls(t)] == ["register_claim_ledger"]
    assert [ast.unparse(a) for a in _register_calls(t)[0].args] == ["app"]
    assert t.handlers
    assert any(isinstance(n, ast.ImportFrom) and n.module == "routes.claim_ledger"
               for n in ast.walk(t))


def test_squasher_claim_wrapper_is_fail_soft(monkeypatch):
    sq = pytest.importorskip("routes.squasher_queue")
    seen = []
    monkeypatch.setattr(cl, "register_finding_claim",
                        lambda key, title, qid, count=None: seen.append((key, title, qid)) or 5)
    assert sq._register_fix_claim(9, "dchub://cron/x", "stale loop") is None
    assert seen == [("dchub://cron/x", "stale loop", 9)]

    def _boom(*a, **k):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(cl, "register_finding_claim", _boom)
    assert sq._register_fix_claim(9, "dchub://cron/x", "stale loop") is None


def test_list_claims_is_newest_first_and_json_safe(monkeypatch):
    when = dt.datetime(2026, 8, 21, 12, 0, tzinfo=_UTC)
    row = (5, when, "post", "social_media_posts:7", "stmt", '{"as_of": "x"}',
           ["linkedin"], "linkedin:7 impressions", ">= 3", 72, when, None, None,
           None, when + dt.timedelta(hours=72))
    cur = _Cur(script=[[row]])
    monkeypatch.setattr(cl, "_db_url", lambda: "postgres://fake")
    monkeypatch.setattr(cl, "ensure_schema", lambda force=False: True)
    monkeypatch.setattr(cl, "_conn", lambda: _Conn(cur))
    out = cl.list_claims(limit=10, kind="post", outcome="open")
    assert len(out) == 1
    c = out[0]
    assert c["id"] == 5 and c["registered_at"] == when.isoformat()
    assert c["regime"] == {"as_of": "x"} and c["surfaces"] == ["linkedin"]
    assert c["due_at"] == (when + dt.timedelta(hours=72)).isoformat()
    assert c["outcome"] is None
    json.dumps(out)
    sql, params = cur.calls[0]
    assert "ORDER BY predicted_at DESC LIMIT %s" in sql
    assert "kind = %s" in sql and "outcome IS NULL" in sql
    assert params == [cl.SOURCE_LAYER, "post", 10]
