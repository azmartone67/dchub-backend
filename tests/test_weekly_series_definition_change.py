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


# ── supersession: the gap `crosses` could not see ────────────────────────────
# ★ THE DEFECT THIS SECTION RETIRES (2026-08-20)
#
# _changes_in asks whether a change lands INSIDE a week the delta touches. A
# correction landing AFTER every week lands inside none of them, so the delta
# was declared comparable and the payload published "every week in this delta
# counts the same population" — true, and an all-clear.
#
# Measured live 2026-08-19, one day after #202:
#     wow.agents_pct = +89.5   (2026-08-03: 38 -> 2026-08-10: 72)
#     wow.comparability.crosses_definition_change = False
# and the funnel dashboard rendered that as "the trend number" beside a
# -28.8% rolling-7d "crash". Both weeks END before the correction, so ~72% of
# the agents in each were DC Hub's own GitHub Actions runners minting a fresh
# agent_id per rotated IP. The delta is arithmetically correct, internally
# consistent, and describes our CI cadence.
#
# The same hole put "-11.3% WoW" into press_headline_metric on 2,100 calls of
# which ~80% were ours — see tests for _weekly_calls_and_wow.

_SUP = "superseded_by_correction"


def test_the_live_plus_89_percent_delta_is_marked_superseded():
    """★ THE REGRESSION. The exact delta the dashboard called "the trend"."""
    got = _comparability(["2026-08-03", "2026-08-10"])
    assert got["crosses_definition_change"] is False, (
        "unchanged: the change lands inside neither week")
    assert got[_SUP] is True, "but both weeks predate the correction"
    assert got["quotable_as_trend"] is False
    assert got["superseded_by"][0]["ref"] == "dchub-mcp-server#202"
    assert "SUPERSEDED" in got["means"]


def test_robust_wow_baseline_before_the_change_is_superseded_too():
    """The delta the payload tells readers to QUOTE is the one that matters."""
    starts = ["2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27",
              "2026-08-03", "2026-08-10"]
    vals = [(43, 3514), (81, 2700), (61, 1951), (84, 8311),
            (38, 2367), (72, 2100)]
    weeks = [_w(s, a, c) for s, (a, c) in zip(starts, vals)]
    got = _robust_wow(weeks)["comparability"]
    assert got["crosses_definition_change"] is False
    assert got[_SUP] is True
    assert got["quotable_as_trend"] is False


def test_weeks_after_the_correction_stay_quotable():
    """★ THE FALSE BRANCH. A flag that fires on everything is not a flag.

    The first CLEAN week-over-week is the whole reason the correction was
    made. If supersession swallowed it too, the endpoint would never publish
    a trend again and the fix would be worse than the bug.
    """
    # ★ 2026-09-02: the first clean pair MOVED. 2026-W36 (08-31..09-07)
    # contains dchub-mcp-server#294 and #302, so the 08-24/08-31 pair this
    # test used to pin is now CROSSING — see
    # test_the_W35_to_W36_delta_is_withheld_by_both_markers. The first pair
    # after every registered marker is W37/W38.
    got = _comparability(["2026-09-07", "2026-09-14"])
    assert got[_SUP] is False
    assert got["superseded_by"] == []
    assert got["quotable_as_trend"] is True
    assert got["means"] == "every week in this delta counts the same population"


def test_a_straddling_delta_is_crossing_not_superseded():
    """The two hazards are distinct and must not both fire on one change.

    A week CONTAINING the change is not wholly before it, so `crosses` owns
    that case and supersession must stay quiet — otherwise a reader gets two
    different explanations for one fact.
    """
    # Isolated to the #202 marker. Against the FULL list this pair is also
    # superseded by #294 — a later correction — which is TWO changes firing
    # two hazards: legitimate, and not the property under test here.
    only_202 = [c for c in _DEFINITION_CHANGES if c["ref"] == "dchub-mcp-server#202"]
    assert len(only_202) == 1
    got = _fresh(only_202)["_comparability"](["2026-08-10", "2026-08-17"])
    assert got["crosses_definition_change"] is True
    assert got[_SUP] is False


def test_the_boundary_week_ending_exactly_at_the_change_is_superseded():
    """Half-open, same boundary _changes_in uses: end <= effective_at.

    W33 ends Mon 2026-08-17 00:00Z; the change is 2026-08-18 06:31Z, so the
    week is wholly before it. A week ending exactly AT a correction counts as
    superseded — every row in it was measured on the old definition.
    """
    at = _dt.datetime(2026, 8, 17, 0, 0, tzinfo=_dt.timezone.utc)
    ns = _fresh([{"effective_at": at.isoformat(), "change": "x",
                  "direction": "REDUCES", "means": "y", "ref": "test#1",
                  "is_correction": True}])
    assert ns["_comparability"](["2026-08-10"])[_SUP] is True
    # and the week that OPENS at that instant is not
    assert ns["_comparability"](["2026-08-17"])[_SUP] is False


