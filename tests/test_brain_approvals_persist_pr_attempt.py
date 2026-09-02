"""brain_approvals persists the approve→PR outcome, the master tick re-drives
FAILED attempts, and the radar reports approvals that stay PR-less.

Measured 2026-09-02 00:30Z (brain-agents sweep finding 2): 40 approved rows,
5 with a merged PR, #100416 known lost to `claude call failed: http_429`
during the gateway spend outage, the rest UNKNOWABLE — the table held only
kind/item_id/decision and `pr_attempt` lived in the HTTP response.
"""
from __future__ import annotations

import inspect
import json

import pytest

dash = pytest.importorskip("routes.brain_innovation_dashboard")


# ── stub DB (same shape as tests/test_squasher_action_classes._Cur) ──────────
class _Cur:
    def __init__(self, answers=None, events=None, rowcount=1):
        self.answers = answers or {}
        self.events = events if events is not None else []
        self.calls = []
        self.rowcount = rowcount
        self._last = ""

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.calls.append((flat, params))
        self.events.append(("SQL", flat, params))
        self._last = flat

    def _rows(self):
        for key, rows in self.answers.items():
            if key in self._last:
                return rows() if callable(rows) else rows
        return []

    def fetchone(self):
        r = self._rows()
        return r[0] if r else None

    def fetchall(self):
        return list(self._rows())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1
        self.cur.events.append(("COMMIT", "", None))

    def rollback(self):
        self.cur.events.append(("ROLLBACK", "", None))

    def close(self):
        pass


_ALL_COLS = [("kind",), ("item_id",), ("decision",), ("note",), ("approved_at",),
             ("pr_attempt",), ("pr_url",), ("pr_attempted_at",), ("pr_redrives",)]


def _sqls(cur, needle):
    return [(s, p) for s, p in cur.calls if needle in s]


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    monkeypatch.setattr(dash, "_APPROVAL_COLUMNS_ENSURED", False)


# ── 1. the PR url of an attempt, per producer shape ─────────────────────────
def test_code_pr_url_is_read_from_the_nested_opener_envelope():
    att = {"ok": True, "acted": True,
           "pr": {"ok": True, "pr": {"number": 3555, "url": "https://github.com/x/y/pull/3555"}}}
    assert dash._pr_url_of(att) == "https://github.com/x/y/pull/3555"


def test_spec_pr_fallback_url_is_found():
    att = {"ok": False, "acted": True, "note": "filed as draft spec PR for a human",
           "fallback_spec_pr": {"ok": True, "acted": True,
                                "pr": {"number": 3556, "html_url": "https://github.com/x/y/pull/3556"}}}
    assert dash._pr_url_of(att) == "https://github.com/x/y/pull/3556"


@pytest.mark.parametrize("att", [
    {"ok": True, "acted": False, "refused": True},
    {"ok": False, "error": "claude call failed: http_429"},
    {"ok": True, "acted": False, "pr": {"url": "https://github.com/x/y/pull/1"}},  # not acted → no PR
    None, "", 0,
])
def test_no_pr_without_acted(att):
    assert dash._pr_url_of(att) is None


# ── 2. which outcomes the redrive re-runs ───────────────────────────────────
def test_only_a_failed_attempt_is_redriven():
    assert dash._redrive_wanted({"ok": False, "error": "claude call failed: http_429"}) is True
    assert dash._redrive_wanted({"ok": False, "error": "autonomy_gate_closed"}) is True
    assert dash._redrive_wanted({"ok": True, "acted": False, "refused": True}) is False
    assert dash._redrive_wanted({"ok": True, "acted": False, "note": "recorded only"}) is False
    assert dash._redrive_wanted(None) is False
    assert dash._redrive_wanted({}) is False


# ── 3. the columns are added only when absent ───────────────────────────────
def test_ensure_adds_only_the_missing_columns():
    cur = _Cur({"information_schema.columns": [("kind",), ("item_id",), ("decision",),
                                               ("note",), ("approved_at",), ("pr_url",)]})
    assert dash._ensure_approval_columns(cur) is True
    alters = [s for s, _ in cur.calls if s.startswith("ALTER TABLE brain_approvals")]
    assert len(alters) == 3, alters
    assert not any("pr_url" in a.split("ADD COLUMN")[1] for a in alters)
    assert any("ADD COLUMN pr_attempt JSONB" in a for a in alters)


def test_ensure_is_a_noop_when_all_present_and_memoises():
    cur = _Cur({"information_schema.columns": _ALL_COLS})
    assert dash._ensure_approval_columns(cur) is True
    assert not [s for s, _ in cur.calls if s.startswith("ALTER")]
    cur2 = _Cur()
    assert dash._ensure_approval_columns(cur2) is True
    assert cur2.calls == [], "memoised: no second read"


def test_ensure_reports_a_missing_table_and_does_not_memoise():
    cur = _Cur({"information_schema.columns": []})
    assert dash._ensure_approval_columns(cur) is False
    assert dash._APPROVAL_COLUMNS_ENSURED is False


