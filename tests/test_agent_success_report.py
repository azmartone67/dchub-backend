"""Guards for routes/agent_success_report.py — the public weekly report.

Pure-function tests over the SQL strings, the versioned metric registry, the
attribution gate and the round-3 contract structure. No DB, no network, never
imports main (green-main convention: nothing in tests/ may exit at module
scope). The offline-build tests run _build_report() with the DB env removed —
the degraded path must produce the complete five-section shape.

The rules this surface ships under, each pinned below:
  1. nothing publishes without the crawler exclusions applied;
  2. every rate is None + UNMEASURED on an empty denominator;
  3. per-platform splits stay gated until the 07-28 attribution fix has both
     accumulated ~7 days AND verifiably dropped the generic-bucket share;
  4. round-3: five contract sections ending with LEARNING; full publish
     contract on every derived metric, fail-closed; no composite scores.
"""
from datetime import date

import pytest

asr = pytest.importorskip("routes.agent_success_report")
pb = pytest.importorskip("routes.planner_bypass")
dl = pytest.importorskip("mcp_calls_deloop")


# ── rule 1: crawler exclusions on every published number ───────────────────

def _identity_queries():
    return [("totals", asr._SQL_TOTALS), ("totals_prev", asr._SQL_TOTALS_PREV),
            ("ttfr", asr._SQL_TTFR), ("share", asr._SQL_MCP_SHARE),
            ("split", asr._SQL_PLATFORM_SPLIT),
            ("cohorts", asr._SQL_COHORT_PENETRATION)]


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
    for sql in (asr._SQL_TOTALS, asr._SQL_TOTALS_PREV):
        assert "COUNT(DISTINCT agent_id)" in sql
        assert "is_public_ip" in sql
        assert "session_id" not in sql


def test_prev_window_is_disjoint():
    """WoW compares two DISJOINT rolling windows — an overlapping prior window
    would flatten every real change toward zero."""
    assert f"created_at < NOW() - ({asr.WINDOW_DAYS} * INTERVAL '1 day')" \
        in asr._SQL_TOTALS_PREV
    assert f"created_at >= NOW() - ({2 * asr.WINDOW_DAYS} * INTERVAL '1 day')" \
        in asr._SQL_TOTALS_PREV


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


def test_second_recipe_sql_binds_and_carries_exclusions():
    """Same emulation discipline for the second-recipe query: 2 placeholders
    (days, front door), episode identity + synthetic + crawler exclusions all
    present, and PLATFORM_CASE (whose ILIKE literals are substitution-unsafe)
    must never appear in a WITH-params query."""
    sql = asr._second_recipe_sql()
    assert "mcp_call_log" in sql
    assert "distinct_intents >= 2" in sql
    assert "api_key" in sql, "episode identity lost (must be the agent-day unit)"
    assert dl.internal_tag_regex_predicate("platform") in sql
    assert "ILIKE" not in sql, "classifier fragments are unsafe next to params"
    out = sql % ("__ARG0__", "__ARG1__")
    assert "__ARG0__" in out and "__ARG1__" in out


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


def test_wow_is_none_on_empty_prior_window():
    """A delta off nothing is not +100%, it is unmeasurable."""
    assert asr._wow_pct(500, 0) is None
    assert asr._wow_pct(500, None) is None
    assert asr._wow_pct(None, 400) is None
    assert asr._wow_pct(440, 400) == 10.0
    assert asr._wow_pct(360, 400) == -10.0


def test_metric_block_never_invents_a_status():
    b = asr._metric_block("tool_calls_7d", None, "UNAVAILABLE", error="x")
    assert b["value"] is None and b["status"] == "UNAVAILABLE"


# ── round-3 rule 2: the publish contract, fail-closed ───────────────────────

def test_registry_covers_exactly_the_published_metrics():
    assert set(asr.METRICS) == {
        "tool_calls_7d", "active_agents_7d", "planner_adoption_pct",
        "manual_orchestration_pct", "median_time_to_first_result_ms",
        "second_recipe_take_up_pct", "episode_result_rate",
        "recipe_completion_rate",   # v4: first-class lifecycle (round-5)
        "tool_calls_wow_pct", "active_agents_wow_pct",
        # v5 (2026-07-31 partner round)
        "calls_per_active_agent_7d", "agent_cohorts_7d",
        "planner_penetration_by_cohort_pct",
    }


