"""ONE PAYLOAD, THREE "real external weekly" POPULATIONS — the fence.

★ Why this exists (measured 2026-08-05, /api/v1/mcp/funnel).
One payload carried three different weekly "real external" figures with three
different WoW signs:

    real_external_calls_7d      6,868  vs 3,665  = +87.4%   canonical, HAS a basis
    real_external_7d            1,566  vs 1,814  = -13.7%   NO basis anywhere
    tool_calls_7d_complete_real 7,159             = +97.1%  complete-days basis

Two distinct defects, both fenced here.

1. THE NAMING TRAP. `real_external_7d` and `real_external_calls_7d` differed by
   4.5x, pointed in OPPOSITE directions, and were four characters apart.
   Whichever a board happened to bind decided whether the week read as doubling
   or shrinking — and the smaller one published no basis string at all, so a
   reader could not tell what it counted. It never was a call count:
   mcp_funnel_real is a VIEW over mcp_upgrade_signals, and main.py runs the
   byte-identical query under the honest name `2_paywall_hits_7d`. The name was
   the trap, so the name is GONE (renamed, not aliased) — a field that does not
   exist cannot be bound by mistake.

2. press_headline_metric BOUND THE LARGEST POPULATION. It quoted 7,159 (+97.1%)
   while canon read 6,868 (+87.4%). That string is designed to be quoted
   verbatim, so it is the last place a flattering-population default belongs.

   ★ FOLLOW-UP, same day: repointing it at canon fixed the POPULATION and left
   the WINDOW rolling, so the sentence still changed between reads (6,764 ->
   6,762 -> 6,757, +73.3% -> +73.2%). It is now bound to the last COMPLETE ISO
   week of /api/v1/reports/weekly-series. The headline tests below were
   re-anchored to that source; the defect they fence is unchanged — of the
   several weekly figures in this payload, the quotable sentence must not
   reach for the biggest, and must not reach for one that moves. See
   tests/test_measurement_defects_0805.py.

Design: every test here is STATIC and PURE — no DB, no network, no import of
flask_mcp_endpoints. That module raises at import time without
NEON_DATABASE_URL, which CI does not set; an import-based test would either
error or, wrapped in a skip, go SILENTLY GREEN and prove nothing. Instead the
real shipped source is parsed, and the real shipped `_build_press_headline` is
exec'd in isolation, so these assertions run against the code that deploys.

Source-shape assertions are AST-based, never grep: a regex over this file's
text passes happily on a COMMENT that mentions the forbidden name, and this
repo has shipped exactly that bug before. Every AST helper asserts it actually
found its target first, because an empty parse satisfies every "not in" check.

MUST-FAIL CONTROLS: test_control_* feed the pre-fix code shapes and payloads to
the same checkers and assert they REJECT them. If an assertion below ever
degrades into a no-op, a control fails loudly instead of the suite going green.
"""
import ast
import os

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FUNNEL = os.path.join(_REPO, "flask_mcp_endpoints.py")
_DELOOP = os.path.join(_REPO, "mcp_calls_deloop.py")
_RETENTION = os.path.join(_REPO, "routes", "agent_retention_master_shell.py")
_EXPANSION = os.path.join(_REPO, "routes", "agent_expansion_master_shell.py")

# The name that must never come back. Four characters from
# real_external_calls_7d, 4.5x smaller, opposite sign, no basis.
_TRAP_NAME = "real_external_7d"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _parse(path):
    """Parse `path` and assert the parse produced a real module body.

    An empty/failed parse makes every downstream "X not in Y" assertion pass
    vacuously, which is how a fence quietly stops fencing.
    """
    tree = ast.parse(_read(path))
    assert tree.body, f"{path} parsed to an EMPTY module body — fence is blind"
    return tree


def _out_keys(tree):
    """Every string key assigned as `out[...] = ...` anywhere in the tree.

    AST, not grep: the pre-fix name appears in prose in this very repo, and a
    text search would match the comment explaining why it was removed.
    """
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name) and tgt.value.id == "out"
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)):
                keys.add(tgt.slice.value)
    assert keys, "found ZERO out[...] assignments — AST walk is not seeing code"
    return keys


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name}() not found — fence is pointing at nothing")


