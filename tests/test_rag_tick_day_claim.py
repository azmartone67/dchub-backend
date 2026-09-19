"""One armed tick per UTC day, even when two heartbeats arrive in the same second.

THE DEFECT. master_tick() guarded itself with a 20h check against
_prev_snapshot(). That read is not a claim: the row that would block a second
tick is written by _persist(), which runs AFTER tier3_act() has already fired.
Two heartbeats landing together both read the same stale `prev`, both pass the
check, and both act. It is not hypothetical — it happened on 2 of the 18 days
in the shell's last 20 snapshots:

    2026-09-17  06:00:54  +  06:00:55
    2026-09-09  06:00:37  +  06:00:40

Harmless while the shell was SHADOW. RAG_MASTER_ARM went to 1 on 2026-09-18,
and the guard's own comment says it exists so an armed shell does not "re-fire
the reindex/deep-dive nudges" — so from that day the guard was load-bearing and
did not hold.

WHY A UNIQUE INDEX ON rag_snapshots WOULD NOT HAVE FIXED IT, though it is the
obvious reading of "make it one row per day": the conflict is detected in
_persist(), which is downstream of tier3_act(). The duplicate ROW is rejected
and the duplicate NUDGE has already left the box. The property is an ORDERING —
the claim must be takeable before the action — which is what these tests pin.

WHAT IS AND IS NOT COVERED HERE. CI has no DATABASE_URL (see
test_one_personal_note_per_customer.py), so the atomicity of the claim itself is
a PostgreSQL property (PRIMARY KEY + ON CONFLICT DO NOTHING) and is asserted
here only as the shape of the statement. The behavioural tests below cover the
half that is ours: that a lost claim stops the action, that an unreachable DB
fails CLOSED, and that the claim is consulted before tier3_act rather than
after. Live verification against production Postgres is the 06:00 UTC tick.
"""
import ast
import os
import pytest
import flask

from routes import rag_master_shell as rms


@pytest.fixture
def app():
    # Deliberately bare: this fixture registers NO url rule. Doing so would make
    # this module declare a Flask route, and the route-table coherence gate reads
    # every declared route in the repo and fails the build for one with no
    # Cloudflare entry — it did, for this file. A test must not invent production
    # surface in order to call a view; drive it through a request context.
    # (The offending call and path are named in the commit message, not here: a
    # text-scanning gate can match a comment that quotes what it forbids.)
    return flask.Flask(__name__)


def _tick(app, query=""):
    """Call the real master_tick() in a request context, no route needed."""
    with app.test_request_context("/" + query, method="POST"):
        rv = rms.master_tick()
    body, status = rv if isinstance(rv, tuple) else (rv, 200)
    return body.get_json(), status


@pytest.fixture
def wired(monkeypatch):
    """Drive the REAL master_tick with every collaborator stubbed except the
    claim, recording the order in which they are called."""
    calls = []
    monkeypatch.setattr(rms, "_admin_ok", lambda: True)
    monkeypatch.setattr(rms, "_disabled", lambda: False)
    monkeypatch.setattr(rms, "_prev_snapshot", lambda: None)
    monkeypatch.setattr(rms, "_act_enabled", lambda: True)
    monkeypatch.setattr(rms, "_levers_off", lambda: [])
    monkeypatch.setattr(rms, "tier1_measure", lambda prev: (calls.append("measure"), {})[1])
    monkeypatch.setattr(rms, "tier2_score_levers",
                        lambda m: {"weakest": "freshness", "scores": {"freshness": 1}})
    monkeypatch.setattr(rms, "tier3_act",
                        lambda m, l: (calls.append("ACT"),
                                      {"action": "reindex_nudge", "findings_filed": 0})[1])
    monkeypatch.setattr(rms, "rag_score", lambda l: 50.0)
    monkeypatch.setattr(rms, "_persist",
                        lambda m, l, s, a: (calls.append("persist"), True)[1])
    return calls


def _claim(monkeypatch, calls, won, why):
    def fake(force=False):
        calls.append("claim")
        return (won, why)
    monkeypatch.setattr(rms, "_claim_utc_day", fake)


# ── the ordering, which IS the defect ────────────────────────────────
def test_claim_is_taken_before_any_action(app, wired, monkeypatch):
    _claim(monkeypatch, wired, True, "claimed")
    _, status = _tick(app)
    assert status == 200
    assert "claim" in wired and "ACT" in wired
    assert wired.index("claim") < wired.index("ACT"), (
        f"claim must precede the action, got {wired}")


