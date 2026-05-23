"""
brain_error_classes.py — seed registry of recurring error CLASSES that the
brain can self-fix.

Today the brain's Layer-4 text loop only fixes HTML placeholder
substitutions; Layer-5 (brain_v2_layer5.py) proposes free-form code
substitutions but does NOT auto-apply. Both are caller-side. Neither
has a *recognizer* for the recurring backend error patterns the healer
already surfaces (NoneType.fetchall, ON CONFLICT mismatch, legacy keys,
brain-radar 401 noise, conn-held forced-reclaim, …).

This module is that recognizer. Each entry is a known error class with
the regex that identifies it in Railway logs, the templated remediation
recipe, and the commit ID of the proven hand-fix that anchors the
confidence. Layer-5 (or a future Layer-6 auto-PR opener) is meant to
consume this registry: for any log line matching a class, propose the
templated fix against the file the regex captured.

Seed list (2026-05-23): the five classes we hit this session, each with
a shipped fix on main as the confidence anchor.

Phase ZZZZZ — brain takeover scaffold #1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ErrorClass:
    id: str
    pattern: str          # regex applied to log lines / heal findings
    fix_template: str     # template id consumed by Layer-5 codegen
    description: str
    confidence: float     # 0.0..1.0
    shipped_proof: Optional[str] = None  # commit SHA where this class was first fixed by hand
    notes: str = ""


# Seed registry. Add a row when a recurring class earns at least one
# shipped hand-fix on main — the SHA is the confidence anchor.
REGISTRY: list[ErrorClass] = [
    ErrorClass(
        id="nonetype_fetchall",
        pattern=r"'NoneType'\s+object\s+has\s+no\s+attribute\s+'fetchall'",
        fix_template="split_chained_execute",
        description=(
            "psycopg2 cursor.execute(sql).fetchall() — execute() returns None "
            "in psycopg2 (unlike sqlite3). Split into c.execute(sql) then c.fetchall()."
        ),
        confidence=0.95,
        shipped_proof="e2e1999d",
        notes="Same shape applies to .fetchone() and .rowcount chained on execute.",
    ),
    ErrorClass(
        id="on_conflict_target_mismatch",
        pattern=r"(?i)on\s+conflict.*does\s+not\s+match|partial[- ]index\s+ON\s+CONFLICT",
        fix_template="include_where_predicate_in_on_conflict",
        description=(
            "Postgres partial-UNIQUE-index ON CONFLICT requires the conflict "
            "clause to include the same WHERE predicate as the index. Without "
            "it the upsert raises and watchdog loops the container."
        ),
        confidence=0.85,
        shipped_proof="fbcf723d",
        notes="Killed the 2026-05-21 gas_pipelines spam → watchdog → container loop.",
    ),
    ErrorClass(
        id="legacy_hardcoded_key_accepted",
        pattern=r"legacy\s+hardcoded\s+key\s+accepted.*migrate\s+caller",
        fix_template="migrate_internal_key_header",
        description=(
            "Caller still authenticates with the legacy hardcoded internal key. "
            "Migrate it to read DCHUB_INTERNAL_KEY from env + send X-Internal-Key "
            "header, and remove the legacy fallback once all callers migrate."
        ),
        confidence=0.7,
        notes="Migration is cross-file (caller + receiver); confidence stays sub-0.8.",
    ),
    ErrorClass(
        id="brain_radar_401_noise",
        pattern=r"\[brain-radar\]\s+\S+\s+HTTP\s+40[13]",
        fix_template="demote_expected_gate_log",
        description=(
            "401/403 on the brain-radar's anonymous probe of a gated endpoint "
            "is the radar's intended SIGNAL, not an error. Return None silently "
            "instead of printing WARN to stderr."
        ),
        confidence=0.95,
        shipped_proof="912b3fd3",
    ),
    ErrorClass(
        id="conn_held_forced_reclaim",
        pattern=r"FORCED\s+RECLAIM:\s+Connection\s+\d+\s+held\s+\d+s",
        fix_template="wrap_caller_try_finally_close",
        description=(
            "DB connection checked out and never released until the watchdog's "
            "forced-reclaim. Wrap the caller's body in try/finally so conn.close() "
            "runs on every exit path (success, return, exception)."
        ),
        confidence=0.85,
        shipped_proof="e2e1999d",
        notes="Stack-trace line in the log names the leaking caller — feed it to the template.",
    ),
    # Phase ZZZZZ-round5 (2026-05-23): 3 new patterns picked up from the
    # 2026-05-23 Railway logs the user shared. Each is a recurring class
    # that already costs cycles in production; getting them registered
    # lets Layer-5 propose templated PRs the next time they fire.
    ErrorClass(
        id="psycopg2_transaction_aborted",
        pattern=r"current\s+transaction\s+is\s+aborted,\s+commands\s+ignored",
        fix_template="rollback_then_retry_or_savepoint",
        description=(
            "psycopg2: one query inside a transaction raised, so every subsequent "
            "query in the same transaction returns 'current transaction is aborted'. "
            "Fix: wrap risky upserts in a SAVEPOINT and ROLLBACK TO that savepoint "
            "on error, OR call conn.rollback() in the except block before continuing. "
            "Don't keep issuing queries on a poisoned connection."
        ),
        confidence=0.9,
        shipped_proof="e95fa29c",
        notes=(
            "2026-05-23: fiber_routes upsert loop logged 20 'transaction aborted' "
            "warnings in succession, then '0 carrier routes written'. Fix shipped "
            "in e95fa29c: SAVEPOINT/ROLLBACK wrapping per-row upserts in both "
            "fiber_network_discovery.py and jobs_api.py. Plus per-cycle log-spam "
            "suppression (first 5 only, then summarized)."
        ),
    ),
    ErrorClass(
        id="external_api_404_silent",
        pattern=r"(PeeringDB|external\s+API)\s+returned\s+4(04|05)",
        fix_template="record_external_404_with_backoff",
        description=(
            "External API returned 404 (or 405) — the endpoint shape changed or "
            "the resource was removed. Fix: don't keep retrying every cycle. Log "
            "once, mark the source DEGRADED in source_health, and pause it from "
            "the discovery rotation until a human revisits. ALSO check for the "
            "malformed_url_format_placeholder class — sometimes the '404' is just "
            "a busted URL template ('%s' where '?' belongs), not the upstream."
        ),
        confidence=0.8,
        shipped_proof="e95fa29c",
        notes=(
            "2026-05-23: PeeringDB 404 fired every cycle for weeks. Root cause "
            "was actually malformed_url_format_placeholder: '/api/ix%scountry=' "
            "instead of '/api/ix?country='. Two brain classes can share root "
            "cause — both should run for cross-checking."
        ),
    ),
    ErrorClass(
        id="slow_request_threshold_breach",
        pattern=r"SLOW\s+REQUEST:\s+\S+\s+\S+\s+took\s+\d+\.\d+s\s+\(>\d+s\)",
        fix_template="move_to_background_task_or_paginate",
        description=(
            "A request exceeded the slow-threshold (typically 30s gunicorn timeout). "
            "Fix: (a) move the work to a background task and return 202 + a poll URL "
            "from the request, OR (b) paginate the underlying query so the request "
            "returns the first page in <2s. Don't leave it as a 60s+ blocking call."
        ),
        confidence=0.85,
        shipped_proof="096c6cd6",
        notes="2026-05-23: /api/v1/energy/eia-ingest/run took 69.9s — fixed in 096c6cd6 by defaulting to async w/ task_id + poll endpoint.",
    ),
    # Phase ZZZZZ-round6 (2026-05-23): 2 more patterns from today's bug
    # sweep. malformed_url_format_placeholder caught 4 instances in
    # one grep (PeeringDB, LinkedIn x2, alerts) — high value class to
    # register so the brain catches the next one. mcp_generic_client_
    # attribution names the data-quality gap we just patched in
    # ai_tracking.py.
    ErrorClass(
        id="malformed_url_format_placeholder",
        pattern=r"['\"](?:https?://[^'\"]*)%s(?:country|limit|page|key|id=|query|filter|offset|tier|status)",
        fix_template="replace_pct_s_with_qmark_in_url",
        description=(
            "A quoted URL string contains '%s' where '?' belongs (separator between "
            "path and query string). Almost always means someone meant to use "
            "%-format substitution but never called it, OR copy-pasted a format "
            "template without converting. Result: every request goes to a literal "
            "'%s' in the path → 404. Run `grep -rn '%s(country|limit|page|id=)' "
            "--include='*.py'` to find all instances."
        ),
        confidence=0.95,
        shipped_proof="e95fa29c",
        notes=(
            "2026-05-23 sweep caught 4 instances: PeeringDB IX endpoint (3 wasted "
            "outbound/day worth of 404), LinkedIn deals_post + news_post (Mon/Wed "
            "topics silently empty for weeks), alerts.py unsubscribe links (every "
            "alert email had a broken unsubscribe URL). Brain didn't flag any of "
            "these because the upstream services returned 404 not 500 — looked "
            "like 'normal API not found' noise."
        ),
    ),
    ErrorClass(
        id="mcp_generic_client_attribution",
        pattern=r"mcp_client\s*=\s*['\"]mcp['\"]|platform\s*=\s*['\"]mcp['\"]",
        fix_template="add_specific_sdk_markers_to_detect_platform",
        description=(
            "AI client attribution defaulting to literal 'mcp' instead of a "
            "specific platform name (claude, chatgpt, cursor, …). Caused by "
            "detect_platform falling through to the generic 'mcp' bucket when "
            "user-agent contains 'mcp' but no specific marker matches. Fix: "
            "add more granular markers (mcp_sdk_ts, mcp_sdk_py, mcp_inspector, "
            "smithery, n8n, …) in detect_platform so the bucket-of-unknowns "
            "shrinks to truly unidentifiable clients."
        ),
        confidence=0.9,
        shipped_proof="437a75b0",
        notes=(
            "2026-05-23: /api/v1/mcp/conversion-funnel/by-client showed 2,903 "
            "paywall signals/7d in the 'mcp' bucket vs 1 each in 'claude-desktop' "
            "and 'verify'. Hidden the real per-client conversion rate, blocked "
            "A/B testing by client. Fix shipped: ai_tracking.detect_platform "
            "now returns mcp_sdk_ts / mcp_sdk_py / mcp_inspector / mcp_generic "
            "instead of the bare 'mcp' default."
        ),
    ),
    # Phase ZZZZZ-round6b (2026-05-23): 6 new classes for findings the
    # brain dashboard was tagging as "unknown class". Each one is a
    # well-defined detector that already exists in
    # brain_consistency_radar.py — these are documentation, not new
    # detection. Registering them grows the brain's recognized
    # vocabulary so Layer-5 can propose templated handlers.
    ErrorClass(
        id="enterprise_bot_present",
        pattern=r"enterprise_bot_present|whale_identified|high.volume.bot",
        fix_template="surface_whale_for_sales_outreach",
        description=(
            "Heavyweight scraper / bot identified — >500 MCP calls over 14 days, "
            "3+ active days. This is an OPPORTUNITY signal, not an error: route "
            "to /api/v1/bots/whales for human review (sales outreach vs block "
            "vs whitelist). Default action 'monitor' until a human triages."
        ),
        confidence=0.85,
        notes="Detector lives at routes/brain_consistency_radar.py:1810 (check_enterprise_bot_present). The 22,677 count is cumulative across all detection runs — same whale keeps surfacing until acted on.",
    ),
    ErrorClass(
        id="dedup_backlog_large",
        pattern=r"dedup_backlog_large|dedup.*backlog|discovered_facilities.*duplicate",
        fix_template="run_dedup_cycle",
        description=(
            "Discovered-facilities table has accumulated >N duplicates awaiting "
            "merge. Fix: trigger the dedup cycle via "
            "POST /api/v1/admin/dedup/run?max=500 (header X-Admin-Key) — "
            "the runner processes up to MAX_PER_RUN per call; for a backlog "
            "of 11k+ chain 6+ calls or schedule via cron. If the count keeps "
            "growing without dropping after dedup runs, investigate why the "
            "dedup detector isn't matching the new duplicates."
        ),
        confidence=0.8,
        shipped_proof="d3cce84f",
        notes="Detector at brain_consistency_radar.py:4546. 11,401 backlog as of 2026-05-23 — endpoint shipped in d3cce84f, ready to drain.",
    ),
    ErrorClass(
        id="auto_trial_signal_mint_mismatch",
        pattern=r"auto_trial_signal_mint_mismatch|signal.*mint.*ratio|trial.*signal.*key.*ratio",
        fix_template="check_auto_trial_mint_gate",
        description=(
            "Mismatch between paywall signals fired and auto-trial keys minted. "
            "Expected 590:1 signal:key ratio (per Phase ZZZZ-T1-paywall-visibility "
            "calibration); seeing significant deviation. Either the mint endpoint "
            "is rejecting more than expected, the dedup window is too aggressive, "
            "or signals are firing without their attached email-capture step."
        ),
        confidence=0.75,
        notes="Detector at brain_consistency_radar.py:6199. 2,860 occurrences suggests a structural mint gap, not a one-off.",
    ),
    ErrorClass(
        id="data_freshness_sla_breach",
        pattern=r"data_freshness_sla_breach|freshness.*sla|dataset.*stale.*past.*sla",
        fix_template="kick_dataset_refresh_cron",
        description=(
            "A tracked dataset hasn't refreshed within its SLA window. The "
            "brain autopilot's REFRESH_MAP (routes/brain_autopilot.py:287) "
            "maps each monitored table → a recovery endpoint that the "
            "autopilot calls autonomously when this class fires:\n"
            "  • dcpi_scores            → /api/v1/dcpi/recompute\n"
            "  • discovered_facilities  → /api/v1/admin/osm-crawl/run\n"
            "  • facilities             → /api/v1/admin/osm-crawl/run\n"
            "  • press_releases         → /api/v1/marketing/auto-generate\n"
            "  • ai_citations           → /api/v1/ai-citations/run-cron\n"
            "  • transmission_lines / substations / gas_pipelines  → respective\n"
            "    HIFLD refresh endpoints (see brain_autopilot.py:286-313).\n"
            "Manual override: POST the endpoint with X-Admin-Key. The autopilot "
            "skips a table after 3 consecutive failed recoveries (escalates "
            "to brain_critical_alerts)."
        ),
        confidence=0.85,
        shipped_proof="DDD",
        notes="Detector at brain_consistency_radar.py:4992. 1,302 occurrences — autopilot's REFRESH_MAP exists since Phase DDD; remaining findings are tables where autopilot's recovery hasn't yet succeeded (escalation cases).",
    ),
    ErrorClass(
        id="site_sentinel_unhealthy",
        pattern=r"site_sentinel_unhealthy[:_]",
        fix_template="fix_unhealthy_page_or_relax_sentinel",
        description=(
            "Site Sentinel polls a manifest of public pages and flags any that "
            "return non-200 OR body smaller than the page's min_bytes floor. "
            "Fix: (a) actually fix the page (most common: a route was renamed, "
            "or a feature was removed but the manifest still expects it), OR "
            "(b) update the sentinel manifest at routes/site_sentinel.py if "
            "the page legitimately moved / category changed."
        ),
        confidence=0.95,
        notes="Detector module: routes/site_sentinel.py. Manifest entries register pages with category + min_bytes + optional max_age_days. 2026-05-23: /vs (301 → /vs/dchawk, which IS healthy — sentinel needs to follow the redirect) and /pocket-listings (200 with 2659 bytes, above 500 floor — should clear on next scan).",
    ),
    ErrorClass(
        id="funnel_leak_critical",
        pattern=r"funnel_leak_critical|paywall_hit.*converting at.*%",
        fix_template="conversion_funnel_step_diagnosis",
        description=(
            "Funnel-step conversion rate dropped below the alarm threshold "
            "(typically 0.5% paywall→click). The leak diagnosis at /api/v1/mcp/"
            "conversion-funnel + new /api/v1/mcp/conversion-funnel/by-client "
            "should localize WHERE in the funnel the leak is. Fix: don't add "
            "auto-retry (was deliberately removed after 0 conversions in 90 days "
            "— Phase ZZZZ-T1-paywall-visibility). Test alternate CTA copy, OR "
            "address the dominant client's specific friction."
        ),
        confidence=0.85,
        notes="Detector emits funnel_leak_critical + funnel_conversion_critical when paywall→click is <0.5% OR paywall→conversion is <0.5%. 2026-05-23: BOTH firing at ~0.05% — paywall hits are real demand, the close is the gap.",
    ),
    # Phase ZZZZZ-round8 (2026-05-23): 5 more classes for findings the
    # latest heal report tagged as "unknown class". These are existing
    # detector outputs; registering them documents the recognized
    # vocabulary + lets Layer-5 propose templated handlers.
    ErrorClass(
        id="operator_profile_gap",
        pattern=r"operator_profile_gap:",
        fix_template="enrich_operator_with_website_or_canonical_name",
        description=(
            "An operator with high facility count is missing rich profile data "
            "(website URL, identified markets, deal history). The detector "
            "surfaces top operators with weak profiles so the discovery "
            "pipeline can prioritize backfilling them. Fix: run the operator-"
            "enrichment job for this operator, or manually update its row in "
            "operator_metadata. Watch for duplicate canonical names "
            "(e.g., 'Equinix' + 'Equinix, Inc.') — those should also be "
            "merged via the canonical-provider stripper added in round 7c."
        ),
        confidence=0.85,
        shipped_proof="ea01a4c1",
        notes="2026-05-23: 280 findings for Amazon Web Services, 279 for Digital Realty, 186 for Equinix Inc. Round 7c canonical-provider stripping addresses the duplicate-canonical sub-case; full enrichment is a separate sync job.",
    ),
    ErrorClass(
        id="mcp_conversion_stale_critical",
        pattern=r"mcp_conversion_stale_critical|conversion.*stale|conversions.*not\s+updated",
        fix_template="trigger_stripe_webhook_replay_or_funnel_recompute",
        description=(
            "The MCP conversion counter hasn't moved in >7 days despite "
            "fresh paywall signals. Either Stripe webhooks aren't replaying "
            "into our conversion table, or the funnel-recompute cron is "
            "stuck. Fix: (a) replay recent Stripe events via "
            "POST /api/stripe/webhook/replay (header X-Admin-Key or "
            "X-Internal-Key; requires STRIPE_SECRET_KEY env), "
            "(b) check the funnel cron's last_run timestamp in "
            "scheduler heartbeat, (c) examine mcp_pair_codes.stripe_"
            "clicked_at column for entries that lack a downstream "
            "tier_change row."
        ),
        confidence=0.8,
        shipped_proof="r33-F",
        notes="2,834 occurrences as of 2026-05-23. The replay endpoint at /api/stripe/webhook/replay has existed since Phase r33-F (routes/brain_autoaction_helpers.py:239). Class entry now points at the correct URL.",
    ),
    ErrorClass(
        id="mcp_funnel_concentration_top5",
        pattern=r"mcp_funnel_concentration_top5|top\s*5.*concentration|top.5.*paywall",
        fix_template="diversify_demand_or_lift_top_tool_paywall",
        description=(
            "Top 5 tools account for >80% of paywall hits — concentrated "
            "demand. Either (a) good news: clear product-market signal "
            "(prioritize those tools for IDENTIFIED-tier teaser data), or "
            "(b) warning: the long tail of tools isn't being discovered "
            "because the manifest under-advertises them. Decide per-tool: "
            "lift the paywall lower on the top 5 (faster conversions) OR "
            "improve discovery surfaces (sitemap, /ai/llms.txt) for the "
            "long tail."
        ),
        confidence=0.8,
        notes="2,655 occurrences. Top 5: get_market_intel, get_grid_data, get_water_risk, get_energy_prices, get_renewable_energy (each 290+ hits/7d).",
    ),
    ErrorClass(
        id="trial_to_paid_stagnation",
        pattern=r"trial_to_paid_stagnation|trial.*paid.*stagnation|conversion.*stagnant",
        fix_template="audit_trial_keys_for_upgrade_path_friction",
        description=(
            "Auto-trial keys are minting but not converting to paid. Detector "
            "fires when N+ active trials have been outstanding for >14 days "
            "without any tier_change. Fix candidates: (a) trial signal:key "
            "mint ratio is off (see auto_trial_signal_mint_mismatch class), "
            "(b) the in-product upgrade CTA on trial keys isn't being "
            "rendered to the human user, (c) the trial tier limit is too "
            "generous (no friction = no upgrade urgency)."
        ),
        confidence=0.75,
        notes="17,527 occurrences as of 2026-05-23. Same paywall-leak signal as funnel_leak_critical but at the trial→paid step specifically.",
    ),
    ErrorClass(
        id="mcp_demand_gap_unaddressed",
        pattern=r"mcp_demand_gap_unaddressed|demand.*gap|unaddressed.*demand",
        fix_template="prioritize_high_demand_low_supply_tool",
        description=(
            "AI agents are repeatedly hitting paywalled tools that have no "
            "free-tier teaser. The detector flags tools where paywall hits "
            "are high but free-tier traffic is zero — those are agents "
            "trying to use a tool they don't know exists at a tier level "
            "they can claim. Fix: add a free-tier teaser response for the "
            "flagged tool (1-3 results free, full data behind X-API-Key), "
            "OR document the tool more prominently in /mcp/manifest so "
            "agents know to request a free key."
        ),
        confidence=0.85,
        notes="933 occurrences. Top demand: get_grid_intelligence (96 distinct users hitting paid tool), get_fiber_intel (97 users). Both should get free-tier teasers.",
    ),
]


def match(line: str) -> Optional[ErrorClass]:
    """Return the FIRST registered class whose pattern matches `line`, or None.

    Intentionally first-match (not best-match) — the registry is curated and
    ordered by specificity. The Layer-5 caller is expected to apply the
    matching class's fix_template against the file/line the log line implies.
    """
    if not line:
        return None
    for cls in REGISTRY:
        try:
            if re.search(cls.pattern, line, flags=re.IGNORECASE):
                return cls
        except re.error:
            continue
    return None


def summary() -> dict:
    """Compact snapshot of the registry — surfaced on /api/v1/brain/error-classes
    so the dashboard can show 'Brain knows N error classes; M have shipped proofs.'"""
    proofs = sum(1 for c in REGISTRY if c.shipped_proof)
    return {
        "total_classes": len(REGISTRY),
        "with_shipped_proof": proofs,
        "avg_confidence": round(sum(c.confidence for c in REGISTRY) / max(len(REGISTRY), 1), 2),
        "classes": [
            {
                "id": c.id,
                "description": c.description,
                "confidence": c.confidence,
                "shipped_proof": c.shipped_proof,
            }
            for c in REGISTRY
        ],
    }
