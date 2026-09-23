"""Every finding that writes a `count` says what the integer means (2026-09-22).

House rule (tests/test_finding_count_typing.py): tests NEVER import main.
Nothing here imports the app at all — it parses source with `ast` — and nothing
runs at module scope.

Why this exists
---------------
brain_work_selector.occurrence_signal() / row_count_is_value() and
brain_enhancer read an UNDECLARED `count` as a recurrence tally unless it
exceeds _UNTYPED_OCCURRENCE_CEILING (10,000). The ceiling only catches huge
magnitudes. On 2026-09-22 check_ai_platform_crawl_drop wrote
`"count": cur_7d` — 503 requests — which rendered as "(seen x503)" and minted
a bogus brain-spec PR (#5316). #5318 labelled that one detector. It was the
FOURTH instance of the class, after frontend_endpoint_slow, dedup_backlog_large
and cron_silently_dead, and each of those was also fixed one row at a time.

A fourth row-fix is a subscription. This file closes the SHAPE: a detector
cannot introduce a new `count` without also declaring `count_kind`.

What is enforced
----------------
1. Every finding dict ({"issue": ..., "count": ...} literal, or
   dict(issue=..., count=...)) in a module that feeds the radar's persister
   carries a `count_kind` — a non-empty string literal from _KINDS.
   "Feeds the radar" is DERIVED: the radar itself plus every routes.* module
   it imports that builds a finding dict. A new detector module wired into
   the radar is scanned without anyone editing this file.
2. Every call to brain_findings_writer.upsert_brain_finding that passes a
   `count` (keyword or 4th positional) also passes `count_kind=`. This is the
   other door into brain_findings: a detector that writes directly never
   builds a radar dict, so (1) alone cannot see it.
3. Neither shape may be bypassed by writing `count` after construction
   (`f["count"] = n`, `.update(count=n)`) or by a `**` splat on the writer.

The existing undeclared sites are PINNED, not labelled
------------------------------------------------------
A declared kind OVERRIDES the ceiling (test_self_identifying_counts_are_
annotated), so a wrong label is worse than none: "occurrence" on a magnitude
would be trusted above 10,000. Only sites whose expression names its own unit
(len(...), *_pct, *_s, *_h, *_days, an HTTP status) were labelled in the
change that introduced this file. The rest are listed below with the NUMBER
of undeclared sites per (file, issue) — not just the issue — so a second
undeclared dict under an already-listed issue still fails.

The registers only shrink. Label a site → its count drops → lower the entry
and the matching _CEILING. Both directions are tested separately, so a stale
entry is reported as stale rather than silently granting headroom.
"""
import ast
import collections
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RADAR = "routes/brain_consistency_radar.py"
WRITER_FN = "upsert_brain_finding"

# The vocabulary in use. Anything other than "occurrence" is read as a
# magnitude, so a typo ("occurence") would silently demote a real tally —
# which is why this is a closed set. Add a kind here when none fits.
_KINDS = frozenset({
    "occurrence",      # a real recurrence tally — the only kind that ranks
    "item_count",      # breadth: how many things are wrong, seen once
    "backlog_size",
    "percent",
    "latency_ms",
    "seconds",
    "seconds_since",
    "hours",
    "days",
    "day_of_month",
    "http_status",
})

# Modules the radar imports that build a finding-shaped dict but are not
# detector sources. Each must still be imported by the radar (tested), so an
# entry cannot outlive its reason.
_NOT_A_DETECTOR = {
    "routes/brain_findings_writer.py":
        "the writer itself — its row dict forwards whatever count/count_kind "
        "the caller passed",
}


