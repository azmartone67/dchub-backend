"""Guard for the definition-change marker — routes/weekly_series.py (2026-08-19).

★ THE DEFECT THIS GUARD EXISTS TO RETIRE

weekly_series was built to stop a week-over-week delta being quoted against a
baseline that MOVED. It had no protection at all against the POPULATION moving,
and on 2026-08-18 06:31Z the population moved: dchub-mcp-server #202 put the CI
self-tag on a per-request header, so DC Hub's own GitHub Actions smoke suites
stopped counting as real external demand.

They were the majority of that population:

    GH Actions share of real traffic, 7d to 2026-08-18
        calls   1,700 / 2,114 = 80.4%
        agents     49 /    68 = 72.1%

Measured on mcp_calls_identity after the deploy: 31 CI-shaped bursts
(>=40 calls by one agent_id within 300s) totalling 1,710 calls in the 193h
before it, and ZERO in the 23h after.

So 2026-W34 publishes a large drop caused by a measurement CORRECTION, and
before this marker existed nothing in the payload distinguished that from a
collapse in demand. The dashboard, the mcp_funnel press headline and the
partner readouts would all have rendered it as one.

★ WHY THE ROBUST BASELINE MAKES IT WORSE, NOT BETTER
A trailing median is robust to an outlier WEEK — a sampling problem. A
definition change is a POPULATION problem, and averaging four weeks of the old
population produces a smooth baseline that hides the break behind a
respectable-looking number. robust_wow is the delta the payload tells readers
to quote, so it is the one that most needs the warning.

Pure functions: no DB, no network, and never imports main.
"""
import datetime as _dt

# Same exec-the-pure-block pattern as the sibling guards: the module opens a DB
# connection at import time.
_SRC = open("routes/weekly_series.py", encoding="utf-8").read()
_NS = {"_dt": _dt}
exec(_SRC[_SRC.index("_DEFINITION_CHANGES = ["):_SRC.index("def _partial_week")], _NS)

_DEFINITION_CHANGES = _NS["_DEFINITION_CHANGES"]
_changes_in = _NS["_changes_in"]
_comparability = _NS["_comparability"]
_assemble = _NS["_assemble"]
_wow = _NS["_wow"]
_robust_wow = _NS["_robust_wow"]

# The real marker. W34 = Mon 2026-08-17 .. Mon 2026-08-24 contains it.
CHANGE_AT = _dt.datetime(2026, 8, 18, 6, 31, tzinfo=_dt.timezone.utc)
W34 = _dt.date(2026, 8, 17)
W33 = _dt.date(2026, 8, 10)


def _fresh(changes):
    """A namespace whose functions actually SEE the substituted marker list.

    ★ `dict(_NS)` does NOT work here and fails silently in the dangerous
    direction. exec(src, ns) binds ns as the functions' __globals__; a COPY of
    ns is a different object, so `copy["_DEFINITION_CHANGES"] = x` leaves every
    function still reading the original. The first draft of the boundary test
    did exactly that and PASSED — not because the boundary logic was right, but
    because it was silently re-reading the real 08-18 marker, which happens to
    fall in the same week it was probing. Re-exec into a fresh namespace so the
    override is the thing under test.
    """
    ns = {"_dt": _dt}
    exec(_SRC[_SRC.index("_DEFINITION_CHANGES = ["):
              _SRC.index("def _partial_week")], ns)
    ns["_DEFINITION_CHANGES"] = changes
    return ns


def _w(start, agents, calls, status="measured"):
    d = _dt.date.fromisoformat(start)
    return {"week_start": start,
            "week_end_exclusive": (d + _dt.timedelta(weeks=1)).isoformat(),
            "agents": agents, "calls": calls,
            "status": status, "partial": False}


# ── the marker itself ────────────────────────────────────────────────────────

def test_the_real_change_is_registered_and_lands_in_W34():
    """The whole point: the 08-18 correction is declared, in the right week."""
    assert _DEFINITION_CHANGES, "the marker list must not be empty"
    hits = _changes_in(W34, W34 + _dt.timedelta(weeks=1))
    assert len(hits) == 1
    assert hits[0]["ref"] == "dchub-mcp-server#202"
    assert hits[0]["direction"].startswith("REDUCES")


def test_a_clean_week_is_clean():
    """★ THE FALSE BRANCH. A marker that fires on every week is not a marker.

    W33 closed before the change landed and must come back empty — this is the
    assertion that proves _changes_in discriminates rather than always-True.
    """
    assert _changes_in(W33, W33 + _dt.timedelta(weeks=1)) == []


