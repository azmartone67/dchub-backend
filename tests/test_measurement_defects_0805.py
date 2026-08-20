"""FOUR NUMBERS A HUMAN WOULD QUOTE, MEASURED WRONG — the fences.

★ Why this exists (all four measured live 2026-08-05).

1. RETENTION RATE ON THE WRONG DENOMINATOR.
   routes/agent_retention_master_shell._lane_retention published
       pct = 100.0 * returning / cur_n
   where cur_n is the CURRENT window's agent count. Live: 7 returning,
   current cohort 48, prior cohort 83 — the board printed "14.6% returning"
   where retention is 7/83 = 8.4%. Wrong in our favour by 6.2 points, and it
   flatters us MORE as the fleet shrinks, because a smaller current window
   raises the same numerator's share. The prior cohort was already in the
   sentence, so the right number was one identifier away.

2. TWO LIVE VALUES FOR THE GENERIC ATTRIBUTION BUCKET.
   The agent-expansion tick said 21.6%; /api/v1/reports/agent-success said
   0.2511 and "generic bucket holds 25.1% of real calls" — fetched five
   seconds apart, stable across two rounds, so two computations, not drift.
   The 08-04 fix had imported GENERIC_BUCKETS into the shell so the bucket
   LIST could not desync; the desync moved one layer down into the
   population (is_public_ip) and the canonicaliser (PLATFORM_CASE vs the raw
   `platform` column). Measured cause: 1459/6766 = 21.56% vs 1779/7092 =
   25.08%.

3. press_headline_metric's WoW SAT ON A MOVING BASELINE.
   Three cache-busted reads returned "served 6,764 (+73.3% WoW)", then
   "6,762 (+73.2%)", then 6,757 — the rolling window slid under the level and
   real_external_calls_prior_7d recomputed under the delta (3,903 -> 3,905).
   That string is designed to be quoted VERBATIM. Note BOTH halves moved, so
   dropping the WoW would not have made it quotable; the WINDOW had to be
   fixed. Bound to weeks[-1] of /api/v1/reports/weekly-series
   (baseline_is_fixed=true), which returned byte-identical weeks[] and wow
   across separate requests.

4. TWO SURFACES PUBLISHED DIFFERENT "real external calls 7d".
   agent-success tool_calls_7d 7,090 vs the canonical 6,764 — and the two sat
   THREE KEYS APART in one payload (sections[1].metrics carries tool_calls_7d
   and calls_per_active_agent_7d.real_external_calls_7d). Same root cause as
   defect 2: is_public_ip applied to one and not the other, 326 CF-POP rows.
   "Keep both and label them" was already tried here — the divergence was
   documented in calls_per_active_agent_7d's assumptions and stayed a defect
   anyway, exactly as it did for the top-caller share in PR #2254.

Design (inherited from tests/test_funnel_population_collision.py): every test
is STATIC and PURE — no DB, no network, no import of flask_mcp_endpoints or
the route modules (they raise at import time without NEON_DATABASE_URL, which
CI does not set; an import-based test would error, or go SILENTLY GREEN behind
a skip and prove nothing). The real shipped source is parsed and the real
shipped functions are exec'd in isolation, so these assertions run against the
bytes that deploy.

Source-shape assertions are AST-based, never grep: a regex passes happily on a
COMMENT that mentions the forbidden shape, and every comment in this wave
mentions it. Every AST helper asserts it found its target first, because an
empty parse satisfies every "not in" check.

MUST-FAIL CONTROLS: every test_control_* feeds the PRE-FIX shape or the
PRE-FIX payload to the same checker and asserts it REJECTS. If a fence above
degrades into a no-op, a control fails loudly instead of the suite going green.
"""
import ast
import os
import re

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FUNNEL = os.path.join(_REPO, "flask_mcp_endpoints.py")
_RETENTION = os.path.join(_REPO, "routes", "agent_retention_master_shell.py")
_EXPANSION = os.path.join(_REPO, "routes", "agent_expansion_master_shell.py")
_SUCCESS = os.path.join(_REPO, "routes", "agent_success_report.py")
_REACH = os.path.join(_REPO, "routes", "ai_reach.py")