# ── register 1: undeclared finding dicts, per (file, issue) ─────────────
# Keys are the issue string for a literal, else the unparsed expression.
_UNDECLARED_DICTS = {
    'routes/brain_actuator_detectors.py': {
        'approved_backlog_unpublished': 1,
        'failsoft_metric_null_streak': 1,
    },
    'routes/brain_autonomy_loop.py': {
        'issue': 2,
    },
    'routes/brain_consistency_radar.py': {
        '404_spike': 1,
        'addressable_demand_unconverted': 1,
        'ai_citations_cron_silent': 1,
        'approve_directive_sent_in_the_clear': 1,
        'approve_directive_waf_blocked': 1,
        'approved_without_pr_stale': 1,
        'auto_press_market_repetition': 1,
        'auto_trial_signal_mint_mismatch': 1,
        'auto_trial_signup_rate_low': 1,
        'autopilot_action_unverified': 1,
        'autopilot_verifier_backlog': 1,
        'backend_pool_unreachable': 1,
        'backend_pool_utilization_high': 1,
        'blueprint_registered_but_not_serving': 1,
        'brain_memory_empty': 1,
        'canonical_floor_above_live_reality': 2,
        'canonical_redirect_loop': 1,
        'cf_analytics_unavailable': 1,
        'cf_cache_rate_low': 1,
        'cf_kv_namespace_pressure': 1,
        'citation_score_dropped': 1,
        'competitor_gap_yield_stale': 1,
        'consistency_radar_detector_crashed:check_cron_freshness': 1,
        'consistency_radar_detector_crashed:check_csp_violation_reports': 1,
        'consistency_radar_detector_crashed:check_customer_activation_health': 1,
        'consistency_radar_detector_crashed:check_facility_duplicate_clusters': 1,
        'consistency_radar_detector_crashed:check_facility_geo_mismatch': 1,
        'consistency_radar_detector_crashed:check_mcp_presence_stale': 1,
        'coverage_gap_alberta': 1,
        'coverage_gap_canada': 1,
        'cron_endpoint_unscheduled': 1,
        'cron_if_check_collides_with_hourly': 1,
        'cron_if_check_mismatched_schedule': 1,
        'cron_phase_missing_schedule': 1,
        'cron_radar_parse_failed': 1,
        'cross_surface_metric_divergence': 1,
        'csp_source_of_truth_drift': 1,
        'customer_activation_systemic_failure': 1,
        'customer_nudge_failed_needs_human': 1,
        'data_freshness_sla_breach': 1,
        'dchub_media_press_silent': 2,
        'dchub_media_press_weak': 1,
        'dcpi_authed_edge_cached': 1,
        'dcpi_partial_recompute': 1,
        'dedup_pipeline_stalled': 1,
        'deploy_queue_churn': 1,
        'developers_funnel_intent_dead': 1,
        'discovery_anemic_7d': 1,
        'discovery_probe_unresolvable': 1,
        'discovery_stalled_7d': 1,
        'enterprise_bot_present': 1,
        'f"mcp_funnel_leak:{f[\'tool\']}"': 1,
        'f"pattern_proposal_candidate:{p[\'issue_prefix\']}"': 1,
        "f'brand_surface_dormant:{sid}'": 1,
        "f'consistency_radar_detector_crashed:{fn.__name__}'": 1,
        "f'consistency_radar_detector_crashed:{name}'": 1,
        "f'consistency_radar_detector_timeout:{fn.__name__}'": 1,
        "f'detector_source_missing:{detector}'": 1,
        "f'llms_txt_dead_link:{path}'": 1,
        "f'operator_profile_gap:{name[:50]}'": 1,
        "f'report_empty_section:{window}.dcpi_movers'": 1,
        "f'report_empty_section:{window}.hyperscaler_deals'": 1,
        "f'report_no_narrative:{window}'": 1,
        "f'report_unreachable:{window}'": 1,
        "f'report_zero_signal:{window}.brand_pulse'": 1,
        "f'schema_drift_column_missing:{category}.{col}'": 1,
        "f'schema_drift_table_missing:{tbl}'": 1,
        "f'surface_health_critical:{sid}'": 1,
        'facility_count_stagnant': 1,
        'facility_country_mislabeled': 1,
        'facility_dedupe_collision': 1,
        'facility_duplicates_unmarked': 1,
        'founding_customer_not_welcomed': 1,
        'frontend_endpoint_unreachable': 1,
        'gated_endpoint_missing_coaching': 1,
        'heartbeat_surfaces_stale': 1,
        'inline_script_unterminated': 1,
        'internal_link_404': 1,
        'internal_link_unreachable': 1,
        'iso_metric_count_dropped': 1,
        'iso_metric_count_zero_24h': 1,
        'issue': 1,
        'link_prober_sweep_failed': 1,
        'linkedin_engagement_readback_stale': 1,
        'mcp_conversion_rate_below_floor': 1,
        'mcp_conversion_stale_warn': 1,
        'mcp_demand_gap_unaddressed': 1,
        'mcp_description_drift:iso_count': 1,
        'mcp_funnel_concentration_top5': 1,
        'mcp_presence_drift_uncorrected': 1,
        'mcp_registry_listing_stale': 1,
        'mcp_tool_sunset_candidate': 1,
        'me_tier_edge_cached': 1,
        'media_topic_unaddressed': 1,
        'multi_cloud_both_down': 1,
        'neon_replication_lag': 2,
        'outbound_distribution_health': 1,
        'package_install_velocity_drop': 1,
        'page_brand_uniformity': 3,
        'paid_key_gated': 1,
        'paid_key_walled_on_map': 1,
        'paywall_anon_leak': 1,
        'paywall_canary_import_failed': 1,
        'pocket_high_mover': 1,
        'press_drafting_lag': 1,
        'railway_down_render_serving': 1,
        'redeem_form_submit_leak': 1,
        'render_down_railway_serving': 1,
        'render_flapping': 1,
        'render_pipeline_blocked': 1,
        'repeated_404_pattern': 1,
        'required_env_var_missing': 1,
        'rest_endpoint_leakage': 1,
        'review_gate_never_disagrees': 1,
        'review_lane_unmeasured': 2,
        'review_lane_unreachable': 1,
        'route_prober_unavailable': 1,
        'scheduler_function_orphaned': 1,
        'schema_drift_for_l7_detector': 1,
        'session_upgrade_silenced': 1,
        'slow_request_ratio': 1,
        'social_publish_silent_failure': 1,
        'source_of_truth_declining': 1,
        'spare_capacity_pending_moderation': 1,
        'stripe_webhook_table_missing': 1,
        'surface_allowlist_path_missing': 1,
        'tier_inconsistency_web_higher_than_mcp': 1,
        'tier_radar_import_failed': 1,
        'tier_vocab_unmapped_paid': 1,
        'tool_signal_conversion_leak': 1,
        'trial_taste_abuse_suspected': 1,
        'trial_to_paid_stagnation': 1,
        'unsafe_db_conn_pattern': 1,
        'upgrade_gate_traffic_active': 1,
        'upgrade_pool_grown': 1,
        'winback_pitches_unsent': 2,
        'worker_source_unreachable': 1,
        'worker_version_constant_not_found': 1,
        'worker_version_drift': 1,
        'worker_version_header_missing': 1,
        'x_publisher_dead': 1,
        'zone_worker_commit_not_pasted': 1,
        'zone_worker_deployed_ahead_of_repo': 1,
        'zone_worker_source_unreadable': 1,
        'zone_worker_version_constant_not_found': 1,
        'zone_worker_version_header_missing': 1,
        'zone_worker_version_suffix_mismatch': 1,
    },
    'routes/brain_honesty_reconciliation.py': {
        'brain_honesty_verdict_disagreement': 1,
        'brain_pattern_acting_but_never_landing': 1,
    },
    'routes/brain_mcp_health.py': {
        'mcp_health_catalog_unreachable': 1,
        'mcp_health_tool_count_drift': 1,
        'mcp_health_wellknown_unreachable': 1,
    },
    'routes/brain_security_detectors.py': {
        'admin_endpoint_open': 1,
        'land_power_endpoint_unreachable': 1,
        'paywall_hole': 1,
        'privacy_traffic_share_high': 1,
        'secret_pattern_in_response': 1,
        'suspicious_admin_scan': 1,
    },
    'routes/glama_listing_probe.py': {
        'glama_duplicate_connector_listed': 1,
        'glama_listing_tool_count_drift': 1,
        'glama_listing_unhealthy': 1,
        'glama_listing_unparseable': 1,
        'glama_origin_publishes_email_ownership': 1,
        'glama_ownership_doc_unparseable': 1,
    },
    'routes/site_sentinel.py': {
        'f"nav_indeterminate:{r[\'path\']}"': 1,
        'f"nav_missing:{r[\'path\']}"': 1,
    },
}
_UNDECLARED_DICTS_CEILING = 177