def test_every_metric_declares_the_full_publish_contract():
    """Round-3 rule, verbatim: 'no derived metric may appear unless its
    observation, assumptions, definition version, and consumer are all
    declared.' Plus the house fields (definition/unit/source/changelog)."""
    for key, m in asr.METRICS.items():
        for field in asr.PUBLISH_CONTRACT_FIELDS:
            assert m.get(field), f"{key}: contract field {field!r} missing/empty"
        v = m["definition_version"]
        assert isinstance(v, int) and v >= 1, f"{key}: bad version {v!r}"
        for i in range(1, v + 1):
            assert i in m["definition_changelog"], \
                f"{key}: version {i} has no changelog entry"
        assert isinstance(m["assumptions"], list) and m["assumptions"], \
            f"{key}: assumptions must be a non-empty list"
        assert isinstance(m["consumers"], list) and m["consumers"], \
            f"{key}: consumers must be a non-empty list"


def test_incomplete_contract_is_unpublishable():
    """The gate is enforced in code, not remembered: a registry entry missing
    a contract field must refuse to carry a value — and must say WHY in a
    sentence a human can act on without reverse-engineering the field list
    (round-2, ChatGPT: publish_block_reason)."""
    asr.METRICS["_test_bad_metric"] = {
        "definition": "x", "unit": "x", "source": "x",
        "definition_version": 1, "definition_changelog": {1: "x"},
        # observation, assumptions, consumers missing
    }
    try:
        b = asr._metric_block("_test_bad_metric", 42, "MEASURED")
        assert b["status"] == "UNPUBLISHABLE"
        assert b["value"] is None, "a value leaked through an incomplete contract"
        assert set(b["missing_contract_fields"]) == \
            {"observation", "assumptions", "consumers"}
        reason = b["publish_block_reason"]
        for f in ("observation", "assumptions", "consumers"):
            assert f in reason, f"block reason does not name {f!r}"
    finally:
        del asr.METRICS["_test_bad_metric"]


def test_metric_blocks_carry_the_contract():
    """Every rendered block inherits the full contract from the registry, so a
    metric physically cannot ship unversioned."""
    b = asr._metric_block("median_time_to_first_result_ms", 812.5, "MEASURED")
    assert b["value"] == 812.5
    for field in asr.PUBLISH_CONTRACT_FIELDS:
        assert field in b


def test_payload_envelope_is_versioned_and_changelog_is_complete():
    assert asr.REPORT_DEFINITION_VERSION >= 3
    for i in range(1, asr.REPORT_DEFINITION_VERSION + 1):
        assert i in asr.REPORT_DEFINITION_CHANGELOG
    assert "learning" in asr.REPORT_DEFINITION_CHANGELOG[2].lower()
    assert "semver" in asr.REPORT_DEFINITION_CHANGELOG[3].lower(), \
        "v3 must document the declined semver ask, not just the additions"


def test_episode_metrics_declare_their_population_difference():
    """The report's adoption numbers run on a DIFFERENT population than the
    admin endpoint (crawler exclusions added). That must be declared in the
    changelog, not discovered by a consumer diffing the two."""
    for key in ("planner_adoption_pct", "manual_orchestration_pct"):
        log = " ".join(asr.METRICS[key]["definition_changelog"].values())
        assert "exclusion" in log.lower(), \
            f"{key}: population difference undeclared"


# ── round-3 rule 1: five sections, learning LAST ────────────────────────────

def test_section_order_is_the_contract_order():
    assert asr.SECTION_ORDER == ("reach", "activation", "planner_adoption",
                                 "execution_quality", "learning")
    assert asr.SECTION_ORDER[-1] == "learning", "the report must END on learning"
    for s in asr.SECTION_ORDER:
        assert s in asr.SECTION_QUESTIONS


