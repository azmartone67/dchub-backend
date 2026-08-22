"""The first three PRODUCT detectors (Claim Loop step 4, 2026-08-21).

Each detector is a thin fail-soft IO shell around a PURE core, so every
contract here is exercised on fixtures — the must-fail case, the control, the
silence branches and the bounds — without a DB or the network:

  check_measurement_definition_changed  -> _undisclosed_definition_bumps
  check_stored_slug_resolves            -> _stored_slug_findings
  check_funnel_adjacent_step_collapse   -> _adjacent_step_collapses

★ Silence is tested explicitly. A detector that is quiet for the wrong reason
is indistinguishable from one that works, so UNMEASURED, the series-break
refusals and the input floors each have a test that would go red if the
branch were removed.

The module imports in-process without a DB (tests/test_brain_consistency_radar.py
already relies on that); IO is monkeypatched at the helper seam.
"""
import datetime as dt
import sys
import time

import pytest

from routes import brain_consistency_radar as radar

STANDARD_KEYS = {"issue", "url", "count", "count_kind", "detail"}
TODAY = dt.date(2026, 9, 10)


# ═════════════════════════════════════════════════════════════════════════════
# detector 3: funnel adjacent step collapse
# ═════════════════════════════════════════════════════════════════════════════

def _hist(days, ratio, upstream=1000.0, today=TODAY, a="a", b="b"):
    h = {a: {}, b: {}}
    for i in range(1, days + 1):
        d = today - dt.timedelta(days=i)
        h[a][d] = upstream
        h[b][d] = upstream * ratio
    return h


def _collapses(current, history, **kw):
    kw.setdefault("chain", ("a", "b"))
    kw.setdefault("today", TODAY)
    return radar._adjacent_step_collapses(current, history, **kw)


def test_a_100x_collapse_fires_and_names_both_steps():
    """This test proves the detector FIRES when a step's conversion falls
    100x under its trailing median."""
    f = _collapses({"a": 1000, "b": 2}, _hist(10, 0.2))
    assert len(f) == 1
    f0 = f[0]
    assert STANDARD_KEYS <= set(f0)
    assert f0["issue"] == "funnel_step_collapse"
    assert f0["url"] == "funnel:a->b"
    assert (f0["step_from"], f0["step_to"]) == ("a", "b")
    assert f0["median_ratio"] == 0.2 and f0["current_ratio"] == 0.002
    assert f0["count"] == 100
    assert "a -> b" in f0["detail"] and "10-day median" in f0["detail"]


def test_a_flat_funnel_is_quiet():
    """CONTROL."""
    assert _collapses({"a": 1000, "b": 190}, _hist(10, 0.2)) == []


def test_the_threshold_is_ten_x():
    assert _collapses({"a": 1000, "b": 21}, _hist(10, 0.2)) == []      # 9.5x under
    assert len(_collapses({"a": 1000, "b": 19}, _hist(10, 0.2))) == 1  # 10.5x under


def test_zero_output_with_live_input_is_the_loudest_collapse():
    f = _collapses({"a": 1000, "b": 0}, _hist(10, 0.2))
    assert len(f) == 1 and f[0]["count"] == 10 ** 6


def test_input_below_the_floor_is_noise():
    assert _collapses({"a": 50, "b": 0}, _hist(10, 0.2, upstream=1000)) == []


def test_too_little_history_is_unmeasured():
    assert _collapses({"a": 1000, "b": 0}, _hist(4, 0.2)) == []
    assert len(_collapses({"a": 1000, "b": 0}, _hist(5, 0.2))) == 1


def test_today_is_never_in_its_own_baseline():
    h = _hist(4, 0.2)
    h["a"][TODAY] = 1000.0
    h["b"][TODAY] = 0.0            # today's point must not become history
    assert _collapses({"a": 1000, "b": 0}, h) == []


def test_a_missing_current_point_is_unmeasured():
    assert _collapses({"a": 1000}, _hist(10, 0.2)) == []


def test_a_declared_break_silences_the_pair():
    """★ THE SILENCE THAT MATTERS: a ratio across a declared break is not a rate."""
    f = _collapses({"a": 1000, "b": 0}, _hist(10, 0.2), refuse=lambda a, b: "2026-08-15")
    assert f == []
    assert len(_collapses({"a": 1000, "b": 0}, _hist(10, 0.2), refuse=lambda a, b: None)) == 1


