#!/usr/bin/env python3
"""tests/test_infra_growth_dominant_source_mask.py — the growth board must say
when a layer's freshness is coming from a lane that supplies almost none of its
rows.

NO NETWORK, NO DB. The real `_summary` runs against a stub cursor.

WHAT WENT WRONG (2026-09-03). #3655 fixed this class in
routes/data_freshness_radar.py. It did not touch routes/infra_growth.py, which
is a SECOND production monitoring surface computing freshness the same bare way
— `_freshness()` is a MAX(col) over the whole table — over the SAME multi-source
tables. Measured against the production read replica using each layer's OWN
declared _FRESH_COL:

    layer                 rows     board_age  threshold   dominant source        lag
    substations         127,271          0d        10d    HIFLD (79,788)         19d
    metro_fiber_routes   64,836          7d        75d    zayo (19,241)          68d
    gas_pipelines        33,771          3d       130d    eia_geodot (32,851)     0d
    transmission_lines   95,566          2d       120d    eia-arcgis (94,619)     0d
    data_centers         27,935          0d        14d    openstreetmap (7,924)   0d

★ SUBSTATIONS DID NOT MERELY READ "FRESH" — IT READ "growing". `_layer_status`
returns on the FIRST branch when delta_window > 0, and auto_discovery's 1-8 rows
a day satisfy it. So the board published a positive verdict on a layer whose
canonical loader had been 500ing for 19 days. That is why the marker below is
appended to every status rather than folded into the staleness branches: the
status ladder never reaches them for this layer.

★ AND THE MASK MUST STAY SILENT ON THE HEALTHY FOUR. A lag of 0 means the
dominant source IS the freshness signal. A check that fired on those would be
the cry-wolf failure this repo has already rejected twice.

Run standalone:   python3 tests/test_infra_growth_dominant_source_mask.py
Run under pytest: pytest tests/test_infra_growth_dominant_source_mask.py
"""
import datetime
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("flask")
from routes import infra_growth as G  # noqa: E402
from util.dominant_source import MASK_LAG_DAYS  # noqa: E402

TODAY = datetime.date(2026, 9, 3)


def _ts(days_ago):
    return datetime.datetime(2026, 9, 3, 2, 0) - datetime.timedelta(days=days_ago)


class _Cur:
    """Answers every query shape `_summary` issues, for ONE layer under test.

    Deliberately dispatches on the SQL rather than on call order: `_summary`
    issues a different number of statements per layer, and a positional stub
    would silently drift the moment the function grows a query.
    """

    def __init__(self, table, groups, history):
        self.table = table          # source table name to answer for
        self.groups = groups        # [(source, rows, max_ts)]
        self.history = history      # [(date, count, captured_at)] newest-first
        self._r = None
        self.connection = self

    def rollback(self):
        pass

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "MAX(snapshot_date)" in s:
            self._r = [TODAY]
        elif "FROM infra_growth_snapshot" in s:
            # Only the layer under test has history; every other layer is
            # skipped by _summary's `if not rows: continue`.
            want = (params or [None])[0]
            self._r = self.history if want == self.layer_label else []
            self._rows = self._r
        elif "information_schema.columns" in s:
            # Two shapes: the batched probe `_summary` issues once for all
            # layers (fetchall), and the helper's own per-table fallback
            # (fetchone). Both must be answered, or a test passes by never
            # reaching the code it names.
            if "ANY(" in s:
                self._rows = [(self.table,)] if self.groups is not None else []
            else:
                self._r = [1] if self.groups is not None else None
        elif "GROUP BY" in s:
            self._rows = list(self.groups or [])
        elif s.startswith("SELECT MAX("):
            newest = max((g[2] for g in (self.groups or []) if g[2] is not None),
                         default=None)
            if newest is None:
                self._r = [None, None]
            else:
                self._r = [newest, (TODAY - newest.date()).days]
        elif "COUNT(*)" in s:
            self._r = [sum(g[1] for g in (self.groups or []))]
        else:
            self._r = [None]
            self._rows = []

    def fetchone(self):
        return self._r

    def fetchall(self):
        return getattr(self, "_rows", [])


def _run(layer_label, table, groups, history):
    cur = _Cur(table, groups, history)
    cur.layer_label = layer_label
    rows, _flatlines = G._summary(cur)   # (layers, flatline warnings)
    match = [r for r in rows if r["layer"] == layer_label]
    assert match, f"_summary returned no row for {layer_label}"
    return match[0]


