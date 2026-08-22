"""tests/test_squasher_action_classes.py — action classes for the squasher
inbox (claim loop step 2, 2026-08-22).

The mechanism lets the drain EXECUTE a production mutation that a model named.
Every clause that keeps that safe is exercised here as BEHAVIOUR — the real
functions run against a cursor stub and a loopback stub, and the assertions
are on what was called, in what order, with what parameters. Nothing here
asserts on source text except where the source IS the guard (the kill-switch
status code and the heartbeat budget constant, both read from the AST).

House rules: no DB, never import main, nothing runs at module scope.
Mutation-verified — see the commit message for the mutations and the test
each one failed.

Run:  python3 -m pytest tests/test_squasher_action_classes.py -v
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import types

import pytest

from routes import squasher_action_classes as sac
from routes import squasher_portal as sp
from routes import squasher_queue as sq

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The two live reason shapes observed on 2026-08-22 (ids 255 and 251).
NL_REASON = ("operator action required: POST /api/v1/admin/facility-dedup/"
             "apply?country=NL&confirm=1 — First check whether merged PR "
             "#3006 already resolved or automated NL dedup.")
AU_REASON = ("operator action required: GET /api/v1/admin/facility-dedup/"
             "analyze?country=AU — Choose one: (1) re-run GET /api/v1/"
             "admin/facility-dedup/analyze?country=AU to check whether PR "
             "#3006 already resolved the unmarked duplicates; (2) if "
             "duplicates remain, authorize the one-off POST /api/v1/admin/"
             "facility-dedup/apply?country=AU&confirm=1.")

APPLY_FR = "/api/v1/admin/facility-dedup/apply?country=FR&confirm=1"
ANALYZE_FR = "/api/v1/admin/facility-dedup/analyze?country=FR"


# ── stubs ─────────────────────────────────────────────────────────────────

class _Cur:
    """Records every execute (into a shared event list when given one) and
    answers fetches from a script keyed by a substring of the last SQL."""

    def __init__(self, answers=None, events=None, raise_on=None):
        self.answers = answers or {}
        self.events = events if events is not None else []
        self.calls = []
        self.raise_on = raise_on
        self.rowcount = 1
        self._last = ""

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.calls.append((flat, params))
        self.events.append(("SQL", flat, params))
        self._last = flat
        if self.raise_on and self.raise_on in flat:
            raise RuntimeError("boom: " + self.raise_on)

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

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Fetch:
    """Loopback stub. GETs serve scripted verifier readings in order (None =
    unreadable); the POST answers with the apply envelope. Every call is
    recorded, and mirrored into the shared event list for ordering checks."""

    def __init__(self, readings=(5, 0), apply_status=200, apply_body=None,
                 events=None):
        self.readings = list(readings)
        self.apply_status = apply_status
        self.apply_body = apply_body
        self.events = events if events is not None else []
        self.calls = []

    def __call__(self, method, path):
        self.calls.append((method, path))
        self.events.append(("FETCH", method, path))
        if method == "GET":
            v = self.readings.pop(0) if self.readings else None
            if v is None:
                return 500, {"ok": False, "error": "db_unavailable"}
            return 200, {"ok": True, "dry_run": True, "duplicate_rows": v}
        body = self.apply_body if self.apply_body is not None else {
            "ok": True, "marked_duplicates": 5, "clusters": 5}
        return self.apply_status, body

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def _cls(**kw):
    d = {"class": "facility_dedup_apply", "granted": True, "granted_by": "op",
         "granted_at": None, "bound_params": {"confirm": "1"},
         "verifier_url": "/api/v1/admin/facility-dedup/analyze",
         "reversible": True, "runs_ok": 0, "runs_failed": 0,
         "consecutive_failed": 0, "last_run_at": None,
         "breaker_tripped": False, "notes": ""}
    d.update(kw)
    return d


def _cls_tuple(d):
    return tuple(d.get(c) for c in sac._CLASS_COLS)


_ROW_COLS = ("id", "finding_key", "title", "status", "action_class",
             "action_url", "action_method", "finished_at")


def _row(**kw):
    d = {"id": 218, "finding_key": ANALYZE_FR,
         "title": "facility_duplicates_unmarked", "status": "awaiting_ops",
         "action_class": "facility_dedup_apply", "action_url": APPLY_FR,
         "action_method": "POST", "finished_at": None}
    d.update(kw)
    return d


def _row_tuple(d):
    return tuple(d.get(c) for c in _ROW_COLS)


def _harness(monkeypatch, *, cls=None, rows=None, day_used=0, enabled="1",
             env=None, events=None):
    """A drain step wired to stubs. Returns (conn, cur)."""
    cls = cls or _cls()
    rows = [_row()] if rows is None else rows
    cur = _Cur({
        "FROM brain_action_classes ORDER BY class": [_cls_tuple(cls)],
        "FROM brain_action_classes WHERE class = %s": [_cls_tuple(cls)],
        "WHERE executed AND NOT dry_run": [(day_used,)],
        "WHERE verified AND NOT dry_run": [(0,)],
        "JOIN brain_action_classes c ON": [_row_tuple(r) for r in rows],
        "INSERT INTO brain_action_class_runs": [(501,)],
        "action_class IS NULL ORDER BY id DESC": [],
    }, events=events)
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    if enabled is None:
        monkeypatch.delenv("ACTION_CLASSES_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ACTION_CLASSES_ENABLED", enabled)
    for k in ("ACTION_CLASS_MAX_PER_DRAIN", "ACTION_CLASS_MAX_PER_DAY",
              "SQUASHER_QUEUE_DISABLE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return conn, cur


def _class_update(cur):
    """(ok_inc, failed_inc, consecutive, trip, cls) of the counters UPDATE,
    or None when the class row was not touched."""
    for sql, params in cur.calls:
        if sql.startswith("UPDATE brain_action_classes SET runs_ok"):
            return params
    return None


def _queue_updates(cur):
    return [(s, p) for s, p in cur.calls
            if s.startswith("UPDATE squasher_work_queue")]


def _resolved_ids(cur):
    return [p[-1] for s, p in _queue_updates(cur)
            if "SET status = 'resolved'" in s]


@pytest.fixture
def claim_ledger(monkeypatch):
    """A stand-in routes.claim_ledger with the Step-1 signature. Records
    registrations; stamp_outcome must never be called by this module."""
    calls = {"register": [], "stamp_outcome": [], "events": None}
    mod = types.ModuleType("routes.claim_ledger")

    def register_claim(kind, subject, statement, *, expected_metric,
                       expected_value, horizon_hours, expected_op="eq",
                       regime=None, surfaces=None, source_layer="claim_ledger",
                       shipped=False):
        rec = dict(kind=kind, subject=subject, statement=statement,
                   expected_metric=expected_metric,
                   expected_value=expected_value,
                   horizon_hours=horizon_hours, expected_op=expected_op,
                   regime=regime, surfaces=surfaces, shipped=shipped)
        calls["register"].append(rec)
        if calls["events"] is not None:
            calls["events"].append(("CLAIM", "register", rec))
        return 77

    def stamp_outcome(*a, **k):
        calls["stamp_outcome"].append((a, k))

    mod.register_claim = register_claim
    mod.stamp_outcome = stamp_outcome
    monkeypatch.setitem(sys.modules, "routes.claim_ledger", mod)
    return calls


# ══════════════════════════════════════════════════════════════════════════
#  1 · the class-extraction rule
# ══════════════════════════════════════════════════════════════════════════

def test_the_live_reason_shape_classifies_to_facility_dedup_apply():
    c = sac.classify_text(NL_REASON)
    assert c is not None
    assert c["action_class"] == "facility_dedup_apply"
    assert c["action_method"] == "POST"
    assert c["params"] == {"country": "NL"}
    assert c["action_url"] == "/api/v1/admin/facility-dedup/apply?country=NL&confirm=1"


def test_a_reason_that_leads_with_the_analyze_GET_still_finds_the_apply():
    """Rows 250/251 (SG, AU) lead with `GET .../analyze`; stopping at the
    first verb+path match would leave them unclassified forever."""
    c = sac.classify_text(AU_REASON)
    assert c is not None and c["action_class"] == "facility_dedup_apply"
    assert c["params"] == {"country": "AU"}


def test_an_unknown_endpoint_is_NOT_invented_into_a_class():
    assert sac.classify_text("operator action required: POST /api/v1/admin/"
                             "heal?confirm=1 — run the healer") is None
    assert sac.classify_text("", None, "no endpoints here") is None


def test_the_verb_must_match_the_class():
    """A GET of the apply path is not the apply; a model's aside must not
    become a mutation by path alone."""
    assert sac.classify_text("GET " + APPLY_FR) is None
    assert sac.classify_text("POST " + APPLY_FR) is not None


@pytest.mark.parametrize("bad", ["fr", "FRA", "F", "", "F1"])
def test_the_row_parameter_is_validated(bad):
    txt = f"POST /api/v1/admin/facility-dedup/apply?country={bad}&confirm=1"
    assert sac.classify_text(txt) is None, bad


def test_a_missing_row_parameter_is_refused():
    assert sac.classify_text("POST /api/v1/admin/facility-dedup/apply?confirm=1") is None


def test_the_action_url_is_rebuilt_from_the_registry_never_the_text():
    """Prose cannot smuggle confirm=0, an extra argument or a trailing
    period into the URL that runs."""
    c = sac.classify_text("run POST /api/v1/admin/facility-dedup/apply"
                          "?country=FR&confirm=0&evil=1&country=DE.")
    assert c is not None
    assert c["action_url"] == APPLY_FR or c["action_url"].endswith("country=DE&confirm=1")
    assert "evil" not in c["action_url"]
    assert "confirm=0" not in c["action_url"]


def test_the_first_text_wins_so_the_reason_outranks_the_finding_key():
    c = sac.classify_text("POST " + APPLY_FR, "POST /api/v1/admin/facility-dedup/apply?country=DE&confirm=1")
    assert c["params"] == {"country": "FR"}


def test_row_params_are_rederived_from_the_stored_url_and_validated():
    assert sac.row_params_of(_row()) == {"country": "FR"}
    assert sac.row_params_of(_row(action_url="/api/v1/admin/facility-dedup/apply?country=f&confirm=1")) is None
    assert sac.row_params_of(_row(action_class="not_a_class")) is None


# ══════════════════════════════════════════════════════════════════════════
#  2 · the grant test
# ══════════════════════════════════════════════════════════════════════════

def test_grant_requires_reversible_AND_verifier_AND_bound_params():
    ok, _ = sac.grant_allowed(_cls())
    assert ok
    for bad in (dict(reversible=False), dict(verifier_url=""),
                dict(verifier_url=None), dict(bound_params={}),
                dict(bound_params=None), dict(bound_params="{}"),
                dict(bound_params="not json")):
        ok, why = sac.grant_allowed(_cls(**bad))
        assert not ok, bad
        assert why.startswith("refused"), why


def test_grant_refuses_a_class_the_code_does_not_know():
    ok, why = sac.grant_allowed(_cls(**{"class": "rm_rf_prod"}))
    assert not ok and "registry" in why
    assert sac.grant_allowed(None)[0] is False


def test_bound_params_may_arrive_as_a_json_string_from_the_driver():
    ok, _ = sac.grant_allowed(_cls(bound_params=json.dumps({"confirm": "1"})))
    assert ok


def test_the_seed_row_is_granted_FALSE_and_never_overwrites_a_grant():
    cur = _Cur()
    sac.ensure_tables(cur)
    seeds = [(s, p) for s, p in cur.calls
             if s.startswith("INSERT INTO brain_action_classes")]
    assert len(seeds) == len(sac.ACTION_CLASSES)
    sql, params = seeds[0]
    assert "granted, reversible" in sql and ", FALSE," in sql, (
        "the seed must insert granted=FALSE — the orchestrator grants")
    assert "ON CONFLICT (class) DO NOTHING" in sql, (
        "a redeploy re-running the seed must not overwrite an operator's grant")
    assert params[0] == "facility_dedup_apply" and params[1] is True
    assert params[2] == "/api/v1/admin/facility-dedup/analyze"
    assert json.loads(params[3]) == {"confirm": "1"}


def test_ensure_adds_the_three_queue_columns_on_both_ensure_paths():
    cur = _Cur()
    sac.ensure_tables(cur)
    ddl = " ".join(s for s, _ in cur.calls)
    for col in ("action_class", "action_url", "action_method"):
        assert f"ADD COLUMN IF NOT EXISTS {col} TEXT" in ddl
    cur2 = _Cur()
    sq._ensure_table(cur2)
    ddl2 = " ".join(s for s, _ in cur2.calls)
    for col in ("action_class", "action_url", "action_method"):
        assert f"ADD COLUMN IF NOT EXISTS {col} TEXT" in ddl2


# ══════════════════════════════════════════════════════════════════════════
#  3 · the executor: verify or fail
# ══════════════════════════════════════════════════════════════════════════

def test_a_granted_class_runs_and_a_verified_drop_resolves_the_row(monkeypatch, claim_ledger):
    events = []
    claim_ledger["events"] = events
    conn, cur = _harness(monkeypatch, events=events)
    fetch = _Fetch(readings=(5, 0), events=events)
    out = sac.run_granted_actions(fetch=fetch, clock=iter([0.0, 1.0, 2.0]).__next__)
    assert out["ok"] and out["enabled"] and out["ran"] == 1
    res = out["results"][0]
    assert res["executed"] and res["verified"] and res["outcome"] == "verified"
    assert res["pre_count"] == 5 and res["post_count"] == 0
    assert fetch.calls == [("GET", ANALYZE_FR), ("POST", APPLY_FR), ("GET", ANALYZE_FR)]
    ok_inc, failed_inc, consec, trip, cls = _class_update(cur)
    assert (ok_inc, failed_inc, consec, trip, cls) == (1, 0, 0, False, "facility_dedup_apply")
    assert _resolved_ids(cur) == [218]
    note = [p for s, p in _queue_updates(cur) if "'resolved'" in s][0][0]
    assert "VERIFIED" in note and "5->0" in note


def test_an_executed_run_whose_verifier_shows_NO_DROP_is_a_FAILURE(monkeypatch, claim_ledger):
    """The apply answered 200 and the count did not move. Without the
    verifier this reads as success — the exact lie the class exists to stop."""
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(5, 5))
    out = sac.run_granted_actions(fetch=fetch)
    res = out["results"][0]
    assert res["executed"] is True
    assert res["verified"] is False
    assert res["outcome"] == "failed_no_drop"
    ok_inc, failed_inc, consec, trip, _ = _class_update(cur)
    assert (ok_inc, failed_inc, consec) == (0, 1, 1)
    assert _resolved_ids(cur) == [], "an unverified run must NOT resolve the row"
    notes = [p[0] for s, p in _queue_updates(cur)]
    assert any("FAILED" in n and "did not drop" in n for n in notes)


def test_the_verifier_is_read_AFTER_the_mutation_not_before_it_only(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(5, 0))
    sac.run_granted_actions(fetch=fetch)
    gets_after_post = [c for c in fetch.calls[fetch.calls.index(("POST", APPLY_FR)) + 1:]
                       if c[0] == "GET"]
    assert gets_after_post, "no verifier read after the POST — the verdict was guessed"


def test_a_non_2xx_apply_is_a_failure_even_if_the_count_moved(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(5, 0), apply_status=500, apply_body={"ok": False})
    res = sac.run_granted_actions(fetch=fetch)["results"][0]
    assert res["executed"] and not res["verified"]
    assert res["outcome"] == "failed_http"
    assert _class_update(cur)[1] == 1


def test_an_unreadable_pre_read_never_executes(monkeypatch, claim_ledger):
    """UNOBSERVED is not zero and not a licence to act: no POST, no class
    counter movement, the row keeps waiting with the reason attached."""
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(None, 0))
    out = sac.run_granted_actions(fetch=fetch)
    assert out["ran"] == 0
    assert fetch.posts == []
    assert out["results"][0]["outcome"] == "skipped_verifier_unreadable"
    assert _class_update(cur) is None
    assert _resolved_ids(cur) == []
    assert claim_ledger["register"] == [], "no claim for a run that never started"


def test_a_pre_read_of_zero_resolves_as_noop_without_touching_production(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(0, 0))
    out = sac.run_granted_actions(fetch=fetch)
    assert fetch.posts == [] and out["ran"] == 0
    assert out["results"][0]["outcome"] == "noop_clean"
    assert _resolved_ids(cur) == [218]
    assert _class_update(cur) is None, "a no-op is neither ok nor failed"


# ══════════════════════════════════════════════════════════════════════════
#  4 · the breaker
# ══════════════════════════════════════════════════════════════════════════

def test_three_consecutive_failures_trip_the_breaker(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch, cls=_cls(consecutive_failed=2))
    res = sac.run_granted_actions(fetch=_Fetch(readings=(5, 5)))["results"][0]
    assert res["consecutive_failed"] == 3 and res["breaker_tripped"] is True
    ok_inc, failed_inc, consec, trip, _ = _class_update(cur)
    assert (consec, trip) == (3, True)


def test_two_consecutive_failures_do_not_trip_it(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch, cls=_cls(consecutive_failed=1))
    res = sac.run_granted_actions(fetch=_Fetch(readings=(5, 5)))["results"][0]
    assert res["breaker_tripped"] is False
    assert _class_update(cur)[3] is False


def test_a_verified_run_resets_the_consecutive_counter(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch, cls=_cls(consecutive_failed=2))
    sac.run_granted_actions(fetch=_Fetch(readings=(5, 1)))
    ok_inc, failed_inc, consec, trip, _ = _class_update(cur)
    assert (ok_inc, consec, trip) == (1, 0, False)


def test_a_tripped_breaker_NEVER_executes(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch, cls=_cls(breaker_tripped=True))
    fetch = _Fetch()
    out = sac.run_granted_actions(fetch=fetch)
    assert fetch.calls == [], "a tripped class must not even read the verifier"
    assert out["ran"] == 0
    assert out["candidates"][0]["skip"] == "breaker tripped"


# ══════════════════════════════════════════════════════════════════════════
#  5 · the kill switches (controls — these stay green across every mutation)
# ══════════════════════════════════════════════════════════════════════════

def test_an_UNGRANTED_class_never_executes(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch, cls=_cls(granted=False))
    fetch = _Fetch()
    out = sac.run_granted_actions(fetch=fetch)
    assert fetch.calls == [] and out["ran"] == 0
    assert out["candidates"][0]["skip"] == "not granted"
    assert _queue_updates(cur) == [], "rows of an ungranted class stay exactly as today"


def test_a_grant_that_fails_the_grant_test_in_the_table_is_still_refused_at_run_time(monkeypatch, claim_ledger):
    """Defence in depth: granted=true written straight into the table with
    no verifier_url does not execute."""
    conn, cur = _harness(monkeypatch, cls=_cls(granted=True, verifier_url=""))
    fetch = _Fetch()
    out = sac.run_granted_actions(fetch=fetch)
    assert fetch.calls == [] and "verifier_url" in out["candidates"][0]["skip"]


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", "on", "1 "])
def test_the_global_switch_is_OFF_unless_exactly_1(monkeypatch, value):
    """Missing env = OFF. Shipped dark."""
    monkeypatch.setattr(sac, "_conn", lambda: (_ for _ in ()).throw(
        AssertionError("the dark step must not open a connection")))
    if value is None:
        monkeypatch.delenv("ACTION_CLASSES_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ACTION_CLASSES_ENABLED", value)
    assert sac.enabled() is False
    out = sac.run_granted_actions(fetch=_Fetch())
    assert out["ok"] and out["enabled"] is False and out["ran"] == 0
    assert out["results"] == []


def test_the_global_switch_on_is_exactly_1(monkeypatch):
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    assert sac.enabled() is True


def test_execute_one_refuses_when_the_switch_is_off_even_if_called_directly(monkeypatch):
    monkeypatch.delenv("ACTION_CLASSES_ENABLED", raising=False)
    cur = _Cur()
    fetch = _Fetch()
    res = sac.execute_one(_Conn(cur), cur, _row(), _cls(), fetch=fetch)
    assert res["outcome"] == "skipped_disabled" and fetch.posts == []


def test_only_an_awaiting_ops_row_may_execute(monkeypatch, claim_ledger):
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    cur = _Cur()
    fetch = _Fetch()
    res = sac.execute_one(_Conn(cur), cur, _row(status="awaiting_decision"), _cls(), fetch=fetch)
    assert res["outcome"] == "skipped_not_awaiting_ops" and fetch.calls == []


# ══════════════════════════════════════════════════════════════════════════
#  6 · the caps and the heartbeat budget
# ══════════════════════════════════════════════════════════════════════════

def test_the_per_day_cap_is_respected(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch, day_used=6)
    fetch = _Fetch()
    out = sac.run_granted_actions(fetch=fetch)
    assert fetch.calls == [] and out["ran"] == 0
    assert out["candidates"][0]["skip"].startswith("day cap 6/6")


def test_one_below_the_per_day_cap_still_runs(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch, day_used=5)
    out = sac.run_granted_actions(fetch=_Fetch())
    assert out["ran"] == 1


def test_the_per_day_cap_reads_its_env_and_clamps_garbage(monkeypatch):
    monkeypatch.delenv("ACTION_CLASS_MAX_PER_DAY", raising=False)
    assert sac.max_per_day() == 6
    monkeypatch.setenv("ACTION_CLASS_MAX_PER_DAY", "2")
    assert sac.max_per_day() == 2
    monkeypatch.setenv("ACTION_CLASS_MAX_PER_DAY", "9999")
    assert sac.max_per_day() == 24
    monkeypatch.setenv("ACTION_CLASS_MAX_PER_DAY", "lots")
    assert sac.max_per_day() == 6


def test_the_env_cap_of_two_stops_at_two(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch, day_used=2, env={"ACTION_CLASS_MAX_PER_DAY": "2"})
    fetch = _Fetch()
    out = sac.run_granted_actions(fetch=fetch)
    assert fetch.calls == [] and out["day_cap"] == 2


def test_the_per_drain_cap_defaults_to_one_and_is_bounded_in_python(monkeypatch, claim_ledger):
    """Even if the selector hands back three rows, one runs. The SQL LIMIT is
    real but a stub cannot prove it; the slice can be proven."""
    rows = [_row(id=218), _row(id=219, action_url=APPLY_FR.replace("FR", "BR")),
            _row(id=220, action_url=APPLY_FR.replace("FR", "NL"))]
    conn, cur = _harness(monkeypatch, rows=rows)
    fetch = _Fetch(readings=(5, 0, 5, 0, 5, 0))
    out = sac.run_granted_actions(fetch=fetch)
    assert sac.max_per_drain() == 1
    assert out["ran"] == 1 and len(fetch.posts) == 1
    limit = [p for s, p in cur.calls if "JOIN brain_action_classes c ON" in s][0][-1]
    assert limit == 1


def test_the_per_drain_cap_env_is_clamped(monkeypatch):
    monkeypatch.setenv("ACTION_CLASS_MAX_PER_DRAIN", "50")
    assert sac.max_per_drain() == 3
    monkeypatch.setenv("ACTION_CLASS_MAX_PER_DRAIN", "2")
    assert sac.max_per_drain() == 2


def _hit_timeout_default():
    tree = ast.parse((ROOT / "routes" / "cron_heartbeat.py").read_text(encoding="utf-8"))
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_hit":
            args = n.args
            names = [a.arg for a in args.args]
            defaults = args.defaults
            offset = len(names) - len(defaults)
            for i, a in enumerate(names):
                if a == "timeout" and i >= offset:
                    return defaults[i - offset].value
    raise AssertionError("EXTRACTION EMPTY: cron_heartbeat._hit(timeout=...) not found")


def test_the_step_fits_inside_the_heartbeat_budget():
    """cron_heartbeat dispatches the drain with _hit(timeout=30). The action
    step may spend at most _WALL_BUDGET_S before it mutates, runs at most
    one action per drain by default, and sits BEFORE the investigations in
    drain() — so its work can never be the part that outruns the window."""
    hit = _hit_timeout_default()
    assert isinstance(hit, int) and hit > 0
    assert sac._WALL_BUDGET_S < hit, (sac._WALL_BUDGET_S, hit)
    import inspect
    src = inspect.getsource(sq.drain)
    assert src.index("_action_classes_step()") < src.index("WHERE status='queued'")


def test_the_mutation_does_not_start_once_the_budget_is_spent(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(5, 0))
    slow = iter([0.0, float(sac._WALL_BUDGET_S) + 5.0, 100.0, 101.0]).__next__
    out = sac.run_granted_actions(fetch=fetch, clock=slow)
    res = out["results"][0]
    assert res["outcome"] == "skipped_budget" and res["executed"] is False
    assert fetch.posts == []
    assert claim_ledger["register"] == []


def test_inside_the_budget_the_mutation_runs(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(5, 0))
    fast = iter([0.0, 1.0, 2.0, 3.0]).__next__
    assert sac.run_granted_actions(fetch=fetch, clock=fast)["ran"] == 1


# ══════════════════════════════════════════════════════════════════════════
#  7 · the claim and the ledger
# ══════════════════════════════════════════════════════════════════════════

def test_each_run_registers_a_step1_claim_BEFORE_the_mutation(monkeypatch, claim_ledger):
    events = []
    claim_ledger["events"] = events
    conn, cur = _harness(monkeypatch, events=events)
    sac.run_granted_actions(fetch=_Fetch(readings=(5, 0), events=events))
    kinds = [e[0] + ":" + str(e[1]) for e in events]
    i_claim = kinds.index("CLAIM:register")
    i_post = kinds.index("FETCH:POST")
    assert i_claim < i_post, kinds
    rec = claim_ledger["register"][0]
    assert rec["kind"] == "fix"
    assert rec["subject"] == "facility_dedup_apply:FR"
    assert rec["expected_op"] == "lt" and rec["expected_value"] == "5"
    assert rec["horizon_hours"] == 1
    assert ANALYZE_FR in rec["expected_metric"] and "duplicate_rows" in rec["expected_metric"]
    assert rec["shipped"] is True
    assert claim_ledger["stamp_outcome"] == [], "never stamp the outcome yourself"


def test_the_claim_id_lands_on_the_run_ledger_row(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch)
    sac.run_granted_actions(fetch=_Fetch(readings=(5, 0)))
    ins = [p for s, p in cur.calls if s.startswith("INSERT INTO brain_action_class_runs")]
    assert len(ins) == 1
    cls, qid, params, action_url, verifier_url, pre, executed, outcome, claim_id, err, dry = ins[0]
    assert (cls, qid, action_url, verifier_url, pre, executed, outcome, claim_id, dry) == (
        "facility_dedup_apply", 218, APPLY_FR, ANALYZE_FR, 5, True, "started", 77, False)
    fin = [p for s, p in cur.calls if s.startswith("UPDATE brain_action_class_runs")]
    assert fin and fin[0][0] == 0 and fin[0][1] is True and fin[0][2] == "verified"


def test_a_missing_claim_ledger_does_not_stop_the_run(monkeypatch):
    monkeypatch.setitem(sys.modules, "routes.claim_ledger", None)  # import fails
    conn, cur = _harness(monkeypatch)
    res = sac.run_granted_actions(fetch=_Fetch(readings=(5, 0)))["results"][0]
    assert res["verified"] and res["claim_id"] is None
    assert "unavailable" in (res["claim_error"] or "")


def test_an_older_ledger_signature_is_adapted_not_crashed(monkeypatch):
    mod = types.ModuleType("routes.claim_ledger")
    seen = {}

    def register_claim(kind, subject, statement, expected_metric,
                       expected_value, horizon_hours, regime=None,
                       surfaces=None, shipped=False):
        seen.update(expected_value=expected_value)
        return {"ok": True, "id": 9}

    mod.register_claim = register_claim
    monkeypatch.setitem(sys.modules, "routes.claim_ledger", mod)
    conn, cur = _harness(monkeypatch)
    res = sac.run_granted_actions(fetch=_Fetch(readings=(5, 0)))["results"][0]
    assert res["claim_id"] == 9 and seen["expected_value"] == "< 5"


def test_the_claim_adapter_matches_the_MERGED_ledger_contract(monkeypatch):
    """PR #3045 shipped register_claim with the comparator INSIDE
    expected_value and a dict return — not the keyword-only `expected_op`
    shape this module was specified against. The adapter must land on the
    real contract, proven against the real module with the database absent."""
    cl = pytest.importorskip("routes.claim_ledger")
    monkeypatch.setitem(sys.modules, "routes.claim_ledger", cl)
    seen = []
    real = cl.register_claim

    def spy(*a, **k):
        seen.append((a, dict(k)))
        return real(*a, **k)

    monkeypatch.setattr(cl, "register_claim", spy)
    monkeypatch.setattr(cl, "_db_url", lambda: None)   # refuse at the DB edge
    cid, err = sac._register_claim("facility_dedup_apply", {"country": "FR"},
                                   ANALYZE_FR, "duplicate_rows", 5, 218)
    assert cid is None and err == "no database", (cid, err)
    a, k = seen[-1]
    assert a[0] == "fix" and a[1] == "facility_dedup_apply:FR"
    assert cl.parse_expectation(k["expected_value"]) == ("<", "5")
    assert cl.parse_metric(k["expected_metric"]) == ("get", ANALYZE_FR, "duplicate_rows")
    assert cl.validate_claim("fix", a[1], a[2], k["expected_metric"],
                             k["expected_value"], k["horizon_hours"],
                             k.get("regime")) is None
    assert k["shipped"] is True


def test_the_ledger_intent_row_is_committed_before_the_mutation(monkeypatch, claim_ledger):
    events = []
    conn, cur = _harness(monkeypatch, events=events)
    sac.run_granted_actions(fetch=_Fetch(readings=(5, 0), events=events))
    i_ins = next(i for i, e in enumerate(events)
                 if e[0] == "SQL" and e[1].startswith("INSERT INTO brain_action_class_runs"))
    i_post = next(i for i, e in enumerate(events) if e[:2] == ("FETCH", "POST"))
    commits_between = [e for e in events[i_ins:i_post] if e[0] == "COMMIT"]
    assert commits_between, "intent must be durable before the mutation fires"


# ══════════════════════════════════════════════════════════════════════════
#  8 · dry run
# ══════════════════════════════════════════════════════════════════════════

def test_dry_run_reports_what_would_run_and_touches_nothing(monkeypatch, claim_ledger):
    conn, cur = _harness(monkeypatch)
    fetch = _Fetch(readings=(5,))
    out = sac.run_granted_actions(dry_run=True, fetch=fetch)
    assert out["dry_run"] and out["ran"] == 0
    res = out["results"][0]
    assert res["outcome"] == "dry_run" and res["would_run"] is True
    assert res["pre_count"] == 5 and res["action_url"] == APPLY_FR
    assert fetch.posts == []
    assert _class_update(cur) is None and _queue_updates(cur) == []
    assert not [s for s, _ in cur.calls if s.startswith("INSERT INTO brain_action_class_runs")]
    assert claim_ledger["register"] == []


def test_dry_run_while_dark_says_the_switch_is_what_stops_it(monkeypatch):
    conn, cur = _harness(monkeypatch, enabled=None)
    out = sac.run_granted_actions(dry_run=True, fetch=_Fetch(readings=(5,)))
    assert out["enabled"] is False
    res = out["results"][0]
    assert res["would_run"] is False and "ACTION_CLASSES_ENABLED" in res["note"]


# ══════════════════════════════════════════════════════════════════════════
#  9 · classification paths: backfill, enqueue, analysis time
# ══════════════════════════════════════════════════════════════════════════

def test_backfill_classifies_open_rows_idempotently():
    cur = _Cur({"action_class IS NULL ORDER BY id DESC": [
        (255, "/api/v1/admin/facility-dedup/analyze?country=NL",
         "facility_duplicates_unmarked", NL_REASON, None, None),
        (247, "/api/v1/schema-org/missing", "schema_org_coverage_low",
         "human decision required: Choose between (1) ... (2) ...", None, None),
    ]})
    out = sac.classify_open_rows(cur)
    assert out["scanned"] == 2 and out["classified"] == 1
    assert out["by_class"] == {"facility_dedup_apply": 1}
    ups = [(s, p) for s, p in cur.calls if s.startswith("UPDATE squasher_work_queue")]
    assert len(ups) == 1
    sql, params = ups[0]
    assert params == ("facility_dedup_apply",
                      "/api/v1/admin/facility-dedup/apply?country=NL&confirm=1",
                      "POST", 255)
    assert "AND action_class IS NULL" in sql, "re-running must not rewrite a classified row"
    sel = [s for s, _ in cur.calls if "action_class IS NULL ORDER BY" in s][0]
    assert "WHERE status IN" in sel


def test_in_transaction_classification_is_savepoint_guarded():
    cur = _Cur()
    assert sac.classify_in_tx(cur, 300, NL_REASON) is True
    sqls = [s for s, _ in cur.calls]
    assert sqls[0].startswith("SAVEPOINT") and sqls[-1].startswith("RELEASE SAVEPOINT")
    broken = _Cur(raise_on="UPDATE squasher_work_queue")
    assert sac.classify_in_tx(broken, 300, NL_REASON) is False
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s, _ in broken.calls)
    quiet = _Cur()
    assert sac.classify_in_tx(quiet, 300, "nothing named") is False
    assert quiet.calls == [], "no class, no write"


def test_enqueue_tags_the_row_inside_its_own_transaction(monkeypatch):
    cur = _Cur({"SELECT COUNT(*) FILTER": [(0, 0)],
                "RETURNING id": [(901,)]})
    conn = _Conn(cur)
    monkeypatch.setattr(sq, "_conn", lambda: conn)
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    out = sq.enqueue("POST " + APPLY_FR, "operator: run the FR dedup")
    assert out["ok"] and out["id"] == 901
    tags = [p for s, p in cur.calls
            if s.startswith("UPDATE squasher_work_queue SET action_class")]
    assert tags and tags[0][:3] == ("facility_dedup_apply", APPLY_FR, "POST")
    assert tags[0][3] == 901


def test_analysis_time_classification_reads_the_settled_row(monkeypatch):
    cur = _Cur({"SELECT reason, decision, analysis, title, finding_key":
                [(NL_REASON, None, None, "facility_duplicates_unmarked",
                  "/api/v1/admin/facility-dedup/analyze?country=NL")]})
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    assert sac.classify_queue_row(255) is True
    assert conn.commits == 1
    tags = [p for s, p in cur.calls if s.startswith("UPDATE squasher_work_queue SET action_class")]
    assert tags[0][0] == "facility_dedup_apply" and tags[0][3] == 255


def test_the_drain_classifies_a_row_when_it_settles_to_a_waiting_state():
    import inspect
    src = inspect.getsource(sq.drain)
    i_finish = src.index("_finish(it[\"id\"], status, reason)")
    assert "_classify_settled(it[\"id\"])" in src[i_finish:i_finish + 200]


def test_queue_and_inbox_reads_carry_the_class(monkeypatch):
    import datetime as _dt
    ts = _dt.datetime(2026, 8, 22, tzinfo=_dt.timezone.utc)
    row15 = (218, ANALYZE_FR, "facility_duplicates_unmarked", "heal", "awaiting_ops",
             NL_REASON, None, ts, ts, None, None, None,
             "facility_dedup_apply", APPLY_FR, "POST")
    cur = _Cur({"ORDER BY requested_at DESC LIMIT": [row15]})
    monkeypatch.setattr(sq, "_conn", lambda: _Conn(cur))
    rows = sq.queue_rows(5)
    assert rows[0]["action_class"] == "facility_dedup_apply"
    assert rows[0]["action_url"] == APPLY_FR and rows[0]["action_method"] == "POST"


# ══════════════════════════════════════════════════════════════════════════
#  10 · endpoints and kill switches
# ══════════════════════════════════════════════════════════════════════════

def _app():
    import flask
    app = flask.Flask("t")
    app.register_blueprint(sq.squasher_queue_bp)   # record_once wires sac
    return app


def test_the_queue_blueprint_registers_the_action_class_routes():
    rules = {r.rule for r in _app().url_map.iter_rules()}
    for path in ("/api/v1/brain/squasher/classes", "/api/v1/brain/squasher/classify",
                 "/api/v1/brain/squasher/grant"):
        assert path in rules, path


def test_endpoints_need_the_admin_key(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    c = _app().test_client()
    assert c.get("/api/v1/brain/squasher/classes").status_code == 401
    assert c.post("/api/v1/brain/squasher/classify").status_code == 401
    assert c.post("/api/v1/brain/squasher/grant", json={}).status_code == 401


def test_kill_switch_answers_404_never_5xx(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.setenv("SQUASHER_QUEUE_DISABLE", "1")
    c = _app().test_client()
    h = {"X-Admin-Key": "adm"}
    for rv in (c.get("/api/v1/brain/squasher/classes", headers=h),
               c.post("/api/v1/brain/squasher/classify", headers=h),
               c.post("/api/v1/brain/squasher/grant", headers=h,
                      json={"class": "facility_dedup_apply", "granted": True})):
        assert rv.status_code == 404, rv.status_code
    rv = c.post("/api/v1/brain/squasher/drain?dry_run=1", headers=h)
    assert rv.status_code == 200 and rv.get_json().get("skipped")
    # and by construction: no 5xx literal inside any `if _disabled():` block
    tree = ast.parse((ROOT / "routes" / "squasher_action_classes.py").read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            bad += [n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, int)
                    and n.value >= 500]
    assert not bad, bad


def test_grant_endpoint_enforces_the_grant_test(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    cur = _Cur({"FROM brain_action_classes WHERE class = %s":
                [_cls_tuple(_cls(granted=False, verifier_url=""))]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    c = _app().test_client()
    rv = c.post("/api/v1/brain/squasher/grant", headers={"X-Admin-Key": "adm"},
                json={"class": "facility_dedup_apply", "granted": True})
    assert rv.status_code == 400
    d = rv.get_json()
    assert d["refused"] and "verifier_url" in d["error"]
    assert not [s for s, _ in cur.calls if s.startswith("UPDATE brain_action_classes")]


def test_grant_endpoint_grants_a_class_that_passes_and_revokes_always(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    cur = _Cur({"FROM brain_action_classes WHERE class = %s": [_cls_tuple(_cls(granted=False))]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    c = _app().test_client()
    rv = c.post("/api/v1/brain/squasher/grant", headers={"X-Admin-Key": "adm"},
                json={"class": "facility_dedup_apply", "granted": True, "by": "orchestrator"})
    assert rv.status_code == 200 and rv.get_json()["ok"]
    up = [p for s, p in cur.calls if s.startswith("UPDATE brain_action_classes SET granted")]
    assert up[0][0] is True and up[0][2] == "orchestrator" and up[0][4] is False
    cur2 = _Cur({"FROM brain_action_classes WHERE class = %s": [_cls_tuple(_cls(verifier_url=""))]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur2))
    rv = c.post("/api/v1/brain/squasher/grant", headers={"X-Admin-Key": "adm"},
                json={"class": "facility_dedup_apply", "granted": False})
    assert rv.status_code == 200, "revoking never needs the grant test"
    rv = c.post("/api/v1/brain/squasher/grant", headers={"X-Admin-Key": "adm"},
                json={"class": "nope", "granted": True})
    assert rv.status_code == 404
    rv = c.post("/api/v1/brain/squasher/grant", headers={"X-Admin-Key": "adm"},
                json={"class": "facility_dedup_apply", "granted": "yes"})
    assert rv.status_code == 400


def test_clear_breaker_is_explicit_and_resets_the_streak(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    cur = _Cur({"FROM brain_action_classes WHERE class = %s":
                [_cls_tuple(_cls(breaker_tripped=True, consecutive_failed=3))]})
    monkeypatch.setattr(sac, "_conn", lambda: _Conn(cur))
    c = _app().test_client()
    rv = c.post("/api/v1/brain/squasher/grant", headers={"X-Admin-Key": "adm"},
                json={"class": "facility_dedup_apply", "granted": True})
    up = [p for s, p in cur.calls if s.startswith("UPDATE brain_action_classes SET granted")]
    assert up[0][4] is False and up[0][5] is False, "re-granting alone must not clear a breaker"
    rv = c.post("/api/v1/brain/squasher/grant", headers={"X-Admin-Key": "adm"},
                json={"class": "facility_dedup_apply", "granted": True, "clear_breaker": True})
    assert rv.status_code == 200
    up = [p for s, p in cur.calls if s.startswith("UPDATE brain_action_classes SET granted")]
    assert up[-1][4] is True and up[-1][5] is True


def test_classify_endpoint_returns_counts(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    cur = _Cur({"action_class IS NULL ORDER BY id DESC": [
        (255, ANALYZE_FR, "facility_duplicates_unmarked", NL_REASON, None, None)]})
    conn = _Conn(cur)
    monkeypatch.setattr(sac, "_conn", lambda: conn)
    rv = _app().test_client().post("/api/v1/brain/squasher/classify",
                                   headers={"X-Admin-Key": "adm"})
    d = rv.get_json()
    assert rv.status_code == 200 and d["ok"] and d["classified"] == 1
    assert d["by_class"] == {"facility_dedup_apply": 1} and conn.commits >= 1


def test_classes_GET_never_acts_even_with_dry_run(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.setenv("ACTION_CLASSES_ENABLED", "1")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    conn, cur = _harness(monkeypatch)
    calls = []
    monkeypatch.setattr(sac, "_loopback", lambda m, p: (calls.append((m, p)) or (200, {"ok": True, "duplicate_rows": 5})))
    rv = _app().test_client().get("/api/v1/brain/squasher/classes?dry_run=1",
                                  headers={"X-Admin-Key": "adm"})
    d = rv.get_json()
    assert rv.status_code == 200 and d["ok"] and d["known"]
    assert d["plan"]["dry_run"] is True and d["plan"]["ran"] == 0
    assert all(m == "GET" for m, _ in calls), calls
    assert _class_update(cur) is None and _queue_updates(cur) == []


def test_drain_dry_run_reports_the_plan_without_investigating(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "adm")
    monkeypatch.delenv("SQUASHER_QUEUE_DISABLE", raising=False)
    monkeypatch.setattr(sq, "drain", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a dry run must not drain")))
    seen = {}

    def _step(dry_run=False):
        seen["dry_run"] = dry_run
        return {"ok": True, "dry_run": dry_run, "results": []}

    monkeypatch.setattr(sq, "_action_classes_step", _step)
    rv = _app().test_client().post("/api/v1/brain/squasher/drain?dry_run=1",
                                   headers={"X-Admin-Key": "adm"})
    d = rv.get_json()
    assert rv.status_code == 200 and d["dry_run"] is True
    assert seen["dry_run"] is True and d["action_classes"]["ok"]


def test_the_drain_step_is_fail_soft_when_the_module_breaks(monkeypatch):
    monkeypatch.setitem(sys.modules, "routes.squasher_action_classes", None)
    out = sq._action_classes_step()
    assert out["ok"] is False and "error" in out


# ══════════════════════════════════════════════════════════════════════════
#  11 · the portal
# ══════════════════════════════════════════════════════════════════════════

def test_verified_class_runs_count_as_fixes_landed_but_executed_ones_do_not():
    act = {"known": True, "enabled": True, "breaker_tripped": False, "landed_7d": 0}
    folded = sp.fold_class_runs(act, {"known": True, "verified_7d": 2, "day_used": 9})
    assert folded["landed_7d"] == 2 and folded["merges_7d"] == 0
    assert folded["class_runs_verified_7d"] == 2
    assert sp.verdict_for({"act": folded, "propose": {}})["state"] == "GREEN"
    folded0 = sp.fold_class_runs(act, {"known": True, "verified_7d": 0, "day_used": 9})
    assert folded0["landed_7d"] == 0, "executed-but-unverified runs are not fixes"


def test_an_unreadable_class_stage_adds_nothing_and_is_not_zero():
    act = {"known": True, "landed_7d": 1}
    folded = sp.fold_class_runs(act, {"known": False})
    assert folded["landed_7d"] == 1 and folded["class_runs_verified_7d"] is None


def test_the_portal_counts_only_VERIFIED_runs():
    cur = _Cur({"WHERE verified AND NOT dry_run": [(3,)]})
    assert sac.verified_runs_7d(cur) == 3
    sql = cur.calls[0][0]
    assert "WHERE verified AND NOT dry_run" in sql and "7 days" in sql


def test_collect_folds_the_class_summary_in(monkeypatch):
    monkeypatch.setattr(sp, "_get", lambda path: {})
    monkeypatch.setattr(sac, "summary", lambda: {"known": True, "verified_7d": 4,
                                                  "enabled": False, "classes": [],
                                                  "inbox_by_class": {}, "caps": {}})
    monkeypatch.setattr(sq, "queue_rows", lambda n: [])
    out = sp.collect()
    assert out["action_classes"]["verified_7d"] == 4
    assert out["act"]["class_runs_verified_7d"] == 4
    assert out["act"]["known"] is False, "an unreadable auto-merge lane stays UNKNOWN"


def _snapshot(**classes):
    d = {"as_of": "t", "detect": {}, "route": {}, "propose": {}, "verify": {},
         "act": {"known": True, "enabled": True, "landed_7d": 0},
         "verdict": {"state": "AMBER", "headline": "h", "detail": "d"},
         "actionable": [], "queue": []}
    d["action_classes"] = classes
    return d


def test_the_page_renders_grant_and_revoke_and_the_switch_state():
    html = sp.render(_snapshot(
        known=True, enabled=False, caps={"per_drain": 1, "per_day": 6, "breaker_after": 3},
        day_used=0, verified_7d=0,
        classes=[_cls(granted=False, grant_ok=True, grant_reason="ok"),
                 dict(_cls(granted=True, breaker_tripped=True), **{"class": "other"})],
        inbox_by_class={"facility_dedup_apply": [_row()], "unclassified": [_row(id=247, action_class=None, action_url=None, action_method=None, status="awaiting_decision")]}))
    assert "Grant class" in html and "Revoke" in html and "Clear breaker" in html
    assert "OFF (dark)" in html and "TRIPPED" in html
    assert "facility_dedup_apply · 1 waiting" in html and "unclassified · 1 waiting" in html
    assert "/api/v1/brain/squasher/grant" in html


def test_an_unreadable_registry_renders_as_unreadable_not_empty():
    html = sp.render(_snapshot(known=False, error="no database url"))
    assert "UNREADABLE" in html and "no database url" in html
    assert "Grant class" not in html


def test_the_run_ledger_is_whitelisted_as_append_only():
    tree = ast.parse((ROOT / "scripts" / "regression_lint.py").read_text(encoding="utf-8"))
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "WHITELIST_TABLES" for t in n.targets):
            names = {e.value for e in ast.walk(n.value)
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            assert "brain_action_class_runs" in names
            return
    raise AssertionError("EXTRACTION EMPTY: WHITELIST_TABLES not found")
