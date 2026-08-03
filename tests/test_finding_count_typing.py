"""#49 lane 3: `count` carries a declared TYPE (2026-08-02).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

The #48 misread was not a one-off. `count` is per-detector free-form, and
impact_weight() read it as a tally, so a duration won the agenda three times —
frontend_endpoint_slow, dedup_backlog_large, cron_silently_dead — and each time
the fix was to add one more string to VALUE_NOT_COUNT_ISSUES. That is a
subscription, not a fix: it only ever runs AFTER the damage.

These pins hold the structural version: the detector declares what its integer
means, and an undeclared magnitude is distrusted on plausibility instead of on
someone's memory.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── layer 1: the declared type decides ────────────────────────────────

def test_declared_magnitude_contributes_nothing():
    from routes.brain_work_selector import occurrence_signal
    occ, why = occurrence_signal(
        {"issue": "anything_at_all", "count": 477455,
         "count_kind": "seconds_since"})
    assert occ == 0
    assert why["source"] == "declared_value"


def test_declared_occurrence_is_trusted_above_the_ceiling():
    """A declared tally is a promise from the detector. We keep it — the
    ceiling exists for integers nobody has vouched for."""
    from routes.brain_work_selector import occurrence_signal
    occ, why = occurrence_signal({"count": 50_000, "count_kind": "occurrence"})
    assert occ == 50_000
    assert why["source"] == "declared_occurrence"


def test_declared_type_beats_a_misleading_issue_string():
    """The string denylist must not be able to override an explicit
    declaration in either direction."""
    from routes.brain_work_selector import occurrence_signal
    occ, _ = occurrence_signal(
        {"issue": "cron_silently_dead @ /api/jobs/x", "count": 7,
         "count_kind": "occurrence"})
    assert occ == 7


# ── layer 2: the legacy denylist still speaks for undeclared rows ─────

def test_legacy_denylist_still_applies_when_undeclared():
    from routes.brain_work_selector import occurrence_signal
    occ, why = occurrence_signal(
        {"issue": "cron_silently_dead @ /api/jobs/site-baseline",
         "count": 477455})
    assert occ == 0
    assert why["source"] == "legacy_denylist"


def test_the_three_historical_classes_are_all_still_covered():
    from routes.brain_work_selector import VALUE_NOT_COUNT_ISSUES, occurrence_signal
    for issue in VALUE_NOT_COUNT_ISSUES:
        occ, _ = occurrence_signal({"issue": issue, "count": 9_999})
        assert occ == 0, f"{issue} lost its guard"


# ── layer 3: THE PROMISE — detector #4, with no tuple edit ────────────

def test_an_undeclared_magnitude_from_an_unknown_detector_is_distrusted():
    """★THE POINT OF THE WHOLE CHANGE. A detector nobody has annotated and
    nobody has added to VALUE_NOT_COUNT_ISSUES writes a magnitude into
    `count`. Before: it hit the occurrence cap and won the agenda until a
    human noticed. Now: implausible as a tally, so it buys nothing."""
    from routes.brain_work_selector import occurrence_signal
    occ, why = occurrence_signal(
        {"issue": "brand_new_detector_nobody_listed", "count": 863_400})
    assert occ == 0
    assert why["source"] == "untyped_over_ceiling"
    assert "Declare count_kind" in why["note"]


def test_a_plausible_undeclared_count_is_still_trusted():
    """The ceiling must not quietly disable occurrence weighting for the ~193
    radar sites that legitimately write small tallies."""
    from routes.brain_work_selector import occurrence_signal
    occ, why = occurrence_signal({"issue": "ordinary_finding", "count": 12})
    assert occ == 12
    assert why["source"] == "untyped_within_ceiling"


def test_ceiling_is_tunable(monkeypatch):
    from routes.brain_work_selector import occurrence_signal
    monkeypatch.setenv("BRAIN_UNTYPED_COUNT_CEILING", "5")
    occ, why = occurrence_signal({"issue": "ordinary_finding", "count": 12})
    assert occ == 0 and why["source"] == "untyped_over_ceiling"


# ── tally keys are never free-form ────────────────────────────────────

def test_seen_count_survives_a_declared_value_on_the_same_row():
    """seen_count is written by the canonical writer under episode semantics —
    it is a tally by construction. The old guard zeroed it along with `count`,
    losing a real recurrence signal to protect against a fake one."""
    from routes.brain_work_selector import occurrence_signal
    occ, _ = occurrence_signal(
        {"issue": "cron_silently_dead", "count": 477455, "seen_count": 4})
    assert occ == 4


def test_non_integer_count_does_not_explode():
    from routes.brain_work_selector import occurrence_signal
    occ, _ = occurrence_signal({"issue": "x", "count": "not a number"})
    assert occ == 0


def test_missing_and_malformed_candidates_are_neutral():
    from routes.brain_work_selector import occurrence_signal
    assert occurrence_signal(None)[0] == 0
    assert occurrence_signal({})[0] == 0


# ── the ranking consequence ───────────────────────────────────────────

def test_a_duration_cannot_outrank_a_real_recurrence():
    """The #48 damage in one assertion: three cron_silently_dead items held the
    top three agenda slots on the strength of a duration while four genuinely
    dead crons went unworked."""
    from routes.brain_work_selector import impact_weight
    duration, _ = impact_weight(
        {"issue": "cron_silently_dead @ /api/jobs/site-baseline",
         "count": 477455, "count_kind": "seconds_since"})
    recurrence, _ = impact_weight(
        {"issue": "facility_duplicates_unmarked", "count": 5})
    assert recurrence > duration


def test_impact_weight_explains_itself():
    """/api/v1/brain/work-plan is the self-model surface. 'This number was
    ignored because the detector declared it a duration' is precisely the
    sentence whose absence let the misread run for days."""
    from routes.brain_work_selector import impact_weight
    _, why = impact_weight({"issue": "x", "count": 999999})
    assert why["occurrence_source"]["source"] == "untyped_over_ceiling"


# ── the rendering counterpart ─────────────────────────────────────────

def test_row_count_is_value_agrees_with_the_ranker():
    """A finding must never be ranked as a magnitude in one place and
    described as a recurrence in another."""
    from routes.brain_work_selector import row_count_is_value
    assert row_count_is_value({"count_kind": "latency_ms", "value": 3}) is True
    assert row_count_is_value({"count_kind": "occurrence", "value": 3}) is False
    assert row_count_is_value({"claim": "cron_silently_dead @ x"}) is True
    assert row_count_is_value({"claim": "ordinary", "value": 999_999}) is True
    assert row_count_is_value({"claim": "ordinary", "value": 4}) is False
    assert row_count_is_value(None) is False


# ── the write path ────────────────────────────────────────────────────

def test_writer_accepts_and_self_heals_count_kind():
    src = _src("routes", "brain_findings_writer.py")
    assert "count_kind: str = \"\"" in src
    assert "_COUNT_KIND_DDL" in src
    assert "ADD COLUMN IF NOT EXISTS {cname} {ctype}" in src


def test_writer_never_clobbers_a_declared_type_with_a_blank():
    """An undeclared write must not erase a type a detector previously
    declared — that silently re-opens the hole the column closes."""
    src = _src("routes", "brain_findings_writer.py")
    assert "write_kind = bool(count_kind) and \"count_kind\" in cols" in src


def test_radar_declares_the_cron_duration():
    """The regression pin from #48, now at the SOURCE rather than in a
    downstream denylist."""
    src = _src("routes", "brain_consistency_radar.py")
    m = re.search(r'"issue":\s*"cron_silently_dead".*?"count":\s*([^,\n]+)',
                  src, re.S)
    assert m and "seconds_since" in m.group(1)
    block = src[m.start():m.start() + 900]
    assert '"count_kind": "seconds_since"' in block


def test_radar_carries_count_kind_to_the_writer():
    """Declaring it in the dict is worthless if the persister drops it."""
    src = _src("routes", "brain_consistency_radar.py")
    assert 'count_kind=f.get("count_kind") or ""' in src


def test_investigator_falls_back_when_the_column_is_absent():
    """★The column is added by the writer's self-heal, so a database that has
    not taken a write since deploy does not have it yet. A failed SELECT lands
    in the outer except, which blanks the ENTIRE live worklist — the brain's
    evidence feed silently empties. Falling back to the original projection is
    what keeps a missing column from doing that."""
    from routes import brain_investigator as bi

    class _Cur:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, args=None):
            self.sqls.append(sql)
            if "count_kind" in sql:
                raise RuntimeError('column "count_kind" does not exist')

        def fetchall(self):
            return [("some_issue", "/u", 3, None)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    cur = _Cur()

    class _Conn:
        def cursor(self, *a, **k):
            return cur

        def rollback(self):
            pass

        def close(self):
            pass

    bi_conn = bi._conn
    try:
        bi._conn = lambda *a, **k: _Conn()
        rows = bi._gather_recent_findings(limit=3)
    finally:
        bi._conn = bi_conn

    assert len(rows) == 1, "a missing count_kind column emptied the worklist"
    assert rows[0]["value"] == 3
    assert rows[0]["count_kind"] == ""
    assert any("count_kind" in s for s in cur.sqls), "never tried the typed read"
    assert any("count_kind" not in s for s in cur.sqls), "never fell back"


def test_investigator_passes_the_type_through_when_present():
    from routes import brain_investigator as bi

    class _Cur:
        def execute(self, sql, args=None):
            pass

        def fetchall(self):
            return [("cron_silently_dead", "/api/jobs/x", 477455, None,
                     "seconds_since")]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self, *a, **k):
            return _Cur()

        def rollback(self):
            pass

        def close(self):
            pass

    bi_conn = bi._conn
    try:
        bi._conn = lambda *a, **k: _Conn()
        rows = bi._gather_recent_findings(limit=1)
    finally:
        bi._conn = bi_conn

    assert rows[0]["count_kind"] == "seconds_since"
    # End to end: the gathered row renders as a VALUE, not "seen x477455".
    from routes.brain_work_selector import row_count_is_value
    assert row_count_is_value(rows[0]) is True


def test_enhancer_uses_the_shared_decision():
    src = _src("routes", "brain_enhancer.py")
    assert "row_count_is_value" in src


# ── the shell's own lane agrees ───────────────────────────────────────

def test_shell_lane_3_sees_the_derived_guard():
    """Shell #49 lane 3 shipped RED on allowlist_derived. With this landed it
    must go green — a stale FAIL on the board is green-by-silence in reverse."""
    from routes.graph_master_shell import _lane_typed_nodes

    class _NullCur:
        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return None

    ch = next(c for c in _lane_typed_nodes(_NullCur())
              if c["id"] == "allowlist_derived")
    assert ch["pass"] is True
    assert "ceiling" in ch["detail"]


# ── the annotation sweep ──────────────────────────────────────────────

def test_self_identifying_counts_are_annotated():
    """The sweep only labelled expressions that NAME themselves (len(...),
    age_h, _ms, seconds, backlog). An ambiguous site is left undeclared on
    purpose: a declared kind OVERRIDES the plausibility ceiling, so a wrong
    label is worse than an absent one."""
    src = _src("routes", "brain_consistency_radar.py")
    assert src.count('"count_kind":') >= 25


def test_breadth_is_not_recurrence():
    """★A finding reporting len(drifted)==12 was seen ONCE, about 12 files. The
    old reading gave it a 12-occurrence bump, conflating how many things are
    wrong with how many times we noticed. Those findings now rank on severity
    and age; if breadth should count it needs its own input, not a lie about
    occurrence."""
    from routes.brain_work_selector import occurrence_signal
    occ, why = occurrence_signal(
        {"issue": "files_drifted", "count": 12, "count_kind": "item_count"})
    assert occ == 0 and why["source"] == "declared_value"


def test_undeclared_sites_are_still_covered_by_the_ceiling():
    """93 sites remain undeclared — deliberately. The ceiling is what makes
    that safe rather than outstanding work."""
    from routes.brain_work_selector import occurrence_signal
    assert occurrence_signal({"issue": "unlabelled", "count": 500_000})[0] == 0