# ── 1 · the trap name is gone from the payload ───────────────────────────────

def test_funnel_no_longer_publishes_the_ambiguous_name():
    keys = _out_keys(_parse(_FUNNEL))
    assert _TRAP_NAME not in keys, (
        f"{_TRAP_NAME} is published again. It counts mcp_upgrade_signals, not "
        f"calls, and sits four characters from real_external_calls_7d with a "
        f"4.5x gap and the opposite WoW sign."
    )
    for dead in ("real_external_prior_7d", "real_external_wow_pct"):
        assert dead not in keys, f"{dead} is the trap name's trend twin"


def test_funnel_publishes_the_renamed_population_with_a_basis():
    keys = _out_keys(_parse(_FUNNEL))
    for required in ("real_external_signals_7d",
                     "real_external_signals_prior_7d",
                     "real_external_signals_wow_pct",
                     "real_external_signals_basis"):
        assert required in keys, f"{required} missing — the rename is half-done"
    # The canonical population must still be published beside it.
    assert "real_external_calls_7d" in keys


def test_signals_basis_names_its_table_and_the_sibling_it_is_not():
    """A basis string that does not disambiguate is decoration."""
    src = _read(_DELOOP)
    tree = _parse(_DELOOP)
    basis = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "CANONICAL_SIGNALS_BASIS"
                        for t in node.targets)):
            basis = ast.literal_eval(node.value)
    assert basis, "CANONICAL_SIGNALS_BASIS not found in mcp_calls_deloop"
    low = basis.lower()
    assert "mcp_upgrade_signals" in low, "basis must name the table it counts"
    assert "real_external_calls_7d" in low, (
        "basis must name the sibling it is NOT, because near-identical naming "
        "is the whole defect")
    assert "not tool calls" in low or "not a call count" in low
    assert src.count("CANONICAL_SIGNALS_BASIS") >= 1


# ── 2 · the press headline binds canon ───────────────────────────────────────

def _load_headline_builder(series=None):
    """Exec the REAL _build_press_headline + its constants in isolation.

    No import of flask_mcp_endpoints (it raises without NEON_DATABASE_URL), but
    also no reimplementation: the bytes under test are the bytes that deploy.

    ★2026-08-05: the sentence no longer reads its weekly figure off `out` — it
    reads the last COMPLETE ISO week of /api/v1/reports/weekly-series, because
    the rolling pair moved between reads (see
    tests/test_measurement_defects_0805.py). The only piece that cannot run
    here is the DB fetch, so `_press_series` is stubbed to return `series`.
    Stubbing it is load-bearing: leave it UNBOUND and every branch degrades to
    the lifetime sentence and every assertion below passes vacuously.
    """
    tree = _parse(_FUNNEL)
    wanted = {"PRESS_HEADLINE_BASIS"}
    funcs = {"_build_press_headline", "_fixed_window_claim"}
    picked = []
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id in wanted
                        for t in node.targets)):
            picked.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in funcs:
            picked.append(node)
    names = {getattr(n, "name", None) for n in picked}
    assert funcs <= names, f"missing at module level: {sorted(funcs - names)}"
    assert len(picked) == len(wanted) + len(funcs), (
        f"expected {len(wanted)} constants + {len(funcs)} functions, "
        f"extracted {len(picked)}")
    ns = {"_press_series": lambda: series}
    exec(compile(ast.Module(body=picked, type_ignores=[]), _FUNNEL, "exec"), ns)
    return ns["_build_press_headline"], ns["PRESS_HEADLINE_BASIS"]


# The fixed week the sentence quotes as of 2026-08-05 — a COMPLETE ISO week.
_WEEK_CALLS, _WEEK_WOW, _WEEK_START = 8334, 322.8, "2026-07-27"