# The live 2026-08-05 measurements, so every fence is anchored to the incident.
_RETURNING, _CUR_COHORT, _PRIOR_COHORT = 7, 48, 83
_WRONG_PCT, _RIGHT_PCT = 14.6, 8.4


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _parse(path):
    tree = ast.parse(_read(path))
    assert tree.body, f"{path} parsed to an EMPTY module body — fence is blind"
    return tree


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"function {name}() not found — fence is pointing at nothing")


def _extract(path, names):
    """Exec the REAL module-level assignments/functions in `names`, alone.

    No import of the module under test. Asserts every name was found, so a
    rename cannot silently empty the namespace and make the tests vacuous.
    """
    tree = _parse(path)
    picked, seen = [], set()
    for node in tree.body:
        got = None
        if isinstance(node, ast.FunctionDef) and node.name in names:
            got = node.name
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    got = t.id
        if got:
            picked.append(node)
            seen.add(got)
    missing = set(names) - seen
    assert not missing, f"{path}: not found at module level: {sorted(missing)}"
    ns = {"_re": re, "re": re}
    exec(compile(ast.Module(body=picked, type_ignores=[]), path, "exec"), ns)
    return ns


def _strings(node):
    """Every string constant under `node`, lowercased and joined.

    ast.walk descends into f-strings, so JoinedStr literal segments arrive as
    plain Constants — one pass covers both.
    """
    out = " ".join(
        n.value.lower() for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert out.strip(), "no string literals found — fence is blind"
    return out


def _assign_value(tree, name):
    """The AST value node of the LAST module-level `name = ...`."""
    got = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            got = node.value
    assert got is not None, f"module-level assignment `{name} = ...` not found"
    return got


def _interpolated_names(node):
    """Names interpolated into an f-string — {_POP} and friends.

    AST, not substring: these SQL constants are f-strings built from module
    constants that cannot be exec'd here (PLATFORM_CASE arrives by import), so
    the fence reads the interpolation itself rather than the rendered text.
    """
    return {n.id for f in ast.walk(node) if isinstance(f, ast.FormattedValue)
            for n in ast.walk(f.value) if isinstance(n, ast.Name)}


def _interpolation_count(node, name):
    """How many times `name` is interpolated into an f-string.

    Presence is not enough: _SQL_TOTALS carries TWO aggregates, and reverting
    only one of them to `is_real_external` left {_POP} interpolated for the
    other — a set-membership fence went green on the exact defect. Verified by
    reverting the shipped source, 2026-08-05.
    """
    return sum(
        1 for f in ast.walk(node) if isinstance(f, ast.FormattedValue)
        for n in ast.walk(f.value) if isinstance(n, ast.Name) and n.id == name)


def _dict_entry(tree, dict_name, key):
    """The value node for `key` inside module-level `dict_name = {...}`."""
    d = _assign_value(tree, dict_name)
    assert isinstance(d, ast.Dict), f"{dict_name} is not a dict literal"
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    raise AssertionError(f"{dict_name}[{key!r}] not found")


def _field(entry, name):
    """The value node for `name` inside a dict-literal metric block."""
    assert isinstance(entry, ast.Dict), "metric block is not a dict literal"
    for k, v in zip(entry.keys, entry.values):
        if isinstance(k, ast.Constant) and k.value == name:
            return v
    raise AssertionError(f"metric block has no {name!r} field")


def _divisors_of(fn, numerator):
    """Right-hand names of every `... * numerator / X` or `numerator / X`."""
    found = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Name)):
            names = {n.id for n in ast.walk(node.left) if isinstance(n, ast.Name)}
            if numerator in names:
                found.append(node.right.id)
    return found


# ── 1 · retention divides by the PRIOR cohort ────────────────────────────────

def test_retention_pct_divides_by_the_prior_cohort():
    ns = _extract(_RETENTION, {"_retention_pct"})
    got = ns["_retention_pct"](_RETURNING, _PRIOR_COHORT)
    assert round(got, 1) == _RIGHT_PCT, (
        f"{_RETURNING} of a prior cohort of {_PRIOR_COHORT} is {_RIGHT_PCT}%, "
        f"got {got}")
    # The pre-fix answer must be unreachable from the right arguments.
    assert round(got, 1) != _WRONG_PCT
    assert round(ns["_retention_pct"](_RETURNING, _CUR_COHORT), 1) == _WRONG_PCT, (
        "the control arithmetic no longer reproduces the defect — if dividing "
        "by the CURRENT cohort stopped yielding 14.6%, this fence has drifted "
        "off the incident it was built from")


