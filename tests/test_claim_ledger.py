"""tests/test_claim_ledger.py — the claim ledger contract (Claim Loop step 1).

What this guards (2026-08-22):
  (1) NO EXPECTATION, NO ROW — register_claim refuses a claim without a
      metric+comparator, and the refusal happens BEFORE any database call.
  (2) THE COMPARATOR — a true expectation is `confirmed`, a false one
      `refuted`, and an instrument that has not measured is `unobserved`
      (deferred inside the grace window, stamped past it). A verdict never
      comes from a gap.
  (3) OUTCOME WRITER ≠ AUTHOR — the stamp is one UPDATE that only touches an
      open claim; producers register and mark shipped, nothing else.
  (4) CLAIMS STAY OUT OF L16'S LLM VERIFY PATH (the must-stay-green control):
      the ledger never sets verification_criterion, and L16's selector still
      filters on it — so the existing self-critique keeps scoring its own
      predictions exactly as before.
  (5) THE THREE PRODUCERS ARE WIRED — asserted on executable text (AST call
      sites), not on comments: media registers BEFORE _post_to_linkedin and
      stamps shipped AFTER it; the squasher registers at enqueue; canon
      registers inside resolve_canon(); L16's run calls the verifier; main.py
      registers the blueprint.
  (6) THE ROUTES FAIL CLOSED — 401 with no credential, key set or not.

House rules: no DB, never import main, nothing heavy at module scope.
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _ledger():
    return importlib.import_module("routes.claim_ledger")


# ── fakes ────────────────────────────────────────────────────────────────

class _Cur:
    """Records every execute; answers fetchone/fetchall from a script."""

    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def _next(self):
        return self.script.pop(0) if self.script else None

    def fetchone(self):
        return self._next()

    def fetchall(self):
        v = self._next()
        return list(v or [])

    def close(self):
        pass


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.committed = 0
        self.closed = False
        self.autocommit = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


def _wire(monkeypatch, cur):
    """Point the ledger at a fake connection with the schema 'ready'."""
    m = _ledger()
    monkeypatch.setattr(m, "_db_url", lambda: "postgres://fake")
    monkeypatch.setattr(m, "ensure_schema", lambda force=False: True)
    conn = _Conn(cur)
    monkeypatch.setattr(m, "_conn", lambda: conn)
    return m, conn


def _sql(cur, verb):
    return [c for c in cur.calls if c[0].upper().startswith(verb)]


# ── (2) the comparator ───────────────────────────────────────────────────

@pytest.mark.parametrize("actual,expected,outcome", [
    (18406, ">= 10", "confirmed"),
    (18406, "< 5", "refuted"),
    ("18,406", ">= 18000", "confirmed"),
    ("18,500+", "== 18,500+", "confirmed"),
    ("18,600+", "== 18,500+", "refuted"),
    ("resolved", "== resolved", "confirmed"),
    ("open", "== resolved", "refuted"),
    ("open", "!= resolved", "confirmed"),
    (0, "!= 0", "refuted"),
    (None, ">= 1", "unobserved"),
    (None, "== resolved", "unobserved"),
    (None, "absent", "confirmed"),
    (3, "absent", "refuted"),
    (3, "present", "confirmed"),
    (None, "present", "refuted"),
    ("not-a-number", ">= 1", "unobserved"),
    (True, "== true", "confirmed"),
])
def test_judge(actual, expected, outcome):
    assert _ledger().judge(actual, expected) == outcome


def test_judge_never_invents_a_verdict_from_a_malformed_expectation():
    assert _ledger().judge(5, "about five") == "unobserved"


@pytest.mark.parametrize("value", [">= 17", "== resolved", "absent", "<5",
                                   "!= 'x'", '== "18,500+"'])
def test_parse_expectation_accepts_every_comparator(value):
    assert _ledger().parse_expectation(value) is not None


@pytest.mark.parametrize("metric,expect", [
    ("linkedin:123 impressions", ("linkedin", "123", "impressions")),
    ("finding:/api/v1/admin/facility-dedup/analyze?country=FR status",
     ("finding", "/api/v1/admin/facility-dedup/analyze?country=FR", "status")),
    ("canon:public.facilities", ("canon", "public.facilities", None)),
    ("get:/api/v1/stats facilities", ("get", "/api/v1/stats", "facilities")),
    ("bogus", None),
    ("sql:select 1", None),
])
def test_parse_metric(metric, expect):
    assert _ledger().parse_metric(metric) == expect


def test_dig_walks_dicts_and_lists():
    dig = _ledger().dig
    assert dig({"public": {"facilities": "18,500+"}}, "public.facilities") == "18,500+"
    assert dig({"rows": [{"n": 3}]}, "rows.0.n") == 3
    assert dig({"rows": []}, "rows.0.n") is None
    assert dig({"a": 1}, "b") is None


# ── (1) no expectation, no row ───────────────────────────────────────────

def _good_claim(**over):
    base = dict(kind="fact", subject="facilities", statement="18,406 buildings",
                expected_metric="get:/api/v1/stats facilities",
                expected_value=">= 18000", horizon_hours=24,
                regime={"basis": "facilities_distinct"})
    base.update(over)
    return base


@pytest.mark.parametrize("over", [
    {"expected_metric": ""},
    {"expected_metric": None},
    {"expected_value": ""},
    {"expected_value": "roughly 18k"},
    {"expected_metric": "sql:select count(*)"},
    {"kind": "wish"},
    {"horizon_hours": 0},
    {"statement": ""},
])
def test_register_refuses_without_a_real_expectation(monkeypatch, over):
    m = _ledger()

    def _never():
        raise AssertionError("a refusal must not touch the database")

    monkeypatch.setattr(m, "_db_url", lambda: "postgres://fake")
    monkeypatch.setattr(m, "ensure_schema", lambda force=False: True)
    monkeypatch.setattr(m, "_conn", _never)
    res = m.register_claim(**_good_claim(**over))
    assert res["ok"] is False and res.get("refused") is True, res
    assert res["error"].startswith("refused:")


def test_register_inserts_a_preregistration_row(monkeypatch):
    cur = _Cur(script=[None, (42,)])     # dedup SELECT -> none; INSERT -> id
    m, conn = _wire(monkeypatch, cur)
    res = m.register_claim(**_good_claim())
    assert res == {"ok": True, "id": 42, "shipped": False}
    ins = _sql(cur, "INSERT")
    assert len(ins) == 1
    sql, params = ins[0]
    assert "ON CONFLICT DO NOTHING" in sql
    assert "verification_criterion" not in sql, (
        "a claim must never enter L16's LLM verify path")
    assert params[0] == m.SOURCE_LAYER == "CLAIM"
    assert "fact" in params and "facilities" in params
    assert "get:/api/v1/stats facilities" in params and ">= 18000" in params
    assert params[-1] is False, "shipped flag must be the last param"
    assert conn.committed == 1 and conn.closed


def test_register_stamps_as_of_into_the_regime(monkeypatch):
    import json
    cur = _Cur(script=[None, (7,)])
    m, _ = _wire(monkeypatch, cur)
    m.register_claim(**_good_claim(regime={"basis": "x"}))
    regime = json.loads(_sql(cur, "INSERT")[0][1][6])
    assert regime["basis"] == "x" and regime["as_of"].endswith("+00:00")


def test_register_dedups_an_open_identical_claim(monkeypatch):
    cur = _Cur(script=[(9,)])            # dedup SELECT finds an open twin
    m, _ = _wire(monkeypatch, cur)
    res = m.register_claim(**_good_claim())
    assert res == {"ok": True, "already": True, "id": 9}
    assert not _sql(cur, "INSERT")


def test_register_is_fail_soft_without_a_database(monkeypatch):
    m = _ledger()
    monkeypatch.setattr(m, "_db_url", lambda: None)
    res = m.register_claim(**_good_claim())
    assert res["ok"] is False and not res.get("refused")


# ── (3) the stamps ───────────────────────────────────────────────────────

def test_stamp_shipped_only_sets_an_unset_clock(monkeypatch):
    cur = _Cur()
    m, conn = _wire(monkeypatch, cur)
    assert m.stamp_shipped(5) is True
    sql, params = _sql(cur, "UPDATE")[0]
    assert "shipped_at = NOW()" in sql and "shipped_at IS NULL" in sql
    assert params == (5, "CLAIM") and conn.committed == 1


def test_stamp_outcome_only_touches_an_open_claim(monkeypatch):
    cur = _Cur()
    m, _ = _wire(monkeypatch, cur)
    assert m.stamp_outcome(5, "refuted", {"why": "x"}) is True
    sql, params = _sql(cur, "UPDATE")[0]
    assert "outcome IS NULL" in sql and "outcome_at = NOW()" in sql
    assert params[0] == "refuted" and params[2] == 5


def test_stamp_outcome_rejects_an_unknown_outcome(monkeypatch):
    cur = _Cur()
    m, _ = _wire(monkeypatch, cur)
    assert m.stamp_outcome(5, "probably", "x") is False
    assert not cur.calls


# ── (2)+(3) the verifier ─────────────────────────────────────────────────

def _due(cid, metric, expected, hours_ago, horizon=24):
    now = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.timezone.utc)
    shipped = now - dt.timedelta(hours=hours_ago)
    return (cid, "fact", f"s{cid}", metric, expected, horizon, shipped, now)


def test_verify_confirms_true_and_refutes_false(monkeypatch):
    rows = [_due(1, "get:/api/v1/stats facilities", ">= 10", 25),
            _due(2, "get:/api/v1/stats facilities", "< 5", 25)]
    cur = _Cur(script=[rows])
    m, _ = _wire(monkeypatch, cur)
    out = m.verify_due_claims(fetch=lambda p: {"facilities": 18406})
    assert out["ok"] and out["due"] == 2 and out["stamped"] == 2
    assert out["outcomes"] == {"confirmed": 1, "refuted": 1}
    ups = _sql(cur, "UPDATE")
    assert [(p[0], p[2]) for _, p in ups] == [("confirmed", 1), ("refuted", 2)]
    assert all("outcome IS NULL" in s for s, _ in ups)
    assert "18406" in ups[0][1][1], "the evidence must carry the actual value"


def test_verify_defers_an_unmeasured_claim_inside_grace(monkeypatch):
    rows = [_due(3, "get:/api/v1/stats facilities", ">= 10", 25, horizon=24)]
    cur = _Cur(script=[rows])
    m, _ = _wire(monkeypatch, cur)
    out = m.verify_due_claims(fetch=lambda p: {})       # instrument failed
    assert out["deferred"] == 1 and out["stamped"] == 0
    assert not _sql(cur, "UPDATE"), "a gap must not become a verdict"
    assert out["results"][0]["deferred_until"]


def test_verify_stamps_unobserved_past_grace(monkeypatch):
    rows = [_due(4, "get:/api/v1/stats facilities", ">= 10", 72, horizon=24)]
    cur = _Cur(script=[rows])
    m, _ = _wire(monkeypatch, cur)
    out = m.verify_due_claims(fetch=lambda p: {})
    assert out["stamped"] == 1 and out["outcomes"] == {"unobserved": 1}
    assert _sql(cur, "UPDATE")[0][1][0] == "unobserved"


def test_verify_selects_only_shipped_due_open_claims(monkeypatch):
    cur = _Cur(script=[[]])
    m, _ = _wire(monkeypatch, cur)
    m.verify_due_claims()
    sel = _sql(cur, "SELECT")[0][0]
    for clause in ("shipped_at IS NOT NULL", "outcome IS NULL",
                   "horizon_hours * INTERVAL '1 hour'", "source_layer = %s"):
        assert clause in sel, clause


def test_finding_resolver_reads_the_radar_writer(monkeypatch):
    m = _ledger()
    cur = _Cur(script=[("resolved", 5, dt.datetime(2026, 8, 22), None)])
    actual, ev = m.resolve_metric(
        "finding:/api/v1/admin/facility-dedup/analyze?country=FR status", cur=cur)
    assert actual == "resolved" and ev["count"] == 5
    assert "brain_findings" in _sql(cur, "SELECT")[0][0]


def test_linkedin_resolver_reports_unmeasured_not_zero(monkeypatch):
    m = _ledger()
    cur = _Cur(script=[(None, None, None, None, None, "urn:li:share:1")])
    actual, ev = m.resolve_metric("linkedin:123 impressions", cur=cur)
    assert actual is None and ev["status"] == "not_measured_yet"
    cur = _Cur(script=[(40, 2, 0, 1, dt.datetime(2026, 8, 22), "urn:li:share:1")])
    actual, ev = m.resolve_metric("linkedin:123 impressions", cur=cur)
    assert actual == 40


# ── (4) the must-stay-green control: claims stay out of L16's LLM path ──

def test_l16_llm_verify_still_selects_on_verification_criterion():
    """L16's own predictions keep verifying exactly as before. The ledger
    never sets verification_criterion (asserted on the INSERT above), and
    L16's selector still requires it — executable SQL, not a comment."""
    src = (_ROOT / "routes" / "brain_layer16_self_critique.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_verify_pending")
    sql = " ".join(c.value for c in ast.walk(fn)
                   if isinstance(c, ast.Constant) and isinstance(c.value, str))
    assert "verification_criterion IS NOT NULL" in sql
    assert "WHERE verified_at IS NULL" in sql