def test_the_boundary_belongs_to_exactly_one_week():
    """A change at Monday 00:00:00Z opens a week; it does not close the last.

    Half-open [start, end) on the same boundary the ISO week uses. If this were
    inclusive on both ends a midnight-Monday change would flag two weeks and
    the reader could not tell which population each held.
    """
    monday = _dt.date(2026, 9, 7)   # a Monday with NO real marker on it
    changes_in = _fresh([{"effective_at": "2026-09-07T00:00:00+00:00",
                          "ref": "boundary-probe"}])["_changes_in"]
    opened = changes_in(monday, monday + _dt.timedelta(weeks=1))
    assert [c["ref"] for c in opened] == ["boundary-probe"]
    prev = monday - _dt.timedelta(weeks=1)
    assert changes_in(prev, monday) == [], "must not also close the prior week"


def test_a_malformed_marker_loses_the_marker_not_the_series():
    """Metadata about honesty must not be able to 500 the endpoint."""
    changes_in = _fresh([{"effective_at": "not-a-timestamp"},
                          {"effective_at": None}, {}])["_changes_in"]
    assert changes_in(W34, W34 + _dt.timedelta(weeks=1)) == []


# ── the week rows ────────────────────────────────────────────────────────────

def test_assemble_flags_the_affected_week_and_only_it():
    rows = {W33: (72, 2100, 4908), W34: (12, 656, 3000)}
    out = _assemble(rows, [W33, W34])
    clean, dirty = out[0], out[1]
    assert clean["definition_changes"] == []
    assert "comparability_warning" not in clean
    assert len(dirty["definition_changes"]) == 1
    assert "not directly comparable" in dirty["comparability_warning"]


def test_the_key_is_always_present_so_a_reader_can_branch_on_it():
    """Absent-vs-empty is the difference between handled and skipped."""
    out = _assemble({W33: (72, 2100, 4908)}, [W33])
    assert "definition_changes" in out[0]
    # and on an unobserved week too, where the payload is otherwise all nulls
    out2 = _assemble({}, [W33])
    assert out2[0]["status"] == "no_observation"
    assert out2[0]["definition_changes"] == []


# ── the deltas ───────────────────────────────────────────────────────────────

def test_wow_across_the_change_is_marked_uncomparable():
    weeks = _assemble({W33: (72, 2100, 4908), W34: (12, 656, 3000)},
                      [W33, W34])
    got = _wow(weeks)
    assert got["calls_pct"] is not None, "the arithmetic still publishes"
    assert got["comparability"]["crosses_definition_change"] is True
    assert "NOT a trend" in got["comparability"]["means"]


def test_wow_wholly_inside_one_population_is_not_marked():
    """★ THE FALSE BRANCH for the delta warning."""
    a, b = _dt.date(2026, 8, 3), W33
    weeks = _assemble({a: (38, 2367, 4771), b: (72, 2100, 4908)}, [a, b])
    got = _wow(weeks)
    assert got["comparability"]["crosses_definition_change"] is False
    assert got["comparability"]["changes"] == []


def test_robust_wow_does_not_launder_the_break_behind_a_median():
    """The delta the payload says to quote must carry the warning too."""
    starts = ["2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27",
              "2026-08-03", "2026-08-10", "2026-08-17"]
    vals = [(43, 3514), (81, 2700), (61, 1951), (84, 8311),
            (38, 2367), (72, 2100), (12, 656)]
    weeks = [_w(s, a, c) for s, (a, c) in zip(starts, vals)]
    got = _robust_wow(weeks)
    assert got["current_week_start"] == "2026-08-17"
    assert got["comparability"]["crosses_definition_change"] is True


def test_robust_wow_before_the_change_is_not_marked():
    """★ FALSE BRANCH: same shape, window ending W33, must read clean."""
    starts = ["2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27",
              "2026-08-03", "2026-08-10"]
    vals = [(43, 3514), (81, 2700), (61, 1951), (84, 8311),
            (38, 2367), (72, 2100)]
    weeks = [_w(s, a, c) for s, (a, c) in zip(starts, vals)]
    got = _robust_wow(weeks)
    assert got["current_week_start"] == "2026-08-10"
    assert got["comparability"]["crosses_definition_change"] is False


def test_a_change_anywhere_in_the_median_baseline_counts():
    """Not just the current week — a tainted baseline week breaks it too."""
    got = _comparability(["2026-08-17", "2026-09-07"])
    assert got["crosses_definition_change"] is True
    assert len(got["changes"]) == 1, "deduped on effective_at"


def test_every_registered_change_declares_what_a_reader_needs():
    """A marker without direction/means is a footnote, not a warning."""
    for ch in _DEFINITION_CHANGES:
        for key in ("effective_at", "change", "direction", "means", "ref"):
            assert ch.get(key), f"{ch.get('ref')} missing {key}"
        assert _dt.datetime.fromisoformat(ch["effective_at"]).tzinfo is not None
