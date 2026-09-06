"""tests/test_ops_claims_feed.py — the PUBLIC claims feed (Claim Loop step 5).

What this guards (2026-08-22):
  (1) THE WEEK MATH — `week` is a cohort over shipped_at inside the current
      ISO week; a retracted claim counts as retracted and never as
      refuted_kept; the median event→served latency is NULL below one
      sample (null = not measured, never 0); an open claim is open, not
      unobserved.
  (2) `since` FILTERS ON outcome_at OR shipped_at — executable SQL, with the
      bound value carried twice; garbage is ignored and named.
  (3) THE OUT-IN ANON PROBE SHAPE — with no key at all, a retracted claim
      appears in GET /api/v1/ops/claims (week.retracted=1, refuted_kept=0,
      claims[0].superseded_by) AND in GET /api/v1/changes/since?since=…
      (payload.claims.retracted) — so `get_changes` carries it with no
      mcp-server tool change. Stubbed connections; the live check is in the
      PR body.
  (4) THE KILL SWITCH answers 404, never 5xx (a 5xx fails the site over to
      the stale Render origin), touches no database, and stays no-store.
  (5) THE LEDGER'S retract() overwrites a refutation, keeps the prior verdict
      in the evidence, sets superseded_by, and never re-retracts.
  (6) /brain-live LEADS WITH THE WEEK LINE and renders "0" / "—", never
      hides; self-critical wording is withheld through the SAME pattern the
      media bridge uses (the fallback copy is pinned equal to it).
  (7) WIRING on executable text: main.py registers the blueprint; the page
      calls the feed; the contract gate pins the route.

House rules: no DB, never import main, nothing at module scope but defs.
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib
import json
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_UTC = dt.timezone.utc
_NOW = dt.datetime(2026, 8, 22, 12, 0, tzinfo=_UTC)        # a Saturday
_WS = dt.datetime(2026, 8, 17, 0, 0, tzinfo=_UTC)          # its Monday 00:00Z
_LAST_WEEK = dt.datetime(2026, 8, 15, 9, 0, tzinfo=_UTC)   # previous ISO week


def _oc():
    return importlib.import_module("routes.ops_claims")


def _ledger():
    return importlib.import_module("routes.claim_ledger")


def _cf():
    return importlib.import_module("routes.changes_feed")


def _bv():
    return importlib.import_module("routes.brain_v2_public")


# ── fakes ────────────────────────────────────────────────────────────────

class _DispatchCur:
    """Answers each execute() from the first marker found in the SQL.
    Records every call; supports the cursor context-manager protocol."""

    def __init__(self, rows_by_marker=None, raise_on=None):
        self.calls = []
        self.rows_by_marker = list((rows_by_marker or {}).items())
        self.raise_on = raise_on
        self._last = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        if self.raise_on and self.raise_on in flat:
            raise RuntimeError("simulated read failure")
        self._last = []
        for marker, rows in self.rows_by_marker:
            if marker in flat:
                self._last = list(rows)
                break

    def fetchall(self):
        return list(self._last)

    def fetchone(self):
        return self._last[0] if self._last else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class _ScriptCur:
    """fetchone/fetchall answer from a script, in order (ledger tests)."""

    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.script.pop(0) if self.script else None

    def fetchall(self):
        v = self.script.pop(0) if self.script else None
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


def _claim_row(cid, outcome, shipped_at, outcome_at=None, as_of=None,
               superseded_by=None, kind="fix", subject="canon:public.markets",
               statement="the literal claim text"):
    """A raw _COLS tuple, as the cursor would hand it over."""
    regime = {"as_of": as_of.isoformat()} if as_of else {"basis": "x"}
    return (cid, kind, subject, statement, regime, shipped_at, outcome,
            outcome_at, superseded_by)


def _claims(*rows):
    """Decoded rows — what read_feed hands week_stats."""
    return [_oc().row_to_claim(r) for r in rows]


def _wire_feed(monkeypatch, cur):
    """Point the feed's ledger plumbing at a fake connection, freeze now,
    and clear the per-process detector cache."""
    oc, L = _oc(), _ledger()
    monkeypatch.setattr(L, "_db_url", lambda: "postgres://fake")
    monkeypatch.setattr(L, "ensure_schema", lambda force=False: True)
    conn = _Conn(cur)
    monkeypatch.setattr(L, "_conn", lambda: conn)
    monkeypatch.setattr(oc, "utcnow", lambda: _NOW)
    monkeypatch.setattr(oc, "_DETECTOR_CACHE", {"key": None, "at": 0.0, "value": None})
    monkeypatch.delenv("OPS_CLAIMS_DISABLE", raising=False)
    monkeypatch.delenv("OPS_CLAIMS_DETECTOR_MODULE", raising=False)
    return oc, conn


def _app(*blueprints):
    from flask import Flask
    app = Flask("ops-claims-test")
    app.config["TESTING"] = True
    for bp in blueprints:
        app.register_blueprint(bp)
    return app


def _sql(cur, marker):
    return [c for c in cur.calls if marker in c[0]]


# ── (1) the week math ────────────────────────────────────────────────────

def test_week_bounds_is_monday_to_monday_utc():
    ws, we = _oc().week_bounds(_NOW)
    assert ws == _WS and we == _WS + dt.timedelta(days=7)
    # Monday 00:00:00Z itself belongs to its own week.
    ws2, _ = _oc().week_bounds(_WS)
    assert ws2 == _WS


def test_median_is_null_below_one_sample_and_a_value_from_one():
    oc = _oc()
    shipped = _WS + dt.timedelta(days=1)
    none = oc.week_stats(_claims(_claim_row(1, None, shipped)), _WS, _NOW)
    assert none["shipped"] == 1
    assert none["median_event_to_served_hours"] is None, \
        "no as_of → no sample → null, never 0"
    assert none["median_event_to_served_samples"] == 0
    one = oc.week_stats(
        _claims(_claim_row(1, None, shipped, as_of=shipped - dt.timedelta(hours=3))),
        _WS, _NOW)
    assert one["median_event_to_served_hours"] == 3.0
    assert one["median_event_to_served_samples"] == 1
    three = oc.week_stats(_claims(
        _claim_row(1, None, shipped, as_of=shipped - dt.timedelta(hours=1)),
        _claim_row(2, None, shipped, as_of=shipped - dt.timedelta(hours=10)),
        _claim_row(3, None, shipped, as_of=shipped - dt.timedelta(hours=4)),
    ), _WS, _NOW)
    assert three["median_event_to_served_hours"] == 4.0


def test_a_retracted_claim_is_retracted_and_never_refuted_kept():
    oc = _oc()
    shipped = _WS + dt.timedelta(days=2)
    judged = shipped + dt.timedelta(days=1)
    week = oc.week_stats(_claims(
        _claim_row(1, "refuted", shipped, judged),
        _claim_row(2, "retracted", shipped, judged, superseded_by=3),
        _claim_row(3, "confirmed", shipped, judged),
        _claim_row(4, "unobserved", shipped, judged),
        _claim_row(5, None, shipped),
    ), _WS, _NOW)
    assert week["shipped"] == 5
    assert week["refuted_kept"] == 1
    assert week["retracted"] == 1
    assert week["confirmed"] == 1
    assert week["unobserved"] == 1
    assert week["open"] == 1
    assert (week["confirmed"] + week["refuted_kept"] + week["retracted"]
            + week["unobserved"] + week["open"]) == week["shipped"]


def test_the_week_is_a_cohort_over_shipped_at():
    """A claim shipped LAST week and confirmed THIS week belongs to last
    week's cohort — it is in neither `shipped` nor `confirmed` here; one
    shipped after `now` (clock skew) is excluded too."""
    oc = _oc()
    week = oc.week_stats(_claims(
        _claim_row(1, "confirmed", _LAST_WEEK, _WS + dt.timedelta(days=1)),
        _claim_row(2, "confirmed", _WS + dt.timedelta(days=1), _NOW),
        _claim_row(3, None, _NOW + dt.timedelta(hours=1)),
        _claim_row(4, None, None),
    ), _WS, _NOW)
    assert week["shipped"] == 1 and week["confirmed"] == 1
    assert week["week_start"] == _WS.isoformat()
    assert week["as_of"] == _NOW.isoformat()


# ── (2) since ────────────────────────────────────────────────────────────

def test_claims_sql_filters_on_outcome_at_or_shipped_at():
    oc = _oc()
    since = dt.datetime(2026, 8, 20, tzinfo=_UTC)
    sql, params = oc.claims_sql("CLAIM", since, 50)
    flat = " ".join(sql.split())
    assert "(outcome_at >= %s OR shipped_at >= %s)" in flat
    assert "shipped_at IS NOT NULL" in flat, "unshipped claims are not public"
    assert params == ("CLAIM", since, since, 50)
    sql0, params0 = oc.claims_sql("CLAIM", None, 7)
    assert "outcome_at >=" not in sql0 and params0 == ("CLAIM", 7)


@pytest.mark.parametrize("raw,expect_dt,mode", [
    ("2026-08-20T00:00:00Z", dt.datetime(2026, 8, 20, tzinfo=_UTC), "iso"),
    ("2026-08-20T00:00:00+00:00", dt.datetime(2026, 8, 20, tzinfo=_UTC), "iso"),
    ("", None, "none"),
    (None, None, "none"),
    ("yesterday", None, "ignored: unparseable (use ISO-8601)"),
])
def test_parse_since(raw, expect_dt, mode):
    got, got_mode = _oc().parse_since(raw)
    assert got == expect_dt and got_mode == mode


def test_limit_is_clamped_to_1_200_default_50():
    oc = _oc()
    assert oc.clamp_limit(None) == 50
    assert oc.clamp_limit("garbage") == 50
    assert oc.clamp_limit("0") == 1
    assert oc.clamp_limit("9999") == 200
    assert oc.clamp_limit("25") == 25


# ── (3) the out-in ANON probe shape ─────────────────────────────────────

def _retracted_rows():
    shipped = _WS + dt.timedelta(days=1, hours=2)
    judged = shipped + dt.timedelta(days=1)
    return [_claim_row(123, "retracted", shipped, judged,
                       as_of=shipped - dt.timedelta(hours=2),
                       superseded_by=124, subject="finding:x",
                       statement="the old claim")]


def test_anon_get_ops_claims_carries_a_retracted_claim(monkeypatch):
    cur = _DispatchCur({
        "to_regclass('public.brain_action_classes')": [(None,)],
        "brain_predictions_log": _retracted_rows(),
    })
    oc, conn = _wire_feed(monkeypatch, cur)
    with _app(oc.ops_claims_bp).test_client() as c:
        rv = c.get("/api/v1/ops/claims")       # no key, no header — ANON
    assert rv.status_code == 200
    # ★2026-09-06: was `"no-store" in Cache-Control`. A SUCCESSFUL read is now
    # briefly shareable so a cold origin hit is absorbed once instead of by every
    # visitor who lands on it. Browsers still revalidate (max-age=0), so a human
    # refreshing never sees a cached verdict; only shared caches may reuse, and
    # the payload carries generated_at/as_of so a reused copy is a timestamped
    # snapshot rather than an undetectable stale read.
    cc = rv.headers.get("Cache-Control", "")
    assert "no-store" not in cc, cc
    assert "max-age=0" in cc and "must-revalidate" in cc, cc
    assert "s-maxage=" in cc, cc
    body = rv.get_json()
    assert body["ok"] is True
    week = body["week"]
    assert week["retracted"] == 1 and week["refuted_kept"] == 0
    assert week["shipped"] == 1 and week["confirmed"] == 0
    assert week["median_event_to_served_hours"] == 2.0
    assert week["granted_action_classes"] == 0
    assert week["granted_action_classes_basis"] == "table absent"
    # Step 4 (#3054) put brain_pr_carries_detector at the default module, so
    # the OPTIONAL field is PRESENT: {with_detector, checked, unknown, prs,
    # basis}. with_detector is an int or None — None = not measured. The fake
    # cursor has no brain_merge_reconciliation, so this is the absent-table
    # shape, and it must never read as a zero. The control below proves the
    # field disappears again when the predicate does not import.
    from routes.brain_pr_detector_gate import brain_pr_carries_detector
    assert oc._detector_predicate() is brain_pr_carries_detector, \
        "the feed must resolve step 4's predicate from its default module"
    assert "brain_prs_with_detector" in week, \
        "step 4's predicate imports → the field is PRESENT"
    det = week["brain_prs_with_detector"]
    assert set(det) == {"with_detector", "checked", "unknown", "prs", "basis"}
    assert det["with_detector"] is None or isinstance(det["with_detector"], int)
    assert det["with_detector"] is None and det["prs"] is None, \
        "no brain_merge_reconciliation table = not measured, never 0"
    assert det["checked"] == 0 and det["unknown"] == 0
    assert det["basis"] == "brain_merge_reconciliation absent"
    claim = body["claims"][0]
    assert claim["id"] == 123 and claim["outcome"] == "retracted"
    assert claim["superseded_by"] == 124
    assert claim["statement"] == "the old claim", "statement is literal, nothing stripped"
    assert isinstance(claim["shipped_at"], str) and isinstance(claim["outcome_at"], str)
    assert body["count"] == 1 and body["limit"] == 50 and body["since"] is None
    assert conn.closed


def test_the_detector_field_is_absent_when_the_predicate_does_not_import(monkeypatch):
    """CONTROL for the presence assertion above: an absent instrument is an
    absent field, never a zero. The env knob points at a module that does
    not exist, which is exactly the pre-step-4 state."""
    cur = _DispatchCur({
        "to_regclass('public.brain_action_classes')": [(None,)],
        "brain_predictions_log": _retracted_rows(),
    })
    oc, _conn = _wire_feed(monkeypatch, cur)
    monkeypatch.setenv("OPS_CLAIMS_DETECTOR_MODULE", "routes.no_such_step4_module_xyz")
    assert oc._detector_predicate() is None
    with _app(oc.ops_claims_bp).test_client() as c:
        body = c.get("/api/v1/ops/claims").get_json()
    assert body["ok"] is True
    assert "brain_prs_with_detector" not in body["week"], \
        "predicate not importable → the field is OMITTED"


def test_the_response_documents_its_own_shape(monkeypatch):
    """Deadman-style: every served key is named in `shape` — a field that
    is served but undocumented is the guessing game the shape exists to end."""
    cur = _DispatchCur({
        "to_regclass('public.brain_action_classes')": [(None,)],
        "brain_predictions_log": _retracted_rows(),
    })
    oc, _ = _wire_feed(monkeypatch, cur)
    with _app(oc.ops_claims_bp).test_client() as c:
        body = c.get("/api/v1/ops/claims").get_json()
    shape = body["shape"]
    for k in body:
        assert k in shape["top"], f"top-level {k!r} is served but not in shape.top"
    for k in body["week"]:
        assert k in shape["week"], f"week.{k} is served but not documented"
    for k in body["claims"][0]:
        assert k in shape["claim_fields"], f"claim.{k} is served but not documented"
    assert shape["kill_switch"].startswith("OPS_CLAIMS_DISABLE=1")


def test_since_is_bound_twice_and_echoed(monkeypatch):
    cur = _DispatchCur({"to_regclass": [(None,)], "brain_predictions_log": []})
    oc, _ = _wire_feed(monkeypatch, cur)
    with _app(oc.ops_claims_bp).test_client() as c:
        body = c.get("/api/v1/ops/claims?since=2026-08-20T00:00:00Z&limit=5").get_json()
    assert body["since_mode"] == "iso" and body["since"].startswith("2026-08-20")
    feed_sql, params = [x for x in _sql(cur, "brain_predictions_log")
                        if "outcome_at >=" in x[0]][0]
    assert "(outcome_at >= %s OR shipped_at >= %s)" in feed_sql
    assert params[1] == params[2] == dt.datetime(2026, 8, 20, tzinfo=_UTC)
    assert params[-1] == 5
    with _app(oc.ops_claims_bp).test_client() as c:
        body = c.get("/api/v1/ops/claims?since=yesterday").get_json()
    assert body["since"] is None and body["since_mode"].startswith("ignored")


def test_granted_action_classes_reads_step_2s_table_when_present(monkeypatch):
    cur = _DispatchCur({
        "to_regclass('public.brain_action_classes')": [("brain_action_classes",)],
        "COUNT(*) FROM brain_action_classes WHERE granted": [(2,)],
        "brain_predictions_log": [],
    })
    oc, _ = _wire_feed(monkeypatch, cur)
    feed = oc.read_feed()
    assert feed["week"]["granted_action_classes"] == 2
    assert "granted = TRUE" in feed["week"]["granted_action_classes_basis"]


def test_a_failed_ledger_read_is_null_not_zero(monkeypatch):
    cur = _DispatchCur(raise_on="brain_predictions_log")
    oc, _ = _wire_feed(monkeypatch, cur)
    with _app(oc.ops_claims_bp).test_client() as c:
        rv = c.get("/api/v1/ops/claims")
    assert rv.status_code == 200, "a read failure must not 5xx a keyless route"
    body = rv.get_json()
    assert body["ok"] is False and body["week"] is None and body["claims"] is None
    assert body["count"] is None and "ledger unavailable" in body["basis"]


def test_anon_changes_since_carries_the_retraction(monkeypatch):
    cf = _cf()
    judged = _WS + dt.timedelta(days=2)
    cur = _DispatchCur({
        "brain_predictions_log": [(123, "fix", "finding:x", "the old claim",
                                   "retracted", judged, 124)],
    })
    conn = _Conn(cur)
    monkeypatch.setattr(cf, "open_conn", lambda *a, **k: conn)
    with _app(cf.changes_feed_bp).test_client() as c:
        rv = c.get("/api/v1/changes/since?since=2026-08-20T00:00:00Z")
    assert rv.status_code == 200
    body = rv.get_json()
    claims = body["claims"]
    assert claims["count"] == 1
    assert claims["retracted"][0]["id"] == 123
    assert claims["retracted"][0]["superseded_by"] == 124
    assert claims["confirmed"] == []
    assert body["drill_deeper"]["claims_full"] == "/api/v1/ops/claims"
    sql, params = _sql(cur, "brain_predictions_log")[0]
    assert "outcome_at >= %s" in sql and "IN ('confirmed', 'retracted')" in sql
    assert params[0] == "CLAIM" and params[1] == dt.datetime(2026, 8, 20, tzinfo=_UTC)
    # The block is additive: the lanes and their counts are untouched.
    assert "counts" in body and "diff" in body


def test_changes_since_is_fail_soft_when_the_ledger_cannot_be_read(monkeypatch):
    cf = _cf()
    cur = _DispatchCur(raise_on="brain_predictions_log")
    monkeypatch.setattr(cf, "open_conn", lambda *a, **k: _Conn(cur))
    with _app(cf.changes_feed_bp).test_client() as c:
        body = c.get("/api/v1/changes/since?since=24h").get_json()
    assert body["claims"]["count"] == 0
    assert body["claims"]["basis"] == "ledger unavailable"
    assert "confirmed" not in body["claims"], "an unreadable ledger publishes no lists"


# ── (4) the kill switch ──────────────────────────────────────────────────

def test_kill_switch_is_404_no_store_and_touches_no_database(monkeypatch):
    oc, L = _oc(), _ledger()
    monkeypatch.setenv("OPS_CLAIMS_DISABLE", "1")

    def _never():
        raise AssertionError("a disabled feed must not open a connection")

    monkeypatch.setattr(L, "_db_url", lambda: "postgres://fake")
    monkeypatch.setattr(L, "_conn", _never)
    with _app(oc.ops_claims_bp).test_client() as c:
        rv = c.get("/api/v1/ops/claims")
    assert rv.status_code == 404
    assert "no-store" in rv.headers.get("Cache-Control", "")
    assert rv.get_json()["ok"] is False


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_the_exact_value_1_disables(monkeypatch, value):
    monkeypatch.setenv("OPS_CLAIMS_DISABLE", value)
    assert _oc()._disabled() is False


def _kill_status_codes(src: str) -> list:
    """Every integer literal returned inside an `if _disabled():` block —
    the same AST walk tests/test_shell_killswitch_never_5xx.py applies to
    the master shells."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            out += [n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, int)]
    return out


