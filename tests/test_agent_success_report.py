"""Guards for routes/agent_success_report.py — the public weekly report.

Pure-function tests over the SQL strings, the versioned metric registry and
the attribution gate. No DB, no network, never imports main (green-main
convention: nothing in tests/ may exit at module scope).

The three hard rules this surface ships under, each pinned below:
  1. nothing publishes without the crawler exclusions applied;
  2. every rate is None + UNMEASURED on an empty denominator;
  3. per-platform splits stay gated until the 07-28 attribution fix has both
     accumulated ~7 days AND verifiably dropped the generic-'mcp' share.
"""
from datetime import date

import pytest

asr = pytest.importorskip("routes.agent_success_report")
pb = pytest.importorskip("routes.planner_bypass")
dl = pytest.importorskip("mcp_calls_deloop")


# ── rule 1: crawler exclusions on every published number ───────────────────

def _identity_queries():
    return [("totals", asr._SQL_TOTALS), ("ttfr", asr._SQL_TTFR),
            ("share", asr._SQL_MCP_SHARE), ("split", asr._SQL_PLATFORM_SPLIT)]


def test_identity_queries_read_the_excluded_view_only():
    """The report reads mcp_calls_identity (where is_real_external carries the
    applied registry-crawler families), never the raw table."""
    for label, sql in _identity_queries():
        assert "mcp_calls_identity" in sql, f"{label}: not on the identity view"
        assert "is_real_external" in sql, f"{label}: crawler exclusion missing"
        assert "mcp_tool_calls" not in sql, f"{label}: raw table leaked in"


def test_agent_grain_is_distinct_agent_id_public_only():
    """Agents = COUNT(DISTINCT agent_id) on public IPs — the /api/v1/reach
    grain. Never session_id (rotates per connection, ~1.2 calls each)."""
    assert "COUNT(DISTINCT agent_id)" in asr._SQL_TOTALS
    assert "is_public_ip" in asr._SQL_TOTALS
    assert "session_id" not in asr._SQL_TOTALS


def test_episode_exclusions_derive_from_the_deloop_module():
    """The episode queries must carry the SAME crawler verdict as the identity
    view — DERIVED from mcp_calls_deloop, never a third hand-kept list (the
    drift class this repo has now hit three times)."""
    w = asr.CRAWLER_EXCLUSION_WHERE
    assert dl.internal_tag_regex_predicate("platform") in w
    assert dl.real_ua_predicate("user_agent") in w
    for fam in dl.REGISTRY_CRAWLER_FAMILIES.split("|"):
        assert fam in w, f"registry family '{fam}' missing from episode exclusions"


def test_crawler_exclusion_where_is_bound_params_safe():
    """The episode queries run WITH bound params, so the exclusion fragment
    must be the regex form — a single literal % here eats a real argument and
    500s the endpoint (the 2026-07-17 /api/v1/map outage class)."""
    assert "%" not in asr.CRAWLER_EXCLUSION_WHERE


def test_episode_sql_binds_with_exclusions_applied():
    """Emulate psycopg2 substitution over the EXACT SQL the report executes
    (planner-bypass tests cover the default path; this covers extra_where).
    The raise, if any, IS the assertion — it is what production would do."""
    main_sql, ft_sql = pb._episode_sql(asr.CRAWLER_EXCLUSION_WHERE)
    for label, sql, n in (("main", main_sql, 6), ("first_tools", ft_sql, 1)):
        assert "mcp_call_log" in sql
        args = tuple(f"__ARG{i}__" for i in range(n))
        out = sql % args
        for i in range(n):
            assert f"__ARG{i}__" in out, f"{label}: argument {i} never bound"


def test_identity_sql_runs_without_params():
    """The identity-view queries inline PLATFORM_CASE, whose ILIKE '%…%'
    literals are only safe because these queries execute with NO params (no
    substitution runs at all). Do NOT assert "'%s' not in sql" — the classifier
    legitimately contains '%smithery%' etc. (the planner-bypass tests document
    that exact false positive). Pin the real property instead: the window is a
    baked literal, and _bounded physically cannot forward params."""
    import inspect
    assert list(inspect.signature(asr._bounded).parameters) == \
        ["cur", "sql", "fetch"], \
        "_bounded grew a params argument — the ILIKE literals become unsafe"
    for label, sql in _identity_queries():
        assert f"{asr.WINDOW_DAYS} * INTERVAL '1 day'" in sql, \
            f"{label}: window is no longer a baked literal"


def test_ttfr_semantics():
    """Median time-to-first-result: agent-day grain, successful calls only,
    episodes without a result never averaged in as zero."""
    s = asr._SQL_TTFR
    assert "agent_id IS NOT NULL" in s
    assert "WHERE success" in s
    assert "PERCENTILE_CONT(0.5)" in s
    assert "FILTER (WHERE o.ok_ts IS NOT NULL)" in s


def test_share_and_split_use_the_canonical_classifier():
    """The gate share and the (gated) split classify with the same
    PLATFORM_CASE as /api/v1/reach — the baseline the 88-90% figures were
    measured with. A different classifier here would gate on a different
    number than the one we promised to watch."""
    for sql in (asr._SQL_MCP_SHARE, asr._SQL_PLATFORM_SPLIT):
        assert "client_name" in sql and "user_agent" in sql, \
            "not the canonical classifier"


