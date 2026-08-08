"""GUARD — avg_time_to_power_months must be ONE name meaning ONE number.

The defect (measured live 2026-08-08T03:17Z):

  get_grid_intelligence  ERCOT -> "avg_time_to_power_months": 71.5
  /api/v1/iso/ERCOT/snapshot   -> "avg_time_to_power_months": 55.3

Identical field name, same instant, two numbers — because two different code
paths computed it from two different columns over two different populations:

  * the MCP shaper (server.mjs shapeGridIntelligence) read
    row.avg_queue_wait_months, the depth-derived interconnection-wait PROXY
    (12 + 0.6/GW, clipped 12-66), and labelled it time-to-power;
  * routes/iso_snapshot._dcpi_for_iso ran its own rollup over
    time_to_power_months, with no `published = true` filter and a
    case-sensitive iso match.

Fix: routes.dcpi._aggregate_iso_stats is now THE single ISO-level rollup and
publishes avg_time_to_power_months from the time_to_power_months column;
_dcpi_for_iso delegates to it. These tests need no database — the rollup is
stubbed and the mapping is a pure function.
"""
import pytest

import routes.dcpi as dcpi
import routes.iso_snapshot as iso_snapshot


# One aggregate row shaped like _aggregate_iso_stats returns, with the two
# figures DELIBERATELY different so a path reading the wrong one is visible.
AGG_ROW = {
    "iso": "ERCOT", "market_count": 19,
    "avg_excess": 66.23, "avg_constraint": 55.41,
    "avg_queue_wait_months": 71.5,          # the interconnection-wait proxy
    "avg_time_to_power_months": 55.3,       # the real time-to-power column
    "build_count": 1, "caution_count": 12, "avoid_count": 6,
    "low_signal_count": 0,
}


def test_snapshot_reads_time_to_power_not_queue_wait():
    """THE regression. Given a row where the two differ, the snapshot's
    avg_time_to_power_months must be the time-to-power one."""
    out = iso_snapshot.dcpi_row_to_snapshot(AGG_ROW)
    assert out["avg_time_to_power_months"] == 55.3
    assert out["avg_time_to_power_months"] != 71.5, (
        "avg_time_to_power_months was filled from avg_queue_wait_months — "
        "that is the proxy, not time-to-power")


def test_snapshot_delegates_to_the_one_rollup(monkeypatch):
    """_dcpi_for_iso must not run its own query. Stub the shared rollup and the
    snapshot block must come entirely from it."""
    calls = []

    def _fake(iso, conn=None):
        calls.append(iso)
        return [AGG_ROW]

    monkeypatch.setattr(iso_snapshot, "_dcpi_aggregate", _fake)

    class _Cur:
        connection = object()
        def execute(self, *a, **k):
            raise AssertionError("_dcpi_for_iso ran its own SQL instead of "
                                 "delegating to the shared rollup")

    out = iso_snapshot._dcpi_for_iso(_Cur(), "ERCOT")
    assert calls == ["ERCOT"]
    assert out["avg_time_to_power_months"] == 55.3
    assert out["markets_scored"] == 19
    assert out["by_verdict"] == {"BUILD": 1, "CAUTION": 12, "AVOID": 6}


def test_snapshot_block_carries_its_basis():
    out = iso_snapshot.dcpi_row_to_snapshot(AGG_ROW)
    assert "_aggregate_iso_stats" in out["basis"]
    assert "published" in out["basis"].lower()


def test_missing_row_is_none_not_zeros():
    assert iso_snapshot.dcpi_row_to_snapshot(None) is None
    assert iso_snapshot.dcpi_row_to_snapshot({}) is None


def test_null_averages_stay_null():
    out = iso_snapshot.dcpi_row_to_snapshot(
        {"iso": "X", "market_count": 3, "avg_time_to_power_months": None,
         "avg_excess": None, "avg_constraint": None})
    assert out["avg_time_to_power_months"] is None
    assert out["avg_excess_power_score"] is None
    assert out["markets_scored"] == 3


def test_rollup_selects_and_aggregates_time_to_power():
    """The shared rollup must expose the column under its own name — without
    it the MCP shaper has nothing correct to read and falls back to the proxy."""
    import inspect
    src = inspect.getsource(dcpi._aggregate_iso_stats)
    code = "\n".join(ln for ln in src.splitlines()
                     if "--" not in ln and not ln.strip().startswith("#"))
    assert code.strip(), "comment-stripping ate the whole function"
    assert "time_to_power_months," in code, "time_to_power_months not selected"
    assert "AVG(time_to_power_months) AS avg_time_to_power_months" in code


def test_the_two_fields_are_never_the_same_expression():
    """queue_wait_months and time_to_power_months are different measurements.
    Neither aggregate may be aliased from the other's column."""
    import inspect
    src = inspect.getsource(dcpi._aggregate_iso_stats)
    code = "\n".join(ln for ln in src.splitlines()
                     if "--" not in ln and not ln.strip().startswith("#"))
    assert "AVG(NULLIF(queue_wait_months, 0)) AS avg_time_to_power_months" not in code
    assert "AVG(time_to_power_months) AS avg_queue_wait_months" not in code


def test_iso_aggregate_time_to_power_is_a_paid_field():
    """time_to_power_months is in _DCPI_MASK_FIELDS; its ISO-level average is
    the same product and must be masked with the rest."""
    assert "avg_time_to_power_months" in dcpi._DCPI_MASK_EXTRA
    row = {"iso": "ERCOT", "market_count": 19, "avg_time_to_power_months": 55.3}
    dcpi._mask_iso_rows_inplace([row])
    assert row["avg_time_to_power_months"] is None
    assert row["market_count"] == 19


def test_aggregate_accepts_a_caller_connection():
    """iso_snapshot passes its own connection so sharing the rollup does not
    cost a second pooled connection per request."""
    import inspect
    sig = inspect.signature(dcpi._aggregate_iso_stats)
    assert "conn" in sig.parameters
    assert sig.parameters["conn"].default is None
