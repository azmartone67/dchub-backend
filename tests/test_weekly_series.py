"""Guards on the fixed-window weekly series.

This endpoint exists so that a published week-over-week delta has a baseline
that cannot move. Each test below pins one of the five defects that motivated
it — the partial week in the series, a hand-written population, a missing
externality filter, an unknown rendered as 0, and an unlabelled boundary.

Every text scan runs against COMMENT-STRIPPED source. Three separate tests in
this repo have passed by matching the comment that explained the bug they were
written to catch, and this file's module is unusually comment-heavy — it names
every defect it guards, so an un-stripped scan here would be almost guaranteed
to pass on prose alone.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "weekly_series.py"
sys.path.insert(0, str(ROOT))


def _stripped() -> str:
    """Source with comments and docstrings removed."""
    raw = SRC.read_text(encoding="utf-8")
    no_comments = re.sub(r"(?m)#.*$", "", raw)
    tree = ast.parse(raw)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                no_comments = no_comments.replace(d, "")
    return no_comments


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {SRC}")


# ── the module is actually there ─────────────────────────────────────────────

def test_module_parses_and_names_resolve():
    """An empty or gutted module would pass every text scan in this file.

    So: it must parse, the functions the other tests exercise must exist, and
    the canonical predicates must be genuinely imported — a module that
    dropped the mcp_calls_deloop import would still satisfy a naive
    "is the predicate string present" scan by carrying a stale copy.
    """
    raw = SRC.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"_window_filters", "_population_filters", "_population",
            "_week_starts", "_assemble", "_wow", "_partial_week", "_run",
            "weekly_series"} <= funcs, f"module gutted — found only {funcs}"

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "mcp_calls_deloop":
            imported |= {a.name for a in node.names}
    assert {"external_platform_predicate", "real_ua_predicate"} <= imported, (
        "the canonical externality predicates must be IMPORTED from "
        f"mcp_calls_deloop, not restated — found {imported}"
    )


# ── rule 1: the in-progress week is never in the series ──────────────────────

def test_series_upper_bound_excludes_the_current_week():
    """The exclusion must live in the SQL WHERE, not in a later post-filter.

    A post-filter is one careless edit away from being dropped; a query that
    never fetches the rows cannot leak them.
    """
    src = _stripped()
    assert "created_at <  date_trunc('week', now())" in src or \
           "created_at < date_trunc('week', now())" in src, (
        "the window's upper bound on the current week start is missing from "
        "the executed SQL"
    )
    filters = __import__(
        "routes.weekly_series", fromlist=["x"])._window_filters(8)
    upper = [f for f in filters if f.strip().startswith("created_at <")]
    assert len(upper) == 1, f"expected exactly one upper bound, got {filters}"
    assert "date_trunc('week', now())" in upper[0]
    assert "interval" not in upper[0], (
        "the upper bound must be the current week start exactly — an interval "
        "offset here would let part of the live week into the series"
    )


def test_week_starts_never_includes_the_current_week():
    from routes.weekly_series import _week_starts
    cur = dt.date(2026, 8, 3)          # the Monday of the live week
    starts = _week_starts(cur, 8)
    assert cur not in starts, (
        f"the in-progress week {cur} is in the series — this is the "
        "/api/v1/ai/reach/trend defect: a 2-day week charted beside 7-day "
        "weeks reads as a collapse"
    )
    assert len(starts) == 8
    assert starts[-1] == dt.date(2026, 7, 27), starts[-1]
    assert starts[0] == dt.date(2026, 6, 8), starts[0]
    assert starts == sorted(starts), "weeks must be ascending for charting"
    # non-overlapping, exactly 7 days apart
    gaps = {(b - a).days for a, b in zip(starts, starts[1:])}
    assert gaps == {7}, f"weeks must be contiguous and non-overlapping: {gaps}"


def test_partial_week_is_labelled_and_barred_from_the_delta():
    from routes.weekly_series import _partial_week, _wow
    now = dt.datetime(2026, 8, 5, 18, 0, tzinfo=dt.timezone.utc)
    p = _partial_week(dt.date(2026, 8, 3), 6, 640, now)
    assert p["partial"] is True
    assert p["excluded_from_series"] is True
    assert p["excluded_from_delta"] is True
    assert p["hours_elapsed_of_168"] < 168
    assert "IN PROGRESS" in p["warning"]

    # Even if a partial row were somehow spliced into the series, the delta
    # must refuse to compute against it.
    complete = [
        {"week_start": "2026-07-20", "status": "measured", "agents": 62,
         "calls": 1971, "partial": False},
        {"week_start": "2026-07-27", "status": "measured", "agents": 85,
         "calls": 8334, "partial": False},
    ]
    poisoned = complete + [dict(p, status="measured", agents=6, calls=640)]
    assert _wow(poisoned)["baseline_week_start"] == "2026-07-20", (
        "a partial week reached the delta — the baseline must stay on the "
        "last two COMPLETE weeks"
    )
    assert _wow(poisoned)["agents_pct"] == _wow(complete)["agents_pct"]


# ── rule 2: the population is built from the executed filters ────────────────

def test_published_population_is_the_executed_filter_list():
    from routes.weekly_series import (_population, _population_filters,
                                      _window_filters)
    pop = _population(8)
    assert pop["sql_where_filters"] == _window_filters(8), (
        "the published WHERE filters are not the ones the query runs"
    )
    assert pop["sql_population_filters"] == _population_filters(), (
        "the published population filters are not the ones the query runs"
    )


def test_run_joins_the_filter_functions_into_its_sql():
    """The declaration must be the SAME list the query uses.

    Asserted structurally: _run must call both filter functions. If a future
    edit inlined the SQL and left _population() describing the old filters,
    the payload would be a second source of truth — which is exactly how a
    docstring came to claim "real external calls" over an unfiltered
    population (PR #2252).
    """
    called = {n.func.id for n in ast.walk(_fn("_run"))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"_window_filters", "_population_filters"} <= called, (
        f"_run does not build its SQL from the published filters: {called}"
    )


# ── rule 3: self-traffic is excluded by the imported predicates ──────────────

def test_population_composes_the_canonical_predicates_verbatim():
    from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
    from routes.weekly_series import _population_filters
    filters = _population_filters()
    assert external_platform_predicate("platform") in filters, (
        "external_platform_predicate is not composed into the population — "
        "PR #2252 shipped because a published p50 had no externality filter "
        "and ~80% of its population was our own probes"
    )
    assert real_ua_predicate("user_agent") in filters, (
        "real_ua_predicate is not composed into the population"
    )
    assert "is_public_ip" in filters and "is_real_external" in filters, (
        "the canonical identity basis is missing — this series would then be "
        "on a different basis than the funnel headline it is compared to"
    )


def test_no_hand_copied_exclusion_list():
    """A copied list is a regex twin, and twins drift (2026-07-30, two days)."""
    src = _stripped()
    for smell in ("dchub-%", "'%dchub%'", "smithery", "INTERNAL_PLATFORM_VALUES = ",
                  "probe|audit|harness"):
        assert smell not in src, (
            f"{smell!r} appears in the executed source — the exclusion list "
            "must be imported from mcp_calls_deloop, never restated here"
        )


def test_no_bound_params_anywhere_the_percent_predicate_is_inlined():
    """external_platform_predicate carries a literal % (LIKE).

    psycopg2 only interprets % when a parameter sequence is supplied, so every
    execute() in _run must take exactly one argument. A second argument turns
    the LIKE patterns into broken format specifiers at runtime — the
    empty-tuple-percent trap.
    """
    for node in ast.walk(_fn("_run")):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"):
            assert len(node.args) == 1 and not node.keywords, (
                f"execute() at line {node.lineno} passes bound params next to "
                "a predicate containing a literal % — this raises at runtime"
            )


# ── rule 4: unknown renders null, never 0 ────────────────────────────────────

def test_unobserved_week_is_null_not_zero():
    from routes.weekly_series import _assemble
    starts = [dt.date(2026, 7, 20), dt.date(2026, 7, 27)]
    rows = {dt.date(2026, 7, 27): (85, 8334, 9001)}   # 07-20 never observed
    out = _assemble(rows, starts)
    missing = out[0]
    assert missing["agents"] is None and missing["calls"] is None, (
        "an unobserved week rendered a number — this is the "
        "/api/v1/ai/reach/trend defect that published 0 distinct IPs and 17 "
        "first-ever IPs in the same row"
    )
    assert missing["status"] == "no_observation"
    assert out[1]["agents"] == 85 and out[1]["status"] == "measured"


def test_zero_rows_observed_is_also_null():
    """A week whose only evidence is `rows_observed = 0` is not a zero.

    The row exists in the GROUP BY only if something was there; a 0 here means
    the aggregate saw nothing to observe, so it must not be published as a
    measured zero either.
    """
    from routes.weekly_series import _assemble
    out = _assemble({dt.date(2026, 7, 20): (0, 0, 0)}, [dt.date(2026, 7, 20)])
    assert out[0]["agents"] is None, "rows_observed=0 published as measured 0"
    assert out[0]["status"] == "no_observation"


def test_observed_week_with_no_real_agents_is_an_honest_zero():
    """The other half of rule 4: a real finding must not be hidden as null.

    We observed 4,000 rows that week and none of them was a real external
    agent. That IS zero, and saying "unknown" would be its own dishonesty.
    """
    from routes.weekly_series import _assemble
    out = _assemble({dt.date(2026, 6, 15): (0, 0, 4000)}, [dt.date(2026, 6, 15)])
    assert out[0]["agents"] == 0 and out[0]["calls"] == 0
    assert out[0]["status"] == "measured"


def test_delta_refuses_against_an_unobserved_or_zero_baseline():
    from routes.weekly_series import _wow
    unobserved = [
        {"week_start": "2026-07-20", "status": "no_observation",
         "agents": None, "calls": None, "partial": False},
        {"week_start": "2026-07-27", "status": "measured", "agents": 85,
         "calls": 8334, "partial": False},
    ]
    w = _wow(unobserved)
    assert w["agents_pct"] is None and w["calls_pct"] is None
    assert w["reason"] and "not a delta" in w["reason"]

    zero_base = [
        {"week_start": "2026-07-20", "status": "measured", "agents": 0,
         "calls": 0, "partial": False},
        {"week_start": "2026-07-27", "status": "measured", "agents": 85,
         "calls": 8334, "partial": False},
    ]
    z = _wow(zero_base)
    assert z["agents_pct"] is None, "percentage change from a zero baseline"
    assert z["reason"] and "undefined" in z["reason"]


def test_delta_is_correct_and_names_its_fixed_baseline():
    from routes.weekly_series import _wow
    weeks = [
        {"week_start": "2026-07-20", "status": "measured", "agents": 62,
         "calls": 1971, "partial": False},
        {"week_start": "2026-07-27", "status": "measured", "agents": 85,
         "calls": 8334, "partial": False},
    ]
    w = _wow(weeks)
    assert w["agents_pct"] == 37.1, w          # (85-62)/62
    assert w["baseline_week_start"] == "2026-07-20"
    assert w["current_week_start"] == "2026-07-27"
    assert w["baseline_is_fixed"] is True


# ── rule 5: the boundary is stated ───────────────────────────────────────────

def test_every_week_states_its_boundary():
    from routes.weekly_series import _assemble
    out = _assemble({dt.date(2026, 7, 27): (85, 8334, 9001)},
                    [dt.date(2026, 7, 27)])
    w = out[0]
    assert w["week_start"] == "2026-07-27"
    assert w["week_end_exclusive"] == "2026-08-03"
    assert w["iso_week"] == "2026-W31", w["iso_week"]
    assert w["days"] == 7 and w["partial"] is False


def test_weeks_are_clamped_to_a_sane_range():
    from routes.weekly_series import _MAX_WEEKS, _MIN_WEEKS, _clamp_weeks
    assert _clamp_weeks(None) >= _MIN_WEEKS
    assert _clamp_weeks("garbage") >= _MIN_WEEKS
    assert _clamp_weeks(1) == _MIN_WEEKS
    assert _clamp_weeks(9999) == _MAX_WEEKS
    assert _clamp_weeks("12") == 12


def test_blueprint_is_registered_in_main():
    """Registration is not function, but non-registration is guaranteed 404."""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "from routes.weekly_series import weekly_series_bp" in main
    assert "app.register_blueprint(weekly_series_bp)" in main


# ── the misleading funnel alias, documented in the payload ───────────────────

def test_funnel_names_its_deprecated_complete_aliases():
    """`external_ips_7d_complete` is rolling, not complete-day.

    PR #2254 renamed it, but the rename lives in a source comment and this
    endpoint's readers get JSON. Measured 2026-08-05 the key was byte-identical
    to the rolling real_external_agents_7d (both 47) and has no `_prior`
    sibling, so trusting the suffix means pairing a rolling numerator with a
    complete-day baseline. The deprecation must be machine-readable, and it
    must be emitted OUTSIDE the try/except — a failed query nulls the aliased
    keys, which is exactly when the mapping matters most.
    """
    src = (ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8")
    assert '"external_ips_7d_complete": "external_agents_7d"' in src, (
        "the deprecated alias is not mapped to its replacement in the payload"
    )
    assert 'out["deprecated_aliases_note"]' in src

    tree = ast.parse(src)
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Subscript)
               and isinstance(n.targets[0].slice, ast.Constant)
               and n.targets[0].slice.value == "deprecated_aliases"]
    assert assigns, "deprecated_aliases is never assigned"
    handler_lines = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ExceptHandler):
            handler_lines.update(range(n.lineno, (n.end_lineno or n.lineno) + 1))
    assert any(a.lineno not in handler_lines for a in assigns), (
        "deprecated_aliases is only emitted from an except handler — it must "
        "be published unconditionally"
    )
