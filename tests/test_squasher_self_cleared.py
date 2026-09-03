"""tests/test_squasher_self_cleared.py — the queue's exit for "it fixed itself".

THE DEFECT, AS MEASURED (GET /api/v1/brain/squasher, 2026-09-03 00:52Z):
60 open rows, oldest 313.9h, and 31 of them were ONE upstream outage — every
EU_* bidding zone carrying its own `iso_metric_count_zero_24h` row in
awaiting_decision. Two separate holes produced that board:

  1. NO EXIT FOR A SELF-CLEARED FINDING. The only writes to a closed status
     were an operator running the named endpoint or a human answering the
     decision. By the time the rows were read the upstream had recovered
     (/api/v1/iso/eu/health: token_configured=true, live_feed_ok=true, 29/33
     zones live) and nothing could say so, so the rows aged forever.
  2. NO FLEET IDENTITY. The collapse groups by action_url else finding_key,
     and "grid_data: iso=EU_CZ" vs "...EU_SK" are byte-distinct — so 31 rows
     for one ENTSO-E token each bought their own ~48s investigation.

What is proved here, as BEHAVIOUR against a params-aware cursor stub:
  · live_finding_keys() REFUSES on a non-200, on _warming_up, and on an empty
    payload — a broken detector and a clean world read identically, so an
    empty answer closes nothing;
  · sweep_self_cleared() closes only rows the detector no longer reports, only
    from sources the detector speaks for, only past a grace age, never a
    'running' row — and PUBLISHES every skip, because a sweep that quietly
    closes nothing is this design's real failure mode;
  · the >90% rename guard refuses a whole-queue clear;
  · drain() actually CALLS it (the _settle_for lesson: testing the helper and
    leaving the call site inline is how a lane no-ops with a green suite);
  · a self-cleared row is NOT a closure — convergence() excludes it from
    `closed` and reports it on its own key;
  · fleet grouping merges EU_* into one row, leaves PJM alone, and never
    overrides an action_url.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_squasher_self_cleared.py -v
"""
from __future__ import annotations

import datetime as _dt
import inspect

import pytest

from routes import squasher_queue as sq
from tests.test_squasher_open_identity import _Conn, _Cur, _open_row

UTC = _dt.timezone.utc
NOW = _dt.datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
OLD = NOW - _dt.timedelta(hours=48)
FRESH = NOW - _dt.timedelta(hours=1)

CZ = "grid_data: iso=EU_CZ"
SK = "grid_data: iso=EU_SK"
SE = "grid_data: iso=EU_SE_1"
PJM = "grid_data: iso=PJM"
FAMILY = "iso_metric_count_zero_24h"

SWEEP_SELECT = "FROM squasher_work_queue WHERE status IN ('queued', 'awaiting_ops'"
SWEEP_UPDATE = "SET status = 'self_cleared'"


def _sweep_row(id_, key, status="awaiting_decision", source="heal", seen=OLD):
    """The sweep SELECT's shape: id, finding_key, status, source, last_seen."""
    return (id_, key, status, source, seen)


def _cur(rows):
    return _Cur([(SWEEP_SELECT, None, list(rows))])


def _live(*keys):
    return {"ok": True, "keys": set(keys), "items": len(keys)}


def _closed(cur):
    return [p for s, p in cur.calls if SWEEP_UPDATE in s]


# ── live_finding_keys: every refusal is positive-signal-only ──────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def get_json(self):
        return self._p


class _Client:
    def __init__(self, resp):
        self._r = resp

    def get(self, *a, **k):
        return self._r

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_heal(monkeypatch, payload, status=200):
    import flask
    app = type("A", (), {"test_client": lambda self: _Client(_Resp(payload, status))})()
    monkeypatch.setattr(flask, "current_app", app, raising=False)
    monkeypatch.setattr(sq, "_self_headers", lambda: {})


def test_live_keys_refuses_a_non_200(monkeypatch):
    _stub_heal(monkeypatch, {}, status=503)
    out = sq.live_finding_keys()
    assert out["ok"] is False and "503" in out["reason"]
    assert out["keys"] == set()


def test_live_keys_refuses_while_the_detector_is_warming_up(monkeypatch):
    _stub_heal(monkeypatch, {"_warming_up": True,
                             "actionable_backend_issues": [{"url": CZ}]})
    out = sq.live_finding_keys()
    assert out["ok"] is False and "warming" in out["reason"]


