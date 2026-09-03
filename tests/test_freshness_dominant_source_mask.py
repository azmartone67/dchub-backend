#!/usr/bin/env python3
"""tests/test_freshness_dominant_source_mask.py — a table's freshness must come
from the source that supplies its rows, not from whichever lane wrote last.

NO NETWORK, NO DB. The real functions run against a stub cursor.

WHAT WENT WRONG (2026-09-03). Both freshness registries read MAX(ts) over a
whole table. For a table fed by several independent lanes that answers the
weakest question available — "is ANY lane alive" — so it is pinned green by the
most trivial writer on the table. Measured against production:

  · substations   127,271 rows. The canonical HIFLD lane is 63% of them and last
    wrote 2026-08-14 while 500ing nightly (it fetches 75,328 and upserts 0).
    `auto_discovery` — 708 rows, 0.56% of the table — writes 1-8 rows EVERY day
    and has not missed one in 21 days. MAX(updated_at) is therefore always a few
    hours old, and NEITHER registry's threshold could ever fire: not
    infra_growth's 10 days, not this module's 60.

  · fiber_routes  64,836 rows. Every real carrier source last moved 2026-06-20
    — 74 days. The 20 hardcoded routes in jobs_api.MAJOR_ROUTES are re-upserted
    daily, and this radar's published last_record_at was byte-identical to that
    write: 2026-09-03T01:20:50.603151.

Both lanes were already known to be broken. Both read `fresh`. Nine other
watched tables measured a lag of exactly 0, so this is bounded, not endemic.

★ THE POINT IS NOT THE TWO TABLES — it is that the module built to catch "a lane
died and every signal stayed green" had that exact failure in its own arithmetic.

Run standalone:   python3 tests/test_freshness_dominant_source_mask.py
Run under pytest: pytest tests/test_freshness_dominant_source_mask.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("flask")
from routes import data_freshness_radar as R  # noqa: E402

NOW = datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)


def _ago(days):
    return NOW - timedelta(days=days)


class _Cur:
    """Stub cursor. `groups` is the (source, rows, max_ts) shape of the table."""

    def __init__(self, groups=None, has_source=True, raise_on=None):
        self.groups = groups or []
        self.has_source = has_source
        self.raise_on = raise_on
        self._one = None
        self._all = []

    def execute(self, sql, params=None):
        sql = str(sql)
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("stub failure")
        if "information_schema.columns" in sql:
            self._one = [1] if self.has_source else None
        elif "GROUP BY" in sql:
            self._all = list(self.groups)
        else:
            self._one = [0]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


# The two production shapes, as measured.
SUBSTATIONS = [("HIFLD", 79788, _ago(19)),
               ("", 46359, _ago(50)),
               ("auto_discovery", 708, _ago(0)),
               ("osm", 374, _ago(7))]

FIBER_ROUTES = [("carrier_kmz:zayo_2016wayback", 19241, _ago(74)),
                ("regional_carrier:bluebird", 11187, _ago(74)),
                ("hifld", 9695, _ago(19)),
                ("seed", 50, _ago(0))]


def test_substations_mask_is_measured_not_missed():
    """63% of the table is 19 days stale; a 708-row lane holds it 'fresh'."""
    lag, src, rows = R._dominant_source_lag(_Cur(SUBSTATIONS), "substations", "updated_at")
    assert src == "HIFLD", "the dominant source is the one with the most rows"
    assert rows == 79788
    assert lag == 19


def test_fiber_routes_mask_is_measured_not_missed():
    lag, src, rows = R._dominant_source_lag(_Cur(FIBER_ROUTES), "fiber_routes", "updated_at")
    assert src == "carrier_kmz:zayo_2016wayback"
    assert lag == 74


def test_both_production_masks_clear_the_threshold():
    """The two real failures must actually trip the fence, not merely be
    computed. A number nobody compares is not a guard."""
    for groups, table in ((SUBSTATIONS, "substations"), (FIBER_ROUTES, "fiber_routes")):
        lag, _, _ = R._dominant_source_lag(_Cur(groups), table, "updated_at")
        assert lag >= R._MASK_LAG_DAYS, f"{table} lag {lag} must reach {R._MASK_LAG_DAYS}"


def test_a_table_whose_sources_are_equally_quiet_is_not_flagged():
    """★ THE MEASURE IS RELATIVE, so slow federal data cannot manufacture a red.
    gas_compressor_stations is 100% one periodic source and measured lag 0."""
    quiet = [("HIFLD via ArcGIS", 1700, _ago(120)), ("manual", 68, _ago(121))]
    lag, _, _ = R._dominant_source_lag(_Cur(quiet), "gas_compressor_stations", "loaded_at")
    assert lag == 0, "everything stale together is the SLA's job, not this check's"


def test_a_dominant_source_that_is_the_newest_has_no_lag():
    healthy = [("eia-arcgis-runner", 94619, _ago(0)), ("hifld", 934, _ago(30))]
    lag, src, _ = R._dominant_source_lag(_Cur(healthy), "transmission_lines", "created_at")
    assert (lag, src) == (0, "eia-arcgis-runner")


def test_a_table_with_no_source_column_says_nothing():
    """Not a failure — a table this check has no opinion about."""
    assert R._dominant_source_lag(
        _Cur(has_source=False), "dcpi_scores", "updated_at") == (None, None, None)


def test_a_single_source_cannot_mask_itself():
    one = [("eia-860", 14599, _ago(7))]
    assert R._dominant_source_lag(_Cur(one), "power_plants", "created_at") == (None, None, None)


def test_it_never_raises_inside_the_scan():
    """It runs inside scan_domains, which must not be breakable by it."""
    for boom in ("information_schema.columns", "GROUP BY"):
        assert R._dominant_source_lag(
            _Cur(SUBSTATIONS, raise_on=boom), "substations", "updated_at") == (None, None, None)


def test_an_invalid_identifier_is_never_interpolated():
    assert R._dominant_source_lag(
        _Cur(SUBSTATIONS), "subs; DROP TABLE x", "updated_at") == (None, None, None)
    assert R._dominant_source_lag(
        _Cur(SUBSTATIONS), "substations", "ts; DROP TABLE x") == (None, None, None)


def test_rows_with_a_null_timestamp_do_not_become_the_dominant_max():
    """A lane that has written nothing measurable must not read as day-zero."""
    with_null = [("HIFLD", 79788, _ago(19)), ("pending", 100000, None),
                 ("auto_discovery", 708, _ago(0))]
    lag, src, _ = R._dominant_source_lag(_Cur(with_null), "substations", "updated_at")
    assert src == "HIFLD", "a source with no usable timestamp is not a measurement"
    assert lag == 19


def test_the_marker_names_the_lane_and_the_gap():
    """The detail string is the only place this state becomes readable, so it
    must carry BOTH numbers a human needs to act."""
    src = open(os.path.join(ROOT, "routes", "data_freshness_radar.py"), encoding="utf-8").read()
    marker = src.split("freshness is NOT coming from the main source")[1][:400]
    for token in ("mask_src", "mask_rows", "mask_lag"):
        assert token in marker, f"the operator-facing marker must interpolate {token}"


# ---------------------------------------------------------------------------
# ★★★ THE WIRING, NOT JUST THE ARITHMETIC.
# The helper above was fully green while scan_domains could stop calling it
# altogether — two mutations ("never append the marker", "never call the
# helper") SURVIVED the isolated tests. A checker that is only tested where it
# is easy to reach certifies the part that publishes by never looking at it.
# These drive the real scan_domains end to end.
# ---------------------------------------------------------------------------

class _ScanCur:
    """Cursor that answers every shape scan_domains issues, for one table."""

    def __init__(self, groups, newest):
        self.groups, self.newest = groups, newest
        self._one, self._all = None, []
        self.upserted = []

    def execute(self, sql, params=None):
        sql = str(sql)
        if "to_regclass" in sql:
            self._one = ["substations"]
        elif "information_schema.columns" in sql:
            self._one = [1]
        elif "GROUP BY" in sql:
            self._all = list(self.groups)
        elif "COUNT(*)" in sql:
            self._one = [127271]
        elif "MAX(" in sql:
            self._one = [self.newest]
        elif "brain_meta" in sql:
            raise RuntimeError("no brain_meta in this stub")
        elif "INSERT INTO data_domain_freshness" in sql:
            self.upserted.append(params)
            self._one = None
        else:
            self._one = [0]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ScanConn:
    def __init__(self, cur):
        self._cur = cur
        self.autocommit = True

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _run_scan(monkeypatch, groups, newest):
    """Drive the real scan_domains against one stubbed table."""
    cur = _ScanCur(groups, newest)
    monkeypatch.setattr(R, "_conn", lambda: _ScanConn(cur))
    monkeypatch.setattr(R, "_ensure_schema", lambda c: None)
    monkeypatch.setattr(R, "_DOMAINS", [
        ("substations", ["substations"], ["updated_at"], 1440)])
    rows = R.scan_domains()
    return next(r for r in rows if r["domain"] == "substations")


def test_scan_publishes_the_mask_on_a_domain_it_calls_FRESH(monkeypatch):
    """★ The whole defect in one assertion: status is `fresh`, and it is fresh
    because a 708-row lane wrote today while 63% of the table is 19 days old.
    Before this change the row said `fresh` and nothing else."""
    row = _run_scan(monkeypatch, SUBSTATIONS, NOW)

    assert row["status"] == "fresh", "the SLA is satisfied — that is the point"
    assert row["dominant_source"] == "HIFLD"
    assert row["dominant_source_rows"] == 79788
    assert row["dominant_source_lag_days"] == 19
    assert "freshness is NOT coming from the main source" in row["detail"]
    assert "HIFLD" in row["detail"] and "19d" in row["detail"], \
        "the operator needs the lane and the gap, not a boolean"


def test_scan_leaves_an_unmasked_domain_unmarked(monkeypatch):
    """The marker must not appear on a healthy table, or it means nothing."""
    healthy = [("eia-arcgis-runner", 94619, NOW), ("hifld", 934, _ago(30))]
    row = _run_scan(monkeypatch, healthy, NOW)
    assert row["status"] == "fresh"
    assert row["dominant_source_lag_days"] == 0
    assert "NOT coming from the main source" not in (row["detail"] or "")


def test_the_mask_is_written_to_the_table_the_board_reads(monkeypatch):
    """data_domain_freshness.detail is what the board renders. A marker that
    only ever reaches the JSON return is invisible to every human consumer."""
    cur = _ScanCur(SUBSTATIONS, NOW)
    monkeypatch.setattr(R, "_conn", lambda: _ScanConn(cur))
    monkeypatch.setattr(R, "_ensure_schema", lambda c: None)
    monkeypatch.setattr(R, "_DOMAINS", [
        ("substations", ["substations"], ["updated_at"], 1440)])
    R.scan_domains()

    assert cur.upserted, "the scan must have written a row"
    stored = " ".join(str(x) for x in cur.upserted[0])
    assert "NOT coming from the main source" in stored, \
        "the marker must reach the stored detail, not just the return value"
    assert "HIFLD" in stored and "19d" in stored


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
