"""Publish-time claim guards — 2026-07-17 double-post fix.

Locks the two atomic claims added after the verbatim output audit found
same-slot LinkedIn pairs published 12-14 SECONDS apart (Rural SPP
2026-07-17 08:08:41 + 08:08:55; AWS 07-16):

  1. QUAD SLOT CLAIM — linkedin_quad_daily._claim_slot: one
     INSERT ... ON CONFLICT ... RETURNING statement claims the
     (slot_date, slot_hour) before the ~30-60s compose+publish window.
     _already_posted was check-then-act (the success row lands only AFTER
     publishing), so two heartbeat ticks seconds apart both passed it and
     both published; ON CONFLICT then collapsed the audit trail to one
     linkedin_quad_posts row, hiding the duplicate.

  2. CONTENT-FP CLAIM — linkedin_poster._claim_publish_fp: the duplicate
     gate SELECTs linkedin_posts before posting (also check-then-act), so
     two replicas publishing the same content both passed. The claim keys
     on a digit-stripped fingerprint (brain_proposal_dedup.norm_condition
     style) so live-count/date-stamp drift between a pair still collides.

Both claims FAIL OPEN on DB errors — the guard must never dark-hold the
feed. DB-free: fake connections/executors only. Never imports main
(pre-merge CI has no DB/JWT_SECRET).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import contextlib  # noqa: E402
import datetime  # noqa: E402

import pytest  # noqa: E402

lq = pytest.importorskip("routes.linkedin_quad_daily")  # noqa: E402
lp = pytest.importorskip("linkedin_poster")  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, fetchone_result=None, raise_on_execute=False):
        self._fetchone_result = fetchone_result
        self._raise = raise_on_execute
        self.executed = []

    def execute(self, sql, params=None):
        if self._raise:
            raise RuntimeError("db down")
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _fake_conn_ctx(cursor):
    @contextlib.contextmanager
    def _ctx():
        yield _FakeConn(cursor)
    return _ctx


SLOT_DATE = datetime.date(2026, 7, 17)


# ── 1. quad slot claim ───────────────────────────────────────────────

def test_claim_slot_winner_proceeds(monkeypatch):
    cur = _FakeCursor(fetchone_result=(123,))
    monkeypatch.setattr(lq, "_pg", object())  # DB "available" for the guard
    monkeypatch.setattr(lq, "_conn", _fake_conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    assert lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data") is True
    sql, params = cur.executed[0]
    # The whole point: a single atomic statement, not check-then-act.
    assert "ON CONFLICT (slot_date, slot_hour) DO UPDATE" in sql
    assert "RETURNING id" in sql
    # Claim only steals from a NON-successful, EXPIRED in-flight claim.
    assert "success IS NOT TRUE" in sql
    assert "make_interval" in sql
    assert params[0] == SLOT_DATE and params[1] == 8


def test_claim_slot_loser_skips(monkeypatch):
    cur = _FakeCursor(fetchone_result=None)  # conflict row held by the peer
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _fake_conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    assert lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data") is False


def test_claim_slot_fails_open_on_db_error(monkeypatch):
    cur = _FakeCursor(raise_on_execute=True)
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _fake_conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    assert lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data") is True


def test_claim_slot_fails_open_without_db(monkeypatch):
    # no DSN → guard is a no-op, never a blocker
    monkeypatch.setattr(lq, "_dsn", lambda: "")
    assert lq._claim_slot(SLOT_DATE, 8, "dcpi_mover", "data") is True


def test_record_on_conflict_fills_claim_row(monkeypatch):
    """_claim_slot pre-inserts a bare row, so the winner's own _record now
    ALWAYS hits the ON CONFLICT path — it must fill the content fields the
    claim left NULL, or every quad row would lose its post_text."""
    cur = _FakeCursor()
    monkeypatch.setattr(lq, "_pg", object())
    monkeypatch.setattr(lq, "_conn", _fake_conn_ctx(cur))
    monkeypatch.setattr(lq, "_dsn", lambda: "postgres://x")
    lq._record(SLOT_DATE, 8, "dcpi_mover", "data", "post body", "https://l",
               "https://og", {"ok": True, "urn": "urn:li:share:1"})
    sql, _params = cur.executed[0]
    assert "post_text=COALESCE(EXCLUDED.post_text" in sql
    assert "landing_url=COALESCE(EXCLUDED.landing_url" in sql
    assert "og_image_url=COALESCE(EXCLUDED.og_image_url" in sql
    assert "posted_at=NOW()" in sql


def test_run_route_claims_before_publish():
    """The claim must sit in run() between the already-posted check and the
    compose+publish — source-level pin so a refactor can't silently drop it."""
    import inspect
    src = inspect.getsource(lq.run)
    assert "_claim_slot(" in src
    assert "slot_claimed_by_peer" in src
    # ordering: claim happens before the editorial decision / composer call
    assert src.index("_claim_slot(") < src.index("editorial_decision")