def test_live_keys_refuses_an_empty_payload(monkeypatch):
    """A detector reporting nothing and a detector that is broken are the same
    bytes from here. ABSENT is not EMPTY — this must never become a sweep."""
    _stub_heal(monkeypatch, {"actionable_backend_issues": [],
                             "actionable_frontend_issues": []})
    out = sq.live_finding_keys()
    assert out["ok"] is False
    assert "closes nothing" in out["reason"]


def test_live_keys_reads_every_bucket_not_just_active(monkeypatch):
    """A finding routed 'terminal' is still REPORTED. Taking only the active
    bucket would read it as absent and sweep a row that is still true."""
    _stub_heal(monkeypatch, {
        "actionable_backend_issues": [{"url": CZ, "issue": FAMILY}],
        "actionable_frontend_issues": [{"url": "/pricing", "issue": "x"}]})
    out = sq.live_finding_keys()
    assert out["ok"] is True
    assert out["keys"] == {CZ, "/pricing"}


# ── the sweep itself ──────────────────────────────────────────────────────

def test_a_finding_the_detector_no_longer_reports_is_closed():
    cur = _cur([_sweep_row(317, CZ)])
    out = sq.sweep_self_cleared(cur, _live(PJM), now=NOW)
    assert out["ok"] is True and out["closed"] == [317]
    note, rid = _closed(cur)[0]
    assert rid == 317
    assert "self-cleared" in note
    assert "no fix was shipped" in note.lower(), (
        "the row must say plainly that nothing was fixed — a self-cleared row "
        "read as a fix is how this lane would start lying")


def test_an_unreadable_detector_closes_nothing():
    cur = _cur([_sweep_row(317, CZ)])
    out = sq.sweep_self_cleared(cur, {"ok": False, "reason": "HTTP 500"}, now=NOW)
    assert out["ok"] is False and out["closed"] == []
    assert _closed(cur) == [], "BLIND is not RED: a failed read closes nothing"


def test_a_finding_still_reported_is_left_alone():
    cur = _cur([_sweep_row(317, CZ)])
    out = sq.sweep_self_cleared(cur, _live(CZ, PJM), now=NOW)
    assert out["closed"] == [] and out["skipped_still_reported"] == 1
    assert _closed(cur) == []


def test_a_row_younger_than_the_grace_window_is_left_alone():
    """Never race a partial detector tick."""
    cur = _cur([_sweep_row(317, CZ, seen=FRESH)])
    out = sq.sweep_self_cleared(cur, _live(PJM), now=NOW)
    assert out["closed"] == [] and out["skipped_recent"] == 1


def test_a_source_the_detector_does_not_speak_for_is_skipped_and_REPORTED():
    """The graduation rows are the action-class grant proposals. No detector
    will ever re-observe them, so absence proves nothing — and sweeping them
    would silently retract every pending grant request.

    The count is asserted because a silent zero is the failure mode: the
    allow-list is a guess about strings written elsewhere in the repo, and if
    it is wrong the sweep no-ops. It must say so on the board."""
    cur = _cur([_sweep_row(901, "action-class-grant:news_entity_reresolve",
                           source="graduation"),
                _sweep_row(902, "x", source="")])
    out = sq.sweep_self_cleared(cur, _live(PJM), now=NOW)
    assert out["closed"] == []
    assert out["skipped_source"] == {"graduation": 1, "(empty)": 1}


def test_running_rows_are_not_even_selected():
    """A 'running' row is mid-investigate; _finish() would resurrect it."""
    assert "running" not in sq._SWEEPABLE_STATUSES
    assert "refused" not in sq._SWEEPABLE_STATUSES, "reclaim_misfiled owns that"
    for st in sq._SWEEPABLE_STATUSES:
        assert "'%s'" % st in sq._SELF_CLEARED_SELECT_SQL
    assert "'running'" not in sq._SELF_CLEARED_SELECT_SQL


def test_a_whole_queue_clear_is_refused_as_a_probable_rename():
    """31 of 60 must sweep. 20 of 20 must not — that is a finding_key format
    change wearing a healed world's clothes."""
    rows = [_sweep_row(i, "grid_data: iso=EU_%d" % i) for i in range(20)]
    assert len(rows) >= sq._SELF_CLEARED_MIN_FOR_FRACTION
    out = sq.sweep_self_cleared(_cur(rows), _live(PJM), now=NOW)
    assert out["ok"] is False
    assert "format change" in out["reason"]
    assert out["would_close"], "it must show what it refused to touch"


