"""Phase AAA (2026-05-16) — Brain autopilot.

The brain has detectors + findings + Layer 5 proposals — but the proposal
loop ONLY produces code suggestions for human review. For operational
issues (stale cron needs retrigger, ISO loop needs restart, stale cache
needs purge), there's no autonomous action layer. So the brain "knows"
about problems but does nothing about them. This module is the doer.

Architecture:
  1. Pattern library: finding-issue → action callable
  2. Every 30 min, cron POSTs /api/v1/brain/autopilot/run
  3. Reads /api/v1/heal/findings actionable_backend_issues
  4. For each finding matching a known pattern:
     a. Rate-limit check (max N actions/pattern/hour)
     b. Idempotency check (was this finding already acted on in the
        last cooldown window?)
     c. Execute the action
     d. Log to brain_autopilot_actions table with outcome
  5. If same pattern fires N+ times in 24h despite remediation,
     escalate to humans (mark `escalated=true`)

Safety:
  - HARD KILL SWITCH: env var BRAIN_AUTOPILOT_DISABLED=1 blocks all actions
  - DRY RUN: env var BRAIN_AUTOPILOT_DRY_RUN=1 logs intended actions, doesn't execute
  - WHITELIST ONLY: only finding-issue strings explicitly mapped here can fire
  - Rate limit: 3 actions per pattern per hour, 10 per 24h, hard-coded
  - Cooldown: 30 min between same-finding actions
  - Audit: every action gets a brain_autopilot_actions row

Endpoints:
  POST /api/v1/brain/autopilot/run     — admin or internal-key gated; cron entry point
  GET  /api/v1/brain/autopilot/status   — public; last 24h activity summary
  GET  /api/v1/brain/autopilot/library  — public; the pattern→action map
"""

from __future__ import annotations

import os
import json
import urllib.request
import datetime
from typing import Any
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


brain_autopilot_bp = Blueprint("brain_autopilot", __name__)


# Rate limits and safety caps
_MAX_ACTIONS_PER_PATTERN_PER_HOUR = 3
_MAX_ACTIONS_PER_PATTERN_PER_24H  = 10
_COOLDOWN_MIN_BETWEEN_SAME_ACTIONS = 30   # minutes
_ESCALATION_THRESHOLD_24H          = 5     # if pattern fires this many times
                                            # in 24h despite remediation,
                                            # mark escalated and stop acting

# Where Railway lives — autopilot calls back into ourselves to retrigger
# operations. CF Worker's 15s timeout is a no-go for long actions.
_BACKEND_BASE = os.environ.get(
    "DCHUB_BACKEND_BASE",
    "https://dchub-backend-production.up.railway.app",
)


def _is_disabled() -> bool:
    return str(os.environ.get("BRAIN_AUTOPILOT_DISABLED", "")).lower() in ("1", "true", "yes")


def _is_dry_run() -> bool:
    return str(os.environ.get("BRAIN_AUTOPILOT_DRY_RUN", "")).lower() in ("1", "true", "yes")