def _fixed_series():
    return {
        "degraded": False,
        "weeks": [
            {"week_start": "2026-07-20", "calls": 1971, "agents": 62,
             "partial": False, "status": "measured"},
            {"week_start": _WEEK_START, "calls": _WEEK_CALLS, "agents": 85,
             "partial": False, "status": "measured"},
        ],
        "wow": {"calls_pct": _WEEK_WOW, "baseline_is_fixed": True,
                "current_week_start": _WEEK_START,
                "baseline_week_start": "2026-07-20"},
    }


# The real 2026-08-05 numbers, so the fence is anchored to the actual incident.
_CANON_CALLS, _CANON_WOW = 6868, 87.4
_COMPLETE_REAL, _COMPLETE_WOW = 7159, 97.1
_SIGNALS = 1566
_LIFETIME = 306896


def _payload(**over):
    p = {
        "ai_agent_requests_external": _LIFETIME,
        "ai_agent_requests_total": _LIFETIME + 50000,
        "ai_agent_top_platforms_external": [{"name": "Claude"}, {"name": "Meta AI"}],
        "real_external_calls_7d": _CANON_CALLS,
        "real_external_calls_wow_pct": _CANON_WOW,
        "tool_calls_7d_complete_real": _COMPLETE_REAL,
        "tool_calls_wow_pct": _COMPLETE_WOW,
        "real_external_signals_7d": _SIGNALS,
    }
    p.update(over)
    return p


def test_headline_quotes_neither_of_the_larger_populations():
    """The original fence, re-anchored to the fixed-window source.

    The defect this file was built for is unchanged: of the weekly figures in
    this payload, the sentence must not reach for the biggest one.
    """
    build, basis = _load_headline_builder(series=_fixed_series())
    out = _payload()
    build(out)
    line = out["press_headline_metric"]
    assert f"{_WEEK_CALLS:,}" in line, f"headline must quote the fixed week: {line}"
    assert f"{_WEEK_WOW:+.1f}% WoW" in line
    assert _WEEK_START in line, f"the sentence must name its week: {line}"
    # Every pre-fix number must be absent — this is the regression.
    for banned in (f"{_COMPLETE_REAL:,}", f"{_COMPLETE_WOW:+.1f}%",
                   f"{_SIGNALS:,}", f"{_CANON_CALLS:,}", f"{_CANON_WOW:+.1f}%"):
        assert banned not in line, f"headline quotes {banned} again: {line}"
    assert out["press_headline_metric_basis"] == basis
    assert "real_external_calls_7d" in basis, (
        "the basis must still name the rolling sibling it is NOT")


def test_headline_refuses_to_substitute_a_population_when_the_series_is_gone():
    """Falling back to another population is the defect, not the fix."""
    build, _ = _load_headline_builder(series={"degraded": True})
    out = _payload()
    build(out)
    line = out["press_headline_metric"]
    for banned in (f"{_COMPLETE_REAL:,}", f"{_SIGNALS:,}", f"{_CANON_CALLS:,}"):
        assert banned not in line, f"silently fell back to {banned}: {line}"
    assert "this week" not in line.lower(), (
        f"made a weekly claim with no fixed-window number: {line}")
    assert "no weekly claim" in out["press_headline_metric_basis"].lower()


def test_every_headline_branch_declares_a_basis():
    for series in (_fixed_series(), {"degraded": True}, None):
        build, _ = _load_headline_builder(series=series)
        for over in ({}, {"ai_agent_requests_external": None}):
            out = _payload(**over)
            build(out)
            assert out.get("press_headline_metric"), f"no headline for {over}"
            assert out.get("press_headline_metric_basis"), (
                f"headline published with NO basis for {over} — the exact gap "
                f"that made real_external_7d unreadable")