def test_the_real_board_ratio_is_not_refused():
    """CONTROL for the guard above: the 2026-09-03 shape — 31 EU zones cleared
    while 29 other rows are still reported — must sweep, not trip."""
    rows = ([_sweep_row(i, "grid_data: iso=EU_%d" % i) for i in range(31)]
            + [_sweep_row(500 + i, "still/true/%d" % i) for i in range(29)])
    live = _live(*["still/true/%d" % i for i in range(29)])
    out = sq.sweep_self_cleared(_cur(rows), live, now=NOW)
    assert out["ok"] is True and len(out["closed"]) == 31
    assert out["skipped_still_reported"] == 29


def test_dry_run_writes_nothing():
    cur = _cur([_sweep_row(317, CZ)])
    out = sq.sweep_self_cleared(cur, _live(PJM), now=NOW, dry_run=True)
    assert out["would_close"] == [317] and out["closed"] == []
    assert _closed(cur) == []


def test_the_update_cannot_touch_a_row_that_moved_underneath_it():
    """The UPDATE re-asserts the sweepable statuses in its WHERE clause, so a
    row that went 'running' between the SELECT and the write is not clobbered."""
    assert "AND status IN (" in sq._SELF_CLEARED_UPDATE_SQL
    assert "'awaiting_decision'" in sq._SELF_CLEARED_UPDATE_SQL


# ── the wiring, not just the helper ──────────────────────────────────────

def test_drain_calls_the_sweep_and_publishes_its_result(monkeypatch):
    """★ The _settle_for lesson, one lane over: a perfect sweep that drain()
    never calls is a green suite and a dead board. Mutation-checked — deleting
    the call from drain() fails HERE and nowhere else."""
    src = inspect.getsource(sq.drain)
    assert "sweep_self_cleared(cur)" in src
    assert 'out["self_cleared"]' in src
    # and it must run BEFORE work is selected, or a cleared finding is still
    # billed an investigation in this very pass.
    assert src.index("sweep_self_cleared") < src.index("WHERE status='queued'")


def test_self_cleared_is_declared_closed_and_never_open():
    assert "self_cleared" in sq.STATUSES
    assert "self_cleared" not in sq._OPEN_STATUSES
    assert "self_cleared" not in sq._NONMECHANICAL_STATUSES


def test_convergence_does_not_count_a_self_cleared_row_as_a_closure():
    """It would push recurrence_rate down for free — the lane taking credit
    for the world changing under it."""
    closed = sq._CONVERGENCE_SQL[: sq._CONVERGENCE_SQL.index("recurred AS")]
    assert "status <> 'self_cleared'" in closed


def test_convergence_still_reports_self_cleared_on_its_own_key():
    """Excluded from the rate, but never hidden: a big number here means the
    detector files findings that outlive their cause."""
    assert "status = 'self_cleared'" in sq._CONVERGENCE_SQL
    src = inspect.getsource(sq.convergence)
    assert '"self_cleared": self_cleared' in src


# ── fleet families ────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,scope", [
    (CZ, "EU"), (SK, "EU"), (SE, "EU"), (PJM, "PJM"),
    ("grid_data: iso=EU_IT_NORD", "EU"),
    # the aggregate is the same module + the same token as its zones
    ("grid_data: iso=ENTSOE", "EU"),
    ("grid_data: iso=ERCOT", "ERCOT"),
    ("/api/v1/something/else", None), ("grid_data: iso=", None), ("", None),
])
def test_iso_fleet_scope(key, scope):
    assert sq._iso_fleet_scope(key) == scope


def test_fleet_key_groups_a_region_and_separates_operators():
    a = sq.fleet_group_key(FAMILY, CZ)
    b = sq.fleet_group_key(FAMILY, SK)
    c = sq.fleet_group_key(FAMILY, PJM)
    assert a == b, "EU_CZ and EU_SK share one ENTSO-E token — one finding"
    assert c and c != a, "PJM is an independent operator — never merged into EU"


def test_an_action_url_always_wins_over_a_fleet_family():
    """The facility_dedup lesson: ?country=FR and ?country=NL are two calls a
    human has to make. Collapsing across them drops one on the floor."""
    assert sq.fleet_group_key(FAMILY, CZ, "/api/v1/admin/x?country=FR") is None


def test_an_unregistered_family_groups_exactly_as_before():
    assert sq.fleet_group_key("page_brand_uniformity", CZ) is None
    assert sq.fleet_group_key("", CZ) is None


