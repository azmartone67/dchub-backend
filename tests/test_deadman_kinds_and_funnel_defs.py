"""Two published-contract gaps, closed and guarded (2026-08-25).

WHAT WAS MEASURED
=================
1. /api/v1/ops/deadman published ONE `overdue` boolean covering five different
   faults, two of which mean opposite things operationally. Live that morning:

       worker:feedback_triage   age 36.4h / cad 18h  status=success  -> LATE
       brain-self-direct        age  8.3h / cad 16h  status=failure  -> BROKE

   Both read `overdue: true`. Telling them apart required parsing English out
   of `reasons[]`, so a consumer either wrote a prose parser or treated a
   failed run as staleness.

2. /api/v1/mcp/handoff-funnel published full definitions for `human_acted` and
   `redeemed` and NONE for `paywall_hit` or `high_intent` — the two stages the
   headline conversion rate is computed from. A 24h read of
   `paywall 24 -> high_intent 2 (8.33%)` against a 7d rate of 55.22% could not
   be judged without reading source.

   Measured, and it settles it: every high_intent session was also a
   paywall_hit session (24h 2/2, 7d 148/148; high_intent with no paywall row =
   0), so it IS a conversion rate. What moves it is the repeat-call share —
   101/268 (37.7%) over 7d against 1/24 (4.2%) in the 8.33% window.

THE CONTRACT being guarded
==========================
- `kinds` ships alongside `reasons`, one token per rule, SAME rules.
- `stale_age` and `run_failed` are separable: late-but-succeeded yields only
  stale_age, failed-but-not-late yields only run_failed.
- `overdue` keeps its meaning — bool(reasons). The off-worker watcher alarms
  on it; narrowing it would silently stop the alarm. A guard that let `overdue`
  drift to `stale_age`-only would be guarding the wrong thing.
- `paywall_hit` and `high_intent` both carry a `basis`, and high_intent's is
  DERIVED from the live entry threshold (★2026-09-03: it used to assert REPEAT
  use unconditionally, which is false at threshold 1 — and prod ran on 1) and
  names its superset — that sentence is the reason the rate is readable at all.

NO NETWORK, NO DB. Rules are re-derived from the module's own constants; the
funnel definitions are read as source text.
"""
import datetime
import importlib.util
import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_IR = os.path.join(_ROOT, "routes", "ingest_runs.py")
_FME = os.path.join(_ROOT, "flask_mcp_endpoints.py")


def _ir():
    spec = importlib.util.spec_from_file_location("ingest_runs_under_test", _IR)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _kinds(age_h, status, cad_h=18.0, cz=0, last_run=True):
    """Re-derive the loop body's rules against the module's real constants."""
    m = _ir()
    kinds = []
    if not last_run:
        kinds.append("never_ran")
    elif age_h > 2 * cad_h:
        kinds.append("stale_age")
    if status and status.lower() not in m._OK_STATUS:
        kinds.append("run_failed")
    if cz and cz >= 3 and (status or "").lower() not in m._NO_NEW_DATA:
        kinds.append("zero_rows")
    return kinds


# ── deadman: the two faults must be separable ──────────────────────────────

def test_late_but_succeeded_is_stale_age_only():
    """worker:feedback_triage — 36.4h into an 18h cadence, status success."""
    assert _kinds(36.4, "success", 18.0) == ["stale_age"]


def test_failed_but_not_late_is_run_failed_only():
    """brain-self-direct — 8.3h into a 16h cadence, latest run failed.

    This is the case the single boolean could not express: NOT late, broke.
    """
    assert _kinds(8.3, "latest-run-failed", 16.0) == ["run_failed"]


def test_healthy_feed_yields_no_kinds():
    assert _kinds(2.0, "success", 18.0) == []


def test_late_and_failed_yields_both():
    assert _kinds(40.0, "error", 18.0) == ["stale_age", "run_failed"]


def test_no_new_data_is_not_a_fault():
    """An affirmative 'upstream had nothing' is health, not a zero-row alarm."""
    assert _kinds(2.0, "no_new_data", 18.0, cz=5) == []


def _rule_body():
    src = open(_IR, encoding="utf-8").read()
    return src[src.index("reasons, kinds = [], []"):src.index('"reasons": reasons,')]


def test_every_rule_appends_a_kind_beside_its_reason():
    """kinds and reasons must stay the SAME set of rules, not drift apart."""
    body = _rule_body()
    n_reasons = len(re.findall(r"reasons\.append\(", body))
    n_kinds = len(re.findall(r"kinds\.append\(", body))
    assert n_reasons == n_kinds, (
        f"{n_reasons} reasons.append vs {n_kinds} kinds.append — a rule was "
        "added to one and not the other, so `kinds` no longer describes "
        "`reasons`"
    )