def test_headline_does_not_read_any_weekly_figure_off_the_payload():
    """Ordering fence, replaced by a stronger one.

    The old fence pinned _build_press_headline() to run AFTER
    out['real_external_calls_7d'] was assigned, because the sentence read its
    weekly figure off `out`. It no longer does — so that fence would now pass
    while proving nothing. What must hold instead: the builder reads NO weekly
    figure from `out` at all. Only the lifetime counters and the platform
    names may come from the payload; every weekly number comes from the fixed
    series. A builder that touches none of these keys cannot be re-bound to a
    rolling window by accident.
    """
    fn = _func(_parse(_FUNNEL), "_build_press_headline")
    read_keys = {
        n.args[0].value
        for n in ast.walk(fn)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "out" and n.args
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str))
    }
    assert read_keys, "no out.get(...) calls found — fence is blind"
    allowed = {"ai_agent_requests_external", "ai_agent_requests_total",
               "ai_agent_top_platforms_external"}
    weekly = read_keys - allowed
    assert not weekly, (
        f"the headline reads weekly figures off the payload again: "
        f"{sorted(weekly)}. Every one of those recomputes per request; the "
        f"sentence is quoted verbatim and must not move between two reads.")


# ── 2b · a FIXED baseline is not a COMPARABLE one (2026-08-19) ───────────────
# The 08-05 work fixed the population, then the window. Both assume "one real
# external agent call" means the same thing in both weeks of the division. On
# 2026-08-18 06:31Z it stopped meaning the same thing: dchub-mcp-server#202 put
# the CI self-tag on a per-request header and DC Hub's own GitHub Actions
# suites left is_real_external — 80.4% of real calls and 72.1% of real agents
# in the 7d before it (31 CI-shaped bursts / 1,710 calls in the 193h before the
# deploy, ZERO in the 23h after).
#
# So the sentence was one week away from publishing, verbatim and to press,
# a ~-80% WoW describing a measurement correction as a collapse in demand.
# The LEVEL stays true and stays published; the DELTA is withheld.

def _series_with_comparability(crosses):
    s = _fixed_series()
    s["wow"]["comparability"] = {
        "crosses_definition_change": crosses,
        "changes": ([{"effective_at": "2026-08-18T06:31:00+00:00",
                      "ref": "dchub-mcp-server#202"}] if crosses else []),
    }
    return s


def test_a_delta_across_a_definition_change_is_not_quoted():
    build, _ = _load_headline_builder(_series_with_comparability(True))
    out = {"ai_agent_requests_external": _LIFETIME}
    build(out)
    line = out["press_headline_metric"]
    assert f"{_WEEK_CALLS:,}" in line, "the LEVEL must still publish"
    assert f"{_WEEK_WOW:+.1f}% WoW" not in line, (
        "the sentence quoted a WoW across a population change — this is the "
        "string that goes to press verbatim")
    assert "counting definition changed" in line, (
        "withheld for the WRONG stated reason: the baseline IS fixed, it is "
        "the population that moved, and a reader must be able to tell those "
        "apart")


def test_a_comparable_delta_is_still_quoted():
    """★ THE FALSE BRANCH. A gate that withholds every delta is not a gate."""
    build, _ = _load_headline_builder(_series_with_comparability(False))
    out = {"ai_agent_requests_external": _LIFETIME}
    build(out)
    line = out["press_headline_metric"]
    assert f"{_WEEK_WOW:+.1f}% WoW" in line
    assert "counting definition changed" not in line


def test_a_payload_predating_the_key_still_quotes_its_delta():
    """Backward compatibility: absent != uncomparable.

    weekly-series is memoised and can be served from a cache written before
    this key existed. Treating a missing key as "unsafe" would silently drop
    the WoW from the press sentence for the life of that cache entry.
    """
    s = _fixed_series()
    assert "comparability" not in s["wow"]
    build, _ = _load_headline_builder(s)
    out = {"ai_agent_requests_external": _LIFETIME}
    build(out)
    assert f"{_WEEK_WOW:+.1f}% WoW" in out["press_headline_metric"]


# ── 3 · a check whose threshold agrees with its own name ─────────────────────

def _check_call(tree, check_id, func_name):
    """The _check(...) call whose first positional arg == check_id."""
    fn = _func(tree, func_name)
    found = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_check" and n.args
        and isinstance(n.args[0], ast.Constant) and n.args[0].value == check_id
        and len(n.args) >= 3
    ]
    assert found, f"no _check('{check_id}', ...) with a verdict arg in {func_name}()"
    return found