def test_offline_build_produces_the_full_shape(monkeypatch):
    """The degraded path (no DB) must still produce all five sections in
    order, every metric present with an explicit non-value status, the gate
    closed, and ok=False so the route never caches it. Sections are an ARRAY
    — JSON object keys carry no order, and 'learning last' must survive
    serialization."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    p = asr._build_report()
    assert p["ok"] is False
    names = [s["section"] for s in p["sections"]]
    assert names == list(asr.SECTION_ORDER)
    assert p["sections"][-1]["section"] == "learning"
    seen = set()
    for s in p["sections"]:
        for key, b in (s.get("metrics") or {}).items():
            seen.add(key)
            assert b["value"] is None
            assert b["status"] in ("UNAVAILABLE", "UNMEASURED")
    learn = p["sections"][-1]
    for key, b in learn["behaviour"]["computed"].items():
        if key in asr.METRICS:
            seen.add(key)
            assert b["status"] in ("UNAVAILABLE", "UNMEASURED")
    assert seen == set(asr.METRICS), f"metrics missing from sections: {set(asr.METRICS) - seen}"
    reach = p["sections"][0]
    assert reach["per_platform"]["gate"]["passed"] is False
    assert "platforms" not in reach["per_platform"]
    assert p["publish_contract"]["required_fields"] == list(asr.PUBLISH_CONTRACT_FIELDS)


def test_offline_learning_split_and_confidence(monkeypatch):
    """Round-2 shape: learning separates measurement_integrity from behaviour
    (a telemetry repair must never read as behaviour change), and confidence
    is DECLARED — word states with a why, never a number."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    p = asr._build_report()
    learn = p["sections"][-1]
    assert "why_split" in learn
    mi, bh = learn["measurement_integrity"], learn["behaviour"]
    assert mi["notes"] and bh["notes"], "both learning groups must carry notes"
    assert "attribution_trend" in mi, \
        "the attribution gauge is measurement quality, not agent behaviour"
    assert all(n["class"] == "measurement_integrity" for n in mi["notes"])
    assert all(n["class"] == "behaviour" for n in bh["notes"])

    conf = p["confidence"]
    assert "not scored" in conf["note"] or "declared" in conf["note"]
    for axis in ("platform_attribution", "planner_denominator",
                 "second_recipe_denominator", "definition_versions",
                 "observation_window"):
        st = conf[axis]["state"]
        assert isinstance(st, str) and not st.replace(".", "").isdigit(), \
            f"{axis}: confidence state must be a word, never a number"
        assert conf[axis]["why"].strip(), f"{axis}: a state without a why"
    assert conf["platform_attribution"]["state"] == "partial"
    assert conf["planner_denominator"]["state"] == "unavailable"

    pc = p["publish_contract"]
    assert "invariant" in pc
    assert "recomputed" in pc["invariant"] and "frozen" in pc["invariant"], \
        "the invariant must name both permitted resolutions"
    assert pc["machine_readable"] == "/api/v1/reports/agent-success/contract"
    # Round-3: Meta cited a metric with the ENVELOPE's version. The payload
    # must tell citers which version to pin, in place.
    assert "ENVELOPE" in pc["version_scopes"] and "metric" in pc["version_scopes"]


def test_contract_endpoint_derives_from_the_constant():
    """The machine-readable contract (round-2, Copilot) must be GENERATED
    from PUBLISH_CONTRACT_FIELDS — a hand-written schema is a second copy,
    and second copies drift. Also pins the declined-semver decision."""
    c = asr._contract_payload()
    assert c["required"] == ["value", "status"] + list(asr.PUBLISH_CONTRACT_FIELDS)
    for field in c["required"]:
        assert field in c["properties"], f"required field {field!r} has no schema"
    dv = c["properties"]["definition_version"]
    assert dv["type"] == "integer"
    assert "semver" in dv["description"], \
        "the version-format decision must be documented where consumers read"
    assert c["report_definition_version"] == asr.REPORT_DEFINITION_VERSION
    assert "UNPUBLISHABLE" in c["properties"]["status"]["enum"]


def test_learning_notes_are_dated_kinded_classed_and_sourced():
    """Learning is editorial state, so its discipline is provenance: every
    entry carries an ISO date, a kind, a class (round-2 split), and a source.
    Undated 'we improved things' notes are how stale claims fossilize."""
    assert asr.LEARNING_NOTES, "the learning section cannot ship empty"
    for n in asr.LEARNING_NOTES:
        date.fromisoformat(n["date"])   # raises on a malformed date
        assert n["kind"] in ("improved", "degraded", "measurement", "observed")
        assert n["class"] in ("measurement_integrity", "behaviour")
        assert n["note"].strip() and n["source"].strip()