@pytest.mark.parametrize("reason_marker,expected_kind", [
    ("never ran", "never_ran"),
    (">2x cadence", "stale_age"),
    ("status={st}", "run_failed"),
    ("consecutive zero-row runs", "zero_rows"),
    ("content date in the FUTURE", "future_content_date"),
])
def test_each_reason_is_paired_with_its_own_kind(reason_marker, expected_kind):
    """The PAIRING, not just the count.

    Mutation-tested: swapping `kinds.append("run_failed")` for
    `kinds.append("stale_age")` keeps the counts equal and would pass a
    count-only guard, while publishing a broken run as mere staleness — the
    exact conflation this whole change exists to remove. The earlier version of
    this test re-implemented the rules in the test file, so mutating the source
    could not fail it at all: a guard that reimplements its subject measures
    the copy, not the code.
    """
    body = _rule_body()
    idx = body.find(reason_marker)
    assert idx != -1, f"reason {reason_marker!r} no longer emitted"
    # the kind for a rule is appended within a few lines of its reason
    window = body[idx:idx + 320]
    assert f'kinds.append("{expected_kind}")' in window, (
        f"the {reason_marker!r} rule no longer appends {expected_kind!r} — "
        f"window was: {window[:200]!r}"
    )


def test_overdue_is_late_only_and_unhealthy_carries_every_reason():
    """★2026-09-02 (D2) — this pin used to read `"overdue": bool(reasons)`
    with the note "the watcher alarms on this boolean; narrowing it stops the
    alarm". The boolean IS now narrowed (overdue = LATE only), and the alarm
    moved with it: the record carries `unhealthy` = late OR red, and
    tools/deadman/watch.py folds `unhealthy`, so a feed whose run FAILED still
    alarms — it just no longer reads as late. Both halves are pinned here:
    narrowing `overdue` without `unhealthy` (or without the watcher reading it)
    would be exactly the silent-alarm-loss the old pin guarded against."""
    src = open(_IR, encoding="utf-8").read()
    assert '"overdue": is_late,' in src, (
        "`overdue` no longer means LATE only — a shell that ran on time and beat "
        "lanes_failing would sit on the overdue list again (the D2 cascade)"
    )
    assert '"unhealthy": is_late or is_red,' in src, (
        "`unhealthy` no longer carries every reason — a feed whose run FAILED "
        "would stop alarming while reading as healthy"
    )
    watch = open(os.path.join(_ROOT, "tools", "deadman", "watch.py"), encoding="utf-8").read()
    assert 'r.get("unhealthy", r.get("overdue"))' in watch, (
        "tools/deadman/watch.py no longer folds `unhealthy` — the off-worker "
        "alarm would fire on LATE feeds only and miss every failed run"
    )


def test_kinds_is_published_next_to_reasons():
    src = open(_IR, encoding="utf-8").read()
    assert '"kinds": kinds,' in src, "kinds stopped shipping in the feed record"


# ── funnel: the two undefined stages now carry a basis ─────────────────────

def _defs_region():
    src = open(_FME, encoding="utf-8").read()
    start = src.index('"definitions": {"human_acted"')
    return src[start:start + 4000]


@pytest.mark.parametrize("stage", ["paywall_hit", "high_intent"])
def test_stage_publishes_a_basis(stage):
    region = _defs_region()
    assert f'"{stage}"' in region, f"{stage} definition stopped shipping"
    seg = region[region.index(f'"{stage}"'):]
    assert '"basis"' in seg[:1200], f"{stage} ships without a basis"


def test_high_intent_basis_is_derived_and_names_its_superset():
    """The one sentence that makes the conversion rate readable.

    ★2026-09-03 (r-threshold-drift): this used to assert the literal word
    REPEAT in the definitions SOURCE region. That is exactly how the sentence
    went false — "requires REPEAT use" is true only at threshold >= 2, the
    threshold is an env var read at module import, and prod ran on 1. The
    contract is now DERIVATION: the region must delegate to
    _high_intent_basis(), and that renderer must still say REPEAT when the
    threshold actually makes it repeat-use. Asserting the literal here would
    re-freeze the bug this guard is supposed to catch.
    """
    region = _defs_region()
    seg = region[region.index('"high_intent"'):]
    assert "_high_intent_basis(" in seg[:1600], (
        "high_intent's basis is no longer derived from the live threshold — a "
        "hardcoded sentence goes false the moment prod overrides the default"
    )
    import flask_mcp_endpoints as _f
    assert "REPEAT" in _f._high_intent_basis(2), (
        "at a repeat-use threshold the basis must still say so — without that, "
        "a rate drop driven by one-shot traffic reads as a defect"
    )
    assert "REPEAT" not in _f._high_intent_basis(1), (
        "at threshold 1 the basis must NOT claim repeat use"
    )
    assert '"subset_of": "paywall_hit"' in seg[:1600], (
        "high_intent no longer declares it is a subset of paywall_hit, which "
        "is what makes the ratio a conversion rate rather than two populations"
    )


def test_both_stages_are_marked_funnel_progress():
    """Unlike `redeemed`, which is a disabled machine diagnostic."""
    region = _defs_region()
    for stage in ("paywall_hit", "high_intent"):
        seg = region[region.index(f'"{stage}"'):]
        assert '"is_funnel_progress": True' in seg[:1600], (
            f"{stage} lost its is_funnel_progress flag"
        )
