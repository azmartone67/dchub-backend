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


def test_lane7_needle_case_matches_its_comparand():
    """Shipped lowercase against body.upper(), so lane 7 could never match and
    rendered a permanent '?'. A gate that CANNOT fire is not a gate."""
    src = _src("routes", "loop_control_master_shell.py")
    m = re.search(r'if body and "([^"]+)" in body\.upper\(\)', src)
    assert m, "lane 7 needle comparison not found"
    needle = m.group(1)
    assert needle == needle.upper(), (
        f"needle {needle!r} is compared against body.upper() but is not "
        f"upper-case — it can never match")


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
    """psycopg2 substitution trap: a literal % in a paramless execute() 500s.
    Every statement in this shell is literal SQL with no params tuple."""
    src = _src("routes", "loop_control_master_shell.py")
    for m in re.finditer(r'_row\(c,\s*(?:f)?"""(.*?)"""', src, re.S):
        assert "%" not in m.group(1), f"percent literal in SQL: {m.group(1)[:80]}"


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