# ── round-3 rule 3: no composite score, ever ────────────────────────────────

def test_no_composite_score_anywhere():
    import re
    for key in list(asr.METRICS) + list(asr.SECTION_ORDER):
        assert not re.search(r"(score|grade|health)", key), \
            f"{key!r} smells like a composite — round-3 explicitly refused these"
    assert "REFUSED" in asr.NO_COMPOSITE_POLICY


# ── v5: the 2026-07-31 partner-round metrics ────────────────────────────────

def test_calls_per_agent_runs_the_canonical_query_verbatim():
    """calls_per_active_agent_7d must run THE canonical external-activity
    query — IMPORTED from mcp_calls_deloop, never transcribed (the regex-twin
    / manifest drift class: a transcribed copy survives the source's next
    change by silently measuring the old definition). Numerator and
    denominator come from one SQL statement by construction."""
    assert asr._SQL_CANONICAL_ACTIVITY == \
        dl.canonical_external_activity_sql(asr.WINDOW_DAYS)
    for marker in ("mcp_calls_identity", "is_public_ip AND is_real_external",
                   "COUNT(DISTINCT agent_id)"):
        assert marker in asr._SQL_CANONICAL_ACTIVITY, f"lost {marker!r}"


def test_calls_per_agent_copy_never_encourages_volume():
    """ChatGPT's own caveat, adopted as a copy rule: calls-per-agent is
    CONTEXT, never a target. The contract must carry the north star, and the
    baseline must label itself not-a-target — a bare '59.1 baseline' next to
    a live number reads as a score to beat, which is exactly the framing this
    surface refuses."""
    m = asr.METRICS["calls_per_active_agent_7d"]
    text = " ".join([m["definition"]] + m["assumptions"]
                    + list(m["definition_changelog"].values())).lower()
    assert "minimum necessary work" in text, "the north star left the contract"
    assert "target" in text, "the not-a-target declaration left the contract"
    assert "not a target" in asr.CALLS_PER_AGENT_BASELINE["note"]
    assert asr.CALLS_PER_AGENT_BASELINE["value"] == 59.1
    assert asr.CALLS_PER_AGENT_BASELINE["measured"] == "2026-07-31"


def test_v5_envelope_carries_the_volume_refusal():
    assert asr.REPORT_DEFINITION_VERSION >= 5
    assert "minimum necessary work" in \
        asr.REPORT_DEFINITION_CHANGELOG[5].lower(), \
        "v5 must document the adopted volume caveat, not just the additions"


def test_cohort_sql_semantics():
    """Cohorts + penetration in ONE query over the canonical view: identity
    unit (agent_id, never session_id), first-call pick by earliest timestamp
    (the episode model's rn=1 discipline in DISTINCT ON form), reactivation
    gap and history lookback baked as literals from the module constants,
    and crawler exclusions ONLY via the view's is_real_external."""
    s = asr._SQL_COHORT_PENETRATION
    assert "agent_id IS NOT NULL" in s
    assert "session_id" not in s, "sessions are never an identity unit"
    assert "DISTINCT ON (agent_id)" in s
    assert "ORDER BY agent_id, created_at ASC" in s, "first-call pick lost"
    assert f"'{pb.FRONT_DOOR}'" in s, "front door no longer matched"
    assert f"{asr.REACTIVATION_GAP_DAYS} * INTERVAL '1 day'" in s
    assert f"{asr.COHORT_HISTORY_LOOKBACK_DAYS} * INTERVAL '1 day'" in s
    for name in asr.COHORT_NAMES:
        assert f"'{name}'" in s, f"cohort {name!r} missing from the CASE"
    # the partition is exhaustive: no prior history → first_week_ever;
    # gap >= threshold → reactivated; everything else → returning
    assert "last_before IS NULL" in s
    assert "ELSE 'returning'" in s
    assert "ILIKE" not in s, "classifier fragments do not belong here"