def test_the_retired_init_to_call_series_is_not_a_funnel_step():
    """The 08-19 misread: connector_init_30d 4,178 -> connector_call_30d 3 is
    ONE event counted under two methods. It must never be in the chain."""
    assert "connector_init_30d" not in radar._FUNNEL_CHAIN
    assert "connector_call_30d" not in radar._FUNNEL_CHAIN
    assert radar._FUNNEL_CHAIN == ("tool_calls_7d", "upgrade_signals_7d", "conversions_30d")


def test_the_same_input_gives_the_same_finding_key():
    a = _collapses({"a": 1000, "b": 2}, _hist(10, 0.2))
    b = _collapses({"a": 1000, "b": 2}, _hist(10, 0.2))
    assert [(x["issue"], x["url"]) for x in a] == [(x["issue"], x["url"]) for x in b]


UTC = dt.timezone.utc


def _rows(days=10, ratio=0.2, upstream=1000.0, now=None, out_now=2.0,
          recent_points=3, a="tool_calls_7d", b="upgrade_signals_7d"):
    """Raw brain_metric_snapshots rows: one point a day for `days` days
    before today, plus `recent_points` snapshots inside the last hour."""
    now = now or dt.datetime.now(UTC)
    rows = []
    for i in range(1, days + 1):
        at = (now - dt.timedelta(days=i)).replace(hour=12, minute=0, second=0, microsecond=0)
        rows += [(a, at, upstream), (b, at, upstream * ratio)]
    for j in range(recent_points):
        at = now - dt.timedelta(minutes=10 * (j + 1))
        rows += [(a, at, upstream), (b, at, out_now)]
    return rows


def test_points_drop_a_fabricated_zero_batch():
    """★ L6 writes funnel.get(key, 0): a failed fetch is a 0 for EVERY key at
    one second. Such a batch is not a measurement."""
    now = dt.datetime.now(UTC)
    rows = _rows(now=now, recent_points=3)
    dead = now - dt.timedelta(minutes=5)
    rows += [("tool_calls_7d", dead, 0.0), ("upgrade_signals_7d", dead, 0.0)]
    current, hist, cur_date = radar._funnel_points(rows, now=now)
    assert current["tool_calls_7d"] == 1000.0 and current["upgrade_signals_7d"] == 2.0
    assert hist["tool_calls_7d"][now.date()] == 1000.0, "the dead batch must not be the day's last value"


def test_points_current_is_the_median_of_recent_snapshots():
    now = dt.datetime.now(UTC)
    rows = _rows(now=now, recent_points=3, out_now=200.0)
    rows += [("tool_calls_7d", now - dt.timedelta(minutes=5), 1000.0),
             ("upgrade_signals_7d", now - dt.timedelta(minutes=5), 0.0)]   # one partial fetch
    current, _h, _d = radar._funnel_points(rows, now=now)
    assert current["upgrade_signals_7d"] == 200.0, "a single fabricated 0 must not become 'current'"


def test_points_need_two_recent_snapshots():
    now = dt.datetime.now(UTC)
    current, _h, _d = radar._funnel_points(_rows(now=now, recent_points=1), now=now)
    assert current == {}
    current, _h, _d = radar._funnel_points(_rows(now=now, recent_points=2), now=now)
    assert set(current) == {"tool_calls_7d", "upgrade_signals_7d"}


def test_points_cut_history_before_the_current_points_own_day():
    """Just after UTC midnight the newest snapshot is yesterday's: the
    current point's date is yesterday, and yesterday must leave the
    baseline."""
    now = dt.datetime(2026, 9, 11, 0, 30, tzinfo=UTC)
    newest = dt.datetime(2026, 9, 10, 23, 55, tzinfo=UTC)
    rows = _rows(days=10, now=now, recent_points=0)
    rows += [("tool_calls_7d", newest, 1000.0), ("upgrade_signals_7d", newest, 0.0),
             ("tool_calls_7d", newest - dt.timedelta(minutes=30), 1000.0),
             ("upgrade_signals_7d", newest - dt.timedelta(minutes=30), 0.0)]
    current, hist, cur_date = radar._funnel_points(rows, now=now)
    assert cur_date == dt.date(2026, 9, 10)
    f = radar._adjacent_step_collapses(current, hist, chain=("tool_calls_7d", "upgrade_signals_7d"), today=cur_date)
    assert len(f) == 1 and f[0]["history_days"] == 9, "the collapsed day is not in its own median"