# ── register 2: undeclared writer calls, per (file, issue) ──────────────
_UNDECLARED_CALLS = {
    'routes/agentic_master_shell.py': {
        'issue[:200]': 1,
    },
    'routes/brain_autonomy_loop.py': {
        "it['issue']": 1,
    },
    'routes/brain_autopilot.py': {
        'pattern_unfixable_needs_rechannel': 1,
    },
    'routes/brain_bug_squash.py': {
        "f.get('issue', 'bug_squash:unknown')": 1,
    },
    'routes/brain_cross_session_pollinator.py': {
        'cross_session_pattern': 1,
    },
    'routes/brain_fast_qa.py': {
        'issue': 1,
    },
    'routes/brain_lane_driver.py': {
        'lane_driver_proposal': 1,
    },
    'routes/brain_layer6_predictive.py': {
        "cand['issue']": 1,
    },
    'routes/brain_micro_cycle.py': {
        "f'micro_anomaly_{severity}'": 1,
    },
    'routes/brain_orphan_decisions.py': {
        'f"orphan_decision:{r[\'sink\']}"': 1,
    },
    'routes/brain_pr_outcome_monitor.py': {
        'brain_pr_regression': 1,
    },
    'routes/brain_v3.py': {
        'selftest:writer-probe': 1,
    },
    'routes/cadence_sentinel.py': {
        "finding_issue(lane['key'])": 2,
    },
    'routes/clarity_insights.py': {
        'ux_dead_clicks': 1,
    },
    'routes/competitor_gap_crawler.py': {
        "f'coverage_gap_competitor:{slug}'": 1,
    },
    'routes/competitor_recon.py': {
        "'competitor_recon:threat:%s' % sh['slug']": 1,
        "'competitor_recon:vs_page_missing:%s' % slug": 1,
        "'competitor_recon:win_move:%s' % m['key']": 2,
        'competitor_recon:weekly_report': 1,
    },
    'routes/daily_render_fanout.py': {
        'issue': 1,
    },
    'routes/depth_master_shell.py': {
        'issue': 1,
    },
    'routes/expired_demote.py': {
        'expired_onetime_demote_run': 1,
    },
    'routes/frontend_reliability_master_shell.py': {
        'issue': 1,
    },
    'routes/grid_data_master_shell.py': {
        'issue': 1,
    },
    'routes/grid_fiber_usage_radar.py': {
        'f"monetize:grid_fiber_heavy:{h[\'key_id\']}"': 1,
        'monetize:grid_fiber_radar_summary': 1,
    },
    'routes/hosting_capacity_ingest.py': {
        'grid_depth:hosting_capacity_ingest': 1,
    },
    'routes/infra_coverage.py': {
        "f['issue']": 1,
    },
    'routes/map_layer_probe.py': {
        "f['issue']": 1,
    },
    'routes/mcp_per_tool_conversion.py': {
        'mcp_tool_high_converter': 1,
        'mcp_tool_zero_conversion': 1,
    },
    'routes/mcp_presence_crawler.py': {
        'issue': 1,
    },
    'routes/monetization_master_shell.py': {
        'issue': 1,
    },
    'routes/news_ingest_health.py': {
        'f"news_ingest_{v[\'verdict\']}"': 1,
    },
    'routes/osm_crawler.py': {
        'ingest_health:osm_insert_failed': 1,
        'ingest_health:osm_overpass_throttle': 1,
    },
    'routes/paywall_test.py': {
        'paywall_render_observation': 1,
    },
    'routes/pockets.py': {
        'pockets_weekly_digest_sent': 1,
    },
    'routes/precision_depth_master_shell.py': {
        'issue': 1,
    },
    'routes/route_auth_master_shell.py': {
        'issue': 1,
    },
    'routes/site_sentinel.py': {
        'issue': 2,
    },
    'routes/upgrade_pool_outreach.py': {
        'upgrade_pool_outreach_sent': 1,
    },
    'routes/webmcp_master_shell.py': {
        "f'webmcp_{lane_key}_broken'": 1,
    },
    'routes/white_glove_agent.py': {
        'issue': 2,
        'white_glove_lane_unmeasured': 2,
    },
    'routes/white_glove_propagation.py': {
        'white_glove_listing_copy_drift': 2,
    },
    'self_growing_index.py': {
        'issue': 1,
    },
}
_UNDECLARED_CALLS_CEILING = 53

