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


# ── lane 2 (sink-watch): the catcher is itself watched ───────────────────
#
# This detector exists because customer_white_glove decided 16 accounts were
# stranded and mcp_outreach_log took zero rows. brain_escalations is the
# catcher built for that hand-off. A catcher nobody empties is the identical
# bug one layer up, so it must be subject to the check it exists to satisfy.

def test_the_escalation_queue_is_a_registered_sink():
    """★ Without this row the queue is exactly what the detector was built to
    find: a table something decides into that nothing reads."""
    spec = _spec("brain_escalations")
    assert spec["consumed"] == "status <> 'open'"
    assert spec["ts"] == "first_seen_at"
    assert spec["owner"] == "brain_escalation_queue"


def test_nine_open_escalations_and_no_drain_is_orphaned():
    """The live state the moment the queue shipped: sync() opened 9 rows for
    the nine stranded payers, and nothing had drained one yet."""
    cur = FakeCursor(
        tables={"brain_escalations"},
        columns={"brain_escalations": ("id", "email", "status",
                                       "first_seen_at", "resolved_at")},
        counts={"WHERE NOT (status <> 'open')": 9,
                "AND (status <> 'open')": 0,
                "WHERE first_seen_at >": 9,
                "FROM brain_escalations": 9},
    )
    out = _check_one(cur, _spec("brain_escalations"))
    assert out["verdict"] == "ORPHANED", out
    assert out["open_total"] == 9
    assert out["consumed_window"] == 0


def test_a_drained_queue_is_ok():
    """THE PAIRED CONTROL. If ORPHANED cannot turn off, it is an alarm rather
    than a measurement — and the owner learns to ignore it."""
    cur = FakeCursor(
        tables={"brain_escalations"},
        columns={"brain_escalations": ("id", "email", "status",
                                       "first_seen_at", "resolved_at")},
        counts={"WHERE NOT (status <> 'open')": 1,
                "AND (status <> 'open')": 8,
                "WHERE first_seen_at >": 9,
                "FROM brain_escalations": 9},
    )
    out = _check_one(cur, _spec("brain_escalations"))
    assert out["verdict"] == "OK", out
    assert out["consumed_ratio"] == round(8 / 9, 3)


def test_an_activated_row_counts_as_drained():
    """`activated` is MEASURED from the account's own first call, not settable
    by hand. It is the outcome the queue exists to produce, so it must read as
    consumption — otherwise the one honest success stays in the backlog."""
    spec = _spec("brain_escalations")
    for status in ("activated", "contacted", "resolved"):
        assert status != "open", "sanity"
    # the predicate is status-agnostic: anything that is not 'open' drains
    assert spec["consumed"] == "status <> 'open'"


def test_the_escalation_sink_declares_no_upstream():
    """sync() refreshes existing rows without moving first_seen_at, so a
    steady-state queue would read as SILENT_WRITER the moment the roster
    re-escalated the same accounts. Consumption is the honest measure; a
    false alarm here would teach the owner to ignore a real one."""
    assert _spec("brain_escalations")["upstream"] is None