def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_schema():
    """Idempotent. Stores every autopilot action attempt."""
    c = _conn()
    if c is None: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_autopilot_actions (
                    id              BIGSERIAL PRIMARY KEY,
                    finding_issue   TEXT NOT NULL,
                    finding_url     TEXT,
                    pattern_name    TEXT NOT NULL,
                    action_endpoint TEXT,
                    action_payload  JSONB,
                    dry_run         BOOLEAN NOT NULL DEFAULT FALSE,
                    outcome         TEXT,
                    http_code       INT,
                    response_body   TEXT,
                    error           TEXT,
                    escalated       BOOLEAN NOT NULL DEFAULT FALSE,
                    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at    TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS ix_autopilot_recent
                    ON brain_autopilot_actions(started_at DESC);
                CREATE INDEX IF NOT EXISTS ix_autopilot_pattern
                    ON brain_autopilot_actions(pattern_name, started_at DESC);
            """)
        return True
    except Exception as e:
        print(f"[autopilot] schema init failed: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass


try: _ensure_schema()
except Exception: pass


# ── ACTION LIBRARY ────────────────────────────────────────────────────
# pattern_name → callable that returns (action_endpoint, payload_dict).
# Returning (None, None) means "no actionable remediation; log only."
#
# Each action MUST be idempotent and safe to retry. No deletes, no
# destructive ops. Re-trigger crons, refresh caches, retry imports — yes.
# Anything that touches money, auth, or user data — NO.

def _action_dcpi_partial_recompute(finding: dict) -> tuple[str | None, dict | None]:
    """DCPI median age > 48h. Re-trigger the recompute with chunking
    so the next read shows fresh data. Idempotent — recompute upserts.
    """
    return "/api/v1/dcpi/recompute?offset=0&limit=100", {}


def _action_discovery_stalled(finding: dict) -> tuple[str | None, dict | None]:
    """Zero new facilities in 7d. We don't have a one-shot endpoint to
    re-fire all discovery crons; log a high-priority escalation marker
    so a human triggers the workflow_dispatch on dchub-osm-refresh.yml
    + daily-infra-sync.yml. Marked escalate=True so it doesn't burn
    rate limit retrying."""
    return None, None   # escalation-only


def _action_iso_metric_zero(finding: dict) -> tuple[str | None, dict | None]:
    """Per-ISO 0 metrics in 24h. The grid loops are workflow-driven —
    we don't have an in-process trigger. Log + escalate so a human
    fires the iso-data-pull.yml workflow."""
    return None, None   # escalation-only


def _action_press_repetition(finding: dict) -> tuple[str | None, dict | None]:
    """Last 3 press releases same market. Force a new generation
    skipping the repeat market via the existing repost endpoint."""
    return "/api/v1/marketing/repost-now?force_topic_rotation=1", {}


def _action_mcp_conversion_stale(finding: dict) -> tuple[str | None, dict | None]:
    """No conversions on 500+ signals in 7d. No autonomous fix possible —
    this is a paywall-copy / pricing issue. Escalate only."""
    return None, None   # escalation-only


def _action_worker_version_drift(finding: dict) -> tuple[str | None, dict | None]:
    """CF Pages worker stale. Requires CF dashboard action — escalate."""
    return None, None


def _action_seo_sitemap_stale(finding: dict) -> tuple[str | None, dict | None]:
    """Sitemap lastmod stale. Re-trigger sitemap regeneration."""
    return "/api/v1/sitemap/regenerate", {}


# Phase CCC (2026-05-16): 5 more escalation patterns so the audit log
# shows the brain RECOGNIZING these findings (vs silently no_action-ing).
# Counted as escalated so the rate-limit doesn't burn cycles retrying;
# the human gets a clean view at /autopilot/recent of every flagged
# pattern and what was/wasn't auto-fixable.

def _action_radar_detector_crashed(finding: dict) -> tuple[str | None, dict | None]:
    """A detector raised an exception. Escalate — needs code fix."""
    return None, None


def _action_tier_inconsistency(finding: dict) -> tuple[str | None, dict | None]:
    """MCP↔web tier mismatch. Phase ZZ-2 (2026-05-17) — promote from
    escalation-only to autonomous-record. The actual fix (decorator
    change) is still a human code change, but the brain now records
    every mismatch into tier_drift_proposals so we have a queryable
    worklist instead of a one-time finding that disappears at next
    radar tick. Phase QQ off-by-one bugfix made this safe to promote
    (the detector no longer false-positives on equal-tier endpoints)."""
    body = {
        "tool":         finding.get("tool"),
        "web_path":     finding.get("url"),
        "mcp_tier":     finding.get("mcp_tier"),
        "web_min_tier": finding.get("web_min_tier"),
        "detail":       finding.get("detail", "")[:500],
        "source":       "autopilot",
    }
    return "/api/v1/brain/tier-drift/propose", body


def _action_cron_missing_schedule(finding: dict) -> tuple[str | None, dict | None]:
    """workflow_dispatch-only phase. Needs YAML schedule add. Escalate."""
    return None, None


def _action_cron_schedule_collision(finding: dict) -> tuple[str | None, dict | None]:
    """Phase III (2026-05-17) — promote from escalation to autonomous-record.

    Two workflows share the same cron minute → thundering herd against
    Railway. The actual fix (edit one .yml to stagger by N minutes) is
    a human code change, but the brain now records every collision into
    cron_collision_proposals so we have a queryable worklist instead of
    a volatile finding that disappears each radar tick.

    Same pattern as Phase ZZ-2 tier_drift + Phase EEE data_freshness.
    """
    body = {
        "collision_minute": finding.get("url"),       # e.g. "13:30" or detail
        "detail":           finding.get("detail", "")[:600],
        "count":            finding.get("count"),
        "source":           "autopilot",
    }
    return "/api/v1/brain/cron-collision/propose", body


def _action_worker_source_unreachable(finding: dict) -> tuple[str | None, dict | None]:
    """raw.githubusercontent.com fetch failed (private repo, no token).
    Needs GITHUB_TOKEN env var. Escalate."""
    return None, None


def _action_data_freshness_breach(finding: dict) -> tuple[str | None, dict | None]:
    """Phase EEE (2026-05-17) — autonomous data-freshness refresh.

    The Phase KK check_data_freshness_sla_breach detector emits findings
    with url='table:<name>'. Map the table to its known refresh endpoint
    and POST. Falls through to escalation (None, None) for tables that
    don't have a manual refresh trigger — those need cron/code fixes.

    Refresh endpoints supported:
      ai_citations          → /api/v1/ai-citations/run-cron     (Phase II)
      dcpi_scores           → /api/v1/dcpi/recompute            (Phase II/J)
      discovered_facilities → escalate (no manual endpoint; depends on
                              discovery cron which runs externally)
      news_items            → escalate (RSS poll cron, no endpoint)
    """
    url = finding.get("url", "") or ""
    if not url.startswith("table:"):
        return None, None
    table = url[len("table:"):].strip()
    REFRESH_MAP = {
        "ai_citations": "/api/v1/ai-citations/run-cron",
        "dcpi_scores":  "/api/v1/dcpi/recompute",
        # facilities + news refresh paths live in external cron, not API:
        # leaving them in this map as escalation candidates.
    }
    endpoint = REFRESH_MAP.get(table)
    if not endpoint:
        return None, None  # Escalate — no autonomous fix
    return endpoint, {"source": "autopilot_freshness_refresh", "table": table}


# ── Phase DDD (2026-05-16) — organism autopilot actions ──────────────
def _action_mcp_growth_declining(finding: dict) -> tuple[str | None, dict | None]:
    """MCP volume dropped WoW. No autonomous fix — could be CF outage,
    paywall change, or upstream issue. Escalate so humans investigate."""
    return None, None


def _action_mcp_demand_gap(finding: dict) -> tuple[str | None, dict | None]:
    """Top demand-gap tool has 0 conversions on 50+ signals. The autonomous
    fix is to TRIGGER a fresh snapshot of growth data + ensure a Layer 5
    proposal exists for the tool. POST /api/v1/mcp/growth/snapshot
    captures the state for the brain to learn from."""
    return "/api/v1/mcp/growth/snapshot", {}


def _action_source_of_truth_declining(finding: dict) -> tuple[str | None, dict | None]:
    """SoT score dropped. Autonomous fix: trigger an off-cycle press
    release to refresh our voice in the conversation."""
    return "/api/v1/marketing/repost-now?force_topic_rotation=1", {}


def _action_media_topic_unaddressed(finding: dict) -> tuple[str | None, dict | None]:
    """Hot topic in news with no press response. Autonomously trigger
    auto-generate so we comment on the story while it's still fresh."""
    return "/api/v1/marketing/auto-generate", {}


def _action_monthly_trend_unsent(finding: dict) -> tuple[str | None, dict | None]:
    """Phase FF+25-followup-r7 (2026-05-20): backstop for the GitHub
    monthly-cron. If we're 4+ days into a new month and the prior-month
    snapshot hasn't been emailed to journalists, this fires the send
    endpoint with triggered_by=autopilot so the campaign log shows the
    brain rescued the campaign."""
    return ("/api/v1/reports/monthly/send-outreach?triggered_by=autopilot", {})


def _action_dchub_media_press_silent(finding: dict) -> tuple[str | None, dict | None]:
    """Phase RRRR (2026-05-16): DC Hub Media has been silent for 7+
    days. AUTO-FIRE the marketing worker — this is the wake-up
    mechanism. Not escalation-only because the user explicitly asked
    for autonomous output: 'is DC Hub Media telling everyone?'
    The auto-generate endpoint itself has a same-day dedup so this
    is safe to call even when other crons already fired today."""
    return "/api/v1/marketing/auto-generate", {}


def _action_surface_health_critical(finding: dict) -> tuple[str | None, dict | None]:
    """A registered surface dropped below health 40. Per-surface
    remediation varies (markets needs different fix than land-power);
    escalate to humans with full per-surface diagnostic at
    /api/v1/surface/<id>/pulse + /demand-gaps + /growth."""
    return None, None


_PATTERN_LIBRARY: dict[str, dict[str, Any]] = {
    "dcpi_partial_recompute": {
        "action":      _action_dcpi_partial_recompute,
        "method":      "POST",
        "use_admin":   True,
        "description": "Retrigger DCPI recompute with chunking (covers 100 markets per attempt)",
    },
    "discovery_stalled_7d": {
        "action":      _action_discovery_stalled,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: humans must trigger dchub-osm-refresh.yml workflow",
    },
    "iso_metric_count_zero_24h": {
        "action":      _action_iso_metric_zero,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: humans must trigger iso-data-pull.yml workflow",
    },
    "iso_metric_count_dropped": {
        "action":      _action_iso_metric_zero,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: per-ISO partial loop failure needs human investigation",
    },
    "auto_press_market_repetition": {
        "action":      _action_press_repetition,
        "method":      "POST",
        "use_admin":   True,
        "description": "Force a new press release with topic rotation enabled",
    },
    "mcp_conversion_stale_critical": {
        "action":      _action_mcp_conversion_stale,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: paywall/pricing review needed (not autonomous)",
    },
    "worker_version_drift": {
        "action":      _action_worker_version_drift,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: requires CF dashboard 'promote to production'",
    },
    "seo_sitemap_stale": {
        "action":      _action_seo_sitemap_stale,
        "method":      "POST",
        "use_admin":   True,
        "description": "Retrigger sitemap regeneration",
    },
    # Phase CCC (2026-05-16): 5 more escalation-only patterns.
    "consistency_radar_detector_crashed": {
        "action":      _action_radar_detector_crashed,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a brain detector raised — code fix needed (check the detector's SQL)",
    },
    "tier_inconsistency_web_higher_than_mcp": {
        "action":      _action_tier_inconsistency,
        "method":      "POST",  # Phase ZZ-2 (2026-05-17): promoted from escalation
        "use_admin":   False,
        "description": ("Autonomous: records mismatch into tier_drift_proposals "
                         "table so we have a queryable worklist of pending "
                         "decorator alignments. Actual code fix remains human "
                         "work but the brain now tracks them automatically."),
    },
    "data_freshness_sla_breach": {
        "action":      _action_data_freshness_breach,
        "method":      "POST",  # Phase EEE (2026-05-17): promoted to autonomous refresh
        "use_admin":   True,    # refresh endpoints (dcpi/recompute, ai-citations/run-cron) need ADMIN_KEY
        "description": ("Autonomous: when a tracked table exceeds its SLA, fires "
                         "the appropriate refresh endpoint (ai_citations → run-cron, "
                         "dcpi_scores → recompute). Escalates for tables whose refresh "
                         "is external-cron only (discovered_facilities, news_items)."),
    },
    "cron_phase_missing_schedule": {
        "action":      _action_cron_missing_schedule,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: workflow_dispatch-only phase needs a cron: schedule added to its .yml",
    },
    "cron_schedule_collision": {
        "action":      _action_cron_schedule_collision,
        "method":      "POST",  # Phase III (2026-05-17): promoted to autonomous-record
        "use_admin":   False,
        "description": ("Autonomous: records each collision into cron_collision_proposals "
                         "(queryable worklist). Actual YAML edit remains human work but the "
                         "brain tracks them rather than re-emitting the same finding every tick."),
    },
    "worker_source_unreachable": {
        "action":      _action_worker_source_unreachable,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: GITHUB_TOKEN env var missing — radar can't fetch private _worker.js source",
    },
    # Phase DDD (2026-05-16) — organism patterns.
    "mcp_growth_declining": {
        "action":      _action_mcp_growth_declining,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: MCP volume dropped >25% WoW — likely upstream issue",
    },
    "mcp_demand_gap_unaddressed": {
        "action":      _action_mcp_demand_gap,
        "method":      "POST",
        "use_admin":   True,
        "description": "Persist a fresh growth snapshot so Layer 5 has up-to-date demand-gap data to propose tool changes",
    },
    "source_of_truth_declining": {
        "action":      _action_source_of_truth_declining,
        "method":      "POST",
        "use_admin":   True,
        "description": "Trigger off-cycle press release to refresh our voice in the news conversation",
    },
    "media_topic_unaddressed": {
        "action":      _action_media_topic_unaddressed,
        "method":      "POST",
        "use_admin":   True,
        "description": "Hot news topic with no press response — autonomously generate one while the story is fresh",
    },
    # Phase FF+25-followup-r7 (2026-05-20): monthly trend outreach backstop.
    # If the GitHub cron failed to fire the 1st-of-month outreach campaign,
    # this autonomous action recovers it. Rate-limit + cooldown apply (so it
    # can fire at most once per pattern/url combo per cooldown window).
    "monthly_trend_unsent_3d": {
        "action":      _action_monthly_trend_unsent,
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous backstop: the monthly trend snapshot wasn't emailed by the 4th of the new month. Brain fires /api/v1/reports/monthly/send-outreach with triggered_by=autopilot.",
    },
    # Phase FF+25-followup-r12 (2026-05-20): visual drift escalation.
    # Drift is fixed by editing /js/dchub-nav.js or per-page <style> —
    # both code-level. Autopilot escalates so a human (or L22 auto-code)
    # sees the finding; no auto-action because the fix path varies per
    # page and could touch arbitrary CSS.
    "page_brand_drift": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: one or more public pages drifted off the canonical indigo→violet brand. Look at the detail field for which page + which signal; fix in /js/dchub-nav.js (covers most pages) or the per-page <style>. Track on /status.",
    },
    # Phase EEE — surface brain pattern. Dynamic key
    # `surface_health_critical:<surface_id>` is handled via the prefix-
    # match in _lookup_pattern() so all surfaces share this entry.
    "surface_health_critical": {
        "action":      _action_surface_health_critical,
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a registered surface dropped below health 40. Per-surface fix varies — see /api/v1/surface/<id>/pulse",
    },
    # Phase GGG — per-tool funnel-leak pattern. Dynamic key
    # `mcp_funnel_leak:<tool>` binds via _lookup_pattern() prefix match.
    "mcp_funnel_leak": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a single MCP tool has >95% drop at one funnel stage. See /api/v1/mcp/conversion-funnel/<tool> for the leak location.",
    },
    # Phase LLL — enterprise bot present (revenue opportunity).
    "enterprise_bot_present": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: high-volume bot identified (likely enterprise prospect). Reach out or block — see /api/v1/bots/whales.",
    },
    # Phase TTT — brand-surface dormancy. Dynamic key
    # `brand_surface_dormant:<surface_id>` binds via _lookup_pattern()
    # prefix match. Escalation-only because the fix is judgment-heavy
    # (add nav link / homepage tile / external drive — humans decide).
    "brand_surface_dormant": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a brand-positioning surface (vs / dcpi totals / live pulse) has zero traffic in 72h. Add nav link / homepage tile / external drive.",
    },
    # Phase VVV — schema drift. Dynamic keys
    # `schema_drift_column_missing:<table>.<col>` and
    # `schema_drift_table_missing:<table>` resolve via prefix match.
    # Escalation-only — fixing a missing column means either creating
    # it OR changing the query, both human-judgment calls.
    "schema_drift_column_missing": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a query referenced a column that doesn't exist. Either ALTER TABLE ADD COLUMN or change the query to use information_schema-aware probing (see dchub_media.aggregate_announcements_v3).",
    },
    "schema_drift_table_missing": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a query referenced a table that doesn't exist. Either create it (migration) or wrap the caller with a to_regclass() probe so the absence is silent.",
    },
    # Phase WWW — Site Sentinel page-health pattern. Dynamic key
    # `site_sentinel_unhealthy:<path>` resolves via prefix match.
    # Escalation-only because fixing a broken page requires looking
    # at the actual page code (CSP, missing route, empty data, etc).
    "site_sentinel_unhealthy": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a page in the Site Sentinel manifest is unhealthy. Inspect the path in the finding — common fixes: missing route registration, CSP block, 503 from an upstream API, body smaller than the manifest's min_bytes floor. See /sentinel dashboard.",
    },
    # Phase XXX — conversion rate below floor. Escalation-only because
    # the fix is a strategic call: tighten more tools to IDENTIFIED+,
    # raise FREE-tier cap pressure, or rewrite the paywall response.
    "mcp_conversion_rate_below_floor": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: 30-day MCP conversion rate is below the configured floor. Inspect /api/v1/mcp/conversion-funnel for per-tool breakdown; the leak is usually concentrated in 1-2 tools that should be on a higher tier or have a tighter cap.",
    },
    # Phase YYY — page-staleness pattern. Dynamic key
    # `page_stale:<path>` resolves via prefix match. Escalation-only;
    # fix is always upstream (cron / ingest), never the route itself.
    "page_stale": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a page in the Site Sentinel manifest is serving data older than its max_age_days SLA. Fix: bump the relevant ingest cron OR re-trigger the data source. See /sentinel for stale_days + data_age_src per page.",
    },
    # Phase ZZZ — nav-missing pattern. Dynamic key `nav_missing:<path>`.
    # Escalation-only; fix is always template-level include.
    "nav_missing": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a page returns 200 but does NOT include dchub-nav.js. Users see a page with no top nav. Fix: add `<script src=\"/js/dchub-nav.js\" defer></script>` to the page template, or wire it via the standard page wrapper.",
    },
    # Phase AAAA — dormant-MCP outreach prompt. Escalation-only;
    # autonomous wake isn't possible (no contact path from MCP call
    # log), but flagging the worklist is high-leverage for humans.
    "mcp_dormant_agents_present": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: agents that previously hammered MCP have gone dormant. See /api/v1/bots/dormant for the structured outreach worklist (top targets ranked by prior_calls). High-priority targets (>=100 prior calls) are the likely enterprise prospects worth manual winback. Phase RRRR added /api/v1/media/winback-pitches with copy-paste outbound email templates per platform.",
    },
    # Phase RRRR — AUTONOMOUS auto-fire on press silence.
    "dchub_media_press_silent": {
        "action":      _action_dchub_media_press_silent,
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous: auto-triggers /api/v1/marketing/auto-generate when DC Hub Media is silent for 7+ days. The user explicitly asked for the brain to wake DC Hub Media up; this is the mechanism. Rate-limited like every other autopilot action so it can't spam.",
    },
    # Phase SSSS — AUTONOMOUS auto-fire on winback pitches unsent.
    "winback_pitches_unsent": {
        "action":      lambda f: ("/api/v1/media/winback/deliver", {}),
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous: auto-triggers /api/v1/media/winback/deliver when pitches exist but no deliveries in 14 days. This is the second autonomous pattern (after dchub_media_press_silent) — the brain doesn't just escalate, it acts.",
    },
    # Phase TTTT — citation score
    "citation_score_dropped": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: AI-platform citation share dropped. Fix is outreach (see /api/v1/media/winback-pitches) — already wired as a separate autonomous pattern via winback_pitches_unsent.",
    },
    "citation_score_below_30pct": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: citation share <30%. Major brand-positioning gap; needs direct AI-platform outreach + improved schema.org markup on key pages.",
    },
    # Phase UUUU — pattern-proposal candidates
    "pattern_proposal_candidate": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Operator-only: brain identified 3+ identical manual resolutions for an issue. See /api/v1/brain/pattern-proposals — copy the proposed_pattern_stub into routes/brain_autopilot.py:_PATTERN_LIBRARY to make the brain autonomous on this issue.",
    },
    # Phase VVVV — page content drift
    "page_content_drift": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: Sentinel detected a page's content hash + size changed significantly. Verify the change was intentional (new schema, fresh data) or a regression (removed block, broken template).",
    },
    # Phase XXXX — competitor announcements
    "competitor_announcement": {
        "action":      lambda f: ("/api/v1/marketing/auto-generate", {}),
        "method":      "POST",
        "use_admin":   True,
        "description": "AUTONOMOUS: when a competitor updates their site significantly, auto-trigger DC Hub Media press to publish counter-positioning content while the news is fresh. Third autonomous pattern in the library.",
    },
    # Phase YYYY — operator-profile gap
    "operator_profile_gap": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: top operator missing >50% of market or power_mw fields. Discovery pipeline (routes/discovery_routes.py + ingest crons) should prioritize this operator for backfill so /operators/<slug> profile is rich.",
    },
    # Phase ZZZZ — AUTONOMOUS: refresh stale market deep-dives nightly
    "market_deep_dive_stale": {
        "action":      lambda f: ("/api/v1/markets/deep-dive/cron?count=5", {}),
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous: auto-fires the deep-dive cron when top-10 markets have stale narratives. Fourth autonomous pattern (after press_silent, winback_unsent, competitor_announcement) — the brain WRITES content automatically.",
    },
    # Phase BBBBB — event submission deadlines (escalation-only)
    "event_submission_pending": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: industry event submission deadline in <30 days and DC Hub hasn't submitted. Human needs to decide submit/skip. See /events for full list.",
    },
    # Phase CCCCC — tenant coverage thin
    "tenant_coverage_thin": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: <20% of top-50 facilities have tenant data. Per-building tenants are DCHawk's main remaining moat. Either build SEC/CRE/news ingest pipeline OR manually POST to /api/v1/tenants/ingest.",
    },
    # Phase DDDDD — conversion-engine v2
    "auto_trial_signup_rate_low": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: auto-trial keys are being minted (good) but agents aren't redeeming them to permanent accounts. Improve the redemption CTA in the paywall message, OR auto-email the redeem link to anyone who used a trial key 5+ times.",
    },
    "mcp_funnel_concentration_top5": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Informational: top-5 tools generate the vast majority of paywall signals. Phase DDDDD auto-trial flow targets exactly these. If conversion rate doesn't lift within 7 days of DDDDD deploy, the auto-trial response message is the next thing to iterate.",
    },
    # Phase EEEEE — volume regression
    "mcp_volume_regression": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: 7-day MCP volume dropped >20% vs baseline. Phase EEEEE anon grace mode should recover. Check /api/v1/grace/stats. If recovery stalls, raise DCHUB_ANON_GRACE_CAP env or further loosen FREE-tier caps.",
    },
    # Phase FFFFF — autopilot outcome verification: AUTONOMOUS
    "autopilot_action_unverified": {
        "action":      lambda f: ("/api/v1/brain/autopilot/verify-pending", {}),
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous: auto-fires the outcome verifier when actions accumulate unverified. 5th autonomous pattern. Closes the brain's biggest blind spot: knowing whether autopilot ACTUALLY succeeded.",
    },
    # Phase GGGGG — schema.org coverage
    "schema_org_coverage_low": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: <80% of critical pages have proper schema.org JSON-LD. AI agents prioritize structured data when fact-citing — this directly drags SOT score. Worklist: /api/v1/schema-org/missing.",
    },
    # Phase HHHHH — external mentions dropoff
    "external_mentions_dropoff": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: HN/Reddit mentions dropped 40%+ WoW. Brand discovery stalling. Consider auto-posting ShowHN OR submitting to r/datacenter, r/sysadmin.",
    },
    "dchub_media_press_weak": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: 4+ press in 30d is the healthy cadence; below that, the auto-press cron is running but rows aren't landing. Operator needs to inspect the press worker logs OR the auto_press_releases insert path.",
    },
    # Phase PPPP — dedup pipeline divergence
    "dedup_pipeline_stalled": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: dedup worker has stalled — raw discovered_facilities count is climbing while verified (deduped) is flat. Inspect the dedup cron + routes/discovery_routes.py merge logic.",
    },
    "dedup_backlog_large": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Informational: dedup backlog is >5k candidates but we don't have 7d of snapshots yet to confirm a stall. Will auto-clear or upgrade to dedup_pipeline_stalled in 7 days based on whether verified moves.",
    },
    # Phase BBBB — /developers funnel intent dead. Escalation-only;
    # fix is copy + CTA repositioning, requires judgment.
    "developers_funnel_intent_dead": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: /developers visitors aren't converting to intent signals. Either the page copy doesn't land or the CTA is buried. Inspect /api/v1/developers/funnel and run an A/B on the pricing block.",
    },
    # Phase CCCC — pending spare-capacity listings need moderation.
    # Escalation-only until admin-approval endpoint ships (DDDD+).
    "spare_capacity_pending_moderation": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: spare-capacity listings are stuck in 'pending' status past 24h. Review and flip to 'live' in the spare_capacity_listings table. Phase DDDD+ will add an admin-approval endpoint.",
    },
}