def test_claim_source_layer_is_distinct_from_l16_layers():
    assert _ledger().SOURCE_LAYER not in ("L14", "QA", "L15", "L8")


# ── (5) the producers are wired — AST call sites, not comments ──────────

def _calls(tree):
    """(lineno, callee-name) for every call in the tree."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if name:
                out.append((n.lineno, name))
    return out


def _aliases(tree, module, name):
    """Local names bound to `from <module> import <name> [as alias]`."""
    found = {name}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == module:
            for a in n.names:
                if a.name == name:
                    found.add(a.asname or a.name)
    return found


def _fn(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def test_media_registers_before_the_share_and_stamps_after():
    tree = ast.parse((_ROOT / "content_publisher.py").read_text())
    reg = _aliases(tree, "routes.claim_ledger", "register_linkedin_post_claim")
    stamp = _aliases(tree, "routes.claim_ledger", "stamp_shipped")
    calls = _calls(tree)
    reg_lines = [ln for ln, nm in calls if nm in reg]
    stamp_lines = [ln for ln, nm in calls if nm in stamp]
    post_lines = [ln for ln, nm in calls if nm == "_post_to_linkedin"]
    assert reg_lines, "content_publisher never pre-registers a post claim"
    assert stamp_lines, "content_publisher never stamps the claim shipped"
    assert any(r < p < s for r in reg_lines for p in post_lines
               for s in stamp_lines), (
        "expected register_linkedin_post_claim BEFORE _post_to_linkedin and "
        "stamp_shipped AFTER it in the same drain")


def test_squasher_registers_at_enqueue():
    tree = ast.parse((_ROOT / "routes" / "squasher_queue.py").read_text())
    assert any(nm == "_register_fix_claim" for _, nm in _calls(_fn(tree, "enqueue")))
    helper = _fn(tree, "_register_fix_claim")
    assert any(nm == "register_finding_claim" for _, nm in _calls(helper))


def test_canon_registers_inside_resolve_canon():
    tree = ast.parse((_ROOT / "ai_surface_canon.py").read_text())
    names = _aliases(tree, "routes.claim_ledger", "register_canon_claims")
    assert any(nm in names for _, nm in _calls(_fn(tree, "resolve_canon")))


def test_l16_run_calls_the_verifier():
    tree = ast.parse((_ROOT / "routes" / "brain_layer16_self_critique.py").read_text())
    assert any(nm == "_verify_due_claims"
               for _, nm in _calls(_fn(tree, "self_critique_run")))
    assert any(nm == "verify_due_claims"
               for _, nm in _calls(_fn(tree, "_verify_due_claims")))


def test_main_registers_the_blueprint():
    src = (_ROOT / "main.py").read_text()
    tree = ast.parse(src)
    assert any(nm == "register_claim_ledger" for _, nm in _calls(tree)), (
        "main.py never calls register_claim_ledger(app)")


# ── producer contracts ───────────────────────────────────────────────────

def _capture_register(monkeypatch):
    m = _ledger()
    seen = []

    def _fake(**kw):
        seen.append(kw)
        return {"ok": True, "id": len(seen)}

    monkeypatch.setattr(m, "register_claim", _fake)
    return m, seen


def test_finding_claim_is_a_fix_shipped_at_enqueue_with_a_7d_horizon(monkeypatch):
    m, seen = _capture_register(monkeypatch)
    key = "/api/v1/admin/facility-dedup/analyze?country=FR"
    assert m.register_finding_claim(key, "facility_duplicates_unmarked", 256) == 1
    kw = seen[0]
    assert kw["kind"] == "fix" and kw["shipped"] is True
    assert kw["expected_metric"] == f"finding:{key} status"
    assert kw["expected_value"] == "== resolved"
    assert kw["horizon_hours"] == m.FINDING_HORIZON_HOURS == 168
    assert kw["regime"]["queue_id"] == 256


def test_linkedin_claim_bar_is_half_the_30d_baseline_floor_one(monkeypatch):
    m, seen = _capture_register(monkeypatch)
    monkeypatch.setattr(m, "_db_url", lambda: None)     # no baseline read
    assert m.linkedin_expectation(33.8) == 16
    assert m.linkedin_expectation(None) == 1
    assert m.linkedin_expectation(1.2) == 1
    assert m.register_linkedin_post_claim(123, "A post", "https://dchub.cloud/x") == 1
    kw = seen[0]
    assert kw["kind"] == "post" and kw["shipped"] is False
    assert kw["expected_metric"] == "linkedin:123 impressions"
    assert kw["expected_value"] == ">= 1"
    assert kw["horizon_hours"] == m.LINKEDIN_HORIZON_HOURS == 72
    assert kw["regime"]["article_url"] == "https://dchub.cloud/x"


def test_canon_claims_expect_the_resolver_value_and_memoise(monkeypatch):
    m, seen = _capture_register(monkeypatch)
    m._CANON_MEMO.clear()
    pinned = {"public": {"facilities": "18,500+", "deals": "1,800+",
                         "markets": "300+", "countries": "170+"},
              "tools_advertised": 82}
    resolved = {"public": {"facilities": "18,600+", "deals": "1,800+",
                           "markets": "300+", "countries": "170+"},
                "tools_advertised": 82}
    assert m.register_canon_claims(pinned, resolved) == 5
    fac = next(k for k in seen if k["subject"] == "canon:public.facilities")
    assert fac["statement"] == "18,500+" and fac["expected_value"] == "== 18,600+"
    assert fac["expected_metric"] == "canon:public.facilities"
    assert fac["kind"] == "canon" and fac["shipped"] is True
    assert m.register_canon_claims(pinned, resolved) == 0, "memoised per pin"
    pinned["public"]["facilities"] = "18,600+"
    assert m.register_canon_claims(pinned, resolved) == 1, "a pin change re-registers"
    m._CANON_MEMO.clear()


# ── (6) the routes fail closed ───────────────────────────────────────────

flask = pytest.importorskip("flask")

_PATHS = (("get", "/api/v1/brain/claims"),
          ("post", "/api/v1/brain/claims"),
          ("get", "/api/v1/brain/claims/verify"),
          ("post", "/api/v1/brain/claims/verify"))


@pytest.mark.parametrize("method,path", _PATHS)
def test_route_is_401_without_a_credential(monkeypatch, method, path):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "a-real-key-is-set")
    app = flask.Flask(__name__)
    app.register_blueprint(_ledger().claim_ledger_bp)
    resp = getattr(app.test_client(), method)(path)
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path", _PATHS)
def test_route_is_401_when_the_env_has_no_key(monkeypatch, method, path):
    for var in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "INTERNAL_KEY",
                "ADMIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    app = flask.Flask(__name__)
    app.register_blueprint(_ledger().claim_ledger_bp)
    resp = getattr(app.test_client(), method)(path)
    assert resp.status_code == 401


def test_post_refuses_a_claim_without_an_expectation_with_422(monkeypatch):
    m = _ledger()
    monkeypatch.setattr(m, "_authed", lambda: True)
    app = flask.Flask(__name__)
    app.register_blueprint(m.claim_ledger_bp)
    resp = app.test_client().post("/api/v1/brain/claims", json={
        "kind": "fact", "subject": "x", "statement": "y"})
    assert resp.status_code == 422
    assert resp.get_json()["refused"] is True