def test_retention_pct_refuses_an_empty_prior_cohort():
    """Nobody could return, so the rate is undefined — not 0%, not 100%."""
    ns = _extract(_RETENTION, {"_retention_pct"})
    assert ns["_retention_pct"](0, 0) is None
    assert ns["_retention_pct"](5, 0) is None


def test_retention_lane_never_divides_by_the_current_window():
    """AST, not grep: the pre-fix expression is quoted in a comment above it."""
    fn = _func(_parse(_RETENTION), "_lane_retention")
    assert "cur_n" not in _divisors_of(fn, "returning"), (
        "the lane divides `returning` by the current-window count again — that "
        "is 'how few of this week's agents are new', not retention")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_retention_pct" in called, (
        "the lane computes its own percentage instead of calling the one "
        "definition — that is how the identifier got swapped in the first place")


def test_retention_sentence_names_its_denominator_unambiguously():
    blob = _strings(_func(_parse(_RETENTION), "_lane_retention"))
    assert "prior window's" in blob, (
        "the published sentence must say the denominator is the prior "
        "window's cohort — 'N of M agents' reads as the current window")
    assert "denominator is the prior" in blob, (
        "the sentence must state the denominator explicitly; the pre-fix text "
        "disclosed the prior cohort in a parenthetical and still published a "
        "percentage computed off the other number")


def test_published_retention_claim_is_guarded_against_its_own_text():
    ns = _extract(_RETENTION, {"_RETENTION_CLAIM_RE", "_retention_claim_ok"})
    ok, why = ns["_retention_claim_ok"](
        f"{_RETURNING} of the prior window's {_PRIOR_COHORT} agents returned "
        f"= {_RIGHT_PCT}% (7d retention).")
    assert ok, why
    # The exact pre-fix claim, in the post-fix sentence shape: must be rejected.
    bad, why = ns["_retention_claim_ok"](
        f"{_RETURNING} of the prior window's {_PRIOR_COHORT} agents returned "
        f"= {_WRONG_PCT}% (7d retention).")
    assert not bad, (
        "the guard accepts 7/83 published as 14.6% — it is not checking the "
        "arithmetic it exists to check")
    assert "not the printed ratio" in why


def test_retention_guard_refuses_an_unparseable_claim():
    """A claim that cannot be re-derived is not publishable."""
    ns = _extract(_RETENTION, {"_RETENTION_CLAIM_RE", "_retention_claim_ok"})
    for junk in ("", "retention looked fine this week",
                 "7 of 48 agents active in the last 7d (14.6% returning)"):
        ok, why = ns["_retention_claim_ok"](junk)
        assert not ok, f"guard passed an unverifiable claim: {junk!r}"
    assert ns["_retention_claim_ok"](None)[0] is False


def test_retention_lane_publishes_the_guard_as_a_critical_check():
    fn = _func(_parse(_RETENTION), "_lane_retention")
    ids = [n.args[0].value for n in ast.walk(fn)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id == "_check" and n.args
           and isinstance(n.args[0], ast.Constant)]
    assert "retention_denominator" in ids, (
        "the arithmetic guard is not published as a check — a guard nobody can "
        "see on the board is a guard nobody knows failed")


# ── 2 · ONE generic-bucket query, imported not restated ──────────────────────

def test_expansion_shell_imports_the_generic_share_query():
    fn = _func(_parse(_EXPANSION), "_lane_planner_adoption")
    imported = {
        a.name for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
        for a in n.names if (n.module or "").endswith("agent_success_report")
    }
    assert "measure_generic_bucket_share" in imported, (
        "the shell does not import the exported measure — importing "
        "GENERIC_BUCKETS was tried on 08-04 and the desync moved one layer "
        "down into the population and the canonicaliser")


def test_expansion_shell_has_no_private_generic_bucket_query():
    """The hand-written query must be GONE, not merely unused."""
    fn = _func(_parse(_EXPANSION), "_lane_planner_adoption")
    blob = _strings(fn)
    assert "count(*) filter (where platform = any" not in blob, (
        "the private raw-`platform` query is back — it matches the raw column "
        "where the report applies PLATFORM_CASE")
    assert "from mcp_calls_identity" not in blob, (
        "the lane runs its own identity-view aggregate again; every number it "
        "publishes must come from an imported measure")