# Production shapes, measured 2026-09-03.
SUBSTATIONS = [("HIFLD", 79788, _ts(20)), ("(blank)", 46359, _ts(50)),
               ("auto_discovery", 708, _ts(0)), ("osm", 374, _ts(7))]
GAS = [("eia_geodot_lines", 32851, _ts(3)), ("eia-ng", 365, _ts(157)),
       ("auto_discovery", 2, _ts(23))]

# Two snapshots a day apart with a rising count -> delta_window > 0 -> "growing",
# which is the branch substations actually takes in production.
GROWING = [(TODAY, 127271, None), (TODAY - datetime.timedelta(days=1), 127265, None)]


def test_a_masked_layer_is_marked_even_though_its_status_is_growing():
    rec = _run("substations", "substations", SUBSTATIONS, GROWING)
    assert rec["status"] == "growing", (
        "precondition: substations must reach the growing branch, or this test "
        "is not exercising the case that shipped")
    assert rec["dominant_source"] == "HIFLD"
    assert rec["dominant_source_rows"] == 79788
    assert rec["dominant_source_lag_days"] >= MASK_LAG_DAYS
    # The marker must name the lane and the lag, not merely exist.
    assert "HIFLD" in rec["status_reason"]
    assert "79,788" in rec["status_reason"]
    assert f"{rec['dominant_source_lag_days']}d behind" in rec["status_reason"]


def test_the_marker_is_absent_when_the_dominant_source_carries_the_signal():
    rec = _run("gas_pipelines", "gas_pipelines", GAS, GROWING)
    assert rec["dominant_source"] == "eia_geodot_lines"
    assert rec["dominant_source_lag_days"] == 0
    assert "NOT coming from the main source" not in (rec["status_reason"] or "")


def test_a_lag_just_under_the_threshold_does_not_fire():
    groups = [("big", 90000, _ts(MASK_LAG_DAYS - 1)), ("tiny", 10, _ts(0))]
    rec = _run("substations", "substations", groups, GROWING)
    assert rec["dominant_source_lag_days"] == MASK_LAG_DAYS - 1
    assert "NOT coming from the main source" not in (rec["status_reason"] or "")


def test_a_lag_exactly_at_the_threshold_fires():
    groups = [("big", 90000, _ts(MASK_LAG_DAYS)), ("tiny", 10, _ts(0))]
    rec = _run("substations", "substations", groups, GROWING)
    assert rec["dominant_source_lag_days"] == MASK_LAG_DAYS
    assert "NOT coming from the main source" in rec["status_reason"]


def test_a_table_with_no_source_column_publishes_nulls_not_a_clean_bill():
    rec = _run("substations", "substations", None, GROWING)
    assert rec["dominant_source"] is None
    assert rec["dominant_source_lag_days"] is None
    assert "NOT coming from the main source" not in (rec["status_reason"] or "")


class _BatchProbeFails(_Cur):
    """The batched schema probe raises; everything else answers normally."""

    def execute(self, sql, params=None):
        if "information_schema.columns" in sql and "ANY(" in sql:
            raise RuntimeError("information_schema unavailable")
        return super().execute(sql, params)


def test_a_failed_batch_probe_falls_back_and_still_catches_the_mask():
    """★ THE DANGEROUS DIRECTION. `tables_with_a_source_column` returns None
    when its own query fails. Treating that as "no table has a source column"
    would disable this entire check in silence — a broken lane reported healthy,
    which is the exact failure the check exists to catch. It must fall back to
    the helper's per-table probe instead.
    """
    cur = _BatchProbeFails("substations", SUBSTATIONS, GROWING)
    cur.layer_label = "substations"
    rows, _ = G._summary(cur)
    rec = [r for r in rows if r["layer"] == "substations"][0]
    assert rec["dominant_source"] == "HIFLD", (
        "the batched probe failed, so the helper must probe per-table itself")
    assert "NOT coming from the main source" in rec["status_reason"]


def test_the_status_itself_is_never_rewritten_by_the_mask():
    """The marker annotates; it must not change the derived verdict."""
    masked = _run("substations", "substations", SUBSTATIONS, GROWING)
    clean = _run("substations", "substations",
                 [("HIFLD", 79788, _ts(0)), ("auto_discovery", 708, _ts(0))],
                 GROWING)
    assert masked["status"] == clean["status"] == "growing"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
