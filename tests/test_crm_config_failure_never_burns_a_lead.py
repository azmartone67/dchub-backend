#!/usr/bin/env python3
"""A CONFIGURATION failure must never spend a queued lead's push budget.

★ THE DEFECT (origin/main, read 2026-09-21). flush_outbound_queue checked only
DISABLE and the DB, then pushed every unsent row. Any non-ok result did
`attempts + 1` and 'failed' at 5 — and the SELECT reads `push_attempts < 5`,
so a failed row is never picked again. A 401 (bad / legacy / quoted token), a
403 (missing scope) or a 400 PROPERTY_DOESNT_EXIST is the same answer for EVERY
row, so each daily flush (07:19 UTC) charged one attempt to all of them.

Measured live 2026-09-21 ~04:40Z (/api/v1/admin/crm/queue): 31 rows, 30
queued_export at 1 attempt, 1 queued at 3 with last_error='hs 401'; 24 of the
31 paid_conversion. /crm/health said destination_configured=false ("does not
start with 'pat-'") — and the flush pushed anyway.

★ The fake DB below EXECUTES the SQL it is handed: the SELECT's column list,
WHERE, ORDER BY and LIMIT decide what comes back; an UPDATE's SET clause
decides what changes. It raises on any statement it does not recognise. A
cursor that answered from canned rows would leave every query here unguarded
(feedback_fake_cursor_ignores_the_sql_it_is_given). HubSpot is a fake
`requests` — no network.

MUST-FAIL CONTROLS included.
"""
from __future__ import annotations

import ast
import copy
import datetime as _dt
import json
import os
import re
import sys

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NOW = _dt.datetime(2026, 9, 21, 7, 19, tzinfo=_dt.timezone.utc)
ADMIN = {"X-Admin-Key": "adm-test"}


# ── a table that runs the SQL it is given ────────────────────────────

