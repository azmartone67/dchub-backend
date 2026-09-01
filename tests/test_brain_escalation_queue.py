"""The escalation queue's two load-bearing rules.

1. `activated` is MEASURED, never set by hand. The whole point of the queue
   is an honest read on whether human touches work; an endpoint that lets
   anyone stamp "activated" turns that into a self-report and the number
   stops meaning anything.
2. sync() never re-opens a row a human closed, and DOES auto-activate a row
   whose customer started calling.

No DB — safe_db is patched with a recording fake, so these are pure
function tests and run in the normal suite.
"""
import sys
import types

import pytest

import routes.brain_escalation_queue as eq


class FakeCursor:
    def __init__(self, script):
        self.script, self.executed, self._row, self._rows = script, [], None, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append((s, params))
        for needle, val in self.script.items():
            if needle in s:
                if isinstance(val, list):
                    self._rows, self._row = val, (val[0] if val else None)
                else:
                    self._row, self._rows = val, [val] if val else []
                return
        self._row, self._rows = None, []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, cur):
        self._cur, self.commits = cur, 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1


@pytest.fixture
def patched(monkeypatch):
    """Patch ensure_schema (no DDL) and safe_db (no DB), return a factory."""
    monkeypatch.setattr(eq, "ensure_schema", lambda: True)

    def make(script):
        cur = FakeCursor(script)
        conn = FakeConn(cur)

        class _CM:
            def __enter__(self_):
                return conn

            def __exit__(self_, *a):
                return False

        fake_db = types.ModuleType("db_utils")
        fake_db.safe_db = lambda: _CM()
        fake_db.ddl_cursor = lambda: _CM()
        monkeypatch.setitem(sys.modules, "db_utils", fake_db)
        return cur, conn

    return make


# ── rule 1: `activated` is measured, not asserted ────────────────────────

def test_activated_cannot_be_set_by_hand(patched):
    patched({})
    out = eq._set_status("a@b.com", "activated", "I think they came back", "me")
    assert out["ok"] is False
    assert "measured" in out["error"]


def test_open_cannot_be_set_by_hand(patched):
    """Re-opening by hand would let a closed row silently return to the
    queue and double-count the same customer."""
    patched({})
    out = eq._set_status("a@b.com", "open", "", "me")
    assert out["ok"] is False


@pytest.mark.parametrize("status", ["contacted", "resolved", "dismissed"])
def test_the_three_human_statuses_are_accepted(patched, status):
    cur, conn = patched({"UPDATE brain_escalations": (7, "a@b.com", status)})
    out = eq._set_status("a@b.com", status, "note", "jonathan")
    assert out["ok"] is True, out
    assert out["status"] == status
    assert conn.commits == 1


def test_resolve_reports_a_miss_rather_than_claiming_success(patched):
    """No matching row must not read as a successful resolve."""
    patched({"UPDATE brain_escalations": None})
    out = eq._set_status("nobody@nowhere.com", "resolved", "", "me")
    assert out["ok"] is False
    assert "no escalation" in out["error"]


# ── rule 2: sync's open/close semantics ──────────────────────────────────

OPEN_SWEEP = "status IN ('open', 'contacted')"
REPAIR_SWEEP = "status = 'activated' AND resolved_by = 'system'"


def _cust(email, calls=0, escalate=True):
    return {"email": email, "name": "N", "plan": "pro", "stage": "stranded",
            "action": "ESCALATE: nudged 39d ago, still zero calls",
            "priority": 3, "total_calls": calls, "mcp_calls": 0,
            "web_calls": calls, "joined_days": 60.0, "idle_days": None,
            "nudge_days": 39.9, "welcomed": True, "nudged": True,
            "welcome_attempted": True, "escalate": escalate}


def _roster(*emails):
    return [_cust(e) for e in emails]


def test_sync_opens_a_row_for_each_escalating_customer(patched, monkeypatch):
    monkeypatch.setattr(eq, "_roster_all",
                        lambda: _roster("a@b.com", "c@d.com"))
    cur, conn = patched({
        "INSERT INTO brain_escalations": (True,),          # xmax=0 -> inserted
        OPEN_SWEEP: [], REPAIR_SWEEP: [],
    })
    out = eq.sync()
    assert out["ok"] is True, out
    assert out["escalating_now"] == 2
    assert out["opened"] == 2 and out["refreshed"] == 0


def test_sync_refreshes_rather_than_duplicating_a_known_escalation(patched, monkeypatch):
    monkeypatch.setattr(eq, "_roster_all", lambda: _roster("a@b.com"))
    cur, conn = patched({
        "INSERT INTO brain_escalations": (False,),         # conflict -> update
        OPEN_SWEEP: [], REPAIR_SWEEP: [],
    })
    out = eq.sync()
    assert out["opened"] == 0 and out["refreshed"] == 1


def test_sync_auto_activates_only_when_the_call_count_actually_moved(patched, monkeypatch):
    """★ The verifier. c@d.com left the escalating set AND its total_calls
    rose above calls_at_open — that, and only that, is activation."""
    monkeypatch.setattr(eq, "_roster_all",
                        lambda: [_cust("a@b.com"),
                                 _cust("c@d.com", calls=5, escalate=False)])
    cur, conn = patched({
        "INSERT INTO brain_escalations": (False,),
        OPEN_SWEEP: [(1, "a@b.com", 0), (2, "c@d.com", 0)],
        REPAIR_SWEEP: [],
    })
    out = eq.sync()
    assert out["auto_activated"] == 1, out
    ids = [p[-1] for s, p in cur.executed if "SET status = 'activated'" in s]
    assert ids == [2], "must activate c@d.com (id 2), not the still-stranded row"