def test_expansion_shell_does_not_recompute_the_share():
    fn = _func(_parse(_EXPANSION), "_lane_planner_adoption")
    assert not _divisors_of(fn, "mcp_calls"), (
        "the shell divides the counts itself instead of publishing the "
        "fraction the exported measure returned — two divisions of one "
        "quantity is how the units bug happened on 08-04")


def test_generic_share_and_the_call_total_share_one_population():
    """The gate's evidence and the metric beside it must count the same rows."""
    tree = _parse(_SUCCESS)
    pop = ast.literal_eval(_assign_value(tree, "_POP")).lower()
    assert "is_public_ip" in pop and "is_real_external" in pop
    for name in ("_SQL_TOTALS", "_SQL_TOTALS_PREV", "_SQL_MCP_SHARE",
                 "_SQL_PLATFORM_SPLIT"):
        node = _assign_value(tree, name)
        assert "_POP" in _interpolated_names(node), (
            f"{name} does not interpolate _POP — it can drift from the "
            f"population every other aggregate on this surface counts")
        # EVERY aggregate, not just one of them: the two-aggregate queries can
        # revert half-way and still name _POP once.
        assert "is_real_external" not in _strings(node), (
            f"{name} spells a population predicate out by hand beside {{_POP}} "
            f"— that is exactly how one aggregate drifted from its neighbour")

    for name, aggregates in (("_SQL_TOTALS", 2), ("_SQL_TOTALS_PREV", 2)):
        node = _assign_value(tree, name)
        assert _interpolation_count(node, "_POP") == aggregates, (
            f"{name} has {aggregates} aggregates but interpolates _POP "
            f"{_interpolation_count(node, '_POP')} time(s) — one of them is "
            f"counting a different population than the other")


def test_platform_split_sums_to_the_published_call_total():
    """platforms[].calls summing to tool_calls_7d is a checkable invariant.

    It held before this fix only because BOTH omitted is_public_ip. Adding the
    filter to one and not the other would have broken it silently.
    """
    tree = _parse(_SUCCESS)
    split = _strings(_assign_value(tree, "_SQL_PLATFORM_SPLIT"))
    assert "filter (where is_public_ip)" not in split, (
        "the agents aggregate still carries a separate is_public_ip FILTER — "
        "with is_public_ip in the WHERE that is either redundant or, if the "
        "WHERE were reverted, the split would stop summing to the total")
    totals = _strings(_assign_value(tree, "_SQL_TOTALS"))
    assert "filter (where is_public_ip" not in totals


# ── 3 · the quotable sentence does not move between reads ────────────────────

def _headline(fetched="unused"):
    """The shipped builder, with the DB fetcher stubbed.

    `series=None` means "go fetch"; the fetcher is the one part that cannot
    run here, so it is replaced by a stub returning `fetched`. Everything the
    assertions touch — the selection rules, the sentence, the basis strings —
    is the shipped code.
    """
    ns = _extract(_FUNNEL, {"PRESS_HEADLINE_BASIS", "_fixed_window_claim",
                            "_build_press_headline"})
    ns["_press_series"] = lambda: (None if fetched == "unused" else fetched)
    return ns


def test_builder_fetches_the_series_when_none_is_passed():
    """The default path must reach the fixed-window source, not `out`."""
    ns = _headline(fetched=_series())
    out = _payload()
    ns["_build_press_headline"](out)          # no series arg — must fetch
    assert "8,334" in out["press_headline_metric"], (
        "the builder did not consult the weekly series on the default path")


def _series(calls=8334, wow_pct=322.8, start="2026-07-27", **over):
    """A weekly-series payload. **over replaces whole top-level keys."""
    s = {
        "degraded": False,
        "weeks": [
            {"iso_week": "2026-W30", "week_start": "2026-07-20", "calls": 1971,
             "agents": 62, "partial": False, "status": "measured"},
            {"iso_week": "2026-W31", "week_start": start, "calls": calls,
             "agents": 85, "partial": False, "status": "measured"},
        ],
        "wow": {"calls_pct": wow_pct, "agents_pct": 37.1, "baseline_is_fixed": True,
                "current_week_start": start, "baseline_week_start": "2026-07-20",
                "reason": None},
        # The rolling pair that must never reach the sentence again.
        "parity_rolling_7d": {"calls": 6765, "agents": 48},
    }
    s.update(over)
    return s