class FakeDB:
    COLS = ("id", "event_type", "captured_at", "lead_email", "lead_session_id",
            "lead_company", "lead_title", "lead_first_name", "lead_last_name",
            "attribution_chain", "intent_score", "crm_pushed_at", "crm_provider",
            "crm_external_id", "crm_response", "status", "push_attempts",
            "last_error")

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.statements = []            # every normalised statement executed
        # crm_flush_last exists only once the schema script has created it;
        # its columns and primary key are read FROM that DDL.
        self.flush_last = {}            # primary key -> row
        self.flush_cols = self.flush_pk = None

    def snapshot(self):
        return copy.deepcopy(self.rows)

    def connect(self):
        return _Conn(self)

    # ---- interpreter ----
    def run(self, sql, params):
        s = " ".join(sql.split())
        self.statements.append(s)
        p = iter(params or ())
        if s.startswith("CREATE TABLE IF NOT EXISTS crm_outbound_queue"):
            if mm := re.search(r"CREATE TABLE IF NOT EXISTS crm_flush_last \((.+?)\);", s):
                defs = [d.strip() for d in mm[1].split(",")]
                self.flush_cols = [d.split()[0] for d in defs]
                self.flush_pk = [d.split()[0] for d in defs if "PRIMARY KEY" in d]
            return [], 0
        if "crm_flush_last" in s:
            return self._run_flush_last(s, p)
        m = re.fullmatch(
            r"SELECT (?P<cols>.+?) FROM crm_outbound_queue"
            r"(?: WHERE (?P<where>.+?))?(?: GROUP BY (?P<group>\w+))?"
            r"(?: ORDER BY (?P<order>\w+) (?P<dir>ASC|DESC))?"
            r"(?: LIMIT (?P<limit>%s|\d+))?", s)
        if m:
            keep = self._where(m["where"], p)
            rows = [r for r in self.rows if keep(r)]
            if m["order"]:
                rows.sort(key=lambda r: r[m["order"]], reverse=m["dir"] == "DESC")
            if m["limit"]:
                rows = rows[:int(next(p) if m["limit"] == "%s" else m["limit"])]
            return self._project(m["cols"], m["group"], rows), 0
        m = re.fullmatch(r"UPDATE crm_outbound_queue SET (?P<set>.+?) WHERE (?P<where>.+)", s)
        if m:
            sets = [self._assignment(a.strip(), p) for a in m["set"].split(",")]
            keep = self._where(m["where"], p)
            hit = [r for r in self.rows if keep(r)]
            for r in hit:
                new = {col: fn(r) for col, fn in sets}
                r.update(new)
            return [], len(hit)
        raise AssertionError(f"fake DB does not understand: {s[:160]}")

    def _run_flush_last(self, s, p):
        if self.flush_cols is None:
            raise AssertionError('relation "crm_flush_last" does not exist')
        m = re.fullmatch(
            r"INSERT INTO crm_flush_last \((?P<cols>[^)]+)\) VALUES \((?P<vals>.+?)\)"
            r" ON CONFLICT \((?P<key>\w+)\) DO UPDATE SET (?P<set>.+)", s)
        if m:
            cols = [c.strip() for c in m["cols"].split(",")]
            if not set(cols) <= set(self.flush_cols):
                raise AssertionError(f"crm_flush_last has no column in {cols}")
            if [m["key"]] != self.flush_pk:
                raise AssertionError("there is no unique or exclusion constraint "
                                     "matching the ON CONFLICT specification")
            row = {}
            for c, v in zip(cols, [v.strip() for v in m["vals"].split(",")], strict=True):
                if v == "%s":
                    row[c] = next(p)
                elif v == "%s::jsonb":
                    row[c] = json.loads(next(p))
                elif v == "NOW()":
                    row[c] = NOW
                else:
                    raise AssertionError(f"fake DB does not understand value: {v}")
            old = self.flush_last.get(row[m["key"]])
            if old is None:
                self.flush_last[row[m["key"]]] = row
            else:
                for a in m["set"].split(","):
                    mm = re.fullmatch(r"(\w+) = EXCLUDED\.(\w+)", a.strip())
                    if not mm or mm[1] not in self.flush_cols:
                        raise AssertionError(f"fake DB does not understand assignment: {a}")
                    old[mm[1]] = row[mm[2]]
            return [], 1
        m = re.fullmatch(r"SELECT (?P<cols>.+?) FROM crm_flush_last", s)
        if m:
            age = "EXTRACT(EPOCH FROM (NOW() - ran_at))/3600.0"
            cols = [c.strip() for c in m["cols"].split(",")]
            for c in cols:                  # checked even when the table is empty
                if c != age and c not in self.flush_cols:
                    raise AssertionError(f"fake DB has no column {c!r}")
            return [tuple((NOW - r["ran_at"]).total_seconds() / 3600.0 if c == age
                          else copy.deepcopy(r[c]) for c in cols)
                    for r in self.flush_last.values()], 0
        raise AssertionError(f"fake DB does not understand: {s[:160]}")

    def _where(self, where, p):
        """Compile a WHERE clause ONCE per statement, consuming its params in
        textual order — so SET params, WHERE params and LIMIT line up."""
        preds = []
        for term in (where.split(" AND ") if where else ()):
            t = term.strip()
            if mm := re.fullmatch(r"(\w+) = ANY\(%s\)", t):
                v = list(next(p)); preds.append(lambda r, c=mm[1], v=v: r[c] in v)
            elif mm := re.fullmatch(r"(\w+) = %s", t):
                v = next(p); preds.append(lambda r, c=mm[1], v=v: r[c] == v)
            elif mm := re.fullmatch(r"(\w+) = '([^']*)'", t):
                preds.append(lambda r, c=mm[1], v=mm[2]: r[c] == v)
            elif mm := re.fullmatch(r"(\w+) (<|>=) (\d+)", t):
                op, n = mm[2], int(mm[3])
                preds.append(lambda r, c=mm[1], op=op, n=n:
                             (r[c] < n) if op == "<" else (r[c] >= n))
            elif mm := re.fullmatch(r"(\w+) IS NOT NULL", t):
                preds.append(lambda r, c=mm[1]: r[c] is not None)
            else:
                raise AssertionError(f"fake DB does not understand predicate: {t}")
        return lambda r: all(f(r) for f in preds)

    def _assignment(self, a, p):
        if mm := re.fullmatch(r"(\w+) = %s(::jsonb)?", a):
            v = next(p)
            v = json.loads(v) if mm[2] else v
            return mm[1], (lambda r, v=v: v)
        if mm := re.fullmatch(r"(\w+) = (\w+) \+ (\d+)", a):
            return mm[1], (lambda r, c=mm[2], n=int(mm[3]): r[c] + n)
        if mm := re.fullmatch(r"(\w+) = NULL", a):
            return mm[1], (lambda r: None)
        if mm := re.fullmatch(r"(\w+) = NOW\(\)", a):
            return mm[1], (lambda r: NOW)
        if mm := re.fullmatch(r"(\w+) = '([^']*)'", a):
            return mm[1], (lambda r, v=mm[2]: v)
        if mm := re.fullmatch(r"(\w+) = (\d+)", a):
            return mm[1], (lambda r, v=int(mm[2]): v)
        raise AssertionError(f"fake DB does not understand assignment: {a}")

    def _project(self, cols, group, rows):
        cols = [c.strip() for c in cols.split(",")]
        if group:
            assert cols == [group, "COUNT(*)"], cols
            out = {}
            for r in rows:
                out[r[group]] = out.get(r[group], 0) + 1
            return list(out.items())
        if cols == ["COUNT(*)"]:
            return [(len(rows),)]
        if cols == ["EXTRACT(EPOCH FROM (NOW() - MIN(captured_at)))/3600.0"]:
            if not rows:
                return [(None,)]
            return [((NOW - min(r["captured_at"] for r in rows)).total_seconds() / 3600.0,)]
        for c in cols:
            if c not in self.COLS:
                raise AssertionError(f"fake DB has no column {c!r}")
        return [tuple(r[c] for c in cols) for r in rows]