def test_sync_does_not_activate_a_row_that_left_the_roster_without_calling(patched, monkeypatch):
    """★ THE REGRESSION. Measured 2026-08-31: nine rows were stamped
    `activated` purely for leaving the escalating set, every one of them at
    total_calls=0. Cancelling, changing plan, or the classifier changing its
    mind all drop an account off that set. None of them is activation."""
    monkeypatch.setattr(eq, "_roster_all",
                        lambda: [_cust("a@b.com"),
                                 _cust("c@d.com", calls=0, escalate=False)])
    cur, conn = patched({
        "INSERT INTO brain_escalations": (False,),
        OPEN_SWEEP: [(2, "c@d.com", 0)],
        REPAIR_SWEEP: [],
    })
    out = eq.sync()
    assert out["auto_activated"] == 0, out
    assert out["left_roster_no_calls"] == 1, out
    assert not [s for s, _ in cur.executed if "SET status = 'activated'" in s]


def test_sync_treats_an_empty_roster_as_unmeasured_and_changes_nothing(patched, monkeypatch):
    """★ THE ROOT CAUSE. A roster read of zero rows is a missing measurement.
    Treated as 'nobody is escalating' it empties the whole queue in one run —
    which is exactly what happened at 2026-08-31T11:19:05Z."""
    monkeypatch.setattr(eq, "_roster_all", lambda: [])
    cur, conn = patched({"INSERT INTO brain_escalations": (False,),
                         OPEN_SWEEP: [(1, "a@b.com", 0), (2, "c@d.com", 0)],
                         REPAIR_SWEEP: []})
    out = eq.sync()
    assert out["ok"] is False, out
    assert "UNMEASURED" in out["error"]
    assert out["auto_activated"] == 0
    assert cur.executed == [], "an unmeasured roster must not touch the table"


def test_sync_reopens_a_row_the_system_wrongly_activated(patched, monkeypatch):
    """★ THE REPAIR. An account the roster still calls escalating, at zero
    calls, cannot be `activated`. Nothing else in this module can set `open`,
    so the undo lives here."""
    monkeypatch.setattr(eq, "_roster_all", lambda: _roster("a@b.com"))
    cur, conn = patched({
        "INSERT INTO brain_escalations": (False,),
        OPEN_SWEEP: [],
        REPAIR_SWEEP: [(1, "a@b.com", 0)],
    })
    out = eq.sync()
    assert out["reopened"] == 1, out
    assert [p[0] for s, p in cur.executed if "SET status = 'open'" in s] == [1]


def test_repair_leaves_a_genuine_activation_alone(patched, monkeypatch):
    """If the call count really did move, the row stays activated even while
    the account lingers on the escalating set."""
    monkeypatch.setattr(eq, "_roster_all",
                        lambda: [_cust("a@b.com", calls=9)])
    cur, conn = patched({
        "INSERT INTO brain_escalations": (False,),
        OPEN_SWEEP: [],
        REPAIR_SWEEP: [(1, "a@b.com", 0)],
    })
    out = eq.sync()
    assert out["reopened"] == 0, out
    assert not [s for s, _ in cur.executed if "SET status = 'open'" in s]


def test_repair_never_overrules_a_human(patched, monkeypatch):
    """The repair sweep reads only resolved_by='system'. A row the owner
    resolved or dismissed must never be dragged back into the queue."""
    monkeypatch.setattr(eq, "_roster_all", lambda: _roster("a@b.com"))
    cur, _ = patched({"INSERT INTO brain_escalations": (False,),
                      OPEN_SWEEP: [], REPAIR_SWEEP: []})
    eq.sync()
    repair = [s for s, _ in cur.executed if "status = 'activated'" in s
              and s.startswith("SELECT")]
    assert len(repair) == 1
    assert "resolved_by = 'system'" in repair[0]


def test_sync_does_not_touch_rows_a_human_already_closed(patched, monkeypatch):
    """The auto-activate sweep reads only open/contacted. A resolved or
    dismissed row must never be revived — that would re-queue a customer
    the owner already decided about."""
    monkeypatch.setattr(eq, "_roster_all", lambda: _roster("a@b.com"))
    cur, _ = patched({"INSERT INTO brain_escalations": (False,),
                      OPEN_SWEEP: [], REPAIR_SWEEP: []})
    eq.sync()
    sweep = [s for s, _ in cur.executed if OPEN_SWEEP in s and s.startswith("SELECT")]
    assert len(sweep) == 1


def test_sync_reports_a_broken_roster_instead_of_an_empty_queue(patched, monkeypatch):
    """A roster that raises must NOT read as 'nobody is escalating' — that
    is the exact failure this whole module exists to stop."""
    def boom():
        raise RuntimeError("white glove down")
    monkeypatch.setattr(eq, "_roster_all", boom)
    patched({})
    out = eq.sync()
    assert out["ok"] is False
    assert "roster unavailable" in out["error"]


def test_queue_counts_contacted_as_still_open_work(patched):
    """A contacted-but-not-activated customer is not done. Counting them as
    closed would let the queue look drained while nine people sit idle."""
    patched({"SELECT status, COUNT(*)": [("open", 4), ("contacted", 3),
                                         ("activated", 2)],
             "SELECT id, email, name": []})
    out = eq.queue()
    assert out["open_total"] == 7