def test_kill_switch_never_returns_5xx_in_source():
    codes = _kill_status_codes((_ROOT / "routes" / "ops_claims.py").read_text())
    assert codes, "no `if _disabled():` block found — the switch is not wired"
    assert 404 in codes
    assert not [c for c in codes if c >= 500]


# ── (5) retract() in the ledger ──────────────────────────────────────────

def _wire_ledger(monkeypatch, cur):
    m = _ledger()
    monkeypatch.setattr(m, "_db_url", lambda: "postgres://fake")
    monkeypatch.setattr(m, "ensure_schema", lambda force=False: True)
    conn = _Conn(cur)
    monkeypatch.setattr(m, "_conn", lambda: conn)
    return m, conn


def test_retract_overwrites_a_refutation_and_keeps_the_prior_verdict(monkeypatch):
    judged = _WS + dt.timedelta(days=1)
    cur = _ScriptCur(script=[("refuted", judged, None)])
    m, conn = _wire_ledger(monkeypatch, cur)
    res = m.retract(123, "basis changed: the resolver moved", superseded_by=124)
    assert res == {"ok": True, "id": 123, "prior_outcome": "refuted",
                   "superseded_by": 124}
    (sql, params), = [c for c in cur.calls if c[0].startswith("UPDATE")]
    assert "outcome = 'retracted'" in sql
    assert "superseded_by = COALESCE(%s, superseded_by)" in sql
    assert "(outcome IS NULL OR outcome <> 'retracted')" in sql, \
        "a retraction may overwrite a verdict but never re-retract"
    evidence = json.loads(params[0])
    assert evidence["prior_outcome"] == "refuted"
    assert evidence["prior_outcome_at"] == judged.isoformat()
    assert evidence["reason"].startswith("basis changed")
    assert params[1] == 124 and params[2] == 123 and params[3] == "CLAIM"
    assert conn.committed == 1 and conn.closed