class _Cur:
    def __init__(self, db):
        self.db, self._rows, self.rowcount = db, [], -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._rows, self.rowcount = self.db.run(sql, params)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cur(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass


# ── a HubSpot that never leaves the process ──────────────────────────

def _hs_validation(*errs):
    """A real-shaped HubSpot 400: the per-property errors are a JSON string
    embedded in `message`, so their quotes arrive escaped."""
    inner = [{"isValid": False, "message": msg, "error": code, "name": name,
              "localizedErrorMessage": msg, "portalId": 247451889}
             for code, name, msg in errs]
    return json.dumps({"status": "error",
                       "message": "Property values were not valid: " + json.dumps(inner),
                       "correlationId": "c0ffee", "category": "VALIDATION_ERROR"})


HS_201 = (201, '{"id": "9001"}')
HS_401 = (401, '{"status":"error","message":"Authentication credentials not found.",'
               '"correlationId":"c0ffee","category":"INVALID_AUTHENTICATION"}')
HS_403 = (403, '{"status":"error","message":"This app hasn\'t been granted all '
               'required scopes to make this call.","correlationId":"c0ffee",'
               '"category":"MISSING_SCOPES"}')
HS_400_NO_PROPS = (400, _hs_validation(
    *[("PROPERTY_DOESNT_EXIST", p, f'Property "{p}" does not exist')
      for p in ("dchub_event_type", "dchub_intent_score", "dchub_attribution")]))
HS_400_BAD_EMAIL = (400, _hs_validation(
    ("INVALID_EMAIL", "email", "Email address x@@example is invalid")))
HS_429 = (429, '{"status":"error","message":"You have reached your secondly limit.",'
               '"errorType":"RATE_LIMIT","policyName":"SECONDLY"}')
HS_503 = (503, "<html>Service Unavailable</html>")


class FakeHubSpot:
    """`requests` stand-in. `answers` maps email -> (status, body) or an
    Exception to raise; `default` answers everyone else."""

    def __init__(self, default=HS_201, answers=None):
        self.default, self.answers, self.calls = default, dict(answers or {}), []

    def post(self, url, headers=None, data=None, timeout=None):
        email = json.loads(data)["properties"]["email"]
        self.calls.append(email)
        a = self.answers.get(email, self.default)
        if isinstance(a, Exception):
            raise a
        status, body = a

        class _R:
            status_code, text = status, body

            def json(self):
                return json.loads(body)
        return _R()


def _row(i, status="queued", attempts=0, event="paid_conversion", age_h=None,
         last_error=None, crm_response=None):
    return {"id": i, "event_type": event,
            "captured_at": NOW - _dt.timedelta(hours=age_h or (1000 - i)),
            "lead_email": f"lead{i}@example.com", "lead_session_id": None,
            "lead_company": "Acme", "lead_title": None, "lead_first_name": None,
            "lead_last_name": None, "attribution_chain": {"trigger": "t"},
            "intent_score": 100, "crm_pushed_at": None, "crm_provider": None,
            "crm_external_id": None, "crm_response": crm_response,
            "status": status, "push_attempts": attempts, "last_error": last_error}


def _incident_rows():
    """The live queue of 2026-09-21: 30 queued_export at 1, 1 queued at 3."""
    rows = [_row(i, status="queued_export", attempts=1) for i in range(1, 31)]
    rows.append(_row(31, status="queued", attempts=3, last_error="hs 401",
                     crm_response={"ok": False, "error": "hs 401",
                                   "status_code": 401, "raw": HS_401[1]}))
    return rows


@pytest.fixture()
def m(monkeypatch):
    import routes.crm_reverse_etl as mod
    # other files importlib.reload() this module with their own env — set
    # every input explicitly rather than inherit whatever they left behind.
    for k, v in dict(CRM_PROVIDER="hubspot", HUBSPOT_API_KEY="pat-na1-test",
                     SF_INSTANCE_URL="", SF_ACCESS_TOKEN="", DRY_RUN=False,
                     DISABLE=False, DCHUB_ADMIN_KEY="adm-test",
                     _SCHEMA_READY=False).items():
        monkeypatch.setattr(mod, k, v)
    # Every flush records itself under this process's role (crm_flush_last).
    for k in ("DCHUB_ROLE", "RAILWAY_SERVICE_NAME", "RAILWAY_REPLICA_ID"):
        monkeypatch.delenv(k, raising=False)
    return mod


def _wire(m, monkeypatch, rows, hubspot=None):
    db = FakeDB(rows)
    monkeypatch.setattr(m, "_conn", db.connect)
    monkeypatch.setattr(m, "_return", lambda c, error=False: None)
    hs = hubspot or FakeHubSpot()
    monkeypatch.setattr(m, "requests", hs)
    return db, hs


def _client(m):
    app = Flask(__name__)
    app.register_blueprint(m.crm_reverse_etl_bp)
    return app.test_client()


def _by_id(db):
    return {r["id"]: r for r in db.rows}


def _queue_reads_or_writes(db):
    """Statements that read lead rows or change the queue. Every flush exit
    now records itself (crm_flush_last), so a skip runs SQL — the schema
    script, one unsent COUNT(*) and the upsert — and none of it is either."""
    return [s for s in db.statements
            if "crm_outbound_queue" in s
            and not s.startswith(("CREATE TABLE IF NOT EXISTS crm_outbound_queue",
                                  "SELECT COUNT(*) FROM crm_outbound_queue"))]


# ── 1. the gate ──────────────────────────────────────────────────────

def test_THE_INCIDENT_a_non_pat_key_touches_zero_rows(m, monkeypatch):
    monkeypatch.setattr(m, "HUBSPOT_API_KEY", "eu1-1234-legacy-api-key")
    db, hs = _wire(m, monkeypatch, _incident_rows(), FakeHubSpot(default=HS_401))
    before = db.snapshot()
    for _ in range(7):                     # a week of daily flushes
        out = m.flush_outbound_queue(limit=100)
        assert out["ok"] is False, out
        assert out["skipped"] == "destination_not_configured", out
        assert out["reason"] == m._destination_state()[1], out
        assert "pat-" in out["reason"], out
        assert "pushed" not in out, "a skip must not carry a green-looking pushed count"
    assert hs.calls == [], f"HubSpot was called {len(hs.calls)}x with a key health calls unusable"
    assert _queue_reads_or_writes(db) == [], f"the gate let SQL through: {db.statements[:3]}"
    assert db.flush_last["all"]["summary"]["skipped"] == "destination_not_configured"
    assert db.rows == before, "rows changed during a flush that should not have run"


def test_MUST_FAIL_CONTROL_the_filter_sees_a_flush_that_ran(m, monkeypatch):
    """_queue_reads_or_writes is what the gate tests assert empty — prove it
    is not empty by construction: a flush that runs shows its SELECT and UPDATE."""
    db, _ = _wire(m, monkeypatch, [_row(1)])
    m.flush_outbound_queue()
    seen = _queue_reads_or_writes(db)
    assert [s.split()[0] for s in seen] == ["SELECT", "UPDATE"], seen


def test_the_gate_IS_the_health_predicate_not_a_copy(m, monkeypatch):
    """Patch the predicate and the flush must follow it both ways — proof it
    calls _destination_state rather than restating the pat- rule."""
    db, hs = _wire(m, monkeypatch, [_row(1)])
    monkeypatch.setattr(m, "_destination_state", lambda: (False, "SENTINEL-GAP-7"))
    out = m.flush_outbound_queue()
    assert (out["skipped"], out["reason"]) == ("destination_not_configured", "SENTINEL-GAP-7")
    assert hs.calls == [] and _queue_reads_or_writes(db) == []
    monkeypatch.setattr(m, "_destination_state", lambda: (True, ""))
    out = m.flush_outbound_queue()
    assert out["ok"] is True and out["pushed"] == 1, out
    assert _by_id(db)[1]["status"] == "pushed"


def test_the_flush_does_not_restate_the_token_rule(m):
    """AST, not substring: the docstring itself names _destination_state().
    A restated `or not HUBSPOT_API_KEY.startswith("pat-")` next to the call
    would pass the behavioural test above, so this one reads the code."""
    import inspect
    import textwrap
    fn = ast.parse(textwrap.dedent(inspect.getsource(m.flush_outbound_queue))).body[0]
    nodes = list(ast.walk(fn))
    calls = {n.func.id for n in nodes
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    names = {n.id for n in nodes if isinstance(n, ast.Name)}
    attrs = {n.attr for n in nodes if isinstance(n, ast.Attribute)}
    assert "_destination_state" in calls
    assert not names & {"_hubspot_token_looks_valid", "HUBSPOT_API_KEY"}, names
    assert "startswith" not in attrs


# ── 2. classification: only a lead's own failure spends its budget ──

@pytest.mark.parametrize("answer", [HS_401, HS_403, HS_400_NO_PROPS],
                         ids=["401", "403", "400-PROPERTY_DOESNT_EXIST"])
def test_a_config_refusal_never_spends_the_budget(m, monkeypatch, answer):
    rows = [_row(1, status="queued", attempts=4), _row(2, status="queued_export", attempts=1)]
    db, hs = _wire(m, monkeypatch, rows, FakeHubSpot(default=answer))
    for _ in range(10):
        out = m.flush_outbound_queue()
        assert out["ok"] is False and out["config_errors"] == 2 and out["failed"] == 0, out
        assert out["reason"].startswith("config: hs %d" % answer[0]), out
    assert len(hs.calls) == 20, "rows stopped being retried"
    r = _by_id(db)
    assert (r[1]["push_attempts"], r[1]["status"]) == (4, "queued"), r[1]
    assert (r[2]["push_attempts"], r[2]["status"]) == (1, "queued_export"), r[2]
    assert r[1]["last_error"] == "hs %d" % answer[0]
    assert r[1]["crm_response"]["status_code"] == answer[0]


def test_a_config_code_past_the_500_char_cut_is_still_config(m, monkeypatch):
    """HubSpot lists one entry per bad property; a long INVALID_EMAIL entry can
    push PROPERTY_DOESNT_EXIST past `raw`'s 500 chars. `codes` is read from the
    full body."""
    long_bad = ("INVALID_EMAIL", "email", "Email address " + "x" * 400 + " is invalid")
    body = _hs_validation(long_bad, ("PROPERTY_DOESNT_EXIST", "dchub_event_type", "nope"))
    assert body.index("PROPERTY_DOESNT_EXIST") > 500, "fixture no longer exercises the cut"
    db, _ = _wire(m, monkeypatch, [_row(1, attempts=4)], FakeHubSpot(default=(400, body)))
    m.flush_outbound_queue()
    assert (_by_id(db)[1]["push_attempts"], _by_id(db)[1]["status"]) == (4, "queued")


@pytest.mark.parametrize("answer", [HS_429, HS_503, TimeoutError("Read timed out")],
                         ids=["429", "503", "timeout"])
def test_a_transient_failure_never_spends_the_budget(m, monkeypatch, answer):
    db, _ = _wire(m, monkeypatch, [_row(1, attempts=4)], FakeHubSpot(default=answer))
    out = m.flush_outbound_queue()
    assert out["ok"] is False and out["transient_errors"] == 1 and out["failed"] == 0, out
    assert (_by_id(db)[1]["push_attempts"], _by_id(db)[1]["status"]) == (4, "queued")


def test_requests_missing_is_config(m, monkeypatch):
    db, _ = _wire(m, monkeypatch, [_row(1, attempts=4)])
    monkeypatch.setattr(m, "requests", None)
    out = m.flush_outbound_queue()
    assert out["config_errors"] == 1, out
    assert (_by_id(db)[1]["push_attempts"], _by_id(db)[1]["last_error"]) == (4, "requests_missing")


def test_MUST_FAIL_CONTROL_a_lead_failure_still_spends_and_fails(m, monkeypatch):
    """Without this, 'never charge anything' would pass every test above."""
    rows = [_row(1, attempts=4), _row(2, attempts=0)]
    db, hs = _wire(m, monkeypatch, rows, FakeHubSpot(default=HS_400_BAD_EMAIL))
    out = m.flush_outbound_queue()
    assert out["ok"] is True and out["failed"] == 2 and out["config_errors"] == 0, out
    r = _by_id(db)
    assert (r[1]["push_attempts"], r[1]["status"]) == (5, "failed")
    assert (r[2]["push_attempts"], r[2]["status"]) == (1, "queued")
    m.flush_outbound_queue()
    assert hs.calls.count("lead1@example.com") == 1, "a failed row was selected again"


def test_one_flush_judges_each_row_on_its_own_answer(m, monkeypatch):
    rows = [_row(1, attempts=2), _row(2, attempts=2), _row(3, attempts=2)]
    hs = FakeHubSpot(answers={"lead1@example.com": HS_201,
                              "lead2@example.com": HS_400_BAD_EMAIL,
                              "lead3@example.com": HS_400_NO_PROPS})
    db, _ = _wire(m, monkeypatch, rows, hs)
    out = m.flush_outbound_queue()
    assert (out["pushed"], out["failed"], out["config_errors"]) == (1, 1, 1), out
    r = _by_id(db)
    assert (r[1]["status"], r[1]["push_attempts"], r[1]["last_error"]) == ("pushed", 3, None)
    assert (r[2]["status"], r[2]["push_attempts"]) == ("queued", 3)
    assert (r[3]["status"], r[3]["push_attempts"]) == ("queued", 2)


def test_a_row_with_no_email_is_the_rows_own_fault(m, monkeypatch):
    """no_email_for_hubspot can never succeed for that row — it must spend its
    budget and reach 'failed', not be retried as 'transient' forever."""
    row = _row(1, attempts=4)
    row["lead_email"] = None
    db, hs = _wire(m, monkeypatch, [row])
    out = m.flush_outbound_queue()
    assert out["failed"] == 1 and hs.calls == [], out
    assert (_by_id(db)[1]["status"], _by_id(db)[1]["last_error"]) == ("failed", "no_email_for_hubspot")


# ── 3. /crm/health says it ───────────────────────────────────────────

def _health_rows():
    return [
        _row(1, status="queued", attempts=1, last_error="hs 401",
             crm_response={"ok": False, "error": "hs 401", "status_code": 401}),
        _row(2, status="queued_export", attempts=1),
        _row(3, status="failed", attempts=5, last_error="hs 403"),     # legacy: no crm_response
        _row(4, status="failed", attempts=5, last_error="hs 400",
             crm_response={"error": "hs 400", "status_code": 400, "raw": HS_400_BAD_EMAIL[1]}),
    ]


def test_health_names_a_refusal_the_shape_check_cannot_see(m, monkeypatch):
    """pat- token → destination_configured=true, yet every push is refused."""
    _wire(m, monkeypatch, _health_rows())
    j = _client(m).get("/api/v1/admin/crm/health", headers=ADMIN).get_json()
    assert j["destination_configured"] is True
    assert j["destination_refusing"] is True, j
    assert j["stalled"] is True and "hs 401" in j["stalled_reason"], j
    assert j["config_errors"]["unsent_rows"] == 1
    assert j["config_errors"]["by_error"] == {"hs 401": 1}
    assert j["config_errors"]["failed_rows_requeueable"] == 1, "legacy 'hs 403' not recognised"
    assert "requeue-config-failures" in j["config_errors"]["requeue"]


def test_MUST_FAIL_CONTROL_health_is_quiet_for_a_lead_failure(m, monkeypatch):
    rows = [_row(1, status="queued", attempts=1, last_error="hs 400",
                 crm_response={"error": "hs 400", "status_code": 400,
                               "raw": HS_400_BAD_EMAIL[1]})]
    _wire(m, monkeypatch, rows)
    j = _client(m).get("/api/v1/admin/crm/health", headers=ADMIN).get_json()
    assert j["destination_refusing"] is False and j["stalled"] is False, j
    assert j["config_errors"]["unsent_rows"] == 0


# ── 4. recovery: requeue what the old flush failed on config ─────────

def test_requeue_is_a_dry_run_by_default_and_names_its_rows(m, monkeypatch):
    rows = [
        _row(1, status="failed", attempts=5, last_error="hs 401",
             crm_response={"error": "hs 401", "status_code": 401}),
        _row(2, status="failed", attempts=5, last_error="hs 403"),     # legacy
        _row(3, status="failed", attempts=5, last_error="hs 400",
             crm_response={"error": "hs 400", "status_code": 400, "raw": HS_400_BAD_EMAIL[1]}),
        _row(4, status="failed", attempts=5, last_error="hs 400",
             crm_response={"error": "hs 400", "status_code": 400, "raw": HS_400_NO_PROPS[1][:500]}),
        _row(5, status="queued", attempts=3, last_error="hs 401"),     # not failed: out of scope
    ]
    db, hs = _wire(m, monkeypatch, rows)
    before = db.snapshot()
    c = _client(m)

    assert c.post("/api/v1/admin/crm/requeue-config-failures").status_code == 401
    j = c.post("/api/v1/admin/crm/requeue-config-failures", headers=ADMIN).get_json()
    assert j["dry_run"] is True
    assert [x["id"] for x in j["would_requeue"]] == [1, 2, 4], j
    assert j["would_requeue"][0]["lead_email"] == "lead1@example.com"
    assert [x["id"] for x in j["kept_failed"]] == [3]
    assert "requeued" not in j
    assert db.rows == before, "a dry run wrote to the queue"
    assert not [s for s in db.statements if s.startswith("UPDATE")]

    j = c.post("/api/v1/admin/crm/requeue-config-failures?apply=1", headers=ADMIN).get_json()
    assert j["dry_run"] is False and j["requeued_count"] == 3, j
    r = _by_id(db)
    for i in (1, 2, 4):
        assert (r[i]["status"], r[i]["push_attempts"]) == ("queued", 0), r[i]
        assert r[i]["last_error"] == before[i - 1]["last_error"], "history erased"
    assert (r[3]["status"], r[3]["push_attempts"]) == ("failed", 5)
    assert (r[5]["status"], r[5]["push_attempts"]) == ("queued", 3)

    # and they are exactly what the next flush sends
    out = m.flush_outbound_queue()
    assert sorted(hs.calls) == ["lead1@example.com", "lead2@example.com",
                                "lead4@example.com", "lead5@example.com"], hs.calls
    assert out["pushed"] == 4 and _by_id(db)[3]["status"] == "failed"


# ── 5. the scheduler names a skip instead of logging pushed=None ─────

class _RecLogger:
    """Records calls directly — immune to a suite-wide logging.disable()."""

    def __init__(self):
        self.lines = []

    def _rec(self, level):
        return lambda fmt, *a: self.lines.append((level, fmt % a))

    def __getattr__(self, level):
        return self._rec(level)


def test_the_scheduler_log_names_a_skip(m, monkeypatch):
    tree = ast.parse(open(os.path.join(ROOT, "crawler_scheduler.py"), encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_run_crm_outbound_flush")
    log = _RecLogger()
    ns = {"logger": log}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "crawler_scheduler.py", "exec"), ns)
    calls = []
    monkeypatch.setattr(m, "flush_outbound_queue", lambda limit=100, **kw: calls.append(kw) or {
        "ok": False, "skipped": "destination_not_configured", "reason": "GAP-XYZ"})
    ret = ns["_run_crm_outbound_flush"]()
    assert [lvl for lvl, _ in log.lines] == ["warning"], log.lines
    assert "destination_not_configured" in log.lines[0][1] and "GAP-XYZ" in log.lines[0][1]
    assert calls == [{"trigger": "scheduler"}], calls
    assert ret == {"slot_status": "stalled: destination_not_configured"}, ret
    log.lines.clear()
    monkeypatch.setattr(m, "flush_outbound_queue", lambda limit=100, **kw: {
        "ok": True, "pushed": 3, "failed": 0, "provider": "hubspot"})
    ret = ns["_run_crm_outbound_flush"]()
    assert [lvl for lvl, _ in log.lines] == ["info"] and "pushed=3" in log.lines[0][1]
    assert ret == {"slot_status": "success"}, ret


# ── the fake itself ──────────────────────────────────────────────────

def test_the_fake_refuses_sql_it_does_not_understand():
    db = FakeDB([_row(1)])
    with pytest.raises(AssertionError, match="does not understand"):
        db.run("DELETE FROM crm_outbound_queue WHERE id = %s", (1,))
    with pytest.raises(AssertionError, match="predicate"):
        db.run("SELECT id FROM crm_outbound_queue WHERE lead_email LIKE %s", ("%",))
    with pytest.raises(AssertionError, match="no column"):
        db.run("SELECT nonsense FROM crm_outbound_queue", ())