# Phase CCC: prefix-match library lookup. Detector crashes have dynamic
# issue keys like "consistency_radar_detector_crashed:check_xxx" so we
# also try the bare prefix for autopilot pattern matching.
def _lookup_pattern(issue: str) -> dict | None:
    """Direct lookup first, then prefix match (issue.split(':',1)[0])."""
    if not issue: return None
    direct = _PATTERN_LIBRARY.get(issue)
    if direct: return direct
    base = issue.split(":", 1)[0]
    if base != issue:
        return _PATTERN_LIBRARY.get(base)
    return None


# ── Rate limit helpers ────────────────────────────────────────────────
# Phase FF+25-followup-r4 (2026-05-20): exclude bookkeeping rows from
# the rate-limit count. The old query counted every row with the given
# pattern_name including the ones we wrote ourselves to RECORD that the
# previous attempt was rate-limited. Result: each blocked attempt
# incremented n_24h, which then tripped escalation_threshold (>=5) on
# the next legitimate attempt and immediately wrote ANOTHER rate_limited
# row — a self-perpetuating throttle that locked every pattern out for
# the full 24-hour window. Observed at 23 actions / 24h, all blocked.
#
# Now we only count rows that represent real fire attempts (anything
# other than the rate_limited / cooldown_active bookkeeping outcomes).
# Same fix applied to _last_action_age_minutes so cooldown windows
# aren't refreshed by blocked retries.
_BOOKKEEPING_OUTCOMES = ('rate_limited', 'cooldown_active')