def test_platform_share_gates_on_the_shared_concentration_constant():
    """The verdict must not contradict the check's own name.

    Pre-fix: pass=True at 78.5% under the name "no single platform carries
    reach", detail reading "A WoW built on one platform's burst is
    concentration, not growth". A 90.0 bar does not test "carries reach", it
    tests "is the only platform left" — so the check could effectively only
    ever PASS. The sibling caller_share correctly failed at 38.6% against 25%,
    which settles which of the three was wrong: the threshold.
    """
    tree = _parse(_RETENTION)
    calls = _check_call(tree, "platform_share", "_lane_concentration")
    verdicts = [c.args[2] for c in calls]
    compares = [v for v in verdicts if isinstance(v, ast.Compare)]
    assert compares, "platform_share publishes no comparison verdict at all"
    for cmp_node in compares:
        names = {n.id for n in ast.walk(cmp_node) if isinstance(n, ast.Name)}
        assert "_CONCENTRATION_PCT" in names, (
            "platform_share gates on a literal, not the shared constant its "
            "sibling caller_share uses — the two can drift apart again")
        literals = [n.value for n in ast.walk(cmp_node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))]
        assert 90.0 not in literals, "the 90.0 monopoly bar is back"


def test_both_concentration_checks_share_one_bar():
    tree = _parse(_RETENTION)
    for check_id in ("caller_share", "platform_share"):
        for call in _check_call(tree, check_id, "_lane_concentration"):
            if not isinstance(call.args[2], ast.Compare):
                continue
            names = {n.id for n in ast.walk(call.args[2]) if isinstance(n, ast.Name)}
            assert "_CONCENTRATION_PCT" in names, (
                f"{check_id} no longer reads the shared bar")


def test_platform_share_states_its_bar_in_the_detail():
    """A number and a verdict a reader cannot reconcile is the defect class.

    Only the MEASURED branch is checked — the UNMEASURED sibling passes
    verdict=None and has no percentage to justify.
    """
    tree = _parse(_RETENTION)
    measured = [c for c in _check_call(tree, "platform_share", "_lane_concentration")
                if isinstance(c.args[2], ast.Compare)]
    assert measured, "platform_share has no measured branch to check"
    for call in measured:
        names = {n.id for n in ast.walk(call.args[3]) if isinstance(n, ast.Name)}
        assert "_CONCENTRATION_PCT" in names, (
            "platform_share prints a percentage but never says what bar it was "
            "judged against, exactly as caller_share does")


# ── 4 · one quantity, one query ──────────────────────────────────────────────