def test_lost_claim_takes_no_action(app, wired, monkeypatch):
    _claim(monkeypatch, wired, False, "already_ran_today")
    body, _ = _tick(app)
    assert body["skipped"] == "already_ran_today"
    assert "ACT" not in wired, "a tick that lost the day still acted"
    assert "persist" not in wired


def test_claim_outage_fails_closed(app, wired, monkeypatch):
    """An unreachable DB must NOT fall through to an unguarded action."""
    _claim(monkeypatch, wired, False, "claim_unavailable")
    body, _ = _tick(app)
    assert body["skipped"] == "claim_unavailable", (
        "an outage must be reported differently from a healthy dedupe")
    assert "ACT" not in wired


def test_two_ticks_in_the_same_second_act_once(app, wired, monkeypatch):
    """The concurrent case, with the claim honouring uniqueness as Postgres
    would: the second caller of the day loses and must not act."""
    taken = set()

    def fake(force=False):
        wired.append("claim")
        if force:
            taken.discard("today")
        if "today" in taken:
            return False, "already_ran_today"
        taken.add("today")
        return True, "claimed"

    monkeypatch.setattr(rms, "_claim_utc_day", fake)
    assert _tick(app)[1] == 200
    assert _tick(app)[0]["skipped"] == "already_ran_today"
    assert wired.count("ACT") == 1, f"expected exactly one action, got {wired}"


def test_force_overrides_the_claim(app, wired, monkeypatch):
    seen = {}

    def fake(force=False):
        seen["force"] = force
        wired.append("claim")
        return True, "claimed"

    monkeypatch.setattr(rms, "_claim_utc_day", fake)
    _tick(app, "?force=1")
    assert seen["force"] is True, "?force=1 must reach the claim, or manual runs are dead"


# ── the half CI cannot execute: the shape of the claim statement ─────
SRC = open(os.path.join(os.path.dirname(__file__), "..",
                        "routes", "rag_master_shell.py"), encoding="utf-8").read()


def _claim_sql():
    fn = [n for n in ast.walk(ast.parse(SRC))
          if isinstance(n, ast.FunctionDef) and n.name == "_claim_utc_day"]
    assert fn, "_claim_utc_day not found — target moved, not passing"
    return " ".join(n.value for n in ast.walk(fn[0])
                    if isinstance(n, ast.Constant) and isinstance(n.value, str))


def test_claim_insert_is_atomic_not_a_read_then_write():
    sql = " ".join(_claim_sql().split()).upper()
    assert "ON CONFLICT (UTC_DAY) DO NOTHING" in sql, (
        "the claim must be a single conflicting INSERT; a SELECT-then-INSERT "
        "is the very race this fixes")
    assert "RETURNING UTC_DAY" in sql, (
        "without RETURNING the caller cannot tell winning the day from losing it")
    assert "SELECT" not in sql.replace("ON CONFLICT", ""), (
        "no read step may creep into the claim")


def test_the_claim_table_is_keyed_on_the_day():
    ddl = " ".join(SRC.split()).upper()
    assert "UTC_DAY DATE PRIMARY KEY" in ddl, (
        "uniqueness must be enforced by the DB, not by application logic")


# ── the bootstrap deadlock ───────────────────────────────────────────
def test_claim_creates_its_table_before_claiming(monkeypatch):
    """The claim runs BEFORE _persist(). If rag_tick_claims is only created by
    _persist() (where the rest of this module's DDL lives), the first tick after
    deploy raises on the INSERT, fails closed, and never reaches _persist() to
    create the table — the shell bricks itself permanently on exactly the deploy
    meant to protect it. So the claim must ensure its own table first."""
    order = []

    class _Cur:
        def execute(self, sql, params=None): order.append("INSERT")
        def fetchone(self): return ("2026-09-19",)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self, *a, **k): return _Cur()
        def close(self): pass

    monkeypatch.setattr(rms, "_ensure_tables", lambda: (order.append("DDL"), True)[1])
    monkeypatch.setattr(rms, "_conn", lambda: _Conn())
    won, why = rms._claim_utc_day()
    assert won and why == "claimed"
    assert "DDL" in order, "the claim never ensured its table exists"
    assert order.index("DDL") < order.index("INSERT"), (
        f"table must be ensured before the claim INSERT, got {order}")


def test_claim_fails_closed_when_the_table_cannot_be_created(monkeypatch):
    monkeypatch.setattr(rms, "_ensure_tables", lambda: False)
    monkeypatch.setattr(rms, "_conn",
                        lambda: pytest.fail("must not reach the DB after DDL failure"))
    assert rms._claim_utc_day() == (False, "claim_unavailable")