# The size of each register when this file landed. A ceiling may be lowered;
# raising one past its high-water mark fails.
_DICTS_HIGH_WATER = 177
_CALLS_HIGH_WATER = 53

# Anti-vacuity floors (~20% under the measured value when this file landed).
# A refactor that moves detectors out from under the scan must not read as
# "every dict declared".
_MIN_DETECTOR_FILES = 7
_MIN_FINDING_DICTS = 200
_MIN_DECLARED_DICTS = 60
_MIN_WRITER_CALLS_WITH_COUNT = 40


# ── helpers ─────────────────────────────────────────────────────────────

def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _parse(rel: str) -> ast.AST:
    return ast.parse(_read(rel), filename=rel)


def _key(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ast.unparse(node)


def _finding_dicts(tree):
    """(lineno, issue_key, count_src, count_kind_node|None) per finding dict."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            km = {k.value: v for k, v in zip(n.keys, n.values)
                  if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "dict"):
            km = {k.arg: k.value for k in n.keywords if k.arg}
        else:
            continue
        if "issue" in km and "count" in km:
            yield (n.lineno, _key(km["issue"]), ast.unparse(km["count"]),
                   km.get("count_kind"))


def _radar_imports() -> set:
    out = set()
    for n in ast.walk(_parse(RADAR)):
        if isinstance(n, ast.ImportFrom) and n.module:
            if n.module.startswith("routes."):
                out.add(n.module.replace(".", "/") + ".py")
            elif n.module == "routes":
                out.update(f"routes/{a.name}.py" for a in n.names)
    return {p for p in out if os.path.isfile(os.path.join(ROOT, p))}


def _detector_files() -> list:
    files = {RADAR}
    for rel in _radar_imports():
        if rel in _NOT_A_DETECTOR:
            continue
        if any(True for _ in _finding_dicts(_parse(rel))):
            files.add(rel)
    return sorted(files)


def _undeclared_dicts() -> dict:
    """{file: Counter(issue_key)} plus the site list for failure messages."""
    live, sites = {}, collections.defaultdict(list)
    for rel in _detector_files():
        c = collections.Counter()
        for line, issue, count_src, kind in _finding_dicts(_parse(rel)):
            if kind is None:
                c[issue] += 1
                sites[(rel, issue)].append(f"{rel}:{line}  count={count_src}")
        if c:
            live[rel] = c
    return live, sites


def _python_files() -> list:
    skip = {"tests", "node_modules", "venv", ".venv", "site-packages",
            "__pycache__"}
    out = []
    for p in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
        rel = os.path.relpath(p, ROOT)
        parts = rel.split(os.sep)
        if any(x in skip or x.startswith(".") for x in parts[:-1]):
            continue
        out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _writer_names(tree) -> set:
    """Local names bound to upsert_brain_finding (handles `import ... as`)."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == WRITER_FN:
                    names.add(a.asname or a.name)
    return names


def _writer_calls(rel: str, tree):
    """(lineno, issue_key, has_count, has_kind, has_splat) per writer call."""
    local = _writer_names(tree)
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not ((isinstance(f, ast.Name) and f.id in local)
                or (isinstance(f, ast.Attribute) and f.attr == WRITER_FN)):
            continue
        kws = {k.arg: k.value for k in n.keywords}
        issue = kws.get("issue", n.args[1] if len(n.args) > 1 else None)
        yield (n.lineno,
               _key(issue) if issue is not None else "<none>",
               "count" in kws or len(n.args) >= 4,
               "count_kind" in kws,
               None in kws or any(isinstance(a, ast.Starred) for a in n.args))


def _all_writer_calls():
    for rel in _python_files():
        src = _read(rel)
        if WRITER_FN not in src or rel == "routes/brain_findings_writer.py":
            continue
        tree = ast.parse(src, filename=rel)
        for call in _writer_calls(rel, tree):
            yield (rel,) + call


def _undeclared_calls():
    live, sites = {}, collections.defaultdict(list)
    for rel, line, issue, has_count, has_kind, _ in _all_writer_calls():
        if has_count and not has_kind:
            live.setdefault(rel, collections.Counter())[issue] += 1
            sites[(rel, issue)].append(f"{rel}:{line}")
    return live, sites


def _excess(live: dict, register: dict, sites: dict) -> list:
    bad = []
    for rel, c in sorted(live.items()):
        for issue, n in sorted(c.items()):
            allowed = register.get(rel, {}).get(issue, 0)
            if n > allowed:
                bad.append(f"{issue!r}: {n} undeclared, register allows "
                           f"{allowed}\n      " + "\n      ".join(
                               sites[(rel, issue)]))
    return bad


def _stale(live: dict, register: dict) -> list:
    bad = []
    for rel, entries in sorted(register.items()):
        for issue, allowed in sorted(entries.items()):
            n = live.get(rel, {}).get(issue, 0)
            if n < allowed:
                bad.append(f"{rel} {issue!r}: register says {allowed}, "
                           f"live is {n} — lower it")
    return bad


def _size(register: dict) -> int:
    return sum(sum(v.values()) for v in register.values())


_HOW = (
    "Declare what the integer means: add \"count_kind\": \"<kind>\" to the "
    "dict (or count_kind=\"<kind>\" to the writer call), with <kind> from "
    "_KINDS in this file. Use \"occurrence\" ONLY for a genuine recurrence "
    "tally — a declared kind overrides the plausibility ceiling, so a "
    "magnitude labelled \"occurrence\" is trusted without limit. Do NOT add "
    "the site to the register: it holds only the sites that existed when "
    "this guard landed.")


# ── scope: the scan is derived and cannot collapse to nothing ──────────

def test_detector_scope_is_derived_from_the_radar():
    files = _detector_files()
    assert RADAR in files
    assert len(files) >= _MIN_DETECTOR_FILES, (
        f"only {len(files)} detector modules scanned: {files}. The radar's "
        f"imports are how new detectors are found — did they move?")


def test_not_a_detector_exclusions_are_still_imported_by_the_radar():
    imported = _radar_imports()
    stale = sorted(set(_NOT_A_DETECTOR) - imported)
    assert not stale, f"exclusions the radar no longer imports: {stale}"


def test_scan_sees_the_finding_population():
    total = declared = 0
    for rel in _detector_files():
        for _, _, _, kind in _finding_dicts(_parse(rel)):
            total += 1
            declared += kind is not None
    assert total >= _MIN_FINDING_DICTS, (total, _MIN_FINDING_DICTS)
    assert declared >= _MIN_DECLARED_DICTS, (declared, _MIN_DECLARED_DICTS)


def test_scan_sees_the_writer_population():
    n = sum(1 for c in _all_writer_calls() if c[3])
    assert n >= _MIN_WRITER_CALLS_WITH_COUNT, (n, _MIN_WRITER_CALLS_WITH_COUNT)


# ── rule 1: finding dicts ───────────────────────────────────────────────

def test_every_finding_dict_declares_count_kind():
    """★THE GUARD. A new detector dict with `count` and no `count_kind` fails
    here, naming file:line and the count expression."""
    live, sites = _undeclared_dicts()
    bad = _excess(live, _UNDECLARED_DICTS, sites)
    assert not bad, (
        "finding dict writes `count` without `count_kind`:\n  "
        + "\n  ".join(bad) + "\n\n" + _HOW)


def test_undeclared_dict_register_only_shrinks():
    live, _ = _undeclared_dicts()
    bad = _stale(live, _UNDECLARED_DICTS)
    assert not bad, (
        "these sites are now declared (or gone) — shrink the register and "
        "_UNDECLARED_DICTS_CEILING:\n  " + "\n  ".join(bad))


def test_declared_kinds_are_known_literals():
    bad = []
    for rel in _detector_files():
        for line, issue, _, kind in _finding_dicts(_parse(rel)):
            if kind is None:
                continue
            if not (isinstance(kind, ast.Constant)
                    and isinstance(kind.value, str)
                    and kind.value in _KINDS):
                bad.append(f"{rel}:{line} {issue!r} count_kind="
                           f"{ast.unparse(kind)}")
    assert not bad, ("count_kind must be a string literal from _KINDS:\n  "
                     + "\n  ".join(bad))


def test_count_is_never_written_after_construction():
    """`f = {"issue": ...}; f["count"] = n` would carry an undeclared count
    past rule 1. Measured zero sites in detector modules when this landed."""
    bad = []
    for rel in _detector_files():
        for n in ast.walk(_parse(rel)):
            targets = (n.targets if isinstance(n, ast.Assign)
                       else [n.target] if isinstance(n, ast.AugAssign)
                       else [])
            for t in targets:
                if (isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "count"):
                    bad.append(f"{rel}:{n.lineno} {ast.unparse(t)} = ...")
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("update", "setdefault")):
                if (any(k.arg == "count" for k in n.keywords)
                        or any(isinstance(a, ast.Constant) and a.value == "count"
                               for a in n.args)
                        or any(isinstance(a, ast.Dict) and any(
                            isinstance(k, ast.Constant) and k.value == "count"
                            for k in a.keys) for a in n.args)):
                    bad.append(f"{rel}:{n.lineno} {ast.unparse(n)[:80]}")
    assert not bad, ("put `count` (and its `count_kind`) in the finding "
                     "literal instead:\n  " + "\n  ".join(bad))