# ── 4. the draft outcome is written on the approval row ─────────────────────
def test_record_pr_attempt_writes_attempt_url_and_time(monkeypatch):
    cur = _Cur({"information_schema.columns": _ALL_COLS})
    monkeypatch.setattr(dash, "_conn", lambda: _Conn(cur))
    att = {"ok": True, "acted": True, "pr": {"pr": {"url": "https://github.com/x/y/pull/9"}}}
    assert dash._record_pr_attempt("inv", 100416, att) is True
    ups = _sqls(cur, "UPDATE brain_approvals SET pr_attempt")
    assert len(ups) == 1
    sql, params = ups[0]
    assert "pr_url = COALESCE(%s, pr_url)" in sql, "a later failure never clears a landed PR"
    assert "pr_attempted_at = NOW()" in sql
    assert json.loads(params[0])["acted"] is True
    assert params[1] == "https://github.com/x/y/pull/9"
    assert params[2:] == ("inv", 100416)


def test_record_pr_attempt_of_a_failure_keeps_pr_url_null(monkeypatch):
    cur = _Cur({"information_schema.columns": _ALL_COLS})
    monkeypatch.setattr(dash, "_conn", lambda: _Conn(cur))
    dash._record_pr_attempt("inv", 100416, {"ok": False, "error": "claude call failed: http_429"})
    (_, params), = _sqls(cur, "UPDATE brain_approvals SET pr_attempt")
    assert params[1] is None


def test_record_pr_attempt_is_fail_soft(monkeypatch):
    monkeypatch.setattr(dash, "_conn", lambda: None)
    assert dash._record_pr_attempt("inv", 1, {"ok": False}) is False


# ── 5. the approve route persists what it returns ───────────────────────────
def test_the_approve_route_persists_the_attempt_it_returns():
    src = inspect.getsource(dash.innovation_approve)
    i = src.index('resp["pr_attempt"] = _pr')
    assert "_record_pr_attempt(kind, item_id, _pr)" in src[i:i + 300]


# ── 6. the redrive ──────────────────────────────────────────────────────────
def _redrive_harness(monkeypatch, rows, allowed=(True, "ok"), attempt=None,
                     claim_rowcount=1):
    events = []
    cur = _Cur({"information_schema.columns": _ALL_COLS,
                "SELECT kind, item_id, pr_attempt FROM brain_approvals": rows},
               events=events, rowcount=claim_rowcount)
    monkeypatch.setattr(dash, "_conn", lambda: _Conn(cur))
    import routes.brain_guardrails as g
    monkeypatch.setattr(g, "can_open_pr", lambda: allowed)
    attempts = []

    def _attempt(kind, item_id, operator_directive=""):
        events.append(("ATTEMPT", f"{kind}:{item_id}", None))
        attempts.append((kind, item_id))
        return dict(attempt or {"ok": True, "acted": True,
                                "pr": {"pr": {"url": "https://github.com/x/y/pull/77"}}})
    monkeypatch.setattr(dash, "_attempt_pr", _attempt)
    return cur, events, attempts


def test_a_failed_attempt_is_claimed_then_redriven_then_recorded(monkeypatch):
    rows = [("inv", 100416, json.dumps({"ok": False, "error": "claude call failed: http_429"}))]
    cur, events, attempts = _redrive_harness(monkeypatch, rows)
    out = dash.redrive_approved_without_pr()
    assert out["ok"] is True and out["redriven"] == 1, out
    assert attempts == [("inv", 100416)]
    kinds = [e[0] for e in events]
    i_claim = next(i for i, e in enumerate(events)
                   if e[0] == "SQL" and "pr_redrives = COALESCE(pr_redrives, 0) + 1" in e[1])
    i_commit_after_claim = next(i for i in range(i_claim, len(events)) if events[i][0] == "COMMIT")
    i_attempt = kinds.index("ATTEMPT")
    assert i_claim < i_commit_after_claim < i_attempt, "claim is COMMITTED before the attempt"
    rec = [e for e in events if e[0] == "SQL" and "SET pr_attempt = %s::jsonb" in e[1]]
    assert len(rec) == 1 and rec[0][2][1] == "https://github.com/x/y/pull/77"
    assert out["results"][0]["pr_url"] == "https://github.com/x/y/pull/77"
    assert json.loads(rec[0][2][0])["redrive"] is True


def test_the_select_scopes_to_approved_pr_less_recent_rows(monkeypatch):
    cur, _, _ = _redrive_harness(monkeypatch, [])
    dash.redrive_approved_without_pr(window_days=7, max_redrives=3)
    (sql, params), = _sqls(cur, "SELECT kind, item_id, pr_attempt FROM brain_approvals")
    assert "decision = 'approved'" in sql and "pr_url IS NULL" in sql
    assert "approved_at > NOW() - (%s * INTERVAL '1 day')" in sql
    assert "COALESCE(pr_redrives, 0) < %s" in sql
    assert params[0] == 7 and params[1] == 3