# ── 2. content-fingerprint claim (linkedin_poster) ───────────────────

def test_publish_fp_survives_digit_and_stamp_drift():
    a = ("427 GW now sit in ERCOT's interconnection queue.\n\n"
         "(DC Hub data · Jul 17, 2026)")
    b = ("428 GW now sit in ERCOT's interconnection queue.\n\n"
         "(DC Hub data · Jul 18, 2026)")
    assert lp._publish_fp(a) == lp._publish_fp(b)


def test_publish_fp_distinguishes_different_stories():
    a = "427 GW now sit in ERCOT's interconnection queue."
    b = "Cheyenne leads the DCPI excess-power index at 70/100."
    assert lp._publish_fp(a) != lp._publish_fp(b)


def test_claim_publish_fp_won(monkeypatch):
    calls = []

    def _fake_execute(sql, params=None, fetch=False, fetchall=False):
        calls.append(sql)
        return {"fp": "abc"} if fetch else None

    monkeypatch.setattr(lp, "_execute", _fake_execute)
    assert lp._claim_publish_fp("some analyst post text") is True
    assert any("ON CONFLICT (fp) DO UPDATE" in s for s in calls)
    assert any("RETURNING fp" in s for s in calls)


def test_claim_publish_fp_lost(monkeypatch):
    monkeypatch.setattr(
        lp, "_execute",
        lambda sql, params=None, fetch=False, fetchall=False: None)
    assert lp._claim_publish_fp("some analyst post text") is False


def test_claim_publish_fp_fails_open_on_db_error(monkeypatch):
    def _boom(sql, params=None, fetch=False, fetchall=False):
        raise RuntimeError("db down")

    monkeypatch.setattr(lp, "_execute", _boom)
    assert lp._claim_publish_fp("some analyst post text") is True


def test_claim_publish_fp_kill_switch(monkeypatch):
    def _boom(sql, params=None, fetch=False, fetchall=False):
        raise AssertionError("must not touch the DB when disabled")

    monkeypatch.setattr(lp, "_execute", _boom)
    monkeypatch.setenv("DCHUB_LINKEDIN_PUBLISH_CLAIM", "0")
    assert lp._claim_publish_fp("text") is True


def test_post_to_linkedin_refuses_on_lost_claim(monkeypatch):
    """End-to-end wiring: dup gate passes (no prior row), claim LOSES → the
    poster returns the standard duplicate_gate refusal BEFORE any token or
    API work (no network in this test by construction)."""
    def _fake_execute(sql, params=None, fetch=False, fetchall=False):
        if "linkedin_publish_claims" in sql and "INSERT" in sql:
            return None  # claim lost
        if fetch or fetchall:
            return None  # dup-gate SELECT: no prior post
        return None

    monkeypatch.setattr(lp, "_execute", _fake_execute)
    ok, meta = lp.post_to_linkedin(
        "A perfectly reasonable analyst post about grid capacity with "
        "enough words to clear the quality gate.")
    assert ok is False
    assert meta.get("error") == "duplicate_gate"
    assert "claim" in (meta.get("reason") or "")


def test_release_claim_on_failure_branches():
    """Source pin: both failure branches release the claim so a transient
    API error can't lock the content out for the whole dup window."""
    import inspect
    src = inspect.getsource(lp.post_to_linkedin)
    assert src.count("_release_publish_fp(") >= 2