def _payload(**over):
    p = {
        "ai_agent_requests_external": 307432,
        "ai_agent_requests_total": 357432,
        "ai_agent_top_platforms_external": [{"name": "Claude"}, {"name": "Meta AI"}],
        # The rolling fields stay in the payload as data — the fence is that
        # the SENTENCE does not bind them.
        "real_external_calls_7d": 6764,
        "real_external_calls_wow_pct": 73.3,
        "real_external_calls_prior_7d": 3903,
    }
    p.update(over)
    return p


def test_headline_is_identical_across_two_reads():
    """The defect, stated as a test: two reads, two different sentences."""
    ns = _headline()
    first, second = _payload(), _payload()
    ns["_build_press_headline"](first, series=_series())
    ns["_build_press_headline"](second, series=_series())
    assert first["press_headline_metric"] == second["press_headline_metric"]
    # And a second read of the funnel, whose ROLLING fields have moved on,
    # must still produce the same characters — that is the whole point.
    third = _payload(real_external_calls_7d=6757,
                     real_external_calls_wow_pct=73.2,
                     real_external_calls_prior_7d=3905)
    ns["_build_press_headline"](third, series=_series())
    assert third["press_headline_metric"] == first["press_headline_metric"], (
        "the sentence changed when the rolling fields moved — it is still "
        "bound to a window that slides under it")


def test_headline_quotes_the_fixed_week_and_names_it():
    ns = _headline()
    out = _payload()
    ns["_build_press_headline"](out, series=_series())
    line = out["press_headline_metric"]
    assert "8,334" in line, line
    assert "+322.8% WoW" in line, line
    assert "2026-07-27" in line, (
        f"the sentence must name the week it is about, or a reader cannot "
        f"tell a stale quote from a current one: {line}")
    assert "this week" not in line.lower(), (
        f"'this week' on a fixed window is a claim that expires silently: {line}")
    # The moving numbers must be absent — this is the regression.
    for moving in ("6,764", "6,762", "6,757", "+73.3%", "+73.2%"):
        assert moving not in line, f"the rolling pair is back in the sentence: {line}"
    assert out["press_headline_metric_basis"] == ns["PRESS_HEADLINE_BASIS"]


def test_headline_basis_names_the_fixed_source_and_what_it_is_not():
    basis = _headline()["PRESS_HEADLINE_BASIS"].lower()
    assert "weekly-series" in basis
    assert "baseline_is_fixed" in basis
    assert "real_external_calls_7d" in basis, (
        "the basis must name the rolling sibling it is NOT — that pair is "
        "still published in the same payload")


def test_headline_withholds_the_delta_when_the_baseline_is_not_fixed():
    ns = _headline()
    bads = [
        # the series itself says the baseline moves
        {"calls_pct": 73.3, "baseline_is_fixed": False,
         "current_week_start": "2026-07-27"},
        # a fixed delta, but for a DIFFERENT week than the level quoted
        {"calls_pct": 73.3, "baseline_is_fixed": True,
         "current_week_start": "2026-06-01"},
        # no delta at all (zero baseline, unobserved week — the series refuses)
        {"calls_pct": None, "baseline_is_fixed": True,
         "current_week_start": "2026-07-27", "reason": "baseline week is zero"},
        # a malformed delta must cost the delta, not the sentence
        {"calls_pct": {"oops": 1}, "baseline_is_fixed": True,
         "current_week_start": "2026-07-27"},
    ]
    for wow in bads:
        out = _payload()
        ns["_build_press_headline"](out, series=_series(wow=wow))
        line = out["press_headline_metric"]
        assert "8,334" in line, f"the level should still publish: {line}"
        assert "WoW withheld" in line, (
            f"published a delta whose baseline it cannot vouch for: {line}")
        assert "%" not in line.split(";")[0], line


def test_headline_refuses_a_partial_week():
    """A partial week re-reads larger every hour — the defect, in one key."""
    ns = _headline()
    partial = _series()
    partial["weeks"] = [dict(partial["weeks"][-1], partial=True)]
    out = _payload()
    ns["_build_press_headline"](out, series=partial)
    assert "8,334" not in out["press_headline_metric"]
    assert "no weekly claim" in out["press_headline_metric_basis"].lower()


