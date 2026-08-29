"""The orphan-decision detector's verdicts, against a scripted cursor.

The property that matters is SILENT_WRITER: an upstream produced decisions
and the sink took zero rows. That is the exact shape of the 2026-08-29
activation gap (customer_lifecycle_events named 16 stranded accounts,
mcp_outreach_log had zero rows), and a detector that cannot distinguish it
from "quiet because there was nothing to do" is worth nothing.

Pure functions only — no DB. `_check_one` takes a cursor, so a scripted one
is the whole harness.
"""
import re

import pytest

from routes.brain_orphan_decisions import (_SINKS, _check_one, _IDENT_RE,
                                           _validate_registry)


class FakeCursor:
    """Answers the three query shapes _check_one issues, from a script.

    counts: dict of substring -> int, matched against the COUNT(*) SQL in
    insertion order (first hit wins), so a test names only what it cares
    about. An unmatched COUNT is an explicit failure, never a silent 0 —
    a scripted cursor that invents zeros would make every assertion here
    vacuous.
    """

    def __init__(self, tables, columns, counts):
        self.tables, self.columns, self.counts = tables, columns, counts
        self._row = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "to_regclass" in s:
            self._row = (params[0] in self.tables,)
        elif "information_schema.columns" in s:
            self._rows = [(c,) for c in self.columns.get(params[0], ())]
            self._row = None
        elif "COUNT(*)" in s:
            for needle, val in self.counts.items():
                if needle in s:
                    self._row = (val,)
                    return
            raise AssertionError(f"unscripted COUNT: {s}")
        else:
            raise AssertionError(f"unscripted SQL: {s}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


def _spec(name):
    return next(s for s in _SINKS if s["sink"] == name)


# ── SILENT_WRITER — the incident this detector exists for ────────────────

def test_silent_writer_fires_when_upstream_decided_and_sink_is_empty():
    spec = _spec("mcp_outreach_log")
    cur = FakeCursor(
        tables={"mcp_outreach_log", "customer_lifecycle_events"},
        columns={"mcp_outreach_log": ("id", "sent_at"),
                 "customer_lifecycle_events": ("id", "at", "to_stage")},
        counts={
            "FROM customer_lifecycle_events": 21,   # 16 stranded + 5 churned
            "FROM mcp_outreach_log": 0,             # nothing ever sent
        })
    out = _check_one(cur, spec)
    assert out["verdict"] == "SILENT_WRITER", out
    assert out["upstream_decisions_window"] == 21
    assert out["rows_written_window"] == 0
    assert "ZERO rows" in out["detail"]


def test_silent_writer_does_not_fire_when_the_sink_received_rows():
    """The wire works. 21 decided, 21 sent — this must be OK, or the
    detector cries wolf on a healthy loop and gets muted."""
    spec = _spec("mcp_outreach_log")
    cur = FakeCursor(
        tables={"mcp_outreach_log", "customer_lifecycle_events"},
        columns={"mcp_outreach_log": ("id", "sent_at"),
                 "customer_lifecycle_events": ("id", "at", "to_stage")},
        counts={"FROM customer_lifecycle_events": 21,
                "FROM mcp_outreach_log": 21})
    out = _check_one(cur, spec)
    assert out["verdict"] == "OK", out


def test_quiet_sink_with_no_upstream_decisions_is_not_a_defect():
    """Zero sent when zero were owed is correct behaviour, not a finding."""
    spec = _spec("mcp_outreach_log")
    cur = FakeCursor(
        tables={"mcp_outreach_log", "customer_lifecycle_events"},
        columns={"mcp_outreach_log": ("id", "sent_at"),
                 "customer_lifecycle_events": ("id", "at", "to_stage")},
        counts={"FROM customer_lifecycle_events": 0,
                "FROM mcp_outreach_log": 0})
    out = _check_one(cur, spec)
    assert out["verdict"] == "OK", out


# ── the structural verdicts ──────────────────────────────────────────────

def test_missing_table_is_data_not_an_exception():
    spec = _spec("mcp_outreach_log")
    cur = FakeCursor(tables=set(), columns={}, counts={})
    out = _check_one(cur, spec)
    assert out["verdict"] == "MISSING"


def test_shape_drift_when_the_timestamp_column_is_gone():
    """A renamed column must report that the CHECK is broken. Reading it as
    a pass is how a dead guard survives a schema change."""
    spec = _spec("brain_lane_decisions")
    cur = FakeCursor(tables={"brain_lane_decisions"},
                     columns={"brain_lane_decisions": ("id", "lane")},
                     counts={})
    out = _check_one(cur, spec)
    assert out["verdict"] == "SHAPE_DRIFT"
    assert "decided_at" in out["detail"]


def test_orphaned_when_rows_pile_up_unconsumed():
    spec = _spec("brain_strategic_recommendations")
    cur = FakeCursor(
        tables={"brain_strategic_recommendations"},
        columns={"brain_strategic_recommendations": ("id", "created_at", "status")},
        counts={"WHERE NOT (status <> 'new')": 45,   # the 45 sitting `new`
                "AND (status <> 'new')": 0,          # none consumed
                "WHERE created_at >": 12,            # writer alive
                "FROM brain_strategic_recommendations": 66})
    out = _check_one(cur, spec)
    assert out["verdict"] == "ORPHANED", out
    assert out["open_total"] == 45


def test_stalled_when_the_consumed_ratio_falls_under_the_floor():
    spec = _spec("brain_lane_decisions")
    cur = FakeCursor(
        tables={"brain_lane_decisions"},
        columns={"brain_lane_decisions": ("id", "decided_at", "outcome")},
        counts={"WHERE NOT (outcome IS NOT NULL)": 4,
                "AND (outcome IS NOT NULL)": 1,   # 1/30 = 3.3%, floor is 30%
                "WHERE decided_at >": 30,
                "FROM brain_lane_decisions": 300})
    out = _check_one(cur, spec)
    assert out["verdict"] == "STALLED", out
    assert out["consumed_ratio"] == pytest.approx(0.033, abs=0.001)


def test_healthy_consumption_is_ok():
    spec = _spec("brain_lane_decisions")
    cur = FakeCursor(
        tables={"brain_lane_decisions"},
        columns={"brain_lane_decisions": ("id", "decided_at", "outcome")},
        counts={"WHERE NOT (outcome IS NOT NULL)": 2,
                "AND (outcome IS NOT NULL)": 26,
                "WHERE decided_at >": 30,
                "FROM brain_lane_decisions": 300})
    out = _check_one(cur, spec)
    assert out["verdict"] == "OK", out


# ── the registry itself ──────────────────────────────────────────────────

def test_every_registry_identifier_is_a_plain_identifier():
    """Identifiers are interpolated into SQL. This is the guard that keeps
    that safe, and it must run against the real registry, not a sample."""
    _validate_registry()
    for s in _SINKS:
        assert _IDENT_RE.match(s["sink"])
        assert _IDENT_RE.match(s["ts"])
        if s["upstream"]:
            assert _IDENT_RE.match(s["upstream"][0])
            assert _IDENT_RE.match(s["upstream"][1])


def test_registry_rejects_an_injected_identifier():
    """Prove _validate_registry can actually FAIL — a validator that never
    rejects anything is decoration."""
    import routes.brain_orphan_decisions as mod
    good = mod._SINKS
    mod._SINKS = ({"sink": "x; DROP TABLE y", "ts": "at", "upstream": None,
                   "owner": "t", "why": "t", "min_open": 1, "min_ratio": 0.0},)
    try:
        with pytest.raises(ValueError):
            mod._validate_registry()
    finally:
        mod._SINKS = good


def test_every_sink_declares_a_reason_a_human_can_act_on():
    for s in _SINKS:
        assert s["why"] and len(s["why"]) > 20, s["sink"]
        assert s["owner"], s["sink"]