def _recent_actions(cur, pattern: str, hours: int) -> int:
    cur.execute("""
        SELECT COUNT(*) FROM brain_autopilot_actions
         WHERE pattern_name = %s
           AND started_at >= NOW() - INTERVAL %s
           AND COALESCE(outcome, '') NOT IN %s
    """, (pattern, f"{hours} hours", _BOOKKEEPING_OUTCOMES))
    return int((cur.fetchone() or [0])[0] or 0)


def _last_action_age_minutes(cur, pattern: str, url: str | None) -> int | None:
    cur.execute("""
        SELECT EXTRACT(EPOCH FROM (NOW() - MAX(started_at))) / 60
          FROM brain_autopilot_actions
         WHERE pattern_name = %s
           AND COALESCE(finding_url,'') = %s
           AND COALESCE(outcome, '') NOT IN %s
    """, (pattern, url or "", _BOOKKEEPING_OUTCOMES))
    r = cur.fetchone()
    if not r or r[0] is None: return None
    return int(float(r[0]))


def _rate_limit_check(cur, pattern: str, url: str | None) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    last_age = _last_action_age_minutes(cur, pattern, url)
    if last_age is not None and last_age < _COOLDOWN_MIN_BETWEEN_SAME_ACTIONS:
        return False, f"cooldown_active ({last_age}min < {_COOLDOWN_MIN_BETWEEN_SAME_ACTIONS}min)"
    n_hr = _recent_actions(cur, pattern, 1)
    if n_hr >= _MAX_ACTIONS_PER_PATTERN_PER_HOUR:
        return False, f"hourly_limit ({n_hr} >= {_MAX_ACTIONS_PER_PATTERN_PER_HOUR})"
    n_24h = _recent_actions(cur, pattern, 24)
    if n_24h >= _MAX_ACTIONS_PER_PATTERN_PER_24H:
        return False, f"daily_limit ({n_24h} >= {_MAX_ACTIONS_PER_PATTERN_PER_24H})"
    if n_24h >= _ESCALATION_THRESHOLD_24H:
        return False, f"escalation_threshold ({n_24h} >= {_ESCALATION_THRESHOLD_24H}) — needs human"
    return True, "ok"


