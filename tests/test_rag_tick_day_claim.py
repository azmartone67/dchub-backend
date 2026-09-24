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
import threading
from datetime import datetime, timezone

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


# ── whose env is being reported ──────────────────────────────────────
def test_web_reports_that_it_is_not_the_actor(monkeypatch):
    """master-tick is proxied to the worker (main.py _WORKER_PROXY_POST_PATHS),
    so on a web box mode/levers_off describe a process that never runs
    tier3_act. The surface must say so — a fence set on the wrong service reads
    exactly like a fence that works."""
    monkeypatch.setenv("DCHUB_ROLE", "web")
    sc = rms._arm_scope()
    assert sc["role"] == "web"
    assert sc["is_actor"] is False, "web must not claim to be the actor"
    assert "worker" in sc["note"], "the note must point at where the truth is"


def test_worker_reports_that_it_is_the_actor(monkeypatch):
    monkeypatch.setenv("DCHUB_ROLE", "worker")
    sc = rms._arm_scope()
    assert sc["is_actor"] is True


def test_unset_role_is_treated_as_the_actor(monkeypatch):
    """DCHUB_ROLE unset means an all-in-one box (local, failover/Render) that
    DOES run its own tick. Defaulting to 'not the actor' would silence the
    fence report on exactly the deployment where it is the only truth."""
    monkeypatch.delenv("DCHUB_ROLE", raising=False)
    assert rms._arm_scope()["is_actor"] is True


# ── the half that needs a real engine ────────────────────────────────
# Postgres, not a fake: the property under test is that ONE of N simultaneous
# INSERTs wins, which is a database guarantee. A stub cursor would report
# whatever its author expected. Opt in by adding this file to the
# a throwaway Postgres via RAG_TICK_CLAIM_DSN; it skips (and says so) without one.
def _pg():
    """A THROWAWAY Postgres, opted into by its own variable.

    ★ Deliberately NOT DATABASE_URL. In this repo DATABASE_URL is the
    PRODUCTION dsn — routes/ai_reach._conn() reads exactly that — and the
    fixture below DELETEs from rag_tick_claims. Keyed on DATABASE_URL, running
    the suite on any box configured for prod would delete the live day-claim,
    and with the shell ARMED that is precisely the permission to fire a second
    time that this whole change exists to remove. A test that can do that is
    more dangerous than the bug.
    """
    dsn = os.environ.get("RAG_TICK_CLAIM_DSN", "")
    if not dsn:
        pytest.skip("set RAG_TICK_CLAIM_DSN to a throwaway Postgres to run this")
    low = dsn.lower()
    if any(x in low for x in ("amazonaws", "azure", "supabase", "rds.", "prod")):
        pytest.fail("RAG_TICK_CLAIM_DSN looks like a managed/production database; "
                    "this fixture deletes rows — point it at a throwaway")
    import psycopg2
    c = psycopg2.connect(dsn, connect_timeout=8)
    c.autocommit = True          # the claim MUST commit before its rival reads
    return c


@pytest.fixture
def pg(monkeypatch):
    monkeypatch.setattr(rms, "_conn", _pg)
    assert rms._ensure_tables(), "could not create rag_tick_claims"
    today = datetime.now(timezone.utc).date()

    def _clear():
        c = _pg()
        try:
            with c.cursor() as cur:
                cur.execute("DELETE FROM rag_tick_claims WHERE utc_day = %s", (today,))
        finally:
            c.close()

    _clear()
    try:
        yield today
    finally:
        _clear()                 # in finally: a failing assert must not leak a claim


def test_only_one_of_eight_simultaneous_claims_wins(pg):
    """The race this whole change exists to lose safely."""
    out, gate = [], threading.Barrier(8)

    def go():
        gate.wait()              # release all eight in the same instant
        out.append(rms._claim_utc_day())

    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=30)

    assert len(out) == 8, f"a thread did not finish: {out}"
    won = [r for r in out if r[0]]
    assert len(won) == 1, f"expected exactly one winner, got {len(won)}: {out}"
    # The losers must lose for the RIGHT reason. Without ON CONFLICT the
    # duplicate INSERT raises, the except returns claim_unavailable, and the
    # winner count is STILL 1 — so counting winners alone cannot tell a working
    # claim from a broken one that happens to serialise.
    assert all(r[1] == "already_ran_today" for r in out if not r[0]), (
        f"a loser reported an outage rather than a lost race: {out}")


def test_the_second_day_claims_cleanly(pg):
    """Yesterday's row must not block today — the key is the day, not a lock."""
    c = _pg()
    try:
        with c.cursor() as cur:
            cur.execute("INSERT INTO rag_tick_claims (utc_day) VALUES (%s) ON CONFLICT DO NOTHING "
                        "ON CONFLICT DO NOTHING", (pg.replace(day=1) if pg.day != 1
                                                   else pg.replace(day=2),))
    finally:
        c.close()
    assert rms._claim_utc_day() == (True, "claimed")
    assert rms._claim_utc_day() == (False, "already_ran_today")


def test_force_reclaims_the_same_day(pg):
    assert rms._claim_utc_day() == (True, "claimed")
    assert rms._claim_utc_day() == (False, "already_ran_today")
    assert rms._claim_utc_day(force=True) == (True, "claimed"), (
        "?force=1 must be able to re-run a day manually")