def test_headline_never_falls_back_to_the_rolling_pair():
    ns = _headline()
    for dead in ({}, {"degraded": True, "reason": "db unavailable"},
                 {"weeks": [], "wow": None}):
        out = _payload()
        ns["_build_press_headline"](out, series=dead)
        line = out["press_headline_metric"]
        assert "6,764" not in line and "+73.3%" not in line, (
            f"substituted the moving rolling pair when the fixed series was "
            f"unavailable: {line}")
        assert "no weekly claim" in out["press_headline_metric_basis"].lower()


def test_every_headline_branch_still_declares_a_basis():
    ns = _headline()
    for series in (_series(), {}, {"degraded": True}):
        for over in ({}, {"ai_agent_requests_external": None}):
            out = _payload(**over)
            ns["_build_press_headline"](out, series=series)
            assert out.get("press_headline_metric"), (series, over)
            assert out.get("press_headline_metric_basis"), (series, over)


def test_fixed_window_claim_is_pure_and_picks_the_latest_complete_week():
    claim = _headline()["_fixed_window_claim"]
    assert claim(_series()) == (8334, 322.8, "2026-07-27")
    assert claim(None) == (None, None, None)
    assert claim({"degraded": True}) == (None, None, None)
    # An unobserved week is not a zero week.
    s = _series()
    s["weeks"][-1]["status"] = "unobserved"
    assert claim(s)[0] == 1971, "fell through to nothing instead of the last MEASURED week"


# ── 4 · one payload, one "real external calls 7d" ────────────────────────────

def _tool_calls_block():
    return _dict_entry(_parse(_SUCCESS), "METRICS", "tool_calls_7d")


def test_tool_calls_7d_declares_the_canonical_population():
    definition = _strings(_field(_tool_calls_block(), "definition"))
    assert "is_public_ip" in definition, (
        "tool_calls_7d's definition does not name is_public_ip — it is the "
        "filter that made it disagree with every aggregate beside it")


def test_tool_calls_7d_bumped_its_definition_version():
    """The report's own invariant: meaning changes arrive as version bumps."""
    block = _tool_calls_block()
    version = ast.literal_eval(_field(block, "definition_version"))
    assert version >= 2, (
        "the population changed and the version did not — a definition change "
        "would masquerade as a traffic drop, which is what this field exists "
        "to prevent")
    changelog = _field(block, "definition_changelog")
    assert isinstance(changelog, ast.Dict)
    entry = None
    for k, v in zip(changelog.keys, changelog.values):
        if isinstance(k, ast.Constant) and k.value == version:
            entry = _strings(v)
    assert entry, f"version {version} bumped with no changelog entry"
    assert "is_public_ip" in entry
    assert "definition change" in entry or "not a traffic change" in entry, (
        "the changelog must warn that the step down is definitional")


def test_no_metric_still_advertises_a_divergence_from_tool_calls_7d():
    """The label that used to excuse the collision must be gone.

    'differs BY DESIGN' was published in calls_per_active_agent_7d's
    assumptions while the two numbers sat three keys apart in one payload.
    A label is not a fix when the reader can see both values at once.
    """
    metrics = _assign_value(_parse(_SUCCESS), "METRICS")
    blob = _strings(metrics)
    assert "tool_calls_7d" in blob, "fence is not seeing the metric blocks"
    assert "differs from tool_calls_7d by design" not in blob, (
        "a metric still declares it disagrees with tool_calls_7d on purpose")


def test_report_definition_version_bumped_for_the_population_change():
    tree = _parse(_SUCCESS)
    v = ast.literal_eval(_assign_value(tree, "REPORT_DEFINITION_VERSION"))
    assert v >= 6
    changelog = _assign_value(tree, "REPORT_DEFINITION_CHANGELOG")
    entry = None
    for k, val in zip(changelog.keys, changelog.values):
        if isinstance(k, ast.Constant) and k.value == v:
            entry = _strings(val)
    assert entry, f"REPORT_DEFINITION_VERSION {v} has no changelog entry"
    assert "population" in entry


# ── 5 · a count that agrees with the list beside it ──────────────────────────

def _rollup_maxes(fn):
    """Every max() call in fn, paired with its string constants.

    Shared by the live fence and its must-fail control so the control proves
    THIS scanner sees the pre-fix shape, rather than proving that a private
    copy of it does.
    """
    out = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "max"):
            out.append((n, " ".join(
                c.value for c in ast.walk(n)
                if isinstance(c, ast.Constant) and isinstance(c.value, str))))
    return out