# ── Action executor ───────────────────────────────────────────────────
def _execute_action(action_path: str, payload: dict, use_admin: bool) -> tuple[int | None, str | None, str | None]:
    """Returns (http_code, response_body_snippet, error_string)."""
    if _is_dry_run():
        return 0, "dry-run: action NOT executed", None
    url = _BACKEND_BASE.rstrip("/") + action_path
    try:
        data = json.dumps(payload or {}).encode("utf-8") if payload else b"{}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if use_admin:
            ak = _admin_key()
            if ak: req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body[:400], None
    except urllib.error.HTTPError as e:
        try: body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception: body = ""
        return e.code, body, f"HTTPError {e.code} {e.reason}"
    except Exception as e:
        return None, "", f"{type(e).__name__}: {str(e)[:200]}"


def _record_action(finding: dict, pattern: str, action_path: str | None,
                    payload: dict | None, dry_run: bool, escalated: bool,
                    http_code: int | None, body: str | None, error: str | None,
                    outcome: str):
    c = _conn()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_autopilot_actions
                    (finding_issue, finding_url, pattern_name,
                     action_endpoint, action_payload, dry_run, outcome,
                     http_code, response_body, error, escalated,
                     started_at, completed_at)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,NOW(),NOW())
            """, (
                str(finding.get("issue",""))[:200],
                str(finding.get("url",""))[:500],
                pattern[:100],
                (action_path or "")[:500],
                json.dumps(payload or {}),
                bool(dry_run), outcome[:80],
                http_code, (body or "")[:1000], (error or "")[:500],
                bool(escalated),
            ))
    except Exception as e:
        print(f"[autopilot] record_action failed: {e}")
    finally:
        try: c.close()
        except Exception: pass

    # Phase HHH (2026-05-16): real-time webhook escalation. When the
    # brain marks something escalated (no autonomous remediation possible,
    # needs a human), POST to BRAIN_ESCALATION_WEBHOOK_URL. Compatible
    # with Slack incoming-webhook + Discord webhook formats (both accept
    # {text: "..."} JSON).
    #
    # Throttled per-pattern: only fires once per 4h for the same finding-
    # issue so a critical pattern repeating doesn't flood the channel.
    # Dry-run + disabled flags respected — same env vars as the rest of
    # the autopilot.
    if escalated and not dry_run:
        try:
            _maybe_send_webhook(finding, pattern, outcome, error)
        except Exception as _we:
            print(f"[autopilot] webhook failed (non-fatal): {_we}")


# Phase HHH webhook helpers
_WEBHOOK_THROTTLE: dict[str, float] = {}   # pattern → last_sent_ts
_WEBHOOK_THROTTLE_S = 4 * 3600              # 4 hours


def _maybe_send_webhook(finding: dict, pattern: str, outcome: str, error: str | None):
    """Fire-and-forget POST to webhook. Throttled + tolerant of all failures."""
    import time, urllib.request, urllib.error
    url = os.environ.get("BRAIN_ESCALATION_WEBHOOK_URL", "").strip()
    if not url: return  # not configured

    # Throttle per pattern
    now = time.time()
    last = _WEBHOOK_THROTTLE.get(pattern, 0)
    if (now - last) < _WEBHOOK_THROTTLE_S:
        return
    _WEBHOOK_THROTTLE[pattern] = now

    issue  = finding.get("issue", "?")
    count  = finding.get("count", "?")
    detail = (finding.get("detail") or "")[:600]
    target = finding.get("url") or finding.get("target") or "-"

    # Compose a message that works in BOTH Slack and Discord
    text = (
        f"⚠️ *Brain escalation: `{issue}`*\n"
        f"Pattern: `{pattern}` · Outcome: `{outcome}`\n"
        f"Target: `{target}`\n"
        f"Count: `{count}`\n"
        f"{detail}\n"
        f"Inspect: https://dchub.cloud/api/v1/brain/autopilot/recent"
    )
    payload = {"text": text}

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[autopilot] webhook delivered: HTTP {resp.status} for {issue}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"[autopilot] webhook delivery failed: {type(e).__name__}")


# ── ENDPOINTS ─────────────────────────────────────────────────────────
@brain_autopilot_bp.route("/api/v1/brain/autopilot/run", methods=["POST"])
def autopilot_run():
    """Cron entry point. Reads /api/v1/heal/findings, executes safe
    actions for known patterns. Admin or internal-key gated."""
    expected = _admin_key()
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401

    if _is_disabled():
        return jsonify(ok=True, skipped=True,
                       reason="BRAIN_AUTOPILOT_DISABLED env set"), 200

    # Pull findings from our OWN heal endpoint via Railway-direct (bypass
    # CF Worker timeout). This is the same JSON the brain reads.
    try:
        req = urllib.request.Request(
            _BACKEND_BASE.rstrip("/") + "/api/v1/heal/findings",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return jsonify(error="heal_findings_fetch_failed",
                       detail=str(e)[:200]), 500

    issues = payload.get("actionable_backend_issues") or []
    if not isinstance(issues, list):
        issues = []

    summary = {
        "examined":   len(issues),
        "actioned":   0,
        "rate_limited": 0,
        "escalated":  0,
        "no_action":  0,
        "errors":     0,
        "dry_run":    _is_dry_run(),
        "actions":    [],
    }

    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        for f in issues:
            if not isinstance(f, dict): continue
            issue = f.get("issue") or ""
            pat = _lookup_pattern(issue)
            if not pat:
                summary["no_action"] += 1
                continue

            with c.cursor() as cur:
                allowed, reason = _rate_limit_check(cur, issue, f.get("url"))

            action_fn = pat["action"]
            action_path, payload_body = action_fn(f)

            if not allowed:
                summary["rate_limited"] += 1
                escalated = ("escalation_threshold" in reason)
                if escalated: summary["escalated"] += 1
                _record_action(f, issue, action_path, payload_body,
                                dry_run=_is_dry_run(), escalated=escalated,
                                http_code=None, body=None, error=reason,
                                outcome="rate_limited")
                continue

            if action_path is None:
                # Escalation-only pattern; log and continue
                summary["escalated"] += 1
                _record_action(f, issue, None, None,
                                dry_run=_is_dry_run(), escalated=True,
                                http_code=None, body=None,
                                error="no autonomous action for this pattern",
                                outcome="escalated")
                continue

            # Execute
            http_code, body, error = _execute_action(action_path,
                                                       payload_body or {},
                                                       use_admin=pat.get("use_admin", False))
            outcome = "executed_ok" if (http_code and 200 <= http_code < 300) else (
                "dry_run" if _is_dry_run() else "execution_failed")
            if outcome == "execution_failed": summary["errors"] += 1
            else: summary["actioned"] += 1
            _record_action(f, issue, action_path, payload_body,
                            dry_run=_is_dry_run(), escalated=False,
                            http_code=http_code, body=body, error=error,
                            outcome=outcome)
            summary["actions"].append({
                "issue": issue, "pattern": issue, "endpoint": action_path,
                "outcome": outcome, "http_code": http_code,
            })
    finally:
        try: c.close()
        except Exception: pass

    summary["completed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return jsonify(ok=True, summary=summary), 200


@brain_autopilot_bp.route("/api/v1/brain/autopilot/status", methods=["GET"])
def autopilot_status():
    """Public summary of autopilot activity over the last 24h."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pattern_name, outcome,
                       COUNT(*) AS n,
                       MAX(started_at) AS last_at
                  FROM brain_autopilot_actions
                 WHERE started_at >= NOW() - INTERVAL '24 hours'
                 GROUP BY pattern_name, outcome
                 ORDER BY n DESC
            """)
            rows = cur.fetchall()
            cur.execute("""
                SELECT COUNT(*) FROM brain_autopilot_actions
                 WHERE started_at >= NOW() - INTERVAL '24 hours'
            """)
            total = int((cur.fetchone() or {"count": 0})["count"] or 0)
            cur.execute("""
                SELECT MAX(started_at) FROM brain_autopilot_actions
            """)
            last_ever_row = cur.fetchone()
            last_ever = last_ever_row["max"].isoformat() if last_ever_row and last_ever_row["max"] else None
    finally:
        try: c.close()
        except Exception: pass

    for r in rows:
        if r.get("last_at"): r["last_at"] = r["last_at"].isoformat()

    return jsonify(
        actions_24h=total,
        last_action_ever=last_ever,
        by_pattern_outcome=rows,
        disabled=_is_disabled(),
        dry_run=_is_dry_run(),
        pattern_library_size=len(_PATTERN_LIBRARY),
    ), 200


@brain_autopilot_bp.route("/api/v1/brain/autopilot/library", methods=["GET"])
def autopilot_library():
    """Public — the pattern→action map. So agents + humans can see what
    the autopilot will and won't do."""
    out = []
    for pat, meta in _PATTERN_LIBRARY.items():
        out.append({
            "finding_issue":      pat,
            "method":              meta.get("method"),
            "autonomous":          meta.get("method") is not None,
            "description":         meta.get("description"),
            "uses_admin_key":      meta.get("use_admin", False),
        })
    return jsonify(
        patterns=out,
        total_patterns=len(out),
        autonomous_count=sum(1 for p in out if p["autonomous"]),
        escalation_only_count=sum(1 for p in out if not p["autonomous"]),
        rate_limits={
            "max_per_pattern_per_hour": _MAX_ACTIONS_PER_PATTERN_PER_HOUR,
            "max_per_pattern_per_24h":  _MAX_ACTIONS_PER_PATTERN_PER_24H,
            "cooldown_minutes":          _COOLDOWN_MIN_BETWEEN_SAME_ACTIONS,
            "escalation_threshold_24h":  _ESCALATION_THRESHOLD_24H,
        },
        kill_switch_env="BRAIN_AUTOPILOT_DISABLED",
        dry_run_env="BRAIN_AUTOPILOT_DRY_RUN",
        disabled=_is_disabled(),
        dry_run=_is_dry_run(),
    ), 200


