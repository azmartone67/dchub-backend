"""tests/test_activation_emails.py — paid-customer activation emails (QA sweep
2026-09-02, finding 5:9). House rule: never import main.

A fake cursor models the ONE piece of Postgres the idempotency depends on —
the UNIQUE (customer_id, step) constraint and INSERT … ON CONFLICT DO NOTHING
— so the mutations below are real:
  * delete the ON CONFLICT clause from CLAIM_SQL  → a repeat sweep raises a
    unique violation per customer → errors > 0                → RED
  * delete UNIQUE (...) from LEDGER_DDL           → a repeat sweep sends again → RED
  * make enabled() default True                   → default-off tests           → RED
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes import activation_emails as ae  # noqa: E402

NOW = dt.datetime(2026, 9, 2, 6, 0, tzinfo=dt.timezone.utc)


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        s = fh.read()
    assert len(s) > 500, "%s read as %d bytes" % (os.path.join(*parts), len(s))
    return s


# ── a fake Postgres that honours UNIQUE + ON CONFLICT DO NOTHING ────────────
class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.log.append(sql)
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE"):
            self.db.ledger_exists = True
            self.db.unique = "UNIQUE (customer_id, step)" in s
            self.rowcount = -1
        elif s.startswith("INSERT INTO activation_email_ledger"):
            key = (str(params[0]), params[1])
            dup = key in self.db.ledger
            if dup and "ON CONFLICT" in s:
                self.rowcount = 0
            elif dup and self.db.unique:
                raise Exception("UniqueViolation: activation_email_ledger_customer_id_step_key")
            else:
                self.db.ledger[key] = {"email": params[2], "status": "reserved", "sent_at": None}
                self.db.inserts.append(key)
                self.rowcount = 1
        elif s.startswith("UPDATE activation_email_ledger"):
            status, sent_at, info, cid, step = params
            self.db.ledger[(str(cid), step)].update(status=status, sent_at=sent_at, info=info)
            self.rowcount = 1
        elif s.startswith("SELECT u.id::text AS customer_id"):
            self._rows = list(self.db.candidates)
        elif "to_regclass" in s:
            self._rows = [(self.db.ledger_exists,)]
        elif s.startswith("SELECT step, COUNT(*)"):
            agg = {}
            since = params[0]
            for (cid, step), r in self.db.ledger.items():
                a = agg.setdefault(step, [0, 0, None])
                if r["status"] == "sent":
                    a[0] += 1
                    if r["sent_at"] and r["sent_at"] >= since:
                        a[1] += 1
                    if r["sent_at"] and (a[2] is None or r["sent_at"] > a[2]):
                        a[2] = r["sent_at"]
            self._rows = [(k, v[0], v[1], v[2]) for k, v in agg.items()]
        else:
            raise AssertionError("unexpected SQL: " + s[:80])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    def __init__(self, candidates):
        self.candidates = candidates
        self.ledger = {}
        self.inserts = []
        self.log = []
        self.unique = False
        self.ledger_exists = False

    def cursor(self):
        return FakeCursor(self)


class FakeSender:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, to, subject, html):
        self.sent.append((to, subject, html))
        return (True, "sent_200") if self.ok else (False, "status_500_boom")


def _cand(cid, hours_old, rest_calls=0, mcp_last=None, mcp_key="dch_live_abc123"):
    return {"customer_id": str(cid), "email": f"c{cid}@example.com", "plan": "founding",
            "created_at": NOW - dt.timedelta(hours=hours_old), "rest_calls": rest_calls,
            "mcp_last_used_at": mcp_last, "mcp_key": mcp_key}


# ── 1 · the switch: OFF unless exactly "1" ──────────────────────────────────
def test_default_is_off_and_only_the_exact_string_one_arms_it():
    assert ae.enabled({}) is False, "unset env must be OFF — customer sends ship DARK"
    for v in ("true", "yes", "on", "1 ", "01", "TRUE", ""):
        assert ae.enabled({ae.ENV_SWITCH: v}) is False, f"{v!r} must not arm the sweep"
    assert ae.enabled({ae.ENV_SWITCH: "1"}) is True
    assert ae.ENV_SWITCH == "ACTIVATION_EMAILS_ENABLED"


def test_disarmed_sweep_sends_nothing_and_writes_nothing(monkeypatch):
    monkeypatch.delenv(ae.ENV_SWITCH, raising=False)
    db = FakeDB([_cand(1, 30), _cand(2, 100)])
    snd = FakeSender()
    out = ae.run_sweep(db, sender=snd, now=NOW)
    assert out["enabled"] is False and out["sent"] == 0 and snd.sent == []
    assert db.ledger == {} and db.inserts == [], "a disarmed sweep must not reserve rows"
    assert not any(s.startswith("CREATE TABLE") for s in db.log), "no DDL while disarmed"
    steps = {(w["customer_id"], w["step"]) for w in out["would_send"]}
    assert steps == {("1", ae.STEP_DAY1), ("2", ae.STEP_DAY1), ("2", ae.STEP_DAY3)}
    assert "DISARMED" in out["note"]


# ── 2 · armed: the right step at the right age, once ────────────────────────
def test_armed_sweep_sends_day1_and_day3_by_age_and_usage():
    db = FakeDB([
        _cand(1, 10),                                  # too young for anything
        _cand(2, 30),                                  # day-1 only
        _cand(3, 100),                                 # day-1 + day-3 (no usage)
        _cand(4, 100, rest_calls=5),                   # used the REST key → no day-3
        _cand(5, 100, mcp_last=NOW - dt.timedelta(hours=2)),  # used MCP → no day-3
        _cand(6, 30, mcp_key=None),                    # no MCP key yet → day-1 withheld
    ])
    snd = FakeSender()
    out = ae.run_sweep(db, sender=snd, now=NOW, armed=True)
    sent = {(s["customer_id"], s["step"]) for s in out["sends"]}
    assert sent == {("2", ae.STEP_DAY1), ("3", ae.STEP_DAY1), ("3", ae.STEP_DAY3),
                    ("4", ae.STEP_DAY1), ("5", ae.STEP_DAY1)}
    assert out["sent"] == 5 == len(snd.sent) and out["errors"] == 0
    reasons = {(s["customer_id"], s["step"]): s["reason"] for s in out["skips"]}
    assert reasons[("4", ae.STEP_DAY3)] == "has_usage"
    assert reasons[("5", ae.STEP_DAY3)] == "has_usage"
    assert reasons[("6", ae.STEP_DAY1)] == "no_mcp_key"
    assert all(r["status"] == "sent" for r in db.ledger.values())


def test_repeat_sweep_is_a_no_op_by_ledger_not_by_clock():
    """The idempotency proof. A second run — same minute, next heartbeat, a
    second replica — sends nothing, raises nothing, inserts nothing."""
    db = FakeDB([_cand(2, 30), _cand(3, 100)])
    snd = FakeSender()
    first = ae.run_sweep(db, sender=snd, now=NOW, armed=True)
    assert first["sent"] == 3 and first["errors"] == 0
    rows_after_first = dict(db.ledger)
    inserts_after_first = len(db.inserts)
    second = ae.run_sweep(db, sender=snd, now=NOW + dt.timedelta(minutes=5), armed=True)
    assert second["sent"] == 0, "the ledger did not stop a repeat send"
    assert second["errors"] == 0, "a repeat claim raised instead of no-op'ing (ON CONFLICT gone?)"
    assert second["already_sent"] == 3
    assert len(snd.sent) == 3
    assert db.ledger == rows_after_first and len(db.inserts) == inserts_after_first
    assert db.unique, "LEDGER_DDL lost UNIQUE (customer_id, step)"


def test_claim_sql_carries_the_conflict_clause_in_the_same_literal():
    assert "ON CONFLICT (customer_id, step) DO NOTHING" in ae.CLAIM_SQL
    assert "UNIQUE (customer_id, step)" in ae.LEDGER_DDL


def test_a_failed_send_is_recorded_and_not_retried_into_a_duplicate():
    db = FakeDB([_cand(2, 30)])
    bad = FakeSender(ok=False)
    out = ae.run_sweep(db, sender=bad, now=NOW, armed=True)
    assert out["sent"] == 0 and out["errors"] == 1
    assert db.ledger[("2", ae.STEP_DAY1)]["status"] == "failed"
    again = ae.run_sweep(db, sender=FakeSender(), now=NOW, armed=True)
    assert again["sent"] == 0 and again["already_sent"] == 1


def test_max_sends_per_run_caps_a_runaway_candidate_query():
    db = FakeDB([_cand(i, 30) for i in range(1, 40)])
    snd = FakeSender()
    out = ae.run_sweep(db, sender=snd, now=NOW, armed=True, max_sends=3)
    assert out["sent"] == 3 and len(snd.sent) == 3
    assert sum(1 for s in out["skips"] if s["reason"] == "max_sends_per_run") == 36


# ── 3 · the day-1 email is a first RESULT, not a key ────────────────────────
def test_day1_email_carries_the_real_connect_url_and_a_paid_only_tool():
    subject, html = ae.render_day1("c@example.com", "dch_live_deadbeef")
    assert "https://dchub.cloud/mcp?api_key=dch_live_deadbeef" in html  # secretscan:allow (placeholder)
    assert "X-API-Key: dch_live_deadbeef" in html
    assert ae.FIRST_QUERY_TOOL in html and ae.SECOND_QUERY_TOOL in html
    assert '"lat": 33.45' in html and '"capacity_mw": 100' in html, "the query must be paste-able"
    assert "first" in subject.lower()


def test_the_prefilled_tools_are_paid_only_in_the_live_gate():
    """A free-tier tool as the 'first paid query' would prove nothing. Read
    PAID_ONLY_TOOLS from mcp_upgrade_gate's source (it imports psycopg at
    module level, so never import it here)."""
    src = _src("mcp_upgrade_gate.py")
    m = re.search(r"PAID_ONLY_TOOLS\s*=\s*\{(.*?)\}", src, re.S)
    assert m, "PAID_ONLY_TOOLS not found"
    paid = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert ae.FIRST_QUERY_TOOL in paid, f"{ae.FIRST_QUERY_TOOL} is not PAID_ONLY any more"
    assert ae.SECOND_QUERY_TOOL in paid, f"{ae.SECOND_QUERY_TOOL} is not PAID_ONLY any more"


def test_day3_email_is_a_reply_path_not_a_pitch():
    subject, html = ae.render_day3("c@example.com")
    assert "Reply" in html and "buy.stripe.com" not in html and "$" not in html


# ── 4 · stats: what the kill-switch probe reads ─────────────────────────────
def test_stats_publish_the_process_view_and_window_sends(monkeypatch):
    monkeypatch.delenv(ae.ENV_SWITCH, raising=False)
    db = FakeDB([_cand(2, 30), _cand(3, 100)])
    ae.run_sweep(db, sender=FakeSender(), now=NOW, armed=True)
    st = ae.read_stats(db, now=NOW + dt.timedelta(minutes=30), window_hours=2)
    assert st["enabled"] is False and st["ledger_exists"] is True
    assert st["sent_total"] == 3 and st["sent_in_window"] == 3
    assert st["by_step"][ae.STEP_DAY1]["sent_total"] == 2
    old = ae.read_stats(db, now=NOW + dt.timedelta(days=2), window_hours=2)
    assert old["sent_in_window"] == 0 and old["sent_total"] == 3


def test_stats_on_a_never_armed_process_is_zero_not_error():
    st = ae.read_stats(FakeDB([]), now=NOW)
    assert st["ok"] is True and st["ledger_exists"] is False and st["sent_total"] == 0


# ── 5 · wiring: scheduler, registry, chokepoint, gate ───────────────────────
def test_the_sweep_is_dispatched_by_cron_heartbeat():
    src = _src("routes", "cron_heartbeat.py")
    m = re.search(r'\(\s*"activation_emails_sweep",\s*f"\{BASE\}/api/v1/admin/activation-emails/run",\s*"POST"', src)
    assert m, "no live _DISPATCH entry for activation_emails_sweep"
    # not commented out
    line_start = src.rfind("\n", 0, m.start()) + 1
    assert not src[line_start:m.start()].lstrip().startswith("#")


def test_the_switch_is_registered_with_intent_off_in_the_probe():
    src = _src("tools", "kill_switch_probe.py")
    m = re.search(r'"ACTIVATION_EMAILS_ENABLED":\s*\{(.*?)\}', src, re.S)
    assert m, "ACTIVATION_EMAILS_ENABLED not registered in kill_switch_probe.SWITCHES"
    assert '"expected": "0"' in m.group(1), "intent must be OFF"
    assert '"observe": "activation_emails"' in m.group(1)
    assert '"activation_emails": observe_activation_emails' in src


def test_registered_in_main_and_in_the_transactional_baseline():
    assert "from routes.activation_emails import activation_emails_bp" in _src("main.py")
    assert '"activation_emails.py"' in _src("tests", "test_marketing_chokepoint.py")


def test_admin_gate_is_fail_closed():
    """No configured key => 401, never 'open because unconfigured'."""
    import flask
    app = flask.Flask("t")
    app.register_blueprint(ae.activation_emails_bp)
    c = app.test_client()
    for k in ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY"):
        os.environ.pop(k, None)
    assert c.get("/api/v1/admin/activation-emails/stats").status_code == 401
    assert c.post("/api/v1/admin/activation-emails/run").status_code == 401
    os.environ["DCHUB_ADMIN_KEY"] = "k-test"
    try:
        assert c.get("/api/v1/admin/activation-emails/stats",
                     headers={"X-Admin-Key": "wrong"}).status_code == 401
        assert c.get("/api/v1/admin/activation-emails/stats?admin_key=k-test").status_code == 401, \
            "query-param keys must not be accepted"
    finally:
        os.environ.pop("DCHUB_ADMIN_KEY", None)


# ── 6 · the probe's evaluator for this switch (pure) ────────────────────────
def _probe():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ksp_act", os.path.join(ROOT, "tools", "kill_switch_probe.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_convicts_a_process_that_publishes_enabled_while_the_registry_says_off():
    p = _probe()
    assert p.SWITCHES["ACTIVATION_EMAILS_ENABLED"]["expected"] == "0"
    v = p.evaluate("ACTIVATION_EMAILS_ENABLED", "0", {"enabled": True, "sent_in_window": 0})
    assert v["state"] == p.VIOLATION and "enabled=True" in v["detail"]


def test_probe_convicts_a_send_while_off_and_agrees_on_silence():
    p = _probe()
    v = p.evaluate("ACTIVATION_EMAILS_ENABLED", "0", {"enabled": False, "sent_in_window": 2})
    assert v["state"] == p.VIOLATION and "2 activation email(s)" in v["detail"]
    ok = p.evaluate("ACTIVATION_EMAILS_ENABLED", "0", {"enabled": False, "sent_in_window": 0})
    assert ok["state"] == p.AGREE
    armed = p.evaluate("ACTIVATION_EMAILS_ENABLED", "1", {"enabled": True, "sent_in_window": 3})
    assert armed["state"] == p.AGREE, "when the owner arms it, sending is permitted"


def test_probe_reads_unknown_never_red_when_the_surface_is_absent():
    p = _probe()
    assert p.evaluate("ACTIVATION_EMAILS_ENABLED", "0", None)["state"] == p.UNKNOWN
    assert p.evaluate("ACTIVATION_EMAILS_ENABLED", "0", {})["state"] == p.UNKNOWN
