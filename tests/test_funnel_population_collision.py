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

def _load_headline_builder():
    """Exec the REAL _build_press_headline + its constants in isolation.

    No import of flask_mcp_endpoints (it raises without NEON_DATABASE_URL), but
    also no reimplementation: the bytes under test are the bytes that deploy.
    """
    tree = _parse(_FUNNEL)
    wanted = {"PRESS_HEADLINE_CANON_FIELD", "PRESS_HEADLINE_CANON_WOW_FIELD",
              "PRESS_HEADLINE_BASIS"}
    picked = []
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id in wanted
                        for t in node.targets)):
            picked.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_build_press_headline":
            picked.append(node)
    names = {getattr(n, "name", None) for n in picked}
    assert "_build_press_headline" in names, "_build_press_headline not at module level"
    assert len(picked) == len(wanted) + 1, (
        f"expected {len(wanted)} constants + the function, extracted {len(picked)}")
    ns = {}
    exec(compile(ast.Module(body=picked, type_ignores=[]), _FUNNEL, "exec"), ns)
    return ns["_build_press_headline"], ns["PRESS_HEADLINE_BASIS"]


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


def test_headline_quotes_canon_and_not_the_larger_population():
    build, basis = _load_headline_builder()
    out = _payload()
    build(out)
    line = out["press_headline_metric"]
    assert f"{_CANON_CALLS:,}" in line, f"headline must quote canon: {line}"
    assert f"{_CANON_WOW:+.1f}% WoW" in line
    # The exact pre-fix numbers must be absent — this is the regression.
    assert f"{_COMPLETE_REAL:,}" not in line, (
        f"headline quotes the complete-days population again: {line}")
    assert f"{_COMPLETE_WOW:+.1f}%" not in line
    assert f"{_SIGNALS:,}" not in line
    assert out["press_headline_metric_basis"] == basis
    assert "real_external_calls_7d" in basis


def test_headline_refuses_to_substitute_a_population_when_canon_is_missing():
    """Falling back to the other population is the defect, not the fix."""
    build, _ = _load_headline_builder()
    out = _payload(real_external_calls_7d=None, real_external_calls_wow_pct=None)
    build(out)
    line = out["press_headline_metric"]
    assert f"{_COMPLETE_REAL:,}" not in line, (
        f"silently fell back to the complete-days figure: {line}")
    assert f"{_SIGNALS:,}" not in line
    assert "this week" not in line.lower(), (
        f"made a weekly claim with no canonical weekly number: {line}")
    assert "no weekly claim" in out["press_headline_metric_basis"].lower()


def test_every_headline_branch_declares_a_basis():
    build, _ = _load_headline_builder()
    for over in ({}, {"real_external_calls_7d": None},
                 {"real_external_calls_7d": None,
                  "ai_agent_requests_external": None}):
        out = _payload(**over)
        build(out)
        assert out.get("press_headline_metric"), f"no headline for {over}"
        assert out.get("press_headline_metric_basis"), (
            f"headline published with NO basis for {over} — the exact gap that "
            f"made real_external_7d unreadable")


def test_headline_is_built_after_the_canonical_fields_exist():
    """Ordering fence — the trap this fix nearly shipped into.

    real_external_calls_7d is assigned ~500 lines BELOW where the headline used
    to be assembled. Repointing the headline in place, without moving it, would
    have read None off `out` and silently degraded every week to the
    lifetime-only sentence: a wrong number traded for a missing one. Nothing in
    the type system catches that, so it is pinned by line order here.
    """
    tree = _parse(_FUNNEL)
    assign_lines = [
        n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign)
        for t in n.targets
        if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
            and t.value.id == "out" and isinstance(t.slice, ast.Constant)
            and t.slice.value == "real_external_calls_7d")
    ]
    call_lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_build_press_headline"
    ]
    assert assign_lines, "no out['real_external_calls_7d'] assignment found"
    assert call_lines, "_build_press_headline() is never called"
    assert min(call_lines) > max(assign_lines), (
        f"_build_press_headline() runs at line {min(call_lines)}, before "
        f"real_external_calls_7d is assigned at {max(assign_lines)} — it will "
        f"read None and drop the weekly claim every week")


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