def test_expansion_lane_reads_the_canonical_episode_measure():
    """The admin gate and the public report must run the SAME query.

    Pre-fix the lane ran a fourth variant — agent-day grain over
    mcp_calls_identity, unconditional — and published 6.2% of 65 agent-days
    while /api/v1/reports/agent-success published planner_adoption_pct 0.52%
    (2/381) for the same week. Same idea, three denominators.
    """
    fn = _func(_parse(_EXPANSION), "_lane_planner_adoption")
    imported = {
        alias.name
        for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
        for alias in n.names
        if (n.module or "").endswith("agent_success_report")
    }
    assert "_episode_measure" in imported, (
        "lane does not import the canonical episode measure the public report "
        "uses — it is computing its own planner adoption again")
    assert "CRAWLER_EXCLUSION_WHERE" in imported, (
        "same function, different population: the report passes the crawler "
        "exclusions and the lane must pass them too")
    called = {
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_episode_measure" in called, "imported but never called"


def test_expansion_lane_has_no_private_first_call_query():
    """The inline fourth query must be gone, not merely unused."""
    fn = _func(_parse(_EXPANSION), "_lane_planner_adoption")
    sql = " ".join(
        n.value.lower() for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert "distinct on (agent_id" not in sql, (
        "the private agent-day first-call query is back; it produces a "
        "denominator no published metric uses")


def test_expansion_lane_labels_the_denominator_it_publishes():
    fn = _func(_parse(_EXPANSION), "_lane_planner_adoption")
    # ast.walk already descends into f-strings, so the literal segments of a
    # JoinedStr arrive here as plain Constants — one pass covers both.
    blob = " ".join(
        n.value.lower() for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert blob.strip(), "no string literals found in the lane — fence is blind"
    assert "opportunity episodes" in blob, (
        "the lane prints a percentage without naming its denominator")
    assert "planner_penetration_by_cohort_pct" in blob, (
        "the lane must point at the identity-grain metric it is NOT, so a "
        "reader comparing the two boards knows which question each answers")


# ── MUST-FAIL CONTROLS ───────────────────────────────────────────────────────
# Each feeds a PRE-FIX shape to the same checker above and asserts rejection.
# If a fence degrades to a no-op, these fail instead of the suite going green.

def test_control_out_key_scan_would_have_caught_the_old_name():
    tree = ast.parse(
        "def f():\n"
        "    out = {}\n"
        "    out['real_external_7d'] = 1566\n"
        "    out['real_external_calls_7d'] = 6868\n"
    )
    keys = _out_keys(tree)
    assert _TRAP_NAME in keys, "the out[] key scanner cannot see the trap name"


def test_control_out_key_scan_is_not_fooled_by_a_comment():
    """A grep-based fence passes on prose. This one must not."""
    tree = ast.parse(
        "# real_external_7d was removed on 2026-08-05\n"
        "def f():\n"
        "    out = {}\n"
        "    s = 'real_external_7d appears in this string too'\n"
        "    out['real_external_signals_7d'] = 1566\n"
    )
    assert _TRAP_NAME not in _out_keys(tree)


def test_control_headline_checker_rejects_the_prefix_sentence():
    """The real pre-fix headline must fail the assertions above."""
    prefix = (
        f"DC Hub served {_COMPLETE_REAL:,} external AI-agent tool calls this "
        f"week ({_COMPLETE_WOW:+.1f}% WoW); {_LIFETIME:,} external requests "
        f"led by Claude and Meta AI since launch."
    )
    assert f"{_COMPLETE_REAL:,}" in prefix and f"{_CANON_CALLS:,}" not in prefix, (
        "the control sentence no longer reproduces the defect it controls for")
    with pytest.raises(AssertionError):
        assert f"{_CANON_CALLS:,}" in prefix
        assert f"{_COMPLETE_REAL:,}" not in prefix


def test_control_threshold_checker_rejects_the_prefix_literal():
    """Feed the checker the pre-fix `sh <= 90.0` shape; it must reject."""
    tree = ast.parse(
        "def _lane_concentration():\n"
        "    checks.append(_check('platform_share', 'no single platform "
        "carries reach', sh <= 90.0, f'top platform {p} = {sh:.1f}%', "
        "critical=False))\n"
    )
    calls = _check_call(tree, "platform_share", "_lane_concentration")
    cmp_node = calls[0].args[2]
    names = {n.id for n in ast.walk(cmp_node) if isinstance(n, ast.Name)}
    assert "_CONCENTRATION_PCT" not in names, (
        "the threshold checker would pass the pre-fix code — it is a no-op")
    literals = [n.value for n in ast.walk(cmp_node) if isinstance(n, ast.Constant)]
    assert 90.0 in literals


def test_control_lane_checker_rejects_the_prefix_inline_query():
    """Feed the checker the pre-fix inline query; it must reject."""
    tree = ast.parse(
        "def _lane_planner_adoption():\n"
        "    cur.execute('''WITH firsts AS (SELECT DISTINCT ON (agent_id, "
        "created_at::date) tool_name FROM mcp_calls_identity)''')\n"
    )
    fn = _func(tree, "_lane_planner_adoption")
    sql = " ".join(
        n.value.lower() for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert "distinct on (agent_id" in sql, (
        "the inline-query detector cannot see the query it exists to ban")
    imported = {
        a.name for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
        for a in n.names if (n.module or "").endswith("agent_success_report")
    }
    assert "_episode_measure" not in imported


def test_control_parse_guard_rejects_an_empty_module():
    """An empty parse satisfies every 'not in' check — that must be caught."""
    with pytest.raises(AssertionError):
        tree = ast.parse("")
        assert tree.body, "empty"
    with pytest.raises(AssertionError):
        _out_keys(ast.parse("x = 1\n"))