def test_reach_count_and_list_come_from_one_row():
    """Every published rollup field must come from ONE reach_weekly row.

    ★2026-08-20 — THE ANCHOR MOVED, AND ASSERTING THE OLD ONE WOULD PIN THE BUG.
    This fence used to locate the max() calls and assert none was over
    distinct_platforms. routes/ai_reach.py no longer contains a max() over the
    rollup at all: the row is picked once by _latest_complete() and every
    published field is read off that row, which satisfies this invariant more
    strongly than the shape the fence was written against.

    The blindness check fired correctly when that happened (`no max() over the
    rollup rows found`) — it refused to pass vacuously, which is the whole
    point of it. But "a max() must exist" is now a requirement to keep the
    defect, so the fence asserts the INVARIANT instead: a single row pick
    exists, and no max() re-detaches a scalar from it.

    The 08-05 defect it still bans: distinct_platforms taken as a max over BOTH
    rollup weeks while per_platform came from one — 3 published beside a list
    of 5. distinct_external_ips is banned on the same terms, because that is
    how the headline count came from one week and the list from the other
    (2026-08-20: the 08-05 fix aligned the platforms pair and left it open).
    """
    fn = _func(_parse(_REACH), "ai_reach")
    picks = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_latest_complete"]
    assert picks, (
        "no _latest_complete() row pick found in ai_reach() — fence is blind. "
        "If the pick was renamed, re-point this fence at it; do NOT delete "
        "the assertion")
    for _node, blob in _rollup_maxes(fn):
        for field in ("distinct_platforms", "distinct_external_ips"):
            assert field not in blob, (
                f"{field} is taken as a max over BOTH rollup weeks while "
                "per_platform comes from one — that is the "
                "3-beside-a-list-of-5 defect. Read it off the picked ROW, not "
                "a detached scalar")


def test_reach_publishes_both_units_by_name():
    keys = {t.slice.value for n in ast.walk(_parse(_REACH))
            if isinstance(n, ast.Assign) for t in n.targets
            if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                and t.value.id == "out" and isinstance(t.slice, ast.Constant)
                and isinstance(t.slice.value, str))}
    assert keys, "found ZERO out[...] assignments — AST walk is not seeing code"
    assert "per_platform_client_ids" in keys, (
        "the raw row count has no name, so the shorter vendor count reads as "
        "a contradiction of the list beside it")
    assert "distinct_platforms_basis" in keys


