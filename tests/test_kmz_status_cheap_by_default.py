#!/usr/bin/env python3
"""tests/test_kmz_status_cheap_by_default.py — the status endpoint the cron
polls must not scan fiber_kmz_routes unless asked.

★ 2026-08-21 live: `POOL HOLD: conn held 30.1s by GET /api/kmz-discovery/status`
and `canceling statement due to statement timeout` — COUNT(*)/SUM/GROUP BY
over 12.3M rows on every poll, ten polls per data-sync run. Only
last_cycle_at is consumed by anything. These tests EXECUTE get_status against
a recording connection stub and assert which statements run.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

import kmz_auto_discovery as kad  # noqa: E402


class _Cur:
    def __init__(self, log):
        self.log = log
        self._row = None

    def execute(self, sql, params=None):
        self.log.append(" ".join(sql.split()))
        q = self.log[-1].lower()
        if "from kmz_discovery_log" in q:
            self._row = ("2026-08-21 18:23:57.088347+00",)   # TEXT, the live shape
        elif "reltuples" in q:
            self._row = (12281879,)
        elif "sum(distance_km)" in q and "group by" not in q:
            self._row = (1.0,)
        else:
            self._row = (7,)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [("Lumen", 5, 10.0)]

    def close(self):
        pass


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cur(self.log)


def _status(monkeypatch, **kw):
    log = []
    monkeypatch.setattr(kad, "_conn", lambda: _Conn(log))
    monkeypatch.setattr(kad, "_release", lambda c: None)
    inst = object.__new__(kad.KMZAutoDiscovery)
    inst._cache = {}
    inst._scheduler_running = False
    return inst.get_status(**kw), log


def _touches_routes_table(stmts):
    return [s for s in stmts if "fiber_kmz_routes" in s.lower() and "reltuples" not in s.lower()]


def test_default_read_never_scans_fiber_kmz_routes(monkeypatch):
    st, log = _status(monkeypatch)
    assert not _touches_routes_table(log), (
        "the default status read scanned fiber_kmz_routes — that is the ~30s "
        f"pooled-connection hold on every cron poll: {_touches_routes_table(log)}")
    assert st["last_cycle_at"] == "2026-08-21T18:23:57.088347+00:00"
    assert st["stats_mode"] == "estimate"
    assert st["total_routes_in_db_estimate"] == 12281879
    assert "total_routes_in_db" not in st and "routes_by_provider" not in st


def test_full_read_still_available_on_request(monkeypatch):
    st, log = _status(monkeypatch, full=True)
    assert _touches_routes_table(log), "full=True must run the exact stats"
    assert st["stats_mode"] == "full"
    assert st["total_routes_in_db"] == 7 and st["routes_by_provider"][0]["provider"] == "Lumen"


def test_watermark_is_read_before_anything_expensive(monkeypatch):
    """If the heavy stats ever come back into the default path, they must not
    precede the watermark — a timeout there would null last_cycle_at again."""
    _st, log = _status(monkeypatch, full=True)
    first_routes = next(i for i, s in enumerate(log) if "fiber_kmz_routes" in s.lower())
    wm = next(i for i, s in enumerate(log) if "kmz_discovery_log" in s.lower())
    assert wm < first_routes