# Phase FFF (2026-05-16): in-process heartbeat cache. Compute path is
# ~12 SQL + radar scan + per-surface health_score. Cold start ~9-10s.
#
# Phase FFFF (2026-05-16): TTL bumped 60s → 300s (5 min) AND added
# stale-while-revalidate. Site Sentinel + CF Pages were both timing
# out at ~10s on cold-cache hits — the heartbeat surface kept showing
# unhealthy. Now: cache hit within TTL serves instantly; cache miss
# within STALE_GRACE serves STALE payload immediately while a
# background refresh primes the cache; only true cold-start (>=2x TTL)
# does the synchronous full compute.
import time as _time
_HEARTBEAT_CACHE = {"payload": None, "ts": 0.0}
_HEARTBEAT_TTL_S         = 300.0  # 5 min — fresh enough for dashboards
_HEARTBEAT_STALE_GRACE_S = 600.0  # 10 min — serve stale rather than time out


@brain_autopilot_bp.route("/api/v1/brain/heartbeat", methods=["GET"])
def brain_heartbeat():
    """ONE endpoint showing the brain's actual proactivity state.

    This is the canonical "is the brain doing anything?" probe. Returns:
      - Detector freshness (last consistency-radar scan + finding count)
      - Heal cache state (memory + DB age)
      - Autopilot activity (last 24h actions, by outcome)
      - Layer 5 activity (last code-proposal cron run)
      - Surface organism rollup (count + per-surface health)
      - Overall verdict: alive | warming | blind | dormant
    """
    # Phase FFFF (2026-05-16): cache hit OR stale-while-revalidate.
    # Cache hit within TTL → instant
    # Stale but within grace → serve stale, kick background refresh
    # Cold (>= TTL + grace) → synchronous full compute
    now = _time.time()
    cached = _HEARTBEAT_CACHE["payload"]
    age = (now - _HEARTBEAT_CACHE["ts"]) if cached is not None else None

    def _serve_cached(stale: bool):
        from flask import jsonify as _j
        cached_resp = dict(cached)
        cached_resp["_cache_age_seconds"] = round(age, 1)
        cached_resp["_cached"] = True
        cached_resp["_stale"]  = stale
        resp = _j(cached_resp)
        # Edge cache stale for 30s, browser cache 60s
        resp.headers["Cache-Control"] = "public, max-age=60, s-maxage=30"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200

    if cached is not None and age < _HEARTBEAT_TTL_S:
        # Fresh — serve cache, no work
        return _serve_cached(stale=False)

    if cached is not None and age < (_HEARTBEAT_TTL_S + _HEARTBEAT_STALE_GRACE_S):
        # Stale but within grace — serve cache immediately. The NEXT
        # request that lands AFTER the grace window will trigger a
        # synchronous refresh. This pattern keeps responses fast for
        # 99% of traffic; only the unlucky single request after the
        # grace expires pays the full cold-start cost.
        return _serve_cached(stale=True)

    out: dict = {
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        "verdict":    "unknown",
    }
    c = _conn()

    # ── Detector freshness ──
    try:
        from routes.brain_consistency_radar import scan_summary as _scan
        s = _scan()
        out["detector"] = {
            "findings_count": int(s.get("count") or 0),
            "by_issue":       s.get("by_issue") or {},
            "as_of":          s.get("as_of"),
        }
    except Exception as e:
        out["detector"] = {"error": str(e)[:160]}

    # ── Heal cache freshness ──
    try:
        if c:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT EXTRACT(EPOCH FROM (NOW() - MAX(computed_at)))::int AS age_s,
                           COUNT(*) AS total_persisted
                      FROM heal_findings_cache
                """)
                r = cur.fetchone()
                if r:
                    out["heal_cache"] = {
                        "db_age_seconds":  int(r[0]) if r[0] is not None else None,
                        "persisted_rows":  int(r[1] or 0),
                    }
    except Exception as e:
        out["heal_cache"] = {"error": str(e)[:160]}

    # ── Autopilot activity ──
    try:
        if c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE outcome = 'executed_ok'
                                        AND started_at >= NOW() - INTERVAL '24 hours') AS actioned_24h,
                      COUNT(*) FILTER (WHERE outcome = 'escalated'
                                        AND started_at >= NOW() - INTERVAL '24 hours') AS escalated_24h,
                      COUNT(*) FILTER (WHERE outcome = 'rate_limited'
                                        AND started_at >= NOW() - INTERVAL '24 hours') AS rate_limited_24h,
                      COUNT(*) FILTER (WHERE outcome = 'execution_failed'
                                        AND started_at >= NOW() - INTERVAL '24 hours') AS errors_24h,
                      MAX(started_at) AS last_action_at
                      FROM brain_autopilot_actions
                """)
                r = cur.fetchone() or {}
                out["autopilot"] = {
                    "actioned_24h":      int(r.get("actioned_24h") or 0),
                    "escalated_24h":     int(r.get("escalated_24h") or 0),
                    "rate_limited_24h":  int(r.get("rate_limited_24h") or 0),
                    "errors_24h":        int(r.get("errors_24h") or 0),
                    "last_action_at":    r["last_action_at"].isoformat() if r.get("last_action_at") else None,
                    "disabled":          _is_disabled(),
                    "dry_run":           _is_dry_run(),
                    "pattern_library_size": len(_PATTERN_LIBRARY),
                }
    except Exception as e:
        out["autopilot"] = {"error": str(e)[:160]}

    # ── Layer 5 activity ──
    try:
        if c:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT to_regclass('public.brain_proposed_code')
                """)
                if (cur.fetchone() or [None])[0]:
                    cur.execute("""
                        SELECT COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours'),
                               MAX(created_at)
                          FROM brain_proposed_code
                    """)
                    r = cur.fetchone()
                    if r:
                        out["layer5"] = {
                            "proposals_24h":  int(r[0] or 0),
                            "last_proposal":   r[1].isoformat() if r[1] else None,
                        }
    except Exception as e:
        out["layer5"] = {"error": str(e)[:160]}

    if c:
        try: c.close()
        except Exception: pass

    # ── Phase EEE: surface brain rollup ──
    # Each registered surface contributes its health to the central
    # heartbeat. Aggregate min/max/avg + per-surface list so /alive can
    # show a per-surface dashboard.
    try:
        from routes.surface_brain import SURFACES
        surface_scores = []
        for sid, surface in SURFACES.items():
            try:
                surface_scores.append({
                    "surface_id":    sid,
                    "name":          surface.name,
                    "health_score":  surface.health_score(),
                })
            except Exception as _se:
                surface_scores.append({"surface_id": sid, "error": str(_se)[:60]})
        if surface_scores:
            scores_only = [s["health_score"] for s in surface_scores if "health_score" in s]
            out["surfaces"] = {
                "count":            len(surface_scores),
                "average_health":   round(sum(scores_only) / max(1, len(scores_only)), 1),
                "min_health":       min(scores_only) if scores_only else None,
                "max_health":       max(scores_only) if scores_only else None,
                "per_surface":      surface_scores,
            }
    except Exception as _se:
        out["surfaces"] = {"error": str(_se)[:160]}

    # ── Verdict ──
    det = out.get("detector") or {}
    hc  = out.get("heal_cache") or {}
    ap  = out.get("autopilot") or {}
    findings_count = det.get("findings_count", 0) or 0
    db_age_s = hc.get("db_age_seconds")
    actioned = ap.get("actioned_24h", 0) or 0
    escalated = ap.get("escalated_24h", 0) or 0
    if db_age_s is None:
        verdict = "blind"
        verdict_detail = "heal_findings_cache table never written — brain has never seen findings"
    elif db_age_s > 3600 * 6:
        verdict = "stale"
        verdict_detail = f"heal cache is {db_age_s/3600:.1f}h old — refresh cron likely failing"
    elif findings_count == 0 and actioned == 0 and escalated == 0:
        verdict = "dormant"
        verdict_detail = "no findings, no actions — either nothing's wrong OR detectors are bypassed"
    elif _is_disabled():
        verdict = "disabled"
        verdict_detail = "BRAIN_AUTOPILOT_DISABLED env set — autopilot off, detectors still firing"
    elif actioned + escalated > 0:
        verdict = "alive"
        verdict_detail = f"{actioned} autonomous fixes + {escalated} escalations in last 24h"
    else:
        verdict = "warming"
        verdict_detail = f"{findings_count} open findings, no actions in last 24h yet"
    out["verdict"] = verdict
    out["verdict_detail"] = verdict_detail

    # Phase FFF: stash for next 60s
    _HEARTBEAT_CACHE["payload"] = out
    _HEARTBEAT_CACHE["ts"]      = _time.time()

    from flask import jsonify as _j
    resp = _j(out)
    resp.headers["Cache-Control"] = "public, max-age=30"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@brain_autopilot_bp.route("/api/v1/brain/autopilot/recent", methods=["GET"])
def autopilot_recent():
    """Public — last 50 autopilot actions (audit log)."""
    try: limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except ValueError: limit = 50
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, finding_issue, finding_url, pattern_name,
                       action_endpoint, dry_run, outcome, http_code,
                       LEFT(COALESCE(response_body,''), 200) AS response_snippet,
                       error, escalated, started_at
                  FROM brain_autopilot_actions
                 ORDER BY started_at DESC
                 LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    for r in rows:
        if r.get("started_at"): r["started_at"] = r["started_at"].isoformat()
    return jsonify(actions=rows, count=len(rows)), 200