def test_a_refusal_is_not_redriven(monkeypatch):
    rows = [("inv", 1, json.dumps({"ok": True, "acted": False, "refused": True}))]
    cur, events, attempts = _redrive_harness(monkeypatch, rows)
    out = dash.redrive_approved_without_pr()
    assert attempts == [] and out["redriven"] == 0
    assert out["results"][0]["skipped"] == "previous attempt is not a failure"
    assert not _sqls(cur, "pr_redrives = COALESCE")


def test_the_guardrail_is_read_first_and_closed_means_nothing_is_touched(monkeypatch):
    rows = [("inv", 1, json.dumps({"ok": False, "error": "x"}))]
    cur, events, attempts = _redrive_harness(monkeypatch, rows, allowed=(False, "kill_switch"))
    out = dash.redrive_approved_without_pr()
    assert out["skipped"] == "kill_switch" and out["redriven"] == 0
    assert cur.calls == [] and attempts == []


def test_the_per_tick_cap_holds(monkeypatch):
    rows = [("inv", i, json.dumps({"ok": False, "error": "x"})) for i in range(1, 8)]
    _, _, attempts = _redrive_harness(monkeypatch, rows)
    out = dash.redrive_approved_without_pr(max_rows=2)
    assert len(attempts) == 2 and out["redriven"] == 2


def test_a_refused_claim_means_no_attempt(monkeypatch):
    rows = [("inv", 1, json.dumps({"ok": False, "error": "x"}))]
    _, _, attempts = _redrive_harness(monkeypatch, rows, claim_rowcount=0)
    out = dash.redrive_approved_without_pr()
    assert attempts == [] and out["results"][0]["skipped"] == "claim refused"


def test_the_master_tick_carries_the_step():
    orch = pytest.importorskip("routes.brain_master_orchestrator")
    src = inspect.getsource(orch._run_master_tick)
    assert '"tier2.approved_without_pr_redrive"' in src
    assert "redrive_approved_without_pr" in src
    assert src.index("tier2.draft_pr_expire") < src.index("tier2.approved_without_pr_redrive")


# ── 7. the stale read + the radar detector ──────────────────────────────────
def test_stale_read_skips_answered_rows_and_keeps_failures_and_never_attempted():
    import datetime as _dt
    ts = _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc)
    cur = _Cur({"FROM brain_approvals WHERE decision = 'approved' AND pr_url IS NULL": [
        ("inv", 1, ts, None, 0),
        ("inv", 2, ts, json.dumps({"ok": False, "error": "http_429"}), 2),
        ("inv", 3, ts, json.dumps({"ok": True, "acted": False, "refused": True}), 0),
    ]})
    out = dash.stale_approvals_without_pr(cur, older_than_hours=24, window_days=7)
    assert [r["key"] for r in out] == ["inv:1", "inv:2"]
    assert out[0]["attempted"] is False and out[1]["error"] == "http_429"
    (sql, params), = cur.calls
    assert "approved_at < NOW() - (%s * INTERVAL '1 hour')" in sql and params == (7, 24)


radar = pytest.importorskip("routes.brain_consistency_radar")


def _radar_db(monkeypatch, *, table=True, column=True):
    cur = _Cur({"to_regclass('public.brain_approvals')": [("brain_approvals",)] if table else [(None,)],
                "column_name = 'pr_url'": [(1,)] if column else []})
    monkeypatch.setattr(radar, "_db", lambda: _Conn(cur))
    return cur


def test_the_detector_fires_with_the_count_and_the_sample(monkeypatch):
    _radar_db(monkeypatch)
    monkeypatch.setattr(dash, "stale_approvals_without_pr", lambda cur, **kw: [
        {"key": "inv:100416", "approved_at": "2026-09-01", "attempted": True,
         "error": "http_429", "redrives": 1},
        {"key": "inv:100420", "approved_at": "2026-09-01", "attempted": False,
         "error": "", "redrives": 0}])
    out = radar.check_approved_without_pr_stale()
    assert len(out) == 1
    f = out[0]
    assert f["issue"] == "approved_without_pr_stale" and f["count"] == 2
    assert "inv:100416" in f["detail"] and "http_429" in f["detail"]
    assert "1 never attempted" in f["detail"]


def test_the_detector_is_silent_when_clean(monkeypatch):
    _radar_db(monkeypatch)
    monkeypatch.setattr(dash, "stale_approvals_without_pr", lambda cur, **kw: [])
    assert radar.check_approved_without_pr_stale() == []


def test_unmeasured_is_not_a_clean_verdict(monkeypatch):
    _radar_db(monkeypatch, column=False)
    monkeypatch.setattr(dash, "stale_approvals_without_pr",
                        lambda cur, **kw: (_ for _ in ()).throw(AssertionError("must not be read")))
    assert radar.check_approved_without_pr_stale() == []
    _radar_db(monkeypatch, table=False)
    assert radar.check_approved_without_pr_stale() == []
    monkeypatch.setattr(radar, "_db", lambda: None)
    assert radar.check_approved_without_pr_stale() == []


def test_the_detector_is_registered_in_scan_all():
    src = inspect.getsource(radar.scan_all)
    assert "check_approved_without_pr_stale" in src
