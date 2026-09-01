"""Loop Control #48 pins (2026-08-02 health sweep).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly.

The sweep's finding was a MISREAD, not a broken subsystem: brain_findings.count
is per-detector free-form, and cron_silently_dead stores SECONDS in it. These
pins keep the misread from coming back, because it came back three times before
anyone noticed (frontend_endpoint_slow, dedup_backlog_large, cron_silently_dead).
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── the fix: a duration must never buy agenda leverage ────────────────

def test_cron_silently_dead_is_value_not_count():
    """THE regression pin. Unlisted, impact_weight() reads 477,455 seconds as
    477,455 occurrences, the finding hits the occurrence cap and re-wins the
    agenda every tick — which is exactly what put three cron_silently_dead
    items in the top three self-agenda slots."""
    from routes.brain_work_selector import VALUE_NOT_COUNT_ISSUES
    assert "cron_silently_dead" in VALUE_NOT_COUNT_ISSUES


def test_classifier_resolves_cron_issue():
    from routes.brain_work_selector import is_value_not_count
    assert is_value_not_count("cron_silently_dead") is True
    assert is_value_not_count("brain_findings: cron_silently_dead @ /api/jobs/x") is True
    # A genuine recurrence issue must NOT be swept into the value bucket.
    assert is_value_not_count("facility_duplicates_unmarked") is False


def test_radar_still_writes_seconds_into_count():
    """The whole premise of the value-not-count listing. If the radar ever
    starts writing a real tally here, this pin fails loudly and the listing
    should be revisited rather than silently mislabelling a true count."""
    src = _src("routes", "brain_consistency_radar.py")
    m = re.search(r'"issue":\s*"cron_silently_dead".*?"count":\s*([^,\n]+)',
                  src, re.S)
    assert m, "cron_silently_dead finding shape not found"
    assert "seconds_since" in m.group(1), (
        "cron_silently_dead no longer stores seconds in `count` — revisit "
        "VALUE_NOT_COUNT_ISSUES before shipping")


# ── the shell itself ──────────────────────────────────────────────────

def test_shell_exposes_eight_lanes():
    src = _src("routes", "loop_control_master_shell.py")
    ids = re.findall(r'\{"id": "([a-z_]+)", "name": "\d+ · ', src)
    assert len(ids) == 8, f"expected 8 lanes, found {len(ids)}: {ids}"
    assert ids == ["cron_liveness", "count_semantics", "triage_wired",
                   "surface_canon", "writer_discipline", "agent_identity",
                   "counter_canon", "relay_two_artifact"]


def test_lane_verdict_honesty():
    """A lane must never read PASS when it could not check (Integrity #25)."""
    from routes.loop_control_master_shell import _check, _lane_verdict
    assert _lane_verdict([_check("a", "a", True, "")]) == "PASS"
    assert _lane_verdict([_check("a", "a", False, "")]) == "FAIL"
    # any failure outranks a pass
    assert _lane_verdict([_check("a", "a", True, ""),
                          _check("b", "b", False, "")]) == "FAIL"
    # an indeterminate CRITICAL check cannot render green
    assert _lane_verdict([_check("a", "a", True, ""),
                          _check("b", "b", None, "", critical=True)]) == "?"
    # a lane that decided nothing at all is not green either
    assert _lane_verdict([_check("a", "a", None, "")]) == "?"
    assert _lane_verdict([]) == "?"


def test_lane4_never_compares_public_copy_to_a_raw_record_key():
    """HARMFUL RED. /api/v1/stats/canonical: 'facilities_distinct = distinct
    BUILDINGS, and the field to cite. facilities_records = facilities_tracked
    = COUNT(*) — raw source records, ~1.5x'. The first version compared
    surfaces against facilities_tracked, so acting on its red would have
    published the raw ~23.7k discovery pile as the public facility count."""
    src = _src("routes", "loop_control_master_shell.py")
    lane = src[src.index("def _lane_surface_canon"):src.index("# ── lane 5")]
    m = re.search(r"_CITABLE = \(([^)]*)\)", lane)
    assert m, "_CITABLE allowlist missing"
    citable = m.group(1)
    assert "facilities_distinct" in citable
    for raw in ("facilities_tracked", "facilities_records", "total_facilities"):
        assert f'"{raw}"' not in citable, f"{raw} is a RAW key, never citable"
    # and the raw keys must be named as forbidden, not merely absent
    assert "_RAW_NEVER" in lane
    # over-claim must be scored separately from staleness (floors round DOWN)
    assert "no_overclaim" in lane and "floors_current" in lane


def test_lane7_does_not_count_itself():
    """SELF-COUNT: the file contains the needle, so it always matched itself
    and inflated 'independent implementations' by one, forever."""
    src = _src("routes", "loop_control_master_shell.py")
    lane = src[src.index("def _lane_counter_canon"):src.index("# ── lane 8")]
    assert "_SELF = os.path.basename(__file__)" in lane
    assert "fn == _SELF" in lane, "lane 7 still counts itself"


def test_lane7_does_not_claim_drift_from_grep_hits():
    """OVERCLAIM: a grep hit is not a distinct counter, and not all hits are
    the same measurement."""
    src = _src("routes", "loop_control_master_shell.py")
    lane = src[src.index("def _lane_counter_canon"):src.index("# ── lane 8")]
    assert "independent implementation" not in lane, \
        "lane 7 still calls grep hits 'independent implementations'"
    assert "candidates only" in lane


def test_lane7_io_failure_is_indeterminate_not_a_fact():
    """DEAD BRANCH: an os.listdir failure used to render the confident
    'no COUNT(DISTINCT agent_id) site found'."""
    src = _src("routes", "loop_control_master_shell.py")
    lane = src[src.index("def _lane_counter_canon"):src.index("# ── lane 8")]
    assert "could not scan the repo" in lane
    assert "no COUNT(DISTINCT agent_id) site found" not in lane


def test_lane3_is_not_red_by_construction():
    """A lane that can never go green is noise. Comparing a shared-DB count
    to THIS process's caches is permanently red on a multi-dyno deploy."""
    src = _src("routes", "loop_control_master_shell.py")
    lane = src[src.index("def _lane_triage_wired"):src.index("# ── lane 4")]
    assert "triage_has_durable_source" in lane, "lane 3 is not structural"
    assert "open_rows > 0 and merged == 0" not in lane, \
        "lane 3 still scores DB-count vs in-process memory"


def test_lane7_match_is_case_insensitive_and_can_actually_fire():
    """Originally a lowercase needle compared against body.upper(): it could
    never match, so the lane rendered a permanent '?'. A gate that CANNOT fire
    is not a gate. Now a regex — pin that it is case-insensitive AND that it
    matches the real-world spellings."""
    from routes.loop_control_master_shell import _lane_counter_canon  # noqa: F401
    src = _src("routes", "loop_control_master_shell.py")
    lane = src[src.index("def _lane_counter_canon"):src.index("# ── lane 8")]
    m = re.search(r're\.compile\(r"([^"]+)",\s*re\.I\)', lane)
    assert m, "lane 7 no longer uses a case-insensitive regex"
    pat = re.compile(m.group(1), re.I)
    for spelling in ("COUNT(DISTINCT agent_id)", "count(distinct agent_id)",
                     "SELECT DISTINCT agent_id", "COUNT(DISTINCT i.agent_id)"):
        assert pat.search(spelling), f"regex misses {spelling!r}"


def test_lane8_does_not_score_probe_rows_as_humans():
    """relay_opens holds only our own probe traffic (human-simulated /
    dchub-ops-verify). A bare count(*) > 0 renders PASS on probes — the
    flattering-zero this shell exists to catch."""
    src = _src("routes", "loop_control_master_shell.py")
    lane = src[src.index("def _lane_relay_two_artifact"):]
    # Assert the markers appear in the SQL FILTER, not merely in the prose.
    # A substring check over the whole lane passes on the explanatory comment
    # even when the filter is broken — caught by a must-fail control, which is
    # the entire point of running one.
    filtered = set(re.findall(r"position\('([^']+)' in ", lane))
    for marker in ("dchub-ops-verify", "human-simulated", "probe"):
        assert marker in filtered, (
            f"lane 8 does not FILTER {marker!r} in SQL (filters: "
            f"{sorted(filtered)})")
    assert "a REAL human has opened a relay link" in lane
    # and it must refuse to score at all when nothing can tell them apart
    assert "refusing to score probe rows as humans" in lane


def test_shell_sql_carries_no_percent_literal():
    """psycopg2 substitution trap: a literal % in a PARAMLESS execute() is read
    as a substitution marker and 500s.

    ★ 2026-08-31: _row gained an optional `params` argument, so the shell now
    has two modes and this check has to tell them apart instead of banning % on
    sight:

      * paramless  _row(c, sql)          -> NO percent character at all. This is
                                            the original contract and it still
                                            governs almost every call here.
      * with params _row(c, sql, params) -> %s / %(name)s placeholders are the
                                            point; any OTHER percent must be
                                            doubled to %%.

    Walked with ast rather than a regex over source text: the old regex matched
    a triple-quoted literal after `_row(c,` and could not see whether a third
    argument followed, so it had no way to distinguish the modes — and a looser
    regex would have quietly stopped guarding the paramless calls, which are
    the dangerous ones.
    """
    import ast as _ast
    src = _src("routes", "loop_control_master_shell.py")
    tree = _ast.parse(src)

    placeholder = re.compile(r"%(?:s|\(\w+\)s|%)")
    checked = 0
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        fn = node.func
        if getattr(fn, "id", None) != "_row":
            continue
        if len(node.args) < 2:
            continue
        sql_node = node.args[1]
        # Only literal / f-string SQL is inspectable; a variable is out of scope.
        if isinstance(sql_node, _ast.Constant) and isinstance(sql_node.value, str):
            sql = sql_node.value
        elif isinstance(sql_node, _ast.JoinedStr):
            sql = "".join(v.value for v in sql_node.values
                          if isinstance(v, _ast.Constant) and isinstance(v.value, str))
        else:
            continue
        checked += 1
        has_params = len(node.args) >= 3 or any(k.arg == "params" for k in node.keywords)
        if not has_params:
            assert "%" not in sql, (
                f"percent literal in PARAMLESS _row SQL (500s at runtime): "
                f"{sql.strip()[:90]}")
        else:
            leftovers = placeholder.sub("", sql)
            assert "%" not in leftovers, (
                f"un-doubled percent alongside placeholders — double it to %%: "
                f"{sql.strip()[:90]}")

    assert checked >= 10, (
        f"only {checked} _row SQL literals inspected — the walker stopped "
        f"seeing them, which would render this guard vacuous")


def test_row_paramless_mode_is_still_the_default():
    """The params path must be opt-in. If `params` ever stops defaulting to
    None, every existing paramless caller changes behaviour at once."""
    import ast as _ast
    src = _src("routes", "loop_control_master_shell.py")
    for node in _ast.parse(src).body:
        if isinstance(node, _ast.FunctionDef) and node.name == "_row":
            names = [a.arg for a in node.args.args]
            assert names[:3] == ["c", "sql", "params"], names
            assert node.args.defaults, "params must have a default"
            last = node.args.defaults[-1]
            assert isinstance(last, _ast.Constant) and last.value is None, \
                "params must default to None (paramless is the default mode)"
            break
    else:
        raise AssertionError("_row not found")


def test_shell_is_read_only():
    """READ-ONLY / DIAGNOSTIC: the shell names actuators and fires nothing.

    Lane 5 GREPS for a raw findings INSERT, so the needle is assembled at
    import (_FINDINGS_INSERT_NEEDLE) — that keeps the literal out of this
    file's source, which is what both this pin and the insert-no-on-conflict
    regression lint check."""
    src = _src("routes", "loop_control_master_shell.py")
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER "):
        assert verb not in src.upper(), f"shell must not write: found {verb!r}"


def test_lane5_needle_still_matches_a_real_writer():
    """The assembled needle must still find the writers lane 5 exists to catch —
    an over-clever split that stops matching would render a silent green."""
    from routes.loop_control_master_shell import _FINDINGS_INSERT_NEEDLE
    assert _FINDINGS_INSERT_NEEDLE == "INSERT INTO brain_findings"
    writer = _src("routes", "brain_findings_writer.py")
    assert _FINDINGS_INSERT_NEEDLE in writer


def test_shell_registered_in_main():
    src = _src("main.py")
    assert "loop_control_master_shell_bp" in src
    assert "register_blueprint(loop_control_master_shell_bp)" in src


def test_writer_allowlist_is_the_canonical_writer():
    from routes.loop_control_master_shell import _WRITER_ALLOWLIST
    assert _WRITER_ALLOWLIST == ("brain_findings_writer.py",)
    assert os.path.exists(os.path.join(ROOT, "routes", "brain_findings_writer.py"))


# ── surface_canon: surfaces track PINNED, PINNED tracks live ─────────

def _surface_canon_lane(monkeypatch, tmp_path, files, live, pinned):
    """Run the real lane against a temp repo of surface files."""
    import types, sys as _sys
    from routes import loop_control_master_shell as lc

    for rel, count in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f'"{count:,}+ facilities across 170+ countries"')
    monkeypatch.setattr(lc, "_repo_root", lambda: str(tmp_path))

    canon_mod = types.ModuleType("ai_surface_canon")
    canon_mod.PINNED = {"public": {"facilities": f"{pinned:,}+"}}
    monkeypatch.setitem(_sys.modules, "ai_surface_canon", canon_mod)

    stats_mod = types.ModuleType("canonical_stats")
    stats_mod.get_canonical_stats = lambda *a, **k: {"facilities_distinct": live}
    monkeypatch.setitem(_sys.modules, "canonical_stats", stats_mod)

    return {ch["id"]: ch for ch in lc._lane_surface_canon(None)}