def test_only_corrections_supersede():
    """★ A change that redefines FORWARD does not invalidate earlier weeks.

    Without the is_correction gate every marker would retroactively withhold
    every delta before it, including ones that are perfectly fine to quote.
    """
    base = {"effective_at": "2026-08-18T06:31:00+00:00", "change": "x",
            "direction": "REDUCES", "means": "y", "ref": "test#2"}
    ns_corr = _fresh([{**base, "is_correction": True}])
    ns_fwd = _fresh([{**base, "is_correction": False}])
    assert ns_corr["_comparability"](["2026-08-03", "2026-08-10"])[_SUP] is True
    assert ns_fwd["_comparability"](["2026-08-03", "2026-08-10"])[_SUP] is False


def test_unparseable_weeks_do_not_report_supersession():
    """★ THE VACUOUS all([]) TRAP.

    all([]) is True. A week list that parses to nothing must NOT come back
    "superseded by everything" — that would withhold deltas for a reason that
    was never measured.
    """
    got = _comparability(["not-a-date", ""])
    assert got[_SUP] is False
    assert got["superseded_by"] == []
    assert _comparability([])[_SUP] is False


def test_quotable_as_trend_is_false_when_either_hazard_fires():
    """The one boolean consumers branch on."""
    assert _comparability(["2026-08-03", "2026-08-10"])["quotable_as_trend"] is False
    assert _comparability(["2026-08-10", "2026-08-17"])["quotable_as_trend"] is False
    assert _comparability(["2026-09-07", "2026-09-14"])["quotable_as_trend"] is True


# ── 2026-09-02: the two enforcement changes inside W36 ───────────────────────
#
# Measured 2026-09-02 00:23Z: weekly-series `definition_changes_all` held ONE
# entry (#202) while dchub-mcp-server#294 (merged 2026-09-01T03:37:23Z, free
# full-answer cap enforced per caller) and #302 (2026-09-01T21:10:22Z,
# anonymous hard wall at 10x the daily cap) had both landed inside 2026-W36.
# W35's top caller `chain-hire` was 1,473 of 1,810 calls (81.4%) and is the
# population #302 removes, so the W35 -> W36 delta was about to publish a
# collapse that is the wall, not demand. comparability_for_spans is shared by
# the funnel's *_wow_pct keys, the press headline and ops/activation, so the
# marker is the ONE place all of them learn to withhold it.

W35 = _dt.date(2026, 8, 24)
W36 = _dt.date(2026, 8, 31)
_REFS_0901 = ["dchub-mcp-server#294", "dchub-mcp-server#302"]


def test_the_0901_enforcement_changes_are_registered_and_land_in_W36():
    hits = _changes_in(W36, W36 + _dt.timedelta(weeks=1))
    assert [h["ref"] for h in hits] == _REFS_0901
    by_ref = {h["ref"]: h for h in hits}
    assert by_ref["dchub-mcp-server#294"]["is_correction"] is True
    assert by_ref["dchub-mcp-server#294"]["direction"].startswith("REDUCES signals")
    assert by_ref["dchub-mcp-server#302"]["direction"] == "REDUCES calls"
    assert "chain-hire" in by_ref["dchub-mcp-server#302"]["change"]
    # and W35 itself is clean of them — the changes are INSIDE W36, not before
    assert [h["ref"] for h in _changes_in(W35, W36)] == []


def test_the_W35_to_W36_delta_is_withheld_by_both_markers():
    """The delta every consumer was about to render as a trend."""
    got = _comparability([W35.isoformat(), W36.isoformat()])
    assert got["crosses_definition_change"] is True
    assert got["quotable_as_trend"] is False
    assert [c["ref"] for c in got["changes"]] == _REFS_0901
    assert "NOT a trend" in got["means"]


def test_wow_across_W36_carries_the_refusal_the_press_headline_reads():
    """flask_mcp_endpoints._fixed_window_claim reads wow.comparability off
    _wow's output — so the refusal must be on THAT dict, not only on the bare
    helper. The level still publishes; only the delta is withheld."""
    weeks = _assemble({W35: (35, 1810, 5000), W36: (30, 400, 3000)}, [W35, W36])
    got = _wow(weeks)
    assert got["calls_pct"] is not None, "the arithmetic still publishes"
    assert got["comparability"]["crosses_definition_change"] is True
    assert got["comparability"]["quotable_as_trend"] is False
    assert [c["ref"] for c in weeks[1]["definition_changes"]] == _REFS_0901
    assert weeks[0]["definition_changes"] == [], "W35 is clean; W36 carries them"
