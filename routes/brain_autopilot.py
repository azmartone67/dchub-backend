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
            # r33-P (2026-05-21) — outcome verification columns. The
            # brain has been firing 322 rate-limited actions because
            # the same patterns refire when underlying issues don't
            # actually resolve. These columns let the verifier cron
            # mark each action verified/failed AFTER it runs, so
            # brain "volume" score reflects ACTUAL fixes, not
            # attempts.
            for stmt in (
                "ALTER TABLE brain_autopilot_actions ADD COLUMN IF NOT EXISTS outcome_verified BOOLEAN",
                "ALTER TABLE brain_autopilot_actions ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ",
                "ALTER TABLE brain_autopilot_actions ADD COLUMN IF NOT EXISTS verification_detail TEXT",
                "CREATE INDEX IF NOT EXISTS ix_autopilot_unverified ON brain_autopilot_actions(started_at DESC) WHERE outcome = 'executed_ok' AND outcome_verified IS NULL",
            ):
                try: cur.execute(stmt)
                except Exception: pass  # ALTER on missing column is fine on first boot
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
    # r33-stale-recovery (2026-05-21): added every refresh endpoint
    # we actually have, including discovery crawls and heal cycle.
    # User caught the gap: facilities table 17d stale because
    # discovered_facilities was marked "escalate — depends on external
    # cron" but we have admin endpoints that trigger OSM + DCM crawls
    # directly. Same for heal_cache (the self_heal scheduler). When
    # Railway flaps and crons miss windows, this autopilot path
    # recovers them autonomously.
    REFRESH_MAP = {
        "ai_citations":          "/api/v1/ai-citations/run-cron",
        "dcpi_scores":           "/api/v1/dcpi/recompute",
        "market_power_scores":   "/api/v1/dcpi/recompute",
        "discovered_facilities": "/api/v1/admin/osm-crawl/run",
        "facilities":            "/api/v1/admin/osm-crawl/run",
        # DCM crawler also seeds facilities — usable when OSM hits
        # rate limit or stalls. Picked as fallback by detector via
        # multiple-finding emission.
        "facilities_dcm":        "/api/v1/admin/dcm-crawl/run",
        "press_releases":        "/api/v1/marketing/auto-generate",
        "monthly_trend":         "/api/v1/reports/monthly/archive",
        # Heal cache refresh — kicks the self_heal scheduler once
        # (idempotent in the heal_cycle implementation).
        "heal_cache":            "/api/v1/heal/run-cycle",
        # r33-D (2026-05-21) — infrastructure layer. These tables
        # back the Land & Power map (50K+ rows each). Each endpoint
        # spawns a daemon thread that runs the HIFLD/EIA loader; the
        # endpoint returns 202 immediately so the autopilot doesn't
        # block on a 60-240s refresh. Status pollable via
        # /api/jobs/infra-refresh-status.
        "transmission_lines":    "/api/jobs/transmission-refresh",
        "gas_pipelines":         "/api/jobs/gas-refresh",
        "gas_compressors":       "/api/jobs/gas-refresh",
        "gas_processings":       "/api/jobs/gas-refresh",
        "substations":           "/api/jobs/substations-refresh",
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


def _action_founding_welcome_rescue(finding: dict) -> tuple[str | None, dict | None]:
    """Phase FF+25-followup-r21 (2026-05-20): autonomous rescue when
    a founding customer was tagged but the welcome email never fired
    (deploy lag, Resend hiccup, etc.). Reads the _email field the
    detector attaches and POSTs the send-welcome endpoint."""
    email = finding.get("_email") or ""
    if not email or "@" not in email:
        return (None, None)
    return (
        "/api/v1/admin/founding-customers/send-welcome",
        {"email": email},
    )


def _action_inspector_l22_handoff(finding: dict) -> tuple[str | None, dict | None]:
    """Phase r32-brain-pipe (2026-05-20). Closes the missing pipe between
    the Inspector's RECIPE candidates and L22 auto-code drafting.

    Reads the brief_id from the detector's finding, POSTs the existing
    /api/v1/brain/brief/<id>/draft-prs admin endpoint. L22 then applies
    its own 3-recipe safety whitelist (route_alias_404, schema_drift_
    guard, cron_if_mismatched) plus the _already_drafted() idempotency
    check. So even if this autopilot fires aggressively, L22 enforces
    the actual mutation safety.

    Effect: brain Inspector identifies a code-level fix, drafts it as
    a RECIPE candidate in the daily brief, this autopilot picks it up
    from the detector, hands it to L22, L22 drafts a GitHub PR, human
    reviews + merges. End-to-end autonomous code change."""
    brief_id = finding.get("_brief_id")
    if not brief_id:
        return (None, None)
    return (
        f"/api/v1/brain/brief/{brief_id}/draft-prs",
        {"trigger": "consistency_radar"},
    )


def _action_pocket_alert_announce(finding: dict) -> tuple[str | None, dict | None]:
    """Phase r28 (2026-05-20). When check_pocket_high_mover fires (a
    market moved ≥15pts on the excess-power index in 7 days), the
    autonomous response is to *announce* it — convert the signal into
    a one-paragraph press/social post and queue it for auto-publish.

    Reads the detector's _market_name / _delta_7d / _iso / _state /
    _verdict fields, drafts a sentence, and posts to the existing
    /api/v1/marketing/queue-pocket-alert endpoint. That endpoint
    (created alongside this) writes to social_post_queue and is
    drained by the existing LinkedIn/X auto-publish cron.

    Why this matters: pre-r28, a +20pt move in Phoenix would land in
    /digest but no one outside daily-brief subscribers would know.
    Now the same signal flows to LinkedIn/X within hours, building
    the "DC Hub spots shifts first" reputation that drives signups.

    Rate-limit/cooldown still applies via the standard autopilot
    machinery — same market won't auto-announce twice in the
    cooldown window."""
    market = finding.get("_market_name") or ""
    iso = finding.get("_iso") or ""
    state = finding.get("_state") or ""
    delta = finding.get("_delta_7d")
    score = finding.get("_score")
    verdict = finding.get("_verdict") or "HOLD"
    slug = finding.get("_market_slug") or ""

    if not market or delta is None:
        return (None, None)

    sign = "+" if delta > 0 else ""
    direction = "accelerating" if delta > 0 else "decelerating"
    # Phase r31 (2026-05-20): link directly to the new /pockets/<slug>
    # detail page rather than the /pockets ranking — gives the social
    # post a sharable canonical URL with schema.org markup, og tags,
    # and the 30d trend chart already rendered.
    body = (
        f"{market} ({iso or 'no ISO'}, {state or 'no state'}) is {direction} "
        f"on DC Hub's excess-power index — {sign}{delta:.1f} pts over the last "
        f"7 days, now at {score:.1f}. Verdict: {verdict}. "
        f"https://dchub.cloud/pockets/{slug}"
    )

    return (
        "/api/v1/marketing/queue-pocket-alert",
        {
            "market_slug":  slug,
            "market_name":  market,
            "iso":          iso,
            "state":        state,
            "delta_7d":     delta,
            "verdict":      verdict,
            "body":         body[:500],  # social platforms cap at ~500-1000
        },
    )


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


# ──────────────────────────────────────────────────────────────────
# Phase r33-F (2026-05-21) — three auto-action upgrades that replace
# the corresponding escalation-only lambdas.
#
#  6. _action_404_spike_add_redirect: find a high-confidence target
#     for the 404'd URL via a sitemap+route similarity check; if
#     found, POST /api/v1/admin/route-redirect/add. Caps at 5/24h
#     (enforced server-side). Falls back to escalation if no
#     confident target.
#
#  7. _action_stripe_webhook_replay: POST /api/stripe/webhook/replay
#     which uses STRIPE_SECRET_KEY to enumerate events since the
#     last webhook receipt and queue them for re-processing. No-op
#     if STRIPE_SECRET_KEY env is missing.
#
#  8. _action_neon_replication_paging: differentiated severity ladder.
#     count == -1 (unreachable)         → POST critical alert
#     count == -2 (URL points at primary) → POST high alert
#     count > 60 (lag only)              → return (None, None) — wait
# ──────────────────────────────────────────────────────────────────


def _find_similar_route(missing_path: str) -> tuple[str, float] | None:
    """Find the most-similar existing route for a 404'd path.

    Strategy:
      1. Read a hand-curated list of known top-level routes
         (cheap, deterministic, no DB hit).
      2. Score each by Levenshtein distance + prefix match.
      3. Return best match if normalized similarity ≥ 0.85.

    Returns (target_path, confidence) or None."""
    import difflib as _dl
    KNOWN_ROUTES = [
        "/", "/pricing", "/markets", "/dcpi", "/pockets",
        "/coverage", "/api", "/developers", "/docs", "/sitemap.xml",
        "/dc-hub-media", "/digest", "/brain", "/brain-live",
        "/brain/brief", "/admin-health", "/state-of-the-data-center",
        "/founders", "/heartbeat", "/status", "/signup", "/login",
        "/devrel-targets", "/visitor-intelligence", "/paywall-test",
        "/grid", "/land-power", "/reports/monthly", "/coverage",
        "/api/v1/energy/summary", "/api/v1/dcpi/recompute",
        "/api/v1/heal/run-cycle", "/api/v1/marketing/auto-generate",
    ]
    src = (missing_path or "").rstrip("/").lower()
    if not src or src == "/":
        return None
    best, best_score = None, 0.0
    for r in KNOWN_ROUTES:
        score = _dl.SequenceMatcher(None, src, r.lower()).ratio()
        if score > best_score:
            best_score, best = score, r
    if best and best_score >= 0.85 and best != missing_path:
        return (best, round(best_score, 3))
    return None


def _action_404_spike_add_redirect(finding: dict) -> tuple[str | None, dict | None]:
    """If the 404'd URL has a strong (Levenshtein ≥ 0.85) match in
    the known-routes list, register a 301 redirect autonomously.
    Otherwise escalate."""
    src = (finding.get("url") or "").strip()
    if not src or not src.startswith("/"):
        return None, None
    match = _find_similar_route(src)
    if not match:
        return None, None  # No confident target → escalate
    target, confidence = match
    return "/api/v1/admin/route-redirect/add", {
        "from":        src,
        "to":          target,
        "confidence":  confidence,
        "created_by":  "autopilot",
        "status_code": 301,
        "source_finding_count": finding.get("count"),
    }


def _action_stripe_webhook_replay(finding: dict) -> tuple[str | None, dict | None]:
    """Trigger the replay endpoint. No body required — it derives
    the `since` window from MAX(received_at) on the stripe_webhooks
    tables. Endpoint no-ops if STRIPE_SECRET_KEY env is missing,
    which gets logged as escalation in the audit table."""
    return "/api/stripe/webhook/replay", {
        "source": "autopilot_stripe_replay",
        "lag_hours": finding.get("count"),
    }


def _action_neon_replication_paging(finding: dict) -> tuple[str | None, dict | None]:
    """Differentiated severity based on the failure mode embedded
    in finding.count (set by check_neon_replication_lag):

       count = -1  → replica unreachable (critical: paging)
       count = -2  → URL pointing at primary (high: misconfig)
       count > 60  → just lagging (escalate only, no page)
    """
    count = finding.get("count")
    try:
        count = int(count)
    except Exception:
        return None, None  # Malformed finding → escalate
    if count == -1:
        return "/api/v1/brain/alerts/critical", {
            "severity":     "critical",
            "issue":        finding.get("issue") or "neon_replication_lag",
            "finding_url":  finding.get("url"),
            "detail":       finding.get("detail", "")[:1500],
            "source":       "autopilot_neon_paging",
        }
    if count == -2:
        return "/api/v1/brain/alerts/critical", {
            "severity":     "high",
            "issue":        finding.get("issue") or "neon_replication_lag",
            "finding_url":  finding.get("url"),
            "detail":       finding.get("detail", "")[:1500],
            "source":       "autopilot_neon_paging",
        }
    # Lag-only — escalate via the normal finding path
    return None, None


def _action_render_restart(finding: dict) -> tuple[str | None, dict | None]:
    """Phase r33-C (2026-05-21) — autonomous Render restart.

    Pair to check_render_flapping detector. When the detector fires
    (≥2/3 probes failed), this action hits Render's deploy hook to
    force a fresh container, clearing the stale-DB-connection /
    pipeline-blocked / pool-leak state classes that all manifest as
    flap. Render's deploy hook is idempotent (no-op if already
    deploying) so safe to fire even on a flapping container.

    Rate-limited by the brain's _is_in_cooldown machinery — won't
    re-fire within 30min, so we don't bounce-loop Render even if the
    underlying issue is persistent. Long-term fix (e.g. add
    pipeline minutes, fix Neon DSN) still requires human action."""
    import os as _os
    hook = (_os.environ.get("RENDER_DEPLOY_HOOK_URL")
            or _os.environ.get("RENDER_DEPLOY_HOOK"))
    if not hook:
        # No deploy hook configured → escalate. The operator gets
        # the finding but no autonomous fire-and-forget happens.
        return None, None
    # Hook is a complete URL; we POST with no body. Authority is
    # baked into the URL path (Render's hook tokens are URL-secret).
    return hook, {"source": "autopilot_render_restart",
                  "fails": finding.get("count", 0)}


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
    # Phase FF+25-followup-r21 (2026-05-20): founding-customer welcome rescue.
    # If a founding customer was tagged but contact_status stays 'new' or
    # 'auto-tagged' for >1h, fire the send-welcome endpoint autonomously.
    # Closes the deploy-lag window that left Kevin without an email tonight.
    "founding_customer_not_welcomed": {
        "action":      _action_founding_welcome_rescue,
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous: a founding customer was tagged but never received their welcome email after 1 hour. Brain fires /api/v1/admin/founding-customers/send-welcome with the specific email. Rate-limit prevents flooding if Resend is down.",
    },
    # Phase r28 (2026-05-20): pocket-of-power high-mover announcement.
    # When a tracked market shifts ≥15pts on the excess-power index in
    # 7 days, autopilot drafts a one-paragraph announcement and queues
    # it for social auto-publish. Turns the DCPI signal into a public
    # narrative without human intervention.
    "pocket_high_mover": {
        "action":      _action_pocket_alert_announce,
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous: a tracked market moved ≥15pts on the excess-power index in 7 days. Brain drafts a one-paragraph announcement and queues it for social auto-publish via /api/v1/marketing/queue-pocket-alert. Rate-limit + cooldown prevent same-market re-announcement within the window.",
    },
    # Phase r32-brain-pipe (2026-05-20): Inspector → L22 auto-PR handoff.
    # The missing pipe that lets brain actually ship code, not just
    # propose it. Three-deep safety: rate-limit + L22 3-recipe whitelist
    # + L22 idempotency. End-to-end: Inspector identifies → autopilot
    # hands off → L22 drafts PR → human reviews + merges.
    "inspector_l22_handoff": {
        "action":      _action_inspector_l22_handoff,
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous: Inspector brief contains RECIPE candidates that haven't been promoted to L22 auto-PR drafting. Brain POSTs /api/v1/brain/brief/<id>/draft-prs. L22's 3-recipe safety whitelist (route_alias_404, schema_drift_guard, cron_if_mismatched) decides whether to draft a PR; L22's _already_drafted() prevents duplicate work. Closes the long-standing gap where Inspector proposed code fixes that nothing executed on.",
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
    # Phase r33-K (2026-05-21) — brand-uniformity audit across all top
    # public pages. The fix is a per-page HTML edit (add <link> or
    # replace off-brand colors / body-font declarations) so this is
    # escalation-only — no API endpoint can apply the patch.
    "page_brand_uniformity": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: page is missing brand.css/Instrument Sans/dchub-nav.js OR has off-brand colors. Edit the page (static .html in dchub-frontend or Flask handler in dchub-backend) to add the missing link and replace off-brand patterns. Audit /api/v1/brain/consistency-radar?issue=page_brand_uniformity for the list.",
    },
    # Phase r33-N (2026-05-21) — outbound discovery health. The
    # daily mcp-outreach.yml cron submits us to 7 MCP registries
    # (Smithery, mcp.so, MCPHub, PulseMCP, Glama, awesome-mcp-servers,
    # Anthropic). Fires if any registry is stale, "not_listed", or
    # has never been audited.
    "outbound_distribution_health": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: an MCP-registry listing is missing OR the daily outreach cron hasn't run in 48h. See /api/v1/admin/outreach/mcp-registry/status. Fix: open the PR/form at the registry's manual_url, OR check GH Actions for the mcp-outreach.yml workflow status.",
    },
    # Phase FF+25-followup-r14 (2026-05-20): coverage gap escalation.
    # User reported DCHawk has Calgary-metro facilities we don't. Fix
    # is upstream in discovery crawler — not an autopilot action.
    "coverage_gap_canada": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: Canadian facility footprint is below the industry baseline. Discovery crawler missing CA sources. Patch immediate gaps via POST /api/v1/admin/facilities/bulk; long-term fix is to add a Canadian source to crawler_scheduler.py.",
    },
    "coverage_gap_alberta": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: Alberta footprint thin. Known builds in Pincher Creek + Strathmore + Calgary metro need to be added. POST /api/v1/admin/facilities/bulk to patch.",
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
    # Phase VVV (UPGRADED r33-P 2026-05-21): schema drift now auto-
    # fires /api/v1/admin/schema/repair, which is idempotent + covers
    # the known drift cases (worker_versions table, press_releases
    # .published_at column, brain_findings, brain_critical_alerts,
    # route_redirects). The repair endpoint uses IF NOT EXISTS so
    # repeated firing is safe — the bound `outcome_verified` cron
    # will catch any drift the canned statements don't cover.
    "schema_drift_column_missing": {
        "action":      lambda f: ("/api/v1/admin/schema/repair",
                                   {"source": "autopilot_schema_drift",
                                    "trigger": (f.get("url") or "")[:120]}),
        "method":      "POST",
        "use_admin":   True,
        "description": "Auto-recovery: hits the idempotent schema-repair endpoint which CREATEs the missing tables/columns using IF NOT EXISTS. Outcome verifier confirms next scan whether the column/table is back. Escalates if repair endpoint returns failure or finding still present after verification.",
    },
    "schema_drift_table_missing": {
        "action":      lambda f: ("/api/v1/admin/schema/repair",
                                   {"source": "autopilot_schema_drift",
                                    "trigger": (f.get("url") or "")[:120]}),
        "method":      "POST",
        "use_admin":   True,
        "description": "Auto-recovery: same repair endpoint as schema_drift_column_missing — creates the missing tables (worker_versions, brain_findings, brain_critical_alerts, route_redirects, stripe_webhook_replay_log) if absent.",
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
    # r42u (2026-05-26): wired to /api/v1/heal/run-cycle. The healer's
    # generic 30-min cycle picks up missing-JSON-LD on tracked pages.
    # Won't add JSON-LD where none exists yet (that requires per-page
    # code), but DOES re-trigger the pages that have it but didn't
    # render it (cache invalidation, render-error recovery). Better
    # than the previous (None, None) which was just dead bookkeeping.
    "schema_org_coverage_low": {
        "action":      lambda f: ("/api/v1/heal/run-cycle", {"source": "autopilot:schema_org_low"}),
        "method":      "POST",
        "use_admin":   True,
        "description": "Autonomous: kick /api/v1/heal/run-cycle to re-render pages whose JSON-LD render failed. Won't synthesize new JSON-LD (needs code), but recovers transient renders. Worklist: /api/v1/schema-org/missing.",
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
    # Phase r33-B (2026-05-21) — three platform-health patterns. Two
    # escalate (CF Pages + Render pipeline both need a human at the
    # dashboard), one stays informational (slow_request just surfaces
    # the SLOW REQUEST aggregate — the fix is per-handler code).
    "cf_pages_deploy_stuck": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: CF Pages worker version hasn't bumped despite recent _worker.js commits. Likely a failed deploy stuck in the queue. Fix: CF dashboard → Pages → dchub-frontend → Deployments → cancel the failed build, then push an empty commit to retrigger.",
    },
    "slow_request_ratio": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Informational: path X had ≥5 SLOW REQUEST (>30s) events in the last hour. Audit the handler for sequential HTTP calls, unbounded queries, or sync wait on slow upstream APIs. This is the failure class that triggers gunicorn worker timeout → restart loop.",
    },
    "render_pipeline_blocked": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: Render is >2h behind the latest dchub-backend commit. Likely pipeline-minutes-blocked. Fix: Render dashboard → Settings → Billing → add pipeline minutes, OR manually trigger a deploy from the Deploys tab.",
    },
    # Phase r33-C (2026-05-21) — Render flap auto-restart. AUTO-FIRES
    # the Render deploy hook when probes fail. Rate-limited via
    # _is_in_cooldown so we can't bounce-loop Render.
    "render_flapping": {
        "action":      _action_render_restart,
        "method":      "POST",
        "use_admin":   False,
        "description": "Auto-recovery: ≥2/3 probes against Render's /api/v1/version failed. Action POSTs the Render deploy hook (RENDER_DEPLOY_HOOK_URL env) to force a fresh container. 30-min cooldown prevents bounce loops. Escalates if hook env var is missing.",
    },
    # ──────────────────────────────────────────────────────────────
    # Phase r33-E (2026-05-21) — QA monitor master shell patterns.
    # All 5 are escalation-only because the fixes are code-level
    # (deploy revert, replica reconfigure, funnel step audit, slow-
    # detector refactor, Stripe webhook endpoint reset). They surface
    # the signal so a human or L22 auto-code can act. Worth adding
    # auto-actions later for the deterministic ones.
    # ──────────────────────────────────────────────────────────────
    # Phase r33-F upgrade: auto-fire when a high-confidence redirect
    # target exists. Falls back to escalation if no confident match.
    # Server-side 24h cap of 5 autopilot redirects prevents runaway.
    "404_spike": {
        "action":      _action_404_spike_add_redirect,
        "method":      "POST",
        "use_admin":   True,
        "description": "Auto-recovery: if the 404'd URL has a Levenshtein ≥0.85 match against the known-routes list, register a 301 via /api/v1/admin/route-redirect/add (capped at 5/24h server-side). If no confident target, escalates so a human can audit the deploy regression.",
    },
    # Phase r33-F upgrade: differentiated escalation by failure mode.
    # count=-1 (unreachable) → critical alert; count=-2 (URL points
    # at primary) → high alert; count>60 (just lagging) → escalate.
    "neon_replication_lag": {
        "action":      _action_neon_replication_paging,
        "method":      "POST",
        "use_admin":   True,
        "description": "Differentiated paging: unreachable replica → critical alert; URL misconfigured at primary → high alert; lag >60s → escalation-only (waits one cycle for natural recovery). All write to brain_critical_alerts and optionally fan out to Slack via BRAIN_ALERT_WEBHOOK_URL.",
    },
    "signup_drop_off_step": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a signup funnel step dropped ≥30% day-over-day. The finding.url is `funnel:<step>` — start from the page that owns that step. Audit: form validation, paywall copy, JS errors in Plausible, recent deploys that touched signup/onboarding routes.",
    },
    "detector_runtime_slow": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a single detector took >15s on the last scan. This is the failure pattern that caused this session's /grid 112s cascade. Audit the named detector for sequential HTTP probes (parallelize), unbounded SQL (add LIMIT or move to a cron), or external API calls without per-call timeout (add 5s ceiling).",
    },
    # Phase r33-F upgrade: auto-fire Stripe replay endpoint which
    # enumerates events since MAX(received_at) and queues them for
    # re-processing. No-op if STRIPE_SECRET_KEY is missing (then
    # the endpoint returns 503 and the autopilot logs it as
    # escalation in the audit table).
    "stripe_webhook_lag": {
        "action":      _action_stripe_webhook_replay,
        "method":      "POST",
        "use_admin":   True,
        "description": "Auto-recovery: POSTs /api/stripe/webhook/replay which uses STRIPE_SECRET_KEY to enumerate events since MAX(received_at) and queue them in stripe_webhook_replay_log for re-processing. Falls back to escalation if STRIPE_SECRET_KEY is missing or Stripe API returns 5xx.",
    },
    # ──────────────────────────────────────────────────────────────
    # Phase r33-F (2026-05-21) — 5 new detector patterns. All
    # escalation-only because their fixes are out-of-band (config,
    # billing, schema, customer outreach, gunicorn restart).
    # ──────────────────────────────────────────────────────────────
    "canonical_redirect_loop": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a top-level page redirects to itself OR to a 404. Audit the rule that owns it (frontend _redirects, _worker.js, or Flask handler). The finding.url is the source; finding.detail explains where it lands.",
    },
    "gunicorn_worker_age": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: the current gunicorn worker has been alive >24h. Memory-drift class — add or fix --max-requests=1000 --max-requests-jitter=100 in the Procfile to enable worker recycling. Restart frees accumulated psycopg2 cursors and per-process caches.",
    },
    "facility_dedupe_collision": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: facilities sharing name + lat/lng at 4 decimal places but with different IDs. POST /api/v1/admin/facilities/merge with the canonical ID + dupe IDs from finding.detail. Downstream aggregations are double-counting until merged.",
    },
    "paid_user_zero_value": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a paid customer has not called any paid MCP tool in 14+ days. Pre-churn signal. Trigger: lost-conversion outreach via /api/v1/admin/lost-conversion/send, personalized welcome-back via the upgrade pool, or sales follow-up.",
    },
    "cf_kv_namespace_pressure": {
        "action":      lambda f: (None, None),
        "method":      None,
        "use_admin":   False,
        "description": "Escalation-only: a CF KV namespace has ≥5000 keys. Cache stampede class — audit writes to the namespace for missing expirationTtl. KV is unlimited but key-count growth past 5K usually signals a write-leak.",
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
        # r42t (2026-05-26): identify ourselves so the rate-limiter
        # bypasses us. Pre-fix every autopilot action was 429'd by the
        # IP/UA limiter, even with valid X-Admin-Key, because the
        # limiter didn't check the admin header. Defense in depth:
        # both X-DC-Probe AND X-Admin-Key now bypass rate-limit.
        req.add_header("X-DC-Probe", "autopilot")
        req.add_header("User-Agent", "dchub-autopilot/1.0 (brain-recovery)")
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
                VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,NOW() ON CONFLICT DO NOTHING,NOW())
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
    #
    # r33-H+auth (2026-05-21): include X-Admin-Key so the outbound fetch
    # bypasses our own IP-based rate limiter. Previous runs were getting
    # HTTP 429 ("heal_findings_fetch_failed") because the GH-Actions
    # runner IP hit the anonymous rate ceiling after 5 triggers in 30min,
    # which caused examined=0 cascades on every subsequent run.
    _outbound_admin = _admin_key()
    try:
        req = urllib.request.Request(
            _BACKEND_BASE.rstrip("/") + "/api/v1/heal/findings",
            method="GET",
        )
        if _outbound_admin:
            req.add_header("X-Admin-Key", _outbound_admin)
        # r33-J round 4 (2026-05-21): bump timeout 20s → 60s. The
        # /heal/findings endpoint has a deferred-cache path (Phase
        # GG) that can take 30-50s when the in-memory cache is cold
        # AND no DB row exists yet. Previous 20s timeout was killing
        # legitimate slow responses with 'read operation timed out'.
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # Don't fail the whole run — fall through with empty issues
        # and let the in-process radar bridge below contribute findings.
        payload = {"actionable_backend_issues": []}
        _heal_fetch_error = f"{type(e).__name__}: {str(e)[:120]}"
    else:
        _heal_fetch_error = None

    issues = payload.get("actionable_backend_issues") or []
    if not isinstance(issues, list):
        issues = []

    # Phase r33-H bridge (2026-05-21) — also pull consistency_radar
    # findings. That feed is where ALL of the r33 detectors land
    # (data_freshness_sla_breach, render_flapping, neon_replication_lag,
    # 404_spike, signup_drop_off_step, etc — 14 new patterns this
    # session). They weren't reaching /heal/findings's
    # actionable_backend_issues, so the autopilot couldn't see them.
    # Now we merge both feeds into a single issue list — deduped by
    # (issue, url) tuple so we don't double-fire when the same
    # finding shows up on both sides.
    #
    # r33-O Wave A (2026-05-21) — DB-backed bridge. Replaces the
    # in-process scan_summary/scan_all calls that have been silently
    # returning empty findings across Railway worker restarts. The
    # consistency_radar now persists findings to brain_findings on
    # every scan completion. Autopilot reads from there — worker-
    # independent, no cache divergence, no lock dance.
    #
    # Fallback chain: brain_findings DB read → if 0 rows, scan_all()
    # in-process (kicks a fresh scan + populates the table for next
    # time) → if still 0, give up + escalate via summary.
    radar_findings: list[dict] = []
    summary_bridge_error = None
    try:
        import os as _os_pb, psycopg2 as _pg_pb
        import psycopg2.extras as _pg_extras_pb
        _db_url = _os_pb.environ.get("DATABASE_URL")
        if _db_url:
            _conn_pb = _pg_pb.connect(
                _db_url, sslmode="require", connect_timeout=5)
            try:
                with _conn_pb.cursor(
                        cursor_factory=_pg_extras_pb.RealDictCursor) as cur:
                    # Only read findings seen in the last 10 minutes —
                    # anything older is stale (radar prunes at 10min too).
                    cur.execute("""
                        SELECT issue, url, count, detail, last_seen
                          FROM brain_findings
                         WHERE last_seen > NOW() - INTERVAL '10 minutes'
                         ORDER BY last_seen DESC
                         LIMIT 200
                    """)
                    rows = cur.fetchall()
                    radar_findings = [
                        {"issue":  r["issue"],
                         "url":    r["url"],
                         "count":  r["count"],
                         "detail": r["detail"] or ""}
                        for r in rows
                    ]
            finally:
                _conn_pb.close()
    except Exception as e:
        summary_bridge_error = f"db_read: {type(e).__name__}: {str(e)[:80]}"

    # Fallback: if DB returned 0 rows, scan_all in-process. Slow but
    # populates brain_findings for the next run, so future runs hit
    # the DB fast path.
    if not radar_findings:
        try:
            from routes.brain_consistency_radar import (
                scan_all as _radar_scan_all,
            )
            radar_findings = _radar_scan_all() or []
        except Exception as e:
            summary_bridge_error = (
                f"scan_all_fallback: {type(e).__name__}: {str(e)[:80]}")
    if radar_findings:
        seen = {(i.get("issue") or "", i.get("url") or "")
                for i in issues if isinstance(i, dict)}
        for f in radar_findings:
            if not isinstance(f, dict): continue
            key = (f.get("issue") or "", f.get("url") or "")
            if key in seen: continue
            seen.add(key)
            issues.append(f)

    summary = {
        "examined":           len(issues),
        "from_heal_findings": len(payload.get("actionable_backend_issues") or []),
        "from_radar_bridge":  len(radar_findings),
        "actioned":           0,
        "rate_limited":       0,
        "escalated":          0,
        "no_action":          0,
        "errors":             0,
        "dry_run":            _is_dry_run(),
        "actions":            [],
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

            action_fn = pat["action"]
            action_path, payload_body = action_fn(f)

            # r42u (2026-05-26): short-circuit escalation-only patterns
            # BEFORE the rate-limit check. Pre-fix, page_brand_uniformity
            # (action=(None,None)) was racking up "rate_limited" log
            # entries each cycle once it had been escalated 5x — every
            # subsequent cycle the rate-limit check tripped and logged
            # a fresh "escalation_threshold" rate_limited row, plus the
            # detector kept re-firing because the finding hadn't been
            # cleared. The escalated state should be terminal until a
            # human resolves it, not a perpetual retry loop.
            if action_path is None:
                summary["escalated"] += 1
                # r43-fix#4 (2026-05-30): the dominant escalation path
                # (~900/day) recorded a brain_autopilot_actions row but never
                # emitted a brain_notifications row, so escalations never
                # surfaced in the notification feed. Emit one — but ONLY on
                # the FIRST escalation of a given (issue, url), so a chronic
                # unresolved finding doesn't re-notify every cycle. Detection
                # + write are both fully guarded; failure NEVER affects the
                # action loop. Done BEFORE _record_action so the "first" check
                # doesn't see the row we're about to write.
                try:
                    _first_escalation = True
                    with c.cursor() as _ecur:
                        _ecur.execute("""
                            SELECT 1 FROM brain_autopilot_actions
                             WHERE pattern_name = %s
                               AND COALESCE(finding_url,'') = %s
                               AND outcome = 'escalated'
                             LIMIT 1
                        """, (issue, f.get("url") or ""))
                        _first_escalation = _ecur.fetchone() is None
                    if _first_escalation:
                        from routes.brain_evolution import log_notification as _logn
                        _logn(
                            kind="autopilot_escalation",
                            summary=f"Escalated finding '{issue}' to human — no autonomous action for this pattern",
                            detail={"issue": issue,
                                    "endpoint": None,
                                    "reason": "no autonomous action for this pattern"},
                            url=f.get("url"),
                            severity="warn",
                        )
                except Exception:
                    try: c.rollback()
                    except Exception: pass
                _record_action(f, issue, None, None,
                                dry_run=_is_dry_run(), escalated=True,
                                http_code=None, body=None,
                                error="no autonomous action for this pattern",
                                outcome="escalated")
                continue

            with c.cursor() as cur:
                allowed, reason = _rate_limit_check(cur, issue, f.get("url"))

            if not allowed:
                summary["rate_limited"] += 1
                escalated = ("escalation_threshold" in reason)
                if escalated: summary["escalated"] += 1
                _record_action(f, issue, action_path, payload_body,
                                dry_run=_is_dry_run(), escalated=escalated,
                                http_code=None, body=None, error=reason,
                                outcome="rate_limited")
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
            # Phase r60-evolution: emit a brain_notifications row + tagged
            # stderr line so the operator FEELS the brain working. Fire-and-
            # forget; failure to log here never affects the action loop.
            try:
                from routes.brain_evolution import log_notification as _logn
                if outcome == "executed_ok":
                    _logn(
                        kind="autopilot_action",
                        summary=f"Auto-fixed finding '{issue}' via {action_path}",
                        detail={"issue": issue, "http_code": http_code,
                                "endpoint": action_path},
                        url=f.get("url"),
                        severity="win",
                    )
                elif outcome == "execution_failed":
                    _logn(
                        kind="autopilot_action_failed",
                        summary=f"Autopilot tried '{issue}' but {action_path} returned {http_code}",
                        detail={"issue": issue, "http_code": http_code,
                                "endpoint": action_path,
                                "error": (error or "")[:200]},
                        url=f.get("url"),
                        severity="warn",
                    )
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass

    summary["completed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    if summary_bridge_error:
        summary["radar_bridge_error"] = summary_bridge_error
    if _heal_fetch_error:
        summary["heal_fetch_error"] = _heal_fetch_error
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
# r42af (2026-05-27): operator caught /intelligence dashboard repeatedly
# showing "WARMING" because cache had gone cold (>15 min). Widening:
# fresh 30 min, stale-grace 4 hours. Background recompute still fires
# on first read past TTL — stale data served meanwhile. With a brain
# tier dashboard polling every 60s, the cache should effectively never
# expire during business hours, and the worst-case experience is "data
# is 4h old" instead of "data unavailable, retry".
_HEARTBEAT_TTL_S         = 1800.0  # 30 min — fresh enough for dashboards
_HEARTBEAT_STALE_GRACE_S = 14400.0 # 4h — serve stale rather than show warming
# Phase ZZZZZ-round32 (2026-05-24): single-flight lock for cold-start
# compute so concurrent cold requests don't pile up identical scans.
_HEARTBEAT_COMPUTING = {"in_progress": False}


# Phase ZZZZZ-round8 (2026-05-23): /api/v1/brain/heartbeat-alt is the
# sibling-path workaround for when the zone-level worker
# (4.34.6-oauth-404) returns a 503 "Backend unreachable" for the
# canonical path. Tested: same Flask handler, but the alt-path URL is
# NOT shadowed by the OOB worker's pattern-matching, so it reaches
# Railway directly. Same workaround we used for /api/v1/mcp/manifest
# and /api/v1/ai-agents.json (see reference_dchub_prod_alias_pin.md).
@brain_autopilot_bp.route("/api/v1/brain/heartbeat-alt", methods=["GET"])
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

    # Phase ZZZZZ-round32 (2026-05-24): cold-start used to compute
    # synchronously here — but scan_summary() probes 40+ URLs and can
    # take 15-30s, which exceeds the Cloudflare Worker timeout → user
    # gets 503. After 15 min of no traffic this happened on every cold
    # request, masking the real brain health and triggering false alerts
    # in the radar/heartbeat-stale detector (it probes itself).
    # Fix: kick the full compute to a daemon thread so the FIRST cold
    # request returns immediately with a "warming" payload. Subsequent
    # requests within 5 min get the freshly-computed cache. Worst case
    # is one user sees "warming" status for ~30s — they don't 503.
    import threading as _threading
    if not _HEARTBEAT_COMPUTING["in_progress"]:
        _HEARTBEAT_COMPUTING["in_progress"] = True
        _threading.Thread(target=_compute_heartbeat_async, daemon=True).start()
    from flask import jsonify as _j2
    warming = {
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        "verdict":    "warming",
        "verdict_detail": "Brain heartbeat cache is cold (15+ min since last fetch). Computing in background. Retry in ~30s for full status.",
        "_cache_age_seconds": None,
        "_cached": False,
        "_warming": True,
    }
    resp = _j2(warming)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Retry-After"] = "30"
    # 2026-05-29: was HTTP 202 — semantically correct but sentinel + uptime
    # monitors + the brain self-probe all classify non-200 as "unhealthy",
    # so a 15-min idle window briefly turned the brain-heartbeat row red
    # while the cache warmed. The "warming" verdict in the JSON body is
    # the right signal for code that cares; HTTP 200 keeps dumb pollers
    # green. (Genuine outages still surface via the verdict field +
    # _cached/_warming booleans, not the status code.)
    return resp, 200

def _compute_heartbeat_async():
    """Run the full heartbeat compute in a daemon thread. Writes result
    to _HEARTBEAT_CACHE. Released the in-progress flag on completion
    or error so subsequent cold-starts can re-trigger."""
    try:
        _compute_heartbeat_sync()
    except Exception as _e:
        # Don't write to cache on failure — next request retries
        try:
            import sys as _s
            print(f"[brain_heartbeat] async compute crashed: {_e}",
                  file=_s.stderr, flush=True)
        except Exception:
            pass
    finally:
        _HEARTBEAT_COMPUTING["in_progress"] = False

def _compute_heartbeat_sync():
    """The original synchronous compute body. Refactored out of
    brain_heartbeat() so it can be called from the async refresh path."""
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

    # ── Phase r60-evolution (2026-05-29): evolution sub-block ──
    # Merge a compact slice of the evolution snapshot into heartbeat so
    # existing dashboard consumers see "how is brain getting better
    # over time?" without any wiring change. Full payload + per-window
    # detail lives at /api/v1/brain/evolution. Fail-soft: if the
    # evolution module is missing or its compute raises, we just omit
    # the sub-block.
    try:
        from routes.brain_evolution import compute_evolution_snapshot
        ev = compute_evolution_snapshot()
        if ev and ev.get("ok"):
            out["evolution"] = {
                "score":                     ev.get("evolution_score"),
                "verdict":                   ev.get("verdict"),
                "findings_resolved_7d":      ev.get("findings_resolved_7d"),
                "autonomous_actions_24h":    ev.get("autonomous_actions_24h"),
                "decisions_taken_30d":       ev.get("decisions_taken_30d"),
                "layer_5_proposals_30d":     ev.get("layer_5_proposals_30d"),
                "top_3_resolved_finding_types":
                    ev.get("top_3_resolved_finding_types"),
            }
    except Exception as _ee:
        # Don't let evolution failure break the heartbeat
        out["evolution"] = {"error": str(_ee)[:160]}

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


# ──────────────────────────────────────────────────────────────────
# Phase r33-P (2026-05-21) — OUTCOME VERIFICATION.
#
# THE problem: brain fires 322 rate-limited actions in 7d because the
# same patterns re-fire when underlying issues don't actually resolve.
# Brain "volume" score = 0/4 because no successful outcomes recorded.
#
# THE fix: every executed action gets re-verified 5 minutes later by
# checking if the SAME finding still exists in brain_findings.
#   - Finding GONE → outcome verified. Brain volume increments.
#   - Finding STILL THERE → outcome failed. Pattern + url blacklisted
#     for 24h (cooldown machinery already handles this once we set
#     a marker outcome).
#
# This turns the brain from "documenting problems" into "measuring
# whether its own fixes worked."
# ──────────────────────────────────────────────────────────────────


@brain_autopilot_bp.route("/api/v1/brain/autopilot/verify",
                           methods=["POST"])
def autopilot_verify():
    """Run a verification pass: for each executed_ok action with
    outcome_verified IS NULL AND started_at < NOW() - 5min, check
    whether the (issue, url) is still present in brain_findings.

    If GONE      → outcome_verified=true,  volume++
    If PRESENT   → outcome_verified=false, mark for re-escalation
    If too young → skip (let next pass handle it)

    Admin or internal key gated. Called by a GH-Actions cron every
    5min via mcp-outreach-style workflow."""
    expected = _admin_key()
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key"))
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401

    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503

    summary = {
        "scanned": 0,
        "verified_ok": 0,
        "verified_failed": 0,
        "skipped_young": 0,
        "errors": 0,
    }
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get all unverified executed actions older than 5 min, capped at 50
            cur.execute("""
                SELECT id, finding_issue, finding_url, pattern_name,
                       started_at
                  FROM brain_autopilot_actions
                 WHERE outcome = 'executed_ok'
                   AND outcome_verified IS NULL
                   AND started_at < NOW() - INTERVAL '5 minutes'
                   AND started_at > NOW() - INTERVAL '24 hours'
                 ORDER BY started_at DESC
                 LIMIT 50
            """)
            unverified = cur.fetchall()
            summary["scanned"] = len(unverified)

            for action in unverified:
                issue = action["finding_issue"] or ""
                url   = action["finding_url"] or ""
                action_id = action["id"]
                try:
                    # Check brain_findings — is this finding still there?
                    cur.execute("""
                        SELECT last_seen FROM brain_findings
                         WHERE issue = %s AND url = %s
                           AND last_seen > NOW() - INTERVAL '10 minutes'
                         LIMIT 1
                    """, (issue, url))
                    still_present = cur.fetchone() is not None

                    if still_present:
                        # Action didn't fix it — verification failed
                        cur.execute("""
                            UPDATE brain_autopilot_actions
                               SET outcome_verified = FALSE,
                                   verified_at = NOW(),
                                   verification_detail = %s
                             WHERE id = %s
                        """, (f"Finding ({issue}, {url}) still present in "
                              f"brain_findings — action did not resolve it.",
                              action_id))
                        summary["verified_failed"] += 1
                    else:
                        # Finding is GONE — action worked!
                        cur.execute("""
                            UPDATE brain_autopilot_actions
                               SET outcome_verified = TRUE,
                                   verified_at = NOW(),
                                   verification_detail = %s
                             WHERE id = %s
                        """, (f"Finding ({issue}, {url}) no longer in "
                              f"brain_findings — action resolved it.",
                              action_id))
                        summary["verified_ok"] += 1
                except Exception as e:
                    summary["errors"] += 1
                    logger.warning("verify action %s: %s", action_id, e)
        c.commit()
    finally:
        try: c.close()
        except Exception: pass

    summary["completed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return jsonify(ok=True, summary=summary), 200


@brain_autopilot_bp.route("/api/v1/brain/autopilot/verification-status",
                           methods=["GET"])
def autopilot_verification_status():
    """Public read — per-pattern verification stats. Powers the brain
    volume / fix-success scores on /alive + /system-status."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pattern_name,
                       COUNT(*) FILTER (WHERE outcome_verified = TRUE) AS verified_ok,
                       COUNT(*) FILTER (WHERE outcome_verified = FALSE) AS verified_failed,
                       COUNT(*) FILTER (WHERE outcome_verified IS NULL
                                          AND outcome = 'executed_ok'
                                          AND started_at > NOW() - INTERVAL '24 hours') AS pending,
                       COUNT(*) FILTER (WHERE outcome = 'executed_ok'
                                          AND started_at > NOW() - INTERVAL '24 hours') AS fired_24h
                  FROM brain_autopilot_actions
                 WHERE started_at > NOW() - INTERVAL '7 days'
                 GROUP BY pattern_name
                 ORDER BY fired_24h DESC NULLS LAST, verified_ok DESC
                 LIMIT 30
            """)
            rows = cur.fetchall()
            # Aggregate fix success rate
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE outcome_verified = TRUE) AS verified_ok,
                  COUNT(*) FILTER (WHERE outcome_verified = FALSE) AS verified_failed,
                  COUNT(*) FILTER (WHERE outcome_verified IS NULL
                                     AND outcome = 'executed_ok'
                                     AND started_at > NOW() - INTERVAL '24 hours') AS pending
                  FROM brain_autopilot_actions
                 WHERE started_at > NOW() - INTERVAL '7 days'
            """)
            totals = cur.fetchone() or {}
    finally:
        try: c.close()
        except Exception: pass

    total = (totals.get("verified_ok") or 0) + (totals.get("verified_failed") or 0)
    fix_success_rate = (
        round((totals.get("verified_ok") or 0) / total * 100, 1)
        if total > 0 else None
    )

    return jsonify({
        "ok": True,
        "totals_7d": dict(totals),
        "fix_success_rate_pct": fix_success_rate,
        "by_pattern": [dict(r) for r in rows],
    }), 200