def test_cohort_rollup_partitions_and_rejects_unknown_labels():
    """Pure-function semantics: absent cohorts are measured zeros, totals are
    sums over the partition, and an unknown label RAISES (the blocks then
    degrade to an honest UNAVAILABLE) rather than silently dropping agents."""
    rows = [("first_week_ever", 5, 1), ("returning", 10, 0),
            ("reactivated", 2, 2)]
    cohorts, total, planner = asr._cohort_rollup(rows)
    assert total == 17 and planner == 3
    assert cohorts["returning"]["agents"] == 10
    assert cohorts["reactivated"]["planner_first_agents"] == 2

    cohorts, total, planner = asr._cohort_rollup([("returning", 4, 1)])
    assert total == 4 and planner == 1
    assert cohorts["first_week_ever"] == {"agents": 0,
                                          "planner_first_agents": 0}

    cohorts, total, planner = asr._cohort_rollup(None)
    assert total == 0 and planner == 0

    with pytest.raises(ValueError):
        asr._cohort_rollup([("weird_cohort", 1, 0)])


def test_cohort_split_distinguishes_identical_toplines():
    """ChatGPT's follow-up fixture (2026-07-31): two weeks with IDENTICAL
    toplines — 95 agents, 5,615 calls, calls/agent 59.1 — but OPPOSITE cohort
    mixes (a discovery week vs a consolidation week). Every topline gauge on
    this surface reads the two weeks as the same; the cohort partition must
    tell them apart, because that is the property the metric exists for:
    identical toplines can be different businesses."""
    week_a = [("first_week_ever", 40, 0), ("returning", 30, 0),
              ("reactivated", 25, 0)]
    week_b = [("first_week_ever", 8, 0), ("returning", 80, 0),
              ("reactivated", 7, 0)]
    cohorts_a, total_a, _ = asr._cohort_rollup(week_a)
    cohorts_b, total_b, _ = asr._cohort_rollup(week_b)
    # identical toplines — agents, calls, and the calls-per-agent gauge
    # (which lands exactly on the published 59.1 baseline)...
    assert total_a == total_b == 95
    calls = 5615
    assert round(calls / total_a, 1) == round(calls / total_b, 1) == 59.1 \
        == asr.CALLS_PER_AGENT_BASELINE["value"]
    # ...and the split is the ONLY reader that distinguishes them
    assert {n: c["agents"] for n, c in cohorts_a.items()} == \
        {"first_week_ever": 40, "returning": 30, "reactivated": 25}
    assert {n: c["agents"] for n, c in cohorts_b.items()} == \
        {"first_week_ever": 8, "returning": 80, "reactivated": 7}
    assert cohorts_a != cohorts_b


def test_expansion_ratio_is_deferred_on_the_record():
    """ChatGPT's follow-up also proposed an expansion ratio (returning work /
    new work) and ITSELF deferred it ('not now — once you have a few months
    of history'). The deferral must be on the record in the cohort contract —
    named as planned, gated on history — so it is neither scope-crept into
    this version nor rediscovered from scratch later."""
    log = " ".join(
        asr.METRICS["agent_cohorts_7d"]["definition_changelog"].values()).lower()
    assert "expansion ratio" in log
    assert "planned" in log and "not built" in log.replace("not built yet",
                                                           "not built")
    assert "history" in log, "the gate (accumulated history) must be named"


def test_cohort_constants_match_the_partner_round_rule():
    assert asr.REACTIVATION_GAP_DAYS == 14
    assert asr.COHORT_NAMES == ("first_week_ever", "returning", "reactivated")
    assert asr.COHORT_HISTORY_LOOKBACK_DAYS >= 90, \
        "a short lookback quietly reclassifies old agents as first_week_ever"


def test_penetration_declares_the_grain_difference():
    """Two planner metrics now coexist on this surface with different grains
    (adoption: opportunity episodes, agent-day, mcp_call_log; penetration:
    active agents, identity, mcp_calls_identity). The difference must be
    DECLARED in the penetration contract, not discovered by a partner diffing
    the two numbers."""
    m = asr.METRICS["planner_penetration_by_cohort_pct"]
    text = (m["definition"] + " " + " ".join(m["assumptions"])).lower()
    assert "planner_adoption_pct" in m["definition"]
    assert "identity" in text and "episode" in text
    assert "session_id" in m["definition"], \
        "the never-session_id discipline must be stated where citers read"


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