# ── rule 2: direct writer calls ─────────────────────────────────────────

def test_every_writer_call_with_a_count_declares_count_kind():
    live, sites = _undeclared_calls()
    bad = []
    for rel, c in sorted(live.items()):
        for issue, n in sorted(c.items()):
            allowed = _UNDECLARED_CALLS.get(rel, {}).get(issue, 0)
            if n > allowed:
                bad.append(f"{issue!r}: {n} undeclared, register allows "
                           f"{allowed}  " + ", ".join(sites[(rel, issue)]))
    assert not bad, (
        f"{WRITER_FN}() passed `count` without `count_kind`:\n  "
        + "\n  ".join(bad) + "\n\nIf the call forwards a finding dict, pass "
        "count_kind=f.get(\"count_kind\") or \"\" alongside count. " + _HOW)


def test_undeclared_call_register_only_shrinks():
    live, _ = _undeclared_calls()
    bad = _stale(live, _UNDECLARED_CALLS)
    assert not bad, (
        "these writer calls now declare (or are gone) — shrink the register "
        "and _UNDECLARED_CALLS_CEILING:\n  " + "\n  ".join(bad))


def test_writer_calls_are_not_splatted():
    """`upsert_brain_finding(cur, **f)` hides both keys from rule 2."""
    bad = [f"{rel}:{line}" for rel, line, _, _, _, splat
           in _all_writer_calls() if splat]
    assert not bad, f"spell out count/count_kind instead of splatting: {bad}"


# ── the registers only shrink ───────────────────────────────────────────

def test_register_sizes_match_their_ceilings():
    """Equality, not <=: slack left by a shrink would let a later edit add an
    entry without touching any number a reviewer would notice."""
    assert _size(_UNDECLARED_DICTS) == _UNDECLARED_DICTS_CEILING, (
        f"_UNDECLARED_DICTS holds {_size(_UNDECLARED_DICTS)} sites; set "
        f"_UNDECLARED_DICTS_CEILING to that (it may only go down)")
    assert _size(_UNDECLARED_CALLS) == _UNDECLARED_CALLS_CEILING, (
        f"_UNDECLARED_CALLS holds {_size(_UNDECLARED_CALLS)} sites; set "
        f"_UNDECLARED_CALLS_CEILING to that (it may only go down)")


def test_ceilings_never_rise_above_the_high_water_mark():
    assert _UNDECLARED_DICTS_CEILING <= _DICTS_HIGH_WATER
    assert _UNDECLARED_CALLS_CEILING <= _CALLS_HIGH_WATER