def test_collapse_merges_the_eu_fleet_and_leaves_pjm_alone():
    T = _dt.datetime(2026, 9, 2, 3, 0, 0, tzinfo=UTC)
    rows = [
        _open_row(311, CZ, "awaiting_decision", T, title=FAMILY),
        _open_row(312, SK, "awaiting_decision", T + _dt.timedelta(minutes=1),
                  title=FAMILY),
        _open_row(313, SE, "awaiting_decision", T + _dt.timedelta(minutes=2),
                  title=FAMILY),
        _open_row(400, PJM, "awaiting_decision", T, title=FAMILY),
    ]
    cur = _Cur([("COALESCE(NULLIF(action_url, ''), finding_key)", None, rows)])
    out = sq.collapse_duplicate_open_rows(cur)
    assert out["kept"] == [311], "the oldest EU row carries the fleet"
    assert sorted(out["superseded"]) == [312, 313]
    assert 400 not in out["superseded"], "PJM has one row and its own upstream"
    entry = out["detail"][0]
    assert entry["fleet"] is True
    assert entry["members"] == [CZ, SK, SE]


def test_the_fleet_keeper_row_names_every_target_it_stands_for():
    """A human answering "is the EU feed down?" must see it settles 31 zones.
    Without this the keeper reads as one Czech zone and gets answered 31
    times — which is the bug this whole family rule exists to stop."""
    T = _dt.datetime(2026, 9, 2, 3, 0, 0, tzinfo=UTC)
    rows = [_open_row(311, CZ, "awaiting_decision", T, title=FAMILY),
            _open_row(312, SK, "awaiting_decision", T + _dt.timedelta(minutes=1),
                      title=FAMILY)]
    cur = _Cur([("COALESCE(NULLIF(action_url, ''), finding_key)", None, rows)])
    sq.collapse_duplicate_open_rows(cur)
    stamps = [p for s, p in cur.calls if "fleet finding:" in str(p)]
    assert stamps, "the keeper's reason must carry the fleet scope"
    text, rid = stamps[0]
    assert rid == 311 and CZ in text and SK in text
    # and it must not grow without bound across repeated drains
    # %% because psycopg2 collapses it to a literal % when the statement
    # carries parameters (the literal-percent trap).
    assert "NOT LIKE 'fleet finding:%%'" in sq._FLEET_KEEPER_REASON_SQL


def test_a_short_row_degrades_to_the_old_grouping_instead_of_vanishing():
    """A SELECT shape mismatch mid-deploy must not turn the collapse into a
    silent no-op — that is the exact failure this function was written for."""
    T = _dt.datetime(2026, 9, 2, 3, 0, 0, tzinfo=UTC)
    short = [(311, CZ, "awaiting_decision", T, 1, T, CZ),
             (312, CZ, "awaiting_decision", T + _dt.timedelta(minutes=1), 1,
              T + _dt.timedelta(minutes=1), CZ)]
    cur = _Cur([("COALESCE(NULLIF(action_url, ''), finding_key)", None, short)])
    out = sq.collapse_duplicate_open_rows(cur)
    assert out["kept"] == [311] and out["superseded"] == [312]


def test_the_rename_guard_needs_a_denominator_before_it_fires():
    """REGRESSION. The guard first shipped without a floor and refused the
    ordinary case: one open row, one cleared finding, 1/1 = 100%. Every small
    board — which is most boards — swept nothing while reporting ok=False.
    A rate over a denominator of 1 is noise, exactly as convergence() says."""
    small = [_sweep_row(i, "grid_data: iso=EU_%d" % i)
             for i in range(sq._SELF_CLEARED_MIN_FOR_FRACTION - 1)]
    out = sq.sweep_self_cleared(_cur(small), _live(PJM), now=NOW)
    assert out["ok"] is True, "a small all-clear is not a rename"
    assert len(out["closed"]) == len(small)


def test_the_entsoe_aggregate_collapses_with_its_own_zones():
    """routes/iso_eu_entsoe.py writes ISO_CODE='ENTSOE' beside every EU_* zone.
    Row 298 on the 2026-09-03 board was that aggregate saying the same thing as
    the 31 zone rows under it."""
    assert (sq.fleet_group_key(FAMILY, "grid_data: iso=ENTSOE")
            == sq.fleet_group_key(FAMILY, CZ))


@pytest.mark.parametrize("seen", [None, "not-a-timestamp", 0])
def test_an_undeterminable_age_is_skipped_not_swept(seen):
    """"Cannot tell how old this is" must not read as "old enough". Every
    other guard in this sweep refuses on doubt; the age gate used to pass on
    it, which is the one direction that closes a row nobody checked."""
    cur = _cur([_sweep_row(317, CZ, seen=seen)])
    out = sq.sweep_self_cleared(cur, _live(PJM), now=NOW)
    assert out["closed"] == [] and out["skipped_recent"] == 1
    assert _closed(cur) == []