def test_gate_measures_the_generic_family_not_the_old_label():
    """Caught against LIVE data 2026-07-30: the 07-28 classifier renamed the
    generic bucket 'mcp' → 'mcp-generic-client' (client_name IN
    ('mcp','mcp-client','client','default') gets its own real bucket). A gate
    matching only the old label reads 0.0% share — a false 'verified drop'
    that would have opened the per-platform split on day 7 with ~78% of calls
    still unattributed. The gate must match the FAMILY, and keep matching the
    old label in case that classifier branch is ever removed."""
    assert set(asr.GENERIC_BUCKETS) == {"mcp", "mcp-generic-client"}
    for b in asr.GENERIC_BUCKETS:
        assert f"'{b}'" in asr._SQL_MCP_SHARE, \
            f"gate SQL no longer matches generic bucket {b!r}"
    assert "IN (" in asr._SQL_MCP_SHARE, \
        "gate collapsed to an equality — the family match was lost"


# ── rule 2: UNMEASURED semantics ────────────────────────────────────────────

def test_rates_are_none_on_empty_denominator():
    """Public surface, hard rule: a rate that could not be measured must never
    render as 0% or 100%."""
    assert asr._rate(5, 0) is None
    assert asr._rate(0, 0) is None
    assert asr._rate(0, 10) == 0.0   # a REAL measured zero stays a zero
    assert asr._rate(3, 4) == 75.0


def test_metric_block_never_invents_a_status():
    b = asr._metric_block("tool_calls_7d", None, "UNAVAILABLE", error="x")
    assert b["value"] is None and b["status"] == "UNAVAILABLE"


# ── versioned metric contract (the house DEFINITION_VERSION rule) ───────────

def test_deliverable_metrics_are_all_registered():
    assert set(asr.METRICS) == {
        "tool_calls_7d", "active_agents_7d", "planner_adoption_pct",
        "manual_orchestration_pct", "median_time_to_first_result_ms",
    }


def test_every_metric_declares_version_and_complete_changelog():
    """A bump with no changelog entry is a version nobody can act on — and a
    consumer reading v1 semantics off a v2 producer is how agent_adoption's
    planner_first recommended the opposite of the fix."""
    for key, m in asr.METRICS.items():
        v = m["definition_version"]
        assert isinstance(v, int) and v >= 1, f"{key}: bad version {v!r}"
        for i in range(1, v + 1):
            assert i in m["definition_changelog"], \
                f"{key}: version {i} has no changelog entry"
        for field in ("definition", "unit", "source"):
            assert m.get(field), f"{key}: missing {field}"


def test_payload_envelope_is_versioned_too():
    assert asr.REPORT_DEFINITION_VERSION >= 1
    for i in range(1, asr.REPORT_DEFINITION_VERSION + 1):
        assert i in asr.REPORT_DEFINITION_CHANGELOG


def test_metric_blocks_carry_the_contract():
    """Every rendered block inherits version + changelog from the registry, so
    a metric physically cannot ship unversioned."""
    b = asr._metric_block("median_time_to_first_result_ms", 812.5, "MEASURED")
    assert b["definition_version"] >= 1
    assert 1 in b["definition_changelog"]
    assert b["value"] == 812.5


def test_episode_metrics_declare_their_population_difference():
    """The report's adoption numbers run on a DIFFERENT population than the
    admin endpoint (crawler exclusions added). That must be declared in the
    changelog, not discovered by a consumer diffing the two."""
    for key in ("planner_adoption_pct", "manual_orchestration_pct"):
        log = " ".join(asr.METRICS[key]["definition_changelog"].values())
        assert "exclusion" in log.lower(), \
            f"{key}: population difference undeclared"


# ── rule 3: the per-platform attribution gate ───────────────────────────────

def test_gate_closed_during_accumulation_regardless_of_share():
    """2 days after the fix even a perfect share stays gated — early data
    still measures the pre-fix writer."""
    passed, status, reason = asr._attribution_gate(2, 0.10)
    assert not passed and status == "GATED_ACCUMULATING"
    assert "2026-07-28" in reason


def test_gate_closed_when_share_unmeasured():
    passed, status, _ = asr._attribution_gate(30, None)
    assert not passed and status == "GATED_ATTRIBUTION_UNVERIFIED"


def test_gate_closed_while_share_has_not_dropped():
    """Aged window + unchanged share = the fix did not take. Publishing a
    split then would be splitting the unattributed remainder."""
    passed, status, _ = asr._attribution_gate(30, 0.85)
    assert not passed and status == "GATED_ATTRIBUTION_UNVERIFIED"


def test_gate_opens_only_with_accumulation_and_verified_drop():
    passed, status, _ = asr._attribution_gate(7, 0.40)
    assert passed and status == "MEASURED"


def test_gate_boundary_is_exact():
    assert not asr._attribution_gate(6, 0.40)[0], "opened a day early"
    assert not asr._attribution_gate(7, asr.MCP_BUCKET_MAX_SHARE_TO_PUBLISH + 0.01)[0]
    assert asr._attribution_gate(7, asr.MCP_BUCKET_MAX_SHARE_TO_PUBLISH)[0], \
        "threshold is a ≤, the documented contract"


def test_gate_constants_match_the_shipped_rule():
    assert asr.ATTRIBUTION_FIX_DATE == date(2026, 7, 28)
    assert asr.ATTRIBUTION_MIN_ACCUMULATION_DAYS == 7
    assert asr.MCP_BUCKET_MAX_SHARE_TO_PUBLISH < asr.MCP_BUCKET_SHARE_PRE_FIX, \
        "publish threshold must sit BELOW the pre-fix baseline or the gate is vacuous"


def test_window_is_weekly():
    assert asr.WINDOW_DAYS == 7


def test_route_is_public_reports_path():
    """The deliverable is a PUBLIC weekly surface (no admin key), under a path
    that says what it is."""
    src = open(asr.__file__, encoding="utf-8").read()
    assert '"/api/v1/reports/agent-success"' in src
    assert "_admin_ok" not in src, "an admin gate crept onto a public surface"
