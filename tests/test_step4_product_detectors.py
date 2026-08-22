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


def _fake_history(collapse=True):
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    hist = _hist(10, 0.2, today=today, a="tool_calls_7d", b="upgrade_signals_7d")
    hist["conversions_30d"] = {}
    out_now = 2.0 if collapse else 200.0
    hist["tool_calls_7d"][today] = 1000.0
    hist["upgrade_signals_7d"][today] = out_now
    latest = {"tool_calls_7d": (1000.0, now), "upgrade_signals_7d": (out_now, now)}
    return hist, latest


def test_shell_fires_on_a_snapshotted_collapse(monkeypatch):
    import routes.weekly_series as ws
    monkeypatch.setattr(ws, "_changes_in", lambda s, e: [])
    monkeypatch.setattr(radar, "_funnel_step_history", lambda keys, days: _fake_history(True))
    f = radar.check_funnel_adjacent_step_collapse()
    assert len(f) == 1 and f[0]["url"] == "funnel:tool_calls_7d->upgrade_signals_7d"


def test_shell_is_silent_inside_a_marked_definition_change(monkeypatch):
    import routes.weekly_series as ws
    monkeypatch.setattr(ws, "_changes_in", lambda s, e: [{"effective_at": "x"}])
    monkeypatch.setattr(radar, "_funnel_step_history", lambda keys, days: _fake_history(True))
    assert radar.check_funnel_adjacent_step_collapse() == []


def test_shell_treats_a_stale_current_point_as_unmeasured(monkeypatch):
    import routes.weekly_series as ws
    monkeypatch.setattr(ws, "_changes_in", lambda s, e: [])
    hist, latest = _fake_history(True)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=radar._FUNNEL_CURRENT_MAX_AGE_H + 1)
    latest = {k: (v, old) for k, (v, _at) in latest.items()}
    monkeypatch.setattr(radar, "_funnel_step_history", lambda keys, days: (hist, latest))
    assert radar.check_funnel_adjacent_step_collapse() == []


def test_shell_is_quiet_when_snapshots_are_unreadable(monkeypatch):
    import routes.weekly_series as ws
    monkeypatch.setattr(ws, "_changes_in", lambda s, e: [])
    monkeypatch.setattr(radar, "_funnel_step_history", lambda keys, days: None)
    assert radar.check_funnel_adjacent_step_collapse() == []


def test_funnel_shell_is_fail_soft(monkeypatch):
    """★ The marker refusal returns BEFORE the snapshot read, so without
    clearing it this test never reached the raising helper and passed
    vacuously (a mutant that re-raised survived). Clear it first."""
    import routes.weekly_series as ws
    monkeypatch.setattr(ws, "_changes_in", lambda s, e: [])

    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(radar, "_funnel_step_history", _boom)
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


def test_targets_cover_both_columns_inside_the_cap():
    rows = [(i, f"old-{i}", f"new-{i}") for i in range(25)]
    t = radar._slug_probe_targets(rows, 25)
    assert len(t) == 25
    assert t[0] == ("slug", "old-0") and t[1] == ("canonical_slug", "new-0")
    assert {c for c, _s in t} == {"slug", "canonical_slug"}
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
    assert radar._SLUG_PROBE_WALL_S <= 10, "scan_all caps a detector at 20s; the sample read needs the rest"
    assert radar._SLUG_PROBE_WORKERS <= 8
    assert radar._SLUG_PROBE_UA == "dchub-radar/1.0"
    assert radar._SLUG_PROBE_BASE == "https://dchub.cloud/facilities/"


def test_the_wall_clock_is_enforced():
    def slow(slug):
        time.sleep(3)
        return 404, 0
    t0 = time.time()
    f = radar._stored_slug_findings(TARGETS, slow, wall_s=0.4, workers=1)
    assert time.time() - t0 < 2.5
    assert f == [], "abandoned probes are unmeasured, never 404s"


@pytest.fixture
def _clear_memo():
    radar._SLUG_PROBE_MEMO.update({"day": None, "at": 0.0, "findings": None})
    yield
    radar._SLUG_PROBE_MEMO.update({"day": None, "at": 0.0, "findings": None})


def test_shell_fires_on_a_sampled_404_and_memoises_the_day(monkeypatch, _clear_memo):
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


def test_shell_unmeasured_sample_is_quiet(monkeypatch, _clear_memo):
    monkeypatch.setattr(radar, "_sample_stored_slugs", lambda n, seed: None)
    monkeypatch.setattr(radar, "_probe_facility_url", lambda slug, timeout=8: (404, 0))
    assert radar.check_stored_slug_resolves() == []


def test_slug_shell_is_fail_soft(monkeypatch, _clear_memo):
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


def test_a_marker_naming_the_metric_discloses_it():
    assert _bumps(markers=[{"effective_at": "2026-01-01T00:00:00+00:00",
                            "ref": "backend#2900 tool_calls_7d v2"}]) == []


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
    assert not any(p["surface"] == "planner_bypass" for p in pub), (
        "the planner payload versions its SHAPE (no `definition`) — out of scope")
    assert not any(p["metric"] == "agent_success_report" for p in pub), (
        "REPORT_DEFINITION_VERSION is a payload-shape version — out of scope")
    assert len(pub) >= 10, "the METRICS table has a dozen contracts"
    for p in pub:
        assert isinstance(p["version"], int) and p["version"] >= 1
        assert isinstance(p["changelog"], dict) and 1 in p["changelog"]


def test_the_surface_list_is_explicit():
    assert radar._DEFINITION_VERSION_SURFACES == ("agent_success_report.py", "planner_bypass.py")


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


def test_a_payload_shape_version_without_a_definition_is_out_of_scope(tmp_path):
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