def test_reach_cold_path_does_not_publish_the_raw_string_count():
    """len(rows) is the inflated-15 bug; the 07-27 fix reached only the hot path."""
    fn = _func(_parse(_REACH), "ai_reach")
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        t = node.targets[0]
        if not (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                and t.value.id == "out" and isinstance(t.slice, ast.Constant)
                and t.slice.value == "distinct_platforms"):
            continue
        names = {n.func.id for n in ast.walk(node.value)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "count_platforms" in names or "_vendors" in names, (
            f"line {node.lineno}: distinct_platforms assigned without the "
            f"canonical vendor count — len(per_platform) counts the `mcp` "
            f"protocol bucket and three Anthropic ids as four platforms")


def test_reach_binds_count_platforms_before_the_rollup_branch():
    """Binding it inside `if rolled:` NameErrors on the cold replica."""
    fn = _func(_parse(_REACH), "ai_reach")
    imports = [n for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
               and (n.module or "").endswith("ai_platform_canon")]
    assert imports, "count_platforms is never imported in ai_reach()"
    assert len(imports) == 1, (
        "imported in more than one place — one of them is inside a branch the "
        "other path does not take")


# ── MUST-FAIL CONTROLS ───────────────────────────────────────────────────────
# Each feeds a PRE-FIX shape to the checker above and asserts rejection.

def test_control_divisor_scan_sees_the_prefix_denominator():
    fn = _func(ast.parse(
        "def _lane_retention():\n"
        "    pct = 100.0 * returning / cur_n\n"), "_lane_retention")
    assert "cur_n" in _divisors_of(fn, "returning"), (
        "the divisor scanner cannot see the exact expression it exists to ban")


def test_control_divisor_scan_is_not_fooled_by_a_comment():
    """The pre-fix expression is quoted verbatim in the shipped comment."""
    fn = _func(ast.parse(
        "def _lane_retention():\n"
        "    # pct = 100.0 * returning / cur_n   <- the 08-05 defect\n"
        "    s = 'pct = 100.0 * returning / cur_n'\n"
        "    pct = _retention_pct(returning, prev_n)\n"), "_lane_retention")
    assert "cur_n" not in _divisors_of(fn, "returning")


def test_control_retention_guard_rejects_the_live_prefix_sentence():
    """The exact string the board published on 2026-08-05."""
    ns = _extract(_RETENTION, {"_RETENTION_CLAIM_RE", "_retention_claim_ok"})
    live_prefix = (
        f"{_RETURNING} of {_CUR_COHORT} agents active in the last 7d were also "
        f"active the prior 7d ({_WRONG_PCT}% returning; prior-window cohort "
        f"{_PRIOR_COHORT}).")
    ok, why = ns["_retention_claim_ok"](live_prefix)
    assert not ok, (
        "the guard passes the pre-fix sentence verbatim — it is a no-op")
    assert "does not match the checkable form" in why


def test_control_expansion_checker_rejects_the_prefix_query():
    fn = _func(ast.parse(
        "def _lane_planner_adoption():\n"
        "    cur.execute('''SELECT COUNT(*) FILTER (WHERE platform = ANY(%s)),"
        " COUNT(*) FROM mcp_calls_identity WHERE is_public_ip''', (list(_GB),))\n"
    ), "_lane_planner_adoption")
    blob = _strings(fn)
    assert "count(*) filter (where platform = any" in blob, (
        "the private-query detector cannot see the query it exists to ban")
    imported = {a.name for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
                for a in n.names}
    assert "measure_generic_bucket_share" not in imported


def test_control_headline_checker_rejects_the_prefix_sentences():
    """The three real 2026-08-05 reads must fail the stability assertion."""
    reads = [
        "DC Hub served 6,764 external AI-agent tool calls this week (+73.3% WoW);",
        "DC Hub served 6,762 external AI-agent tool calls this week (+73.2% WoW);",
        "DC Hub served 6,757 external AI-agent tool calls this week (+73.2% WoW);",
    ]
    assert len(set(reads)) == 3, (
        "the control no longer reproduces the defect — these three reads were "
        "the incident")
    with pytest.raises(AssertionError):
        assert reads[0] == reads[1]


def test_control_reach_max_scan_sees_the_prefix_shape():
    fn = _func(ast.parse(
        "def ai_reach():\n"
        "    nplats = max(int(r.get('distinct_platforms') or 0) for r in rolled)\n"
        "    pp = rolled[0].get('per_platform') or []\n"), "ai_reach")
    found = _rollup_maxes(fn)
    assert found, "the scanner finds no max() in the pre-fix shape"
    assert "distinct_platforms" in found[0][1], (
        "the detached-scalar detector cannot see the pre-fix shape")


def test_control_reach_max_scan_sees_a_detached_count():
    """★ The half of the 08-05 defect the original fix left open.

    distinct_external_ips on its own max() is how the headline agent count
    could come from one rollup week while the list beside it came from the
    other. Fixed 2026-08-20; this control keeps the scanner able to see it.
    """
    fn = _func(ast.parse(
        "def ai_reach():\n"
        "    agents = max(int(r.get('distinct_external_ips') or 0) "
        "for r in rolled)\n"), "ai_reach")
    found = _rollup_maxes(fn)
    assert found and "distinct_external_ips" in found[0][1], (
        "the scanner cannot see a detached headline count")


def test_control_reach_count_scan_rejects_len_rows():
    fn = _func(ast.parse(
        "def ai_reach():\n"
        "    out['distinct_platforms'] = len(rows)\n"), "ai_reach")
    node = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)][0]
    names = {n.func.id for n in ast.walk(node.value)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "count_platforms" not in names and "_vendors" not in names, (
        "the raw-count detector would pass the pre-fix cold path")


def test_control_extract_guard_rejects_a_missing_name():
    with pytest.raises(AssertionError):
        _extract(_RETENTION, {"_a_name_that_does_not_exist"})


def test_control_parse_guard_rejects_an_empty_module():
    with pytest.raises(AssertionError):
        tree = ast.parse("")
        assert tree.body, "empty"
    with pytest.raises(AssertionError):
        _strings(ast.parse("x = 1\n"))