def test_points_are_empty_without_rows():
    assert radar._funnel_points([], now=dt.datetime.now(UTC)) == ({}, {}, None)


def test_detail_says_which_side_moved():
    out_fell = _collapses({"a": 1000, "b": 2}, _hist(10, 0.2))[0]
    assert out_fell["output_fell"] is True and "STEP stopped producing" in out_fell["detail"]
    spiked = _collapses({"a": 10500, "b": 200}, _hist(10, 0.2))[0]
    assert spiked["output_fell"] is False and "output HELD" in spiked["detail"]
    assert "inflation of `a`" in spiked["detail"]


def _quiet_markers(monkeypatch):
    import routes.weekly_series as ws
    monkeypatch.setattr(ws, "_changes_in", lambda s, e: [])


def test_shell_fires_on_a_snapshotted_collapse(monkeypatch):
    _quiet_markers(monkeypatch)
    monkeypatch.setattr(radar, "_funnel_step_rows", lambda keys, days: _rows())
    f = radar.check_funnel_adjacent_step_collapse()
    assert len(f) == 1 and f[0]["url"] == "funnel:tool_calls_7d->upgrade_signals_7d"


def test_shell_is_silent_inside_a_marked_definition_change(monkeypatch):
    import routes.weekly_series as ws
    monkeypatch.setattr(ws, "_changes_in", lambda s, e: [{"effective_at": "x"}])
    monkeypatch.setattr(radar, "_funnel_step_rows", lambda keys, days: _rows())
    assert radar.check_funnel_adjacent_step_collapse() == []


def test_shell_treats_a_stale_current_point_as_unmeasured(monkeypatch):
    _quiet_markers(monkeypatch)
    stale = dt.datetime.now(UTC) - dt.timedelta(hours=radar._FUNNEL_CURRENT_MAX_AGE_H + 1)
    monkeypatch.setattr(radar, "_funnel_step_rows", lambda keys, days: _rows(now=stale))
    assert radar.check_funnel_adjacent_step_collapse() == []


def test_shell_is_quiet_when_snapshots_are_unreadable(monkeypatch):
    _quiet_markers(monkeypatch)
    monkeypatch.setattr(radar, "_funnel_step_rows", lambda keys, days: None)
    assert radar.check_funnel_adjacent_step_collapse() == []