def test_surfaces_at_pinned_are_current_even_when_live_has_moved(monkeypatch, tmp_path):
    """★ THE FIX. Hand-maintained files carry PINNED; live canon moves on its
    own. Measuring the files against LIVE made them permanently stale the moment
    live drifted 500 past the pin — and contradicted surface_truth, which
    ACCEPTS PINNED. Two guards disagreeing about one file is how one gets
    ignored."""
    out = _surface_canon_lane(monkeypatch, tmp_path,
                              {"llms.txt": 18500, "mcp.json": 18500},
                              live=19935, pinned=18500)
    assert out["floors_current"]["pass"] is True, out["floors_current"]["detail"]


def test_a_surface_below_pinned_is_still_caught(monkeypatch, tmp_path):
    """Loosening must not blind it — 15,000 against a pin of 18,500 is the real
    defect this lane exists for, and it is what repo-root mcp.json carried."""
    out = _surface_canon_lane(monkeypatch, tmp_path,
                              {"llms.txt": 18500, "mcp.json": 15000},
                              live=19935, pinned=18500)
    assert out["floors_current"]["pass"] is False
    assert "mcp.json" in out["floors_current"]["detail"]


def test_a_live_healed_surface_inside_the_band_passes(monkeypatch, tmp_path):
    """Some surfaces are heal-bound and carry the live floor. Within PINNED
    x 1.10 — the same band surface_truth uses — that is correct, not stale."""
    out = _surface_canon_lane(monkeypatch, tmp_path,
                              {"llms.txt": 19700}, live=19935, pinned=18500)
    assert out["floors_current"]["pass"] is True