def test_retract_is_idempotent_and_refuses_malformed_calls(monkeypatch):
    cur = _ScriptCur(script=[("retracted", _NOW, 124)])
    m, _ = _wire_ledger(monkeypatch, cur)
    assert m.retract(123, "again") == {"ok": True, "already": True, "id": 123,
                                       "superseded_by": 124}
    assert not [c for c in cur.calls if c[0].startswith("UPDATE")]

    cur2 = _ScriptCur(script=[None])
    m, _ = _wire_ledger(monkeypatch, cur2)
    assert m.retract(999, "x") == {"ok": False, "error": "no such claim", "id": 999}

    def _never():
        raise AssertionError("a refusal must not touch the database")

    monkeypatch.setattr(m, "_conn", _never)
    for bad in ((5, ""), (5, "   "), (5, "x", 5), ("abc", "x"), (5, "x", "y")):
        res = m.retract(*bad)
        assert res["ok"] is False and res.get("refused") is True, (bad, res)


def test_superseded_by_is_an_ensured_column():
    m = _ledger()
    assert ("superseded_by", "INTEGER") in m.CLAIM_COLUMNS
    tree = ast.parse((_ROOT / "routes" / "claim_ledger.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "ensure_schema")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)
             and getattr(n.iter, "id", None) == "CLAIM_COLUMNS"]
    assert loops, "ensure_schema must ADD COLUMN IF NOT EXISTS every CLAIM_COLUMNS entry"
    assert "IF NOT EXISTS" in " ".join(
        c.value for c in ast.walk(loops[0])
        if isinstance(c, ast.Constant) and isinstance(c.value, str))


def test_admin_retract_route_is_401_without_a_credential(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-test")
    m = _ledger()
    with _app(m.claim_ledger_bp).test_client() as c:
        rv = c.post("/api/v1/brain/claims/retract", json={"id": 1, "reason": "x"})
    assert rv.status_code == 401


# ── (6) /brain-live ──────────────────────────────────────────────────────

def _feed(week_over=None, claims=None, ok=True):
    week = {"week_start": _WS.isoformat(), "week_end": (_WS + dt.timedelta(days=7)).isoformat(),
            "as_of": _NOW.isoformat(), "shipped": 0, "confirmed": 0, "refuted_kept": 0,
            "retracted": 0, "unobserved": 0, "open": 0,
            "median_event_to_served_hours": None, "median_event_to_served_samples": 0,
            "granted_action_classes": 0, "granted_action_classes_basis": "table absent"}
    week.update(week_over or {})
    return {"ok": ok, "week": week, "claims": claims or []}


def test_brain_live_headline_renders_zero_not_blank():
    html = _bv()._claims_section_html(_feed())
    assert "0 claims confirmed at horizon this week · as of 2026-08-22" in html
    assert "0 shipped" in html and "0 retracted" in html


def test_brain_live_headline_renders_a_dash_when_the_ledger_is_unreadable():
    bv = _bv()
    for feed in (None, {"ok": False, "week": None, "claims": None}):
        html = bv._claims_section_html(feed)
        assert "— claims confirmed at horizon this week · as of " in html
        assert "not readable" in html
        assert "0 claims confirmed" not in html, "unreadable must not read as zero"


def test_brain_live_singular_and_counts():
    html = _bv()._claims_section_html(_feed({"confirmed": 1, "shipped": 3, "open": 2}))
    assert "1 claim confirmed at horizon this week" in html
    assert "3 shipped" in html and "2 awaiting horizon" in html


def test_brain_live_withholds_self_critical_wording_but_keeps_the_row():
    bv = _bv()
    claims = [
        {"id": 1, "kind": "fix", "subject": "finding:/api/v1/admin/facility-dedup/analyze?country=US",
         "statement": "facility duplicates resolved within 168h", "outcome": "refuted",
         "outcome_at": "2026-08-21T04:00:00+00:00", "shipped_at": "2026-08-20T04:00:00+00:00"},
        {"id": 2, "kind": "canon", "subject": "canon:public.markets", "statement": "the literal claim text",
         "outcome": "confirmed", "outcome_at": "2026-08-21T05:00:00+00:00",
         "shipped_at": "2026-08-20T05:00:00+00:00"},
        {"id": 3, "kind": "post", "subject": "social_media_posts:9", "statement": "a post",
         "outcome": None, "outcome_at": None, "shipped_at": "2026-08-22T01:00:00+00:00"},
    ]
    html = bv._claims_section_html(_feed({"shipped": 3}, claims))
    assert bv._WITHHELD in html and "dedup" not in html, \
        "self-critical wording must be withheld on the public page"
    assert "canon:public.markets" in html and "the literal claim text" in html
    assert ">refuted<" in html, "the outcome is a fact and is never hidden"
    assert ">open<" in html and "2026-08-22 01:00" in html
    assert html.count("<tr><td>") == 3, "the withheld claim keeps its row"


def test_brain_live_fallback_pattern_equals_the_media_bridge_pattern():
    """The page imports the bridge's _SELF_CRITICAL; the fallback is for an
    import failure only and must not drift from it."""
    bv = _bv()
    bridge = importlib.import_module("routes.brain_media_bridge")
    assert bv._SELF_CRITICAL_FALLBACK.pattern == bridge._SELF_CRITICAL.pattern
    assert bv._SELF_CRITICAL_FALLBACK.flags == bridge._SELF_CRITICAL.flags
    assert bv._SELF_CRITICAL_RE is bridge._SELF_CRITICAL


def test_brain_live_shows_at_most_ten_claims():
    claims = [{"id": i, "kind": "canon", "subject": f"canon:k{i}", "statement": "s",
               "outcome": "confirmed", "outcome_at": "2026-08-21T05:00:00+00:00",
               "shipped_at": "2026-08-20T05:00:00+00:00"} for i in range(14)]
    html = _bv()._claims_section_html(_feed({"shipped": 14}, claims))
    assert html.count("<tr><td>") == 10


# ── (7) wiring — executable text, not comments ──────────────────────────

def _calls(tree):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if name:
                out.append(name)
    return out


def _fn(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def test_main_registers_the_public_feed():
    tree = ast.parse((_ROOT / "main.py").read_text())
    assert "register_ops_claims" in _calls(tree), \
        "main.py never calls register_ops_claims(app)"


def test_the_route_is_exactly_the_ops_prefix():
    app = _app(_oc().ops_claims_bp)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/ops/claims" in rules
    assert not [r for r in rules if r.startswith("/api/v1/") and not r.startswith("/api/v1/ops/")], \
        "no new prefix: /api/v1/ops/* is the edge-bypassed one"


def test_brain_live_calls_the_feed_and_the_section():
    tree = ast.parse((_ROOT / "routes" / "brain_v2_public.py").read_text())
    calls = _calls(_fn(tree, "brain_public_page"))
    assert "_claims_section_html" in calls and "read_feed" in calls


def test_changes_since_calls_the_claims_block_inside_the_read():
    tree = ast.parse((_ROOT / "routes" / "changes_feed.py").read_text())
    assert "_claims_block" in _calls(_fn(tree, "changes_since"))
    helper = _fn(tree, "_claims_block")
    assert "try_fetchall" in _calls(helper), "the block must use the honest read helper"


def test_contract_gate_pins_the_route():
    contract = json.loads((_ROOT / "tests" / "app_contract.json").read_text())
    assert "/api/v1/ops/claims" in contract["contract_routes"]


def test_detector_field_is_built_from_step_4s_predicate_when_importable(monkeypatch):
    oc = _oc()
    fake = types.ModuleType("_fake_step4_detector")
    fake.brain_pr_carries_detector = lambda pr: (None if pr == 3012 else pr % 2 == 0)
    monkeypatch.setitem(sys.modules, "_fake_step4_detector", fake)
    cur = _DispatchCur({
        "to_regclass('public.brain_merge_reconciliation')": [("brain_merge_reconciliation",)],
        "FROM brain_merge_reconciliation": [(3010,), (3011,), (3012,)],
    })
    monkeypatch.setenv("OPS_CLAIMS_DETECTOR_MODULE", "_fake_step4_detector")
    got = oc.brain_prs_with_detector(cur, _WS, _NOW)
    assert got["with_detector"] == 1 and got["checked"] == 2 and got["unknown"] == 1
    assert got["prs"] == 3
    monkeypatch.setenv("OPS_CLAIMS_DETECTOR_MODULE", "routes.this_module_does_not_exist")
    assert oc.brain_prs_with_detector(cur, _WS, _NOW) is None


# ── the detector step must never dominate the endpoint (2026-09-06) ─────────
# brain_pr_carries_detector -> evaluate_pr_remote calls the GitHub API per PR
# with its own 10s default timeout, and this endpoint called it for up to 10 PRs
# SEQUENTIALLY on the request path of a keyless feed the homepage fetches on
# load. Worst case 100s against a 15s edge route timeout. Measured on production
# before the fix: 7.62s and 11.41s cold, ~0.8s warm (the 600s per-process cache),
# and the homepage strip — which aborted at 6s and only asked once — went blank
# for that visitor's whole session.
def _detector_cur(n_prs=10):
    class _Cur:
        def execute(self, q, *a): self._q = q
        def fetchone(self): return ("brain_merge_reconciliation",)
        def fetchall(self): return [(i,) for i in range(n_prs)]
    return _Cur()


def _week_now():
    import datetime as _d
    return (_d.datetime(2026, 8, 31, tzinfo=_d.timezone.utc),
            _d.datetime(2026, 9, 6, tzinfo=_d.timezone.utc))


def test_a_slow_detector_cannot_run_away_with_the_request():
    """A predicate that sleeps its whole timeout must still be bounded."""
    import time
    from routes import ops_claims as oc
    ws, now = _week_now()

    def slow(pr, **kw):
        time.sleep(kw.get("timeout") or 10)
        return None

    t0 = time.monotonic()
    out = oc.brain_prs_with_detector(_detector_cur(), ws, now, predicate=slow)
    elapsed = time.monotonic() - t0

    ceiling = oc._DETECTOR_BUDGET_S + oc._DETECTOR_HTTP_TIMEOUT_S + 1.0
    assert elapsed < ceiling, (
        f"the detector step took {elapsed:.2f}s against a {ceiling:.1f}s ceiling. "
        f"Unbounded this is 10 PRs x the rule's 10s default = 100s, on the request "
        f"path of a keyless feed, behind a 15s edge timeout.")
    # Not attempted is UNKNOWN, never silently dropped and never counted as checked.
    assert out["prs"] == 10 and out["checked"] == 0 and out["unknown"] == 10
    assert "not attempted" in out["basis"], (
        "the basis must say how many PRs were skipped — 'we asked and could not "
        f"tell' and 'we did not ask' are different answers. basis={out['basis']!r}")


def test_the_budget_stops_ISSUING_calls_not_merely_waiting():
    """A deadline checked AFTER each call still pays for every call."""
    import time
    from routes import ops_claims as oc
    ws, now = _week_now()
    calls = []

    def slow(pr, **kw):
        calls.append(pr)
        time.sleep(kw.get("timeout") or 10)
        return None

    oc.brain_prs_with_detector(_detector_cur(), ws, now, predicate=slow)
    assert len(calls) < 10, (
        f"every one of the 10 PRs was still called ({len(calls)}) — the budget is "
        f"being checked after the work instead of before it")


def test_the_short_http_timeout_is_actually_passed_down():
    """The rule's own default is 10s; one call must not outlast the budget."""
    from routes import ops_claims as oc
    ws, now = _week_now()
    seen = []

    def probe(pr, **kw):
        seen.append(kw.get("timeout"))
        return None

    oc.brain_prs_with_detector(_detector_cur(3), ws, now, predicate=probe)
    assert seen and all(t == oc._DETECTOR_HTTP_TIMEOUT_S for t in seen), (
        f"predicate received timeout={seen!r}; without it the FIRST call can "
        f"outlast any total budget on its own")


def test_a_predicate_without_a_timeout_kwarg_still_works():
    """Probed, not assumed — and never called twice to find out."""
    from routes import ops_claims as oc
    ws, now = _week_now()
    calls = []

    def no_timeout(pr):          # positional only, no **kw
        calls.append(pr)
        return True

    out = oc.brain_prs_with_detector(_detector_cur(3), ws, now, predicate=no_timeout)
    assert out["checked"] == 3 and out["with_detector"] == 3
    assert calls == [0, 1, 2], f"predicate called {calls} — expected each PR once"


def test_a_failed_read_is_never_cached(monkeypatch):
    """ok=false must not be pinned at the edge while the ledger is already fine.

    The success path is briefly shareable; a FAILURE answered 200 with ok=false
    is the one shape where reuse is actively harmful, because the caller cannot
    distinguish "the ledger is down" from "it was down a minute ago".
    """
    from routes import ops_claims as oc
    monkeypatch.setattr(oc, "read_feed",
                        lambda **kw: {"ok": False, "error": "no database",
                                      "week": None, "claims": None})
    with _app(oc.ops_claims_bp).test_client() as c:
        rv = c.get("/api/v1/ops/claims")
    assert rv.status_code == 200 and rv.get_json()["ok"] is False
    cc = rv.headers.get("Cache-Control", "")
    assert "no-store" in cc, f"a failed read is cacheable: {cc!r}"
    assert "s-maxage" not in cc, cc