def test_funnel_shell_is_fail_soft(monkeypatch):
    """★ The marker refusal returns BEFORE the snapshot read, so without
    clearing it this test never reached the raising helper and passed
    vacuously (a mutant that re-raised survived). Clear it first."""
    _quiet_markers(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(radar, "_funnel_step_rows", _boom)
    assert radar.check_funnel_adjacent_step_collapse() == []


# ═════════════════════════════════════════════════════════════════════════════
# detector 2: stored slug resolves (outside-in)
# ═════════════════════════════════════════════════════════════════════════════

TARGETS = [("slug", "a-old"), ("canonical_slug", "a-new"), ("slug", "b-old")]


def _probe(table):
    return lambda slug: table.get(slug, (None, 0))


def _slug_findings(table, targets=TARGETS, **kw):
    kw.setdefault("sample_day", "2026-08-21")
    return radar._stored_slug_findings(targets, _probe(table), **kw)


def test_one_404_in_the_sample_is_a_finding():
    """This test proves the detector FIRES when one sampled stored slug 404s."""
    f = _slug_findings({"a-old": (404, 0), "a-new": (200, 0), "b-old": (200, 1)})
    assert len(f) == 1
    f0 = f[0]
    assert STANDARD_KEYS <= set(f0)
    assert f0["issue"] == "stored_slug_404"
    assert f0["count"] == 1 and f0["probed"] == 3 and f0["unmeasured"] == 0
    assert f0["bad_by_column"] == {"slug": 1}
    assert f0["examples"] == ["/facilities/a-old -> 404 (slug)"]
    assert "1/3" in f0["detail"] and "33.3%" in f0["detail"]
    assert "2026-08-21" in f0["detail"]


def test_all_200_is_quiet():
    """CONTROL."""
    assert _slug_findings({"a-old": (200, 1), "a-new": (200, 0), "b-old": (200, 1)}) == []


def test_a_3xx_that_lands_on_200_is_fine():
    assert _slug_findings({"a-old": (200, 2), "a-new": (200, 0), "b-old": (200, 1)}) == []


def test_timeouts_are_unmeasured_not_bad():
    assert _slug_findings({"a-old": (None, 0), "a-new": (200, 0), "b-old": (None, 0)}) == []
    f = _slug_findings({"a-old": (404, 0), "a-new": (200, 0), "b-old": (None, 0)})
    assert len(f) == 1 and f[0]["probed"] == 2 and f[0]["unmeasured"] == 1
    assert "50.0%" in f[0]["detail"]


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_edge_errors_are_unmeasured(status):
    assert _slug_findings({"a-old": (status, 0), "a-new": (200, 0), "b-old": (200, 1)}) == []


def test_410_counts_as_gone():
    f = _slug_findings({"a-old": (410, 0), "a-new": (200, 0), "b-old": (200, 1)})
    assert len(f) == 1 and f[0]["examples"] == ["/facilities/a-old -> 410 (slug)"]


def test_an_empty_sample_is_quiet():
    assert radar._stored_slug_findings([], _probe({})) == []


def test_targets_favour_the_stored_column_and_keep_a_canonical_control():
    rows = [(i, f"old-{i}", f"new-{i}") for i in range(25)]
    t = radar._slug_probe_targets(rows, 25)
    assert len(t) == 25
    assert sum(1 for c, _s in t if c == "slug") == 20, "20 rows' STORED slugs — the column that 404s"
    assert [s for c, s in t if c == "canonical_slug"] == [f"new-{i}" for i in range(5)]
    same = radar._slug_probe_targets([(1, "x", "x"), (2, None, "y"), (3, "", "")], 25)
    assert same == [("slug", "x"), ("canonical_slug", "y")]


def test_the_sample_is_seeded_per_day():
    d1, s1 = radar._slug_sample_seed(dt.date(2026, 8, 21))
    d1b, s1b = radar._slug_sample_seed(dt.date(2026, 8, 21))
    d2, s2 = radar._slug_sample_seed(dt.date(2026, 8, 22))
    assert (d1, s1) == (d1b, s1b) and s1 != s2
    assert d1 == "2026-08-21" and len(s1) == 16 and int(s1, 16) >= 0


def test_the_probe_is_bounded():
    assert radar._SLUG_PROBE_ROWS == 25
    assert radar._SLUG_PROBE_MAX == 25
    assert radar._SLUG_PROBE_TIMEOUT_S == 8
    assert radar._SLUG_PROBE_WALL_S <= 8, "the whole scan has a 25s budget; this detector must not own it"
    assert radar._SLUG_PROBE_WORKERS <= 8
    assert radar._SLUG_PROBE_UA.startswith("dchub-brain-radar/"), (
        "brain_http_capture exempts the dchub-brain-radar UA family only; any "
        "other UA files the 404s this probe elicits as real caller errors")
    assert radar._SLUG_PROBE_BASE == "https://dchub.cloud/facilities/"


def test_the_wall_clock_is_enforced():
    def slow(slug):
        time.sleep(3)
        return 404, 0
    t0 = time.time()
    f = radar._stored_slug_findings(TARGETS, slow, wall_s=0.4, workers=1)
    assert time.time() - t0 < 2.5
    assert f == [], "abandoned probes are unmeasured, never 404s"


def test_stats_report_what_was_measured():
    stats = {}
    radar._stored_slug_findings(TARGETS, _probe({"a-old": (None, 0), "a-new": (200, 0), "b-old": (None, 0)}), sample_day="d", stats=stats)
    assert stats == {"targeted": 3, "measured": 1, "bad": 0, "unmeasured": 2}


class _MetaStore(dict):
    """Stands in for routes.brain_v2_store get_meta/set_meta."""

    def get_meta(self, key):
        return {"value": self[key], "updated_at": None} if key in self else None

    def set_meta(self, key, value):
        self[key] = value
        return True


@pytest.fixture
def _memo(monkeypatch):
    import routes.brain_v2_store as store
    shared = _MetaStore()
    monkeypatch.setattr(store, "get_meta", shared.get_meta)
    monkeypatch.setattr(store, "set_meta", shared.set_meta)
    radar._SLUG_PROBE_MEMO.update({"day": None, "at": 0.0, "findings": None})
    yield shared
    radar._SLUG_PROBE_MEMO.update({"day": None, "at": 0.0, "findings": None})


def test_shell_fires_on_a_sampled_404_and_memoises_the_day(monkeypatch, _memo):
    rows = [(1, "a-old", "a-new"), (2, "b-old", "b-new")]
    monkeypatch.setattr(radar, "_sample_stored_slugs", lambda n, seed: rows)
    calls = []

    def probe(slug, timeout=8):
        calls.append(slug)
        return (404, 0) if slug == "b-old" else (200, 1)
    monkeypatch.setattr(radar, "_probe_facility_url", probe)
    f = radar.check_stored_slug_resolves()
    assert len(f) == 1 and f[0]["examples"] == ["/facilities/b-old -> 404 (slug)"]
    assert sorted(calls) == ["a-new", "a-old", "b-new", "b-old"]
    again = radar.check_stored_slug_resolves()
    assert again == f and sorted(calls) == ["a-new", "a-old", "b-new", "b-old"], (
        "the same day's sample must not be re-probed every 5-minute sweep")
    assert radar._SLUG_PROBE_META_KEY in _memo, "the verdict is shared through brain_meta"


def test_a_fleet_mate_s_verdict_is_reused_without_probing(monkeypatch, _memo):
    import json
    day, _seed = radar._slug_sample_seed()
    verdict = [{"issue": "stored_slug_404", "url": "sample:x", "count": 1, "count_kind": "item_count", "detail": "d"}]
    _memo.set_meta(radar._SLUG_PROBE_META_KEY, json.dumps(
        {"day": day, "at": time.time(), "measured": 9, "findings": verdict}))
    monkeypatch.setattr(radar, "_sample_stored_slugs", lambda n, seed: pytest.fail("must not sample"))
    assert radar.check_stored_slug_resolves() == verdict


def test_an_all_unmeasured_run_is_not_memoised(monkeypatch, _memo):
    """★ An all-timeout probe is UNMEASURED; remembering its [] for 6h would
    turn an edge flap into a clean verdict for the whole fleet."""
    rows = [(1, "a-old", "a-new"), (2, "b-old", "b-new")]
    monkeypatch.setattr(radar, "_sample_stored_slugs", lambda n, seed: rows)
    calls = []

    def probe(slug, timeout=8):
        calls.append(slug)
        return (None, 0)
    monkeypatch.setattr(radar, "_probe_facility_url", probe)
    assert radar.check_stored_slug_resolves() == []
    assert radar.check_stored_slug_resolves() == []
    assert len(calls) == 8, "the second sweep must probe again"
    assert radar._SLUG_PROBE_META_KEY not in _memo


def test_shell_unmeasured_sample_is_quiet(monkeypatch, _memo):
    monkeypatch.setattr(radar, "_sample_stored_slugs", lambda n, seed: None)
    monkeypatch.setattr(radar, "_probe_facility_url", lambda slug, timeout=8: (404, 0))
    assert radar.check_stored_slug_resolves() == []


def test_slug_shell_is_fail_soft(monkeypatch, _memo):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(radar, "_sample_stored_slugs", _boom)
    assert radar.check_stored_slug_resolves() == []


# ═════════════════════════════════════════════════════════════════════════════
# detector 1: measurement definition changed
# ═════════════════════════════════════════════════════════════════════════════

PUB = [{"surface": "agent_success_report", "metric": "tool_calls_7d", "version": 2,
        "changelog": {1: "initial", 2: "2026-08-05 POPULATION COLLISION FIXED: adds is_public_ip"}}]
MARK_0818 = [{"effective_at": "2026-08-18T06:31:00+00:00",
              "change": "the CI self-tag moved to a per-request header",
              "ref": "dchub-mcp-server#202"}]


def _bumps(published=PUB, markers=MARK_0818, **kw):
    return radar._undisclosed_definition_bumps(published, markers, **kw)


def test_a_bump_with_no_marker_is_a_finding():
    """This test proves the detector FIRES on a v2 bump the marker registry
    does not know."""
    f = _bumps()
    assert len(f) == 1
    f0 = f[0]
    assert STANDARD_KEYS <= set(f0)
    assert f0["issue"] == "measurement_definition_changed"
    assert f0["url"] == "definition:agent_success_report:tool_calls_7d:v2"
    assert f0["metric"] == "tool_calls_7d" and f0["definition_version"] == 2
    assert f0["effective"] == "2026-08-05"
    assert "tool_calls_7d" in f0["detail"] and "_DEFINITION_CHANGES" in f0["detail"]


def test_a_marker_on_the_bump_date_discloses_it():
    """CONTROL."""
    assert _bumps(markers=[{"effective_at": "2026-08-05T00:00:00+00:00"}]) == []


def test_the_tolerance_is_two_days():
    assert _bumps(markers=[{"effective_at": "2026-08-07T23:00:00+00:00"}]) == []
    assert len(_bumps(markers=[{"effective_at": "2026-08-08T00:00:00+00:00"}])) == 1


def test_a_marker_naming_the_metric_AND_version_discloses_it():
    assert _bumps(markers=[{"effective_at": "2026-01-01T00:00:00+00:00",
                            "ref": "backend#2900 tool_calls_7d v2"}]) == []
    assert _bumps(markers=[{"effective_at": "2026-01-01T00:00:00+00:00",
                            "change": "tool_calls_7d moved to version 2"}]) == []


def test_a_marker_naming_only_the_metric_does_not_disarm_later_bumps():
    """★ Following the finding's own remediation must not silence the metric
    forever: the v2 marker does not cover a v3 bump."""
    pub = [dict(PUB[0], version=3, changelog={**PUB[0]["changelog"], 3: "2026-10-03 counts per-episode now"})]
    f = _bumps(published=pub, markers=[{"effective_at": "2026-08-05T00:00:00+00:00",
                                        "ref": "backend#2950 tool_calls_7d v2"}])
    assert [x["url"] for x in f] == ["definition:agent_success_report:tool_calls_7d:v3"]


def test_every_version_is_judged_not_just_the_latest():
    pub = [dict(PUB[0], version=3, changelog={**PUB[0]["changelog"], 3: "2026-09-01 x"})]
    f = _bumps(published=pub, markers=[{"effective_at": "2026-09-01T00:00:00+00:00"}])
    assert [x["url"] for x in f] == ["definition:agent_success_report:tool_calls_7d:v2"]
    assert _bumps(published=pub, markers=[{"effective_at": "2026-09-01T00:00:00+00:00"},
                                          {"effective_at": "2026-08-05T00:00:00+00:00"}]) == []


@pytest.mark.parametrize("text,expected", [
    ("2026-08-05 POPULATION COLLISION FIXED", "2026-08-05"),
    ("2026-08-05: the share moves", "2026-08-05"),
    ("replaces the 2026-07-28 classifier; effective 2026-08-05", "2026-08-05"),
    ("applied 2026-07-28, families verified 2026-07-30", "2026-07-28"),
    ("no date at all", None),
])
def test_the_effective_date_is_picked_the_way_the_house_writes_it(text, expected):
    got = radar._definition_bump_date(text)
    assert (got.isoformat() if got else None) == expected


def test_version_one_is_never_a_bump():
    pub = [dict(PUB[0], version=1)]
    assert _bumps(published=pub) == []


def test_an_undated_bump_is_a_finding_that_says_so():
    pub = [{"surface": "planner_bypass", "metric": "planner_bypass", "version": 2,
            "changelog": {1: "session-scoped", 2: "agent-day episodes"}}]
    f = _bumps(published=pub)
    assert len(f) == 1 and f[0]["effective"] is None and "UNDATED" in f[0]["detail"]


def test_malformed_markers_neither_crash_nor_disclose():
    f = _bumps(markers=[{"effective_at": "garbage"}, "not-a-dict", {}, None])
    assert len(f) == 1


def test_the_same_input_gives_the_same_definition_key():
    assert [x["url"] for x in _bumps()] == [x["url"] for x in _bumps()]


def test_the_real_surfaces_parse():
    pub = radar._published_definition_versions()
    by = {(p["surface"], p["metric"]): p for p in pub}
    assert ("agent_success_report", "tool_calls_7d") in by
    tc = by[("agent_success_report", "tool_calls_7d")]
    assert tc["version"] == 2 and "2026-08-05" in tc["changelog"][2]
    assert ("handoff_definition", "human_acted") in by, (
        "the most-redefined metric in the repo publishes a bare version block")
    ha = by[("handoff_definition", "human_acted")]
    assert ha["version"] >= 4 and set(ha["changelog"]) >= {1, 2, 3, 4}
    assert not any(p["surface"] == "planner_bypass" for p in pub), (
        "the planner payload versions its SHAPE — out of scope")
    assert not any(p["metric"] == "agent_success_report" for p in pub), (
        "REPORT_DEFINITION_VERSION is a payload-shape version — out of scope")
    assert len(pub) >= 10, "the METRICS table has a dozen contracts"
    for p in pub:
        assert isinstance(p["version"], int) and p["version"] >= 1
        assert isinstance(p["changelog"], dict) and 1 in p["changelog"]


def test_the_surface_list_is_explicit():
    assert radar._DEFINITION_VERSION_SURFACES == (
        "agent_success_report.py", "planner_bypass.py", "handoff_definition.py")


def test_a_dict_copy_of_a_named_changelog_is_followed(tmp_path):
    good = tmp_path / "handoff_like.py"
    good.write_text(
        'V = 4\nLOG = {1: "a", 2: "2026-07-30 b", 3: "2026-08-16 c", 4: "2026-08-17 d"}\n'
        'def human_acted_definition():\n'
        '    return {"definition_version": V, "definition_changelog": dict(LOG)}\n',
        encoding="utf-8")
    pub = radar._published_definition_versions([str(good)])
    assert pub == [{"surface": "handoff_like", "metric": "human_acted", "version": 4,
                    "changelog": {1: "a", 2: "2026-07-30 b", 3: "2026-08-16 c", 4: "2026-08-17 d"}}]


def test_a_surface_that_does_not_parse_is_skipped(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def x(:\n", encoding="utf-8")
    assert radar._published_definition_versions([str(bad)]) == []


def test_constants_names_and_fstrings_are_resolved(tmp_path):
    good = tmp_path / "surface.py"
    good.write_text(
        'DEFINITION_VERSION = 3\nN = 7\n'
        'LOG = {1: "a", 2: f"{N}d later " "2026-07-01", 3: "x"}\n'
        'def _m():\n'
        '    return {"definition": "what it means",\n'
        '            "definition_version": DEFINITION_VERSION,\n'
        '            "definition_changelog": LOG}\n',
        encoding="utf-8")
    pub = radar._published_definition_versions([str(good)])
    assert pub == [{"surface": "surface", "metric": "surface", "version": 3,
                    "changelog": {1: "a", 2: "d later 2026-07-01", 3: "x"}}]


def test_a_payload_shape_version_is_out_of_scope(tmp_path):
    """A dict carrying payload keys beside the version keys versions a
    layout, not a number's meaning."""
    shape = tmp_path / "shape.py"
    shape.write_text(
        'def _m():\n'
        '    return {"report": "x", "definition_version": 6,\n'
        '            "definition_changelog": {6: "2026-08-05 sections added"}}\n',
        encoding="utf-8")
    assert radar._published_definition_versions([str(shape)]) == []


def test_shell_is_quiet_when_the_marker_registry_is_unreadable(monkeypatch):
    monkeypatch.setitem(sys.modules, "routes.weekly_series", None)
    assert radar.check_measurement_definition_changed() == []


def test_definition_shell_is_fail_soft(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("fs")
    monkeypatch.setattr(radar, "_published_definition_versions", _boom)
    assert radar.check_measurement_definition_changed() == []


def test_definition_shell_runs_on_the_real_files():
    f = radar.check_measurement_definition_changed()
    for x in f:
        assert STANDARD_KEYS <= set(x) and x["issue"] == "measurement_definition_changed"