def test_pinned_falling_behind_live_is_its_own_single_finding(monkeypatch, tmp_path):
    """The other half of the relationship, reported ONCE — "bump the pin" — not
    as N identical per-file failures."""
    out = _surface_canon_lane(monkeypatch, tmp_path,
                              {"llms.txt": 15000}, live=25000, pinned=15000)
    assert out["floors_current"]["pass"] is True, "the file matches its pin"
    assert out["pinned_tracks_live"]["pass"] is False
    assert "bump" in out["pinned_tracks_live"]["detail"].lower()


def test_unreadable_pin_refuses_to_judge(monkeypatch, tmp_path):
    """No pin means no contract to measure against — indeterminate, never a
    silent pass."""
    import types, sys as _sys
    from routes import loop_control_master_shell as lc
    (tmp_path / "llms.txt").write_text('"18,500+ facilities"')
    monkeypatch.setattr(lc, "_repo_root", lambda: str(tmp_path))
    bad = types.ModuleType("ai_surface_canon")   # no PINNED attribute
    monkeypatch.setitem(_sys.modules, "ai_surface_canon", bad)
    stats = types.ModuleType("canonical_stats")
    stats.get_canonical_stats = lambda *a, **k: {"facilities_distinct": 19935}
    monkeypatch.setitem(_sys.modules, "canonical_stats", stats)
    out = {ch["id"]: ch for ch in lc._lane_surface_canon(None)}
    assert out["floors_current"]["pass"] is None
