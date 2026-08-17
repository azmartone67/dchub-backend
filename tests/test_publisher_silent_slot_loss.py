"""tests/test_publisher_silent_slot_loss.py — the publisher's THIRD silent failure (2026-08-16).

Two defects, one root cause: a slot that produces nothing must SAY so, and a slot
whose hour has passed must not be dumped hours late.

★ ABANDONED CLAIMS. `_claim_slot` pre-inserts
`success=FALSE, error_msg='claimed_in_flight'` and sets ONLY `claimed_at`;
`_record` fills `posted_at` later. Die in between and the row is stranded — and
it reads like "in progress", never like "failed". Worse, EVERY counter feeding
the verdict filtered on `posted_at`, which a stranded row never gets, so the
rows were invisible even to `attempts_7d`.

Measured 08-08..08-15: 10 of 30 slots ended `claimed_in_flight`, none with a
success row for the same (slot_date, slot_hour) — they produced nothing. With 8
stranded claims in-window the publisher still reported **healthy**, because
`MEDIA_LINKEDIN_WEEKLY_FLOOR=7` sat against a 4-slots/day = **28/wk** cadence:
lose 75% of the feed, still pass.

★ THE BURST. The catch-up backfill had no age bound. Once the publish block
cleared on 08-15, four consecutive ticks each took the next-most-recent due slot
and fired 20:48 / 20:49 / 20:54 / 21:01 — four posts to one company page in 13
minutes, three carrying the same headline.

Run:  python3 -m pytest tests/test_publisher_silent_slot_loss.py -v
"""
from __future__ import annotations

import pytest

from routes.dchub_media_revival import linkedin_publisher_verdict
from routes import linkedin_quad_daily as lq


# A publisher that is otherwise perfectly healthy: publishing at cadence, on
# time, with cards, nothing gate-blocked. Only the field under test varies.
def _healthy(**over):
    q = {
        "hours_since_success": 2.0,
        "published_24h": 4,
        "published_7d": 28,
        "attempts_7d": 28,
        "with_image_7d": 28,
        "gate_blocked_3d": 0,
        "abandoned_claims_7d": 0,
    }
    q.update(over)
    return q


# ── abandoned claims must be counted, surfaced, and must move the verdict ──

def test_healthy_baseline_is_healthy():
    """Control: without this, every assertion below could pass vacuously."""
    assert linkedin_publisher_verdict(_healthy())["verdict"] == "healthy"


def test_abandoned_claims_are_surfaced():
    v = linkedin_publisher_verdict(_healthy(abandoned_claims_7d=8))
    assert v["abandoned_claims_7d"] == 8


def test_stranded_claims_degrade_the_verdict():
    """THE PIN: 8 slots that produced nothing must not read 'healthy'."""
    v = linkedin_publisher_verdict(_healthy(abandoned_claims_7d=8))
    assert v["verdict"] == "degraded"
    assert any("claimed then abandoned" in r for r in v["reasons"])


def test_a_couple_of_stranded_claims_is_tolerated():
    """Below the threshold this is noise, not an outage — no false alarm."""
    assert linkedin_publisher_verdict(_healthy(abandoned_claims_7d=2))["verdict"] == "healthy"


def test_missing_key_does_not_crash_or_alarm():
    """A pre-migration snapshot has no such key; absence must read as 0."""
    q = _healthy()
    del q["abandoned_claims_7d"]
    v = linkedin_publisher_verdict(q)
    assert v["verdict"] == "healthy"
    assert v["abandoned_claims_7d"] == 0


# ── the cadence floor must reflect the real 28/wk cadence ──────────────────

def test_quarter_strength_feed_is_not_healthy():
    """13/wk against a 4-slots/day cadence is a broken feed, not a healthy one.

    This is the exact reading the old floor of 7 passed.
    """
    v = linkedin_publisher_verdict(_healthy(published_7d=13, with_image_7d=13))
    assert v["verdict"] == "weak"


def test_full_cadence_still_healthy():
    """The raised floor must not condemn a publisher that is doing its job."""
    assert linkedin_publisher_verdict(_healthy(published_7d=28))["verdict"] == "healthy"


# ── the catch-up must skip stale slots ─────────────────────────────────────

SLOTS = [{"hour": h, "topic": f"t{h}", "style": "s"} for h in (8, 12, 16, 20)]
NONE_POSTED = lambda h: False


def test_catchup_takes_a_slot_inside_the_window():
    """A genuinely recent miss is still backfilled — the feature still works."""
    got = lq._catchup_slot(SLOTS, 21, NONE_POSTED)
    assert got is not None and got["hour"] == 20


def test_catchup_refuses_the_whole_stale_day():
    """THE BURST PIN: at 21:00 with nothing posted, only the 20:00 slot is
    eligible. 08/12/16 are missed, not pending."""
    got = lq._catchup_slot(SLOTS, 21, NONE_POSTED)
    assert got["hour"] == 20
    # and once 20 is posted, the day is over — no cascade down to 16/12/8
    assert lq._catchup_slot(SLOTS, 21, lambda h: h == 20) is None


def test_catchup_does_not_fire_0800_at_2100():
    """The literal 08-15 event: 08:00 must never be published at 21:01."""
    got = lq._catchup_slot(SLOTS, 21, lambda h: h in (12, 16, 20))
    assert got is None, "the 08:00 slot is 13h stale and must be abandoned"


def test_catchup_never_returns_a_future_slot():
    got = lq._catchup_slot(SLOTS, 9, NONE_POSTED)
    assert got["hour"] == 8


def test_catchup_window_is_configurable(monkeypatch):
    monkeypatch.setenv("LINKEDIN_QUAD_CATCHUP_MAX_AGE_HOURS", "13")
    got = lq._catchup_slot(SLOTS, 21, lambda h: h in (12, 16, 20))
    assert got is not None and got["hour"] == 8, "an explicit wide window must still work"


# ── suppressed slots (2026-08-17): stamped, counted apart, named in reasons ──
#
# The editorial suppress and the composer skip both exit BETWEEN _claim_slot
# and _record, which used to leave the row reading 'claimed_in_flight' — so
# the abandoned counter above reported deliberate desk silence as "the run
# died mid-flight". The 08-10..08-13 strands sat beside gate-refused slots
# from the same lead-supply drought (#2718), not beside crashes.


def _fake_db(monkeypatch):
    """Route lq's module-level DB plumbing at an in-memory recorder."""
    from contextlib import contextmanager

    class _Cur:
        def __init__(self):
            self.executed = []
        def execute(self, sql, params=None):
            self.executed.append((sql, params))
        def fetchone(self):
            return (1,)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self, cur):
            self._cur = cur
            self.commits = 0
        def cursor(self, **k):
            return self._cur
        def commit(self):
            self.commits += 1

    cur = _Cur()
    conn = _Conn(cur)

    @contextmanager
    def _conn_cm():
        yield conn

    monkeypatch.setattr(lq, "_pg", object())          # truthy stand-in
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://stub")
    monkeypatch.setattr(lq, "_conn", _conn_cm)
    return cur, conn


def test_suppressed_7d_flows_through_the_verdict():
    """Proves the verdict would lose the suppression count if the
    passthrough were removed (or the counter blanket-zeroed)."""
    out = linkedin_publisher_verdict(_healthy(suppressed_7d=5))
    assert out["suppressed_7d"] == 5


def test_suppression_alone_never_degrades():
    """Suppression is the desk doing its job — at full cadence it must not
    move the verdict, no matter how many slots were chosen silence."""
    out = linkedin_publisher_verdict(_healthy(suppressed_7d=20))
    assert out["verdict"] == "healthy"
    assert out["reasons"] == []


def test_floor_miss_names_lead_supply_when_slots_were_suppressed():
    """A floor miss with suppressions must say so, or the operator hunts a
    publisher crash that never happened."""
    out = linkedin_publisher_verdict(_healthy(published_7d=12, suppressed_7d=9))
    assert out["verdict"] == "weak"
    floor = [r for r in out["reasons"] if "floor" in r]
    assert floor and "suppress" in floor[0] and "9" in floor[0]


def test_floor_miss_without_suppressions_does_not_blame_lead_supply():
    """Inverse control: no suppressions, no lead-supply claim."""
    out = linkedin_publisher_verdict(_healthy(published_7d=12))
    floor = [r for r in out["reasons"] if "floor" in r]
    assert floor and "suppress" not in floor[0]


def test_stamp_rewrites_only_the_inflight_marker(monkeypatch):
    """The stamp must never clobber a recorded outcome or a peer's live
    claim: both WHERE guards have to reach the executed SQL, and the
    outcome must arrive truncated."""
    import datetime as _dt
    cur, conn = _fake_db(monkeypatch)
    lq._stamp_claim_outcome(_dt.date(2026, 8, 13), 16, "suppressed: x" * 200)
    assert conn.commits == 1
    sql, params = cur.executed[0]
    assert "error_msg = 'claimed_in_flight'" in sql, \
        "stamp lost its in-flight-state guard"
    assert "success IS NOT TRUE" in sql, "stamp lost its done-row guard"
    assert len(params[0]) == 500, "outcome must be truncated to 500"
    assert params[1:] == (_dt.date(2026, 8, 13), 16)


def test_stamp_swallows_db_failure(monkeypatch):
    """The stamp sits on the suppress fast-path — a DB blip must never turn
    a deliberate silence into a 500."""
    import datetime as _dt
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://stub")
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(lq, "_conn", _boom)
    lq._stamp_claim_outcome(_dt.date(2026, 8, 13), 16, "suppressed: y")  # no raise


def test_reclaim_resets_state_but_preserves_gate_refusals(monkeypatch):
    """The re-claim reset is what keeps error_msg describing the LATEST
    attempt — but it must carry the CASE that preserves 'gate:' refusals,
    or the #2718 selector feedback goes blind mid-retry and the desk
    re-elects the exact lead the gate just refused."""
    import datetime as _dt
    cur, conn = _fake_db(monkeypatch)
    won = lq._claim_slot(_dt.date(2026, 8, 13), 16, "capability", "data")
    assert won is True
    sql, _params = cur.executed[0]
    set_clause = sql.split("DO UPDATE", 1)[1].split("WHERE", 1)[0]
    assert "error_msg" in set_clause, "re-claim no longer resets the state"
    assert "claimed_in_flight" in set_clause
    assert "gate:%%" in set_clause, \
        "gate refusals must survive re-claims (and the %% doubling matters: " \
        "this execute() has an args tuple)"


def test_both_suppress_paths_stamp_before_returning():
    """Structure pin on run(): each suppress return block must contain a
    _stamp_claim_outcome call, or the strand comes straight back."""
    import ast, inspect
    fn = next(n for n in ast.walk(ast.parse(inspect.getsource(lq)))
              if isinstance(n, ast.FunctionDef) and n.name == "run")
    found = {"editorial_suppress_no_novel_event": None,
             "composer_skip_nothing_new": None}
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        dump = "\n".join(ast.dump(s) for s in node.body)
        for marker in found:
            if f"'{marker}'" in dump and any(isinstance(s, ast.Return)
                                             for s in node.body):
                found[marker] = ("_stamp_claim_outcome" in dump)
    # positive control first: both suppress returns must still exist at all
    assert None not in found.values(), f"suppress return not found: {found}"
    assert all(found.values()), f"suppress path without a stamp: {found}"
