"""Phase FF+25-followup-r9 (2026-05-20) — Brain Inspector (Opus 4.7).
==========================================================================

The user's vision: "i feel like the brain should become the inspector of
everything proactively improve systems, squash bugs reactively, and come
to the rescue when errors arise."

The L21 autopilot already covers reactive (squash + rescue). The missing
piece is PROACTIVE — reading everything, synthesizing across signals,
and writing a coherent narrative that humans + the system itself can act
on. That's this module.

THE INSPECTOR LOOP

Every BRAIN_INSPECTOR_INTERVAL_HOURS (default 6), the Inspector:
  1. Reads from 10+ surfaces:
     - Last 24h autopilot actions (pattern, outcome, target)
     - Last 24h consistency-radar findings
     - Site Sentinel top-10 unhealthy surfaces
     - MCP funnel (calls 7d, real-traffic split, conversions)
     - Press cadence (last 5 publishes + age)
     - Brain pulse (last_action + 24h count)
     - Source-of-truth + citation score trend
     - Sponsorship queue state
     - Monthly outreach status
     - Worker version drift
  2. Calls Opus 4.7 with a system prompt that defines the Inspector
     persona: dry, evidence-first, no hype, never invents numbers.
     Asks for structured Markdown.
  3. Persists the response to brain_briefs table.
  4. Surfaces the latest brief at /api/v1/brain/brief/latest +
     /brain/brief (HTML page).

SAFETY:
  · Inspector NEVER mutates state on its own. It produces text.
  · Findings + would-do recommendations feed the existing autopilot
    (which DOES rate-limit + cooldown like before).
  · Every claim must cite which surface it came from. The system
    prompt forbids invented numbers.
  · If ANTHROPIC_API_KEY is unset, all endpoints return 503 with a
    clear next-step hint.
  · The autonomous Inspector→L22 auto-fire (hook E) is LIVE by
    default (it fills the /brain/innovation proposal stream), but has
    a master kill-switch: set BRAIN_L22_AUTOPR_LIVE=0 to flip it to
    dry-run (records what L22 WOULD draft, opens no Issues/PRs).
    L22's own whitelist/diff-caps/dedup still apply; real PRs still
    additionally need DCHUB_L22_REAL_PR=1. See _l22_autofire_mode().

ENDPOINTS:
  GET  /api/v1/brain/brief/latest           public summary
  GET  /api/v1/brain/brief/<id>             specific brief
  GET  /api/v1/brain/brief/list             recent briefs
  POST /api/v1/brain/brief/generate         admin: trigger fresh
  GET  /brain/brief                         HTML rendering of latest
  GET  /api/v1/brain/models                 which model each tier uses
"""
import os
from internal_auth import accepted_internal_keys
import json
import logging
import datetime
from flask import Blueprint, jsonify, request, Response
from utils.anthropic_helper import anthropic_messages_url

logger = logging.getLogger(__name__)
brain_inspector_bp = Blueprint("brain_inspector", __name__)


# ── Auth ─────────────────────────────────────────────────────────────
_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    if not sent:
        return False
    if sent in _INTERNAL_KEYS:
        return True
    # r-admin-gate-clean (2026-08-08): route through the shared internal_auth
    # gate. is_valid_internal_key() _clean_key()s BOTH sides, so a key rotated /
    # re-pasted with trailing pollution (the 2026-08-08 rotation put a trailing
    # token on DCHUB_ADMIN_KEY) still matches — the raw-compare fallback below
    # does NOT clean the env side, so it 403'd the rotated key even though
    # layer5's raw==raw compare tolerated it. Same fix as
    # brain_mechanical_classifier._admin_ok; pinned by
    # tests/test_brain_admin_gate_parity.py.
    try:
        from internal_auth import require_internal_or_admin
        if require_internal_or_admin(request):
            return True
    except Exception:
        pass
    # r-admin-gate-fresh (2026-06-18): raw request-time re-read fallback for a
    # key set/rotated after import. Kept as an additional accept path (it reads
    # bare INTERNAL_KEY, which internal_auth does not) — acceptance only widens.
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v and sent == _v:
            return True
    return False


# r-brain-gate (2026-06-18): the brain brief/innovation surfaces were PUBLIC and
# leaked customer emails (incl. a .gov domain), revenue, and internal ops — this
# is an INTERNAL ops console, not a marketing page. Splash shown to anon viewers.
_ADMIN_ONLY_HTML = (
    "<!doctype html><meta charset=utf-8><title>DC Hub · internal</title>"
    "<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
    "background:#0a0a0a;color:#9a9a9a;display:flex;align-items:center;"
    "justify-content:center;height:90vh;text-align:center'>"
    "<div><h2 style='color:#e6e6e6;font-weight:300;letter-spacing:-.02em'>"
    "Internal console</h2><p>The DC Hub brain dashboard is admin-only.</p></div>")


@brain_inspector_bp.after_request
def _inspector_no_store(resp):
    # r-brain-gate (2026-06-18): brain briefs embed customer PII + revenue — never
    # let CF edge-cache any inspector response (an admin 200 would otherwise be
    # cached and served to anon).
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@brain_inspector_bp.route("/api/v1/brain/conversions-audit", methods=["GET"])
def conversions_audit():
    """Read-only honest-numbers audit of mcp_conversions (admin-gated).

    A REAL paid conversion has a Stripe customer id (cus_...). Rows with
    mrr_cents set but stripe_customer_id NULL are seeded/synthetic and must
    NOT count as conversions — that's the loophole in paid_conversions_7d's
    `OR mrr_cents>0`. Classifies the recent rows so we can see which named
    'conversions' (e.g. the $300K research-seed line) are actually Stripe."""
    if not _admin_ok():
        return jsonify({"error": "admin only"}), 403
    c = _get_db()
    if c is None:
        return jsonify({"error": "no db"}), 500
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT user_email, plan_to, mrr_cents,
                          stripe_customer_id, stripe_subscription_id,
                          source, created_at
                     FROM mcp_conversions
                    ORDER BY created_at DESC LIMIT 30""")
            cols = [d[0] for d in cur.description]
            raw = [row if isinstance(row, dict) else dict(zip(cols, row))
                   for row in cur.fetchall()]
        rows, real, seeded = [], 0, 0
        for r in raw:
            cust = r.get("stripe_customer_id")
            mrr = r.get("mrr_cents") or 0
            if cust:
                real += 1
                cls = "real_stripe"
            elif mrr > 0:
                seeded += 1
                cls = "seeded_mrr_only"
            else:
                cls = "no_value"
            rows.append({
                "email": r.get("user_email"),
                "plan_to": r.get("plan_to"),
                "mrr_cents": r.get("mrr_cents"),
                "has_stripe_customer": bool(cust),
                "has_stripe_sub": bool(r.get("stripe_subscription_id")),
                "source": r.get("source"),
                "created_at": str(r.get("created_at")),
                "classify": cls,
            })
        return jsonify({
            "rows": rows,
            "real_stripe_count": real,
            "seeded_mrr_only_count": seeded,
            "note": ("real_stripe = has stripe_customer_id (cus_...). "
                     "seeded_mrr_only = mrr set but NO Stripe charge → must "
                     "NOT count as a conversion."),
        })
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


# ── Autonomous L22 auto-fire gate ────────────────────────────────────
# The Inspector loop hands its code-fix candidates to L22 after every
# brief (hook E in _generate_brief). That arm is AUTONOMOUS — no human in
# the loop — but it has been PRODUCTIVE (it's what fills the
# /brain/innovation proposal stream), so it stays LIVE by default and
# gets a master kill-switch rather than an opt-in.
#
# default / BRAIN_L22_AUTOPR_LIVE != "0"  → live: hit /auto-code/run,
#     L22 may open review Issues (and real PRs when DCHUB_L22_REAL_PR=1).
# BRAIN_L22_AUTOPR_LIVE=0                  → dry-run KILL-SWITCH: hit
#     /auto-code/dry-run, which still parses + records every proposal to
#     brain_layer22_actions (state=dry_run) so /brain/innovation shows
#     what WOULD ship — but opens nothing.
#
# Either way L22's own safety still applies (whitelist, diff caps,
# forbidden-path list, 7d dedup; real PRs additionally need
# DCHUB_L22_REAL_PR=1). The human-triggered admin endpoints
# (brief_draft_prs, auto-code/run) are unaffected.
def _l22_autofire_mode() -> tuple[str, str]:
    """Return (mode, endpoint_path) for the autonomous Inspector→L22 fire.
    mode is "live" or "dry_run". Default is LIVE; BRAIN_L22_AUTOPR_LIVE=0
    is the kill-switch that flips it to dry-run."""
    off = os.environ.get("BRAIN_L22_AUTOPR_LIVE", "1").strip() == "0"
    if off:
        return ("dry_run", "/api/v1/brain/auto-code/dry-run")
    return ("live", "/api/v1/brain/auto-code/run")


# ── DB ───────────────────────────────────────────────────────────────
def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_brief_table():
    # FIX r10 (2026-05-20): the original version of this function ran
    # CREATE TABLE inside a cursor block but never called c.commit(),
    # so on connections that aren't in autocommit mode the DDL never
    # landed. Symptom: every brief generation reported
    # "relation brain_briefs does not exist". Explicit commit + a
    # NOT NULL relaxation on brief_md (so error-state rows can be
    # logged with the empty body for debugging).
    c = _get_db()
    if c is None: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_briefs (
                    id            SERIAL PRIMARY KEY,
                    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    model         TEXT NOT NULL,
                    inputs        JSONB,
                    brief_md      TEXT,
                    summary       TEXT,
                    healthy_count INT,
                    degrading_count INT,
                    attention_count INT,
                    tokens_in     INT,
                    tokens_out    INT,
                    duration_ms   INT,
                    error         TEXT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_brain_briefs_generated "
                "ON brain_briefs(generated_at DESC)"
            )
        try: c.commit()
        except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"[brain-inspector] table create failed: {e}")
        try: c.rollback()
        except Exception: pass
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── Signal gathering ─────────────────────────────────────────────────
def _gather_signals() -> dict:
    """Read the 10+ surfaces the Inspector reasons over. Each block is
    independently fault-tolerant — one bad query doesn't blank the
    brief."""
    out: dict = {
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
    }
    c = _get_db()
    if c is None:
        out["error"] = "no_database"
        return out

    def _try(label: str, sql: str, params=None, one=False):
        try:
            with c.cursor() as cur:
                cur.execute(sql, params)
                if one:
                    r = cur.fetchone()
                    out[label] = (list(r) if r else None)
                else:
                    out[label] = [list(r) for r in cur.fetchall()]
        except Exception as e:
            try: c.rollback()
            except Exception: pass
            out[f"{label}_error"] = str(e).split("\n")[0][:120]

    try:
        # Autopilot recent activity
        _try("autopilot_24h",
             """SELECT pattern_name, outcome, COUNT(*) AS n
                  FROM brain_autopilot_actions
                 WHERE started_at >= NOW() - INTERVAL '24 hours'
                 GROUP BY pattern_name, outcome
                 ORDER BY n DESC LIMIT 20""")
        # Consistency findings
        _try("consistency_findings_24h",
             """SELECT issue, COUNT(*) AS n FROM brain_findings
                 WHERE created_at >= NOW() - INTERVAL '24 hours'
                 GROUP BY issue ORDER BY n DESC LIMIT 15""")
        # MCP funnel
        # r85: include distinct_callers_7d (distinct caller IPs) so the brain
        # reasons on the REAL convertible base, not raw calls_7d. calls_7d is
        # dominated by a few looping anonymous callers (tens of distinct IPs
        # generate ~all the volume) — dividing conv by calls_7d manufactures a
        # fake "99% leak" that every brief used to panic about. NOTE: mcp_tool_calls
        # has NO api_key column (cols: tool_name/platform/client_name/ip_address/
        # session_id/...); ip_address is the right distinct-caller proxy (== the
        # funnel's unique_ips_7d_real).
        # r86f: distinct_callers_7d now counts EXTERNAL callers by client identity,
        # not ip_address. ip_address is the MCP proxy's egress IP (server.mjs),
        # so it collapsed to ~4 proxy IPs and was a useless denominator. calls_7d
        # is also dominated by internal self-traffic (platform='dchub-selfheal'
        # ~33k/wk), so external_calls_7d excludes internal platforms — that's the
        # honest demand number the funnel should reason on, not the gross.
        _try("mcp_funnel",
             """SELECT
                  (SELECT COUNT(*) FROM mcp_tool_calls
                    WHERE created_at >= NOW() - INTERVAL '7 days') AS calls_7d,
                  (SELECT COUNT(*) FROM mcp_tool_calls
                    WHERE created_at >= NOW() - INTERVAL '7 days'
                      AND COALESCE(LOWER(platform),'') NOT LIKE 'dchub-%'
                      AND COALESCE(LOWER(platform),'') NOT LIKE '%-probe'
                      AND COALESCE(LOWER(platform),'') NOT LIKE '%-test'
                      AND COALESCE(LOWER(platform),'') NOT LIKE 'sweep%'
                      AND COALESCE(LOWER(platform),'') NOT IN ('dchub-selfheal','mcp-probe','diag','')) AS external_calls_7d,
                  (SELECT COUNT(DISTINCT COALESCE(NULLIF(LOWER(client_name),'unknown'), platform)) FROM mcp_tool_calls
                    WHERE created_at >= NOW() - INTERVAL '7 days'
                      AND COALESCE(LOWER(platform),'') NOT LIKE 'dchub-%'
                      AND COALESCE(LOWER(platform),'') NOT LIKE '%-probe'
                      AND COALESCE(LOWER(platform),'') NOT LIKE '%-test'
                      AND COALESCE(LOWER(platform),'') NOT LIKE 'sweep%'
                      AND COALESCE(LOWER(platform),'') NOT IN ('dchub-selfheal','mcp-probe','diag','')) AS distinct_callers_7d,
                  (SELECT COUNT(*) FROM mcp_upgrade_signals
                    WHERE created_at >= NOW() - INTERVAL '7 days') AS signals_7d,
                  (SELECT COUNT(*) FROM mcp_conversions
                    WHERE created_at >= NOW() - INTERVAL '30 days') AS conv_30d
             """, one=True)
        # Press cadence
        _try("press_recent",
             """SELECT title, published_at FROM press_releases
                 WHERE published_at IS NOT NULL
                 ORDER BY published_at DESC LIMIT 5""")
        # Citation pulse
        _try("citation_score",
             """SELECT score_pct, score_date FROM citation_scores
                 ORDER BY score_date DESC LIMIT 1""", one=True)
        # Worker drift
        _try("worker_version",
             """SELECT version, observed_at FROM worker_versions
                 ORDER BY observed_at DESC LIMIT 1""", one=True)
        # Top facilities counts.
        # r48.1 (2026-05-27): query `discovered_facilities` (canonical,
        # 21,433 rows), NOT the legacy `facilities` table (12,907 rows).
        # The brain LLM consumes this signal and narrated "12,907
        # facilities tracked" in the inspector_brief which then surfaced
        # on /daily — making the page look stale because every other
        # page (homepage, /by-the-numbers, /about) shows 15,000+. Same
        # canonical-table trap that bit /daily before (memory:
        # dchub_daily_count_gap) and that we just fixed across the
        # frontend in r48.0.
        _try("facilities_total",
             """SELECT COUNT(*) FROM discovered_facilities""", one=True)
        # r-accuracy (2026-06-04): do NOT feed the legacy `facilities`
        # table count (12,907) to the LLM at all. It repeatedly narrated
        # that stale number ("12,907 facilities tracked") instead of the
        # canonical 21,433, which then leaked onto the public homepage.
        # discovered_facilities is the ONLY facility count the brain sees.
        # Phase r14 — facility coverage by country (catches gaps DCHawk
        # / dcByte have that we don't). If Canada/UK/Singapore counts
        # look thin compared to the public industry baseline, the
        # Inspector calls it out in the brief.
        # r80: LIMIT 12 hid mid-tier DC countries (SG=217, IE=70 both exist)
        # below the window, so the Inspector kept hallucinating "Singapore/
        # Ireland coverage gap". 40 surfaces every country with a real DC
        # presence so the brief stops crying false gaps.
        _try("facilities_by_country",
             """SELECT COALESCE(NULLIF(UPPER(country),''),'?') AS c,
                       COUNT(*) AS n
                  FROM discovered_facilities
                 GROUP BY UPPER(country)
                 ORDER BY n DESC LIMIT 40""")
        # Phase r14 — recent facility additions (gives the Inspector a
        # sense of whether discovery is alive). If we haven't added
        # anything in 7d, the pipeline likely needs a kick.
        # r33-O (2026-05-21): discovered_at column is TEXT not TIMESTAMP,
        # so the bare >= comparison errored on every brief with
        # "operator does not exist: text >= timestamp with time zone".
        # CAST to timestamptz, guard against empty strings + NULL.
        # r33-Q (2026-05-21, fix-2): the join `facilities.id IN (
        # SELECT id FROM discovered_facilities)` was a TEXT-vs-INTEGER
        # type mismatch (facilities.id=text, discovered_facilities.id=int)
        # and they're separate ID spaces anyway. The metric Inspector
        # actually wants is "how many new facilities surfaced in the
        # last 7 days" — count discovered_facilities directly.
        _try("facilities_added_7d",
             """SELECT COUNT(*) FROM discovered_facilities
                 WHERE COALESCE(discovered_at, '') != ''
                   AND discovered_at::timestamptz
                       >= NOW() - INTERVAL '7 days'""", one=True)
        # Most recent deal
        _try("deals_recent",
             """SELECT date, buyer, seller, value FROM deals
                 WHERE date IS NOT NULL ORDER BY date DESC LIMIT 5""")
        # Phase r19 — founding customers cohort. First-12 paid customers
        # matter disproportionately: they're proof the value-prop lands,
        # they become references, they tolerate rough edges. Surface
        # the count + the most recent so the Inspector can name them
        # in the Healthy section.
        _try("founding_customers",
             """SELECT email, tagged_at, plan_at_tag,
                       first_payment_at, contact_status
                  FROM founding_customers
                 ORDER BY tagged_at DESC LIMIT 12""")
        # Phase r19 — recent paid conversions (last 7 days). Inspector
        # uses this to detect "first paid conversion this week" or
        # "conversions accelerating" as positive trends.
        # r33-O (2026-05-21): api_keys has user_id but not email — that
        # lives on users(id). Previous query errored with
        # "column 'email' does not exist" every brief. Join users via
        # LEFT JOIN so api keys without a linked user still appear.
        # r33-Q (2026-05-21, fix-2): api_keys.created_at is TEXT not
        # TIMESTAMP (same trap as discovered_facilities.discovered_at).
        # Bare >= comparison errored "text >= timestamp with time zone".
        # CAST to timestamptz with empty-string guard.
        # r-honest-conv (2026-06-17): READ REAL PAYMENTS, not api_keys.plan.
        # The old query counted ANY api_keys row with a paid plan as a
        # "conversion" — so 9 SEEDED developer keys for partnerships@nvidia.com /
        # coreweave / lambda / groq / deepmind (planted in a 6-second batch on
        # 2026-06-16 with ZERO Stripe linkage) showed up as "9 developer-tier
        # conversions — a genuine inflection" in every brief. The honest source
        # is mcp_conversions WITH a real Stripe charge (stripe_customer_id) or
        # real MRR. Seeded/test/free-upgraded keys never have those, so they
        # can't fake an inflection anymore.
        # mrr_cents is CENTS — the brief LLM read the raw integer as dollars
        # (9900 -> "$9,900" instead of $99/mo; 300000 -> "$300K" instead of
        # $3,000/mo), a 100x over-statement that inflated every "inflection"
        # line. Hand it mrr_usd so the brief reports the true figure. (Audit
        # /api/v1/brain/conversions-audit shows 0 mrr-only rows today, so the
        # stripe-or-mrr filter isn't currently counting seeded rows.)
        _try("paid_conversions_7d",
             """SELECT user_email, plan_to, created_at,
                       ROUND(COALESCE(mrr_cents, 0) / 100.0, 2) AS mrr_usd
                  FROM mcp_conversions
                 WHERE created_at >= NOW() - INTERVAL '7 days'
                   AND (stripe_customer_id IS NOT NULL
                        OR COALESCE(mrr_cents, 0) > 0)
                 ORDER BY created_at DESC LIMIT 25""")
        # Phase r24 — news entity discovery candidates. Surfaces unknown
        # operator/facility names that appeared in recent news but
        # aren't in our facilities table yet. Inspector flags these as
        # Degrading items + recommends seeding via the bulk endpoint.
        # Excludes status='rejected' (operator already dismissed them).
        _try("news_unknown_entities",
             """SELECT entity_name, mention_count, sample_headline,
                       sample_url, last_seen_at
                  FROM news_discovered_entities
                 WHERE in_facilities = FALSE
                   AND COALESCE(status, 'unknown') != 'rejected'
                   AND last_seen_at >= NOW() - INTERVAL '14 days'
                 ORDER BY mention_count DESC, last_seen_at DESC
                 LIMIT 15""")
        # Phase r28 (2026-05-20) — Pocket-of-power 7-day movers. Surfaces
        # markets whose excess-power index shifted ≥10 points in the last
        # week. This is the autonomous product signal: when Phoenix
        # jumps +18, that's a *recommendation* the user should know about,
        # not buried in /digest. Inspector flags movers in the brief +
        # the autopilot can act on the same signal via
        # check_pocket_high_mover detector → _action_pocket_alert_announce.
        _try("pocket_movers_7d",
             """WITH latest AS (
                  SELECT DISTINCT ON (market_slug)
                         market_slug, market_name, iso, state, verdict,
                         excess_power_score AS now_e
                    FROM market_power_scores
                   ORDER BY market_slug, computed_at DESC
                ),
                week_ago AS (
                  SELECT DISTINCT ON (market_slug)
                         market_slug, excess_power_score AS prev_e
                    FROM market_power_scores
                   WHERE computed_at < NOW() - INTERVAL '7 days'
                   ORDER BY market_slug, computed_at DESC
                )
                SELECT l.market_name, l.iso, l.state, l.verdict,
                       l.now_e,
                       (l.now_e - w.prev_e) AS delta_7d
                  FROM latest l
                  JOIN week_ago w ON l.market_slug = w.market_slug
                 WHERE ABS(l.now_e - w.prev_e) >= 10
                 ORDER BY ABS(l.now_e - w.prev_e) DESC
                 LIMIT 8""")
    finally:
        try: c.close()
        except Exception: pass

    return out


# ── LLM call ─────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are the DC Hub Inspector — an autonomous senior infrastructure engineer reviewing the health of dchub.cloud.

Your job: read the signal block below and produce a single coherent Markdown brief. Your voice is dry, observational, evidence-first. Never overpromise, never invent numbers, never use exclamation marks or emojis.

You must:
  - Cite which signal each claim came from (e.g. "per autopilot_24h" or "per mcp_funnel" or "per facilities_by_country").
  - If a number isn't in the signal block, do NOT invent one. Say "not yet measured" or omit.
  - Mark each item with confidence: high / medium / low.
  - Suggest concrete next actions ONLY for items where the action is well-defined; otherwise mark as "needs human review".
  - Forecast what's likely to change in the next 24 hours, with explicit caveats.
  - If facilities_by_country shows any country with fewer than 50 facilities AND that country has a known active DC industry (Canada, UK, Germany, Singapore, Australia, Japan, France, Netherlands, Ireland), flag it as a "coverage gap" in Degrading with a specific recommendation.
  - If facilities_added_7d is 0, flag the discovery pipeline as Degrading — fresh additions are how we stay ahead of DCHawk + dcByte.
  - If founding_customers has any rows, NAME each customer by email (first 5) in the Healthy section with their tagged_at date. Founding customers matter disproportionately and the Inspector brief should make them visible.
  - If paid_conversions_7d has rows AND the previous brief mentioned zero conversions, flag this as a positive inflection in the One-line take.
  - MCP FUNNEL DENOMINATOR (critical): mcp_funnel.calls_7d is RAW tool-call volume, dominated by a handful of looping anonymous agents — tens of distinct callers generate nearly all of it. NEVER compute conversion as conv_30d ÷ calls_7d; that manufactures a fake "~99% leak" and is wrong. The honest convertible base is distinct_callers_7d plus the issued-key count, and free→paid on that base is typically a healthy single-to-double-digit %. Flag the MCP funnel as Degrading ONLY if paid_conversions_7d is genuinely zero/declining or distinct-caller conversion is low — never on a raw-volume ratio. If you previously called this a severe leak, correct course.
  - If news_unknown_entities has rows, list the top 3 in the Needs-attention section by name, with mention_count + sample_headline. Recommend that the operator review them at /admin/news-ner/candidates and either seed via /api/v1/admin/facilities/bulk or reject as noise via /api/v1/admin/news-ner/reject. These are operator/facility names appearing in news that we don't track yet — high-signal discovery candidates.
  - If pocket_movers_7d has rows, mention the top 1-2 movers BY NAME in the Healthy or Degrading section depending on direction. Format: "<market> (<iso>, <state>) <±delta> on the excess-power index this week — verdict <BUILD/HOLD/AVOID>." These are the most actionable market-level shifts and represent the autonomous DCPI surface. A +15 mover should be flagged as a "BUILD candidate accelerating"; a −15 mover as "constraint emerging."

Output the brief in this exact Markdown structure:

# DC Hub · Brain Brief · {timestamp}

**One-line take:** [a single sentence summarizing system state]

## Healthy
[bulleted list of things working as expected, each with the signal that proves it]

## Degrading
[bulleted list of things trending in the wrong direction, each with the signal + a confidence tag]

## Needs attention
[items that need human review or where autopilot action is uncertain]

## Would-do (autonomous recommendations)
[concrete actions the autopilot could take. Format each bullet as:
   - PATTERN: <pattern_name> · <one-line rationale>
 where <pattern_name> is from this list of known autopilot patterns:
   dcpi_partial_recompute, auto_press_market_repetition, seo_sitemap_stale,
   data_freshness_sla_breach, mcp_demand_gap_unaddressed,
   source_of_truth_declining, media_topic_unaddressed,
   dchub_media_press_silent, monthly_trend_unsent_3d,
   tier_inconsistency_web_higher_than_mcp, cron_schedule_collision.
 Omit any pattern that doesn't actually apply. If nothing fits, write
 "(no autonomous actions warranted this pass)".]

## Code-fix candidates
[bulleted list of issues whose fix is code-level, suitable for the L22
 auto-code PR drafter. Format each bullet as:
   - RECIPE: <route_alias_404 | schema_drift_guard | cron_if_mismatched> · <target> · <one-line rationale>
 Only emit if a finding clearly maps to one of those three recipes.
 Otherwise write "(none this pass)".]

## Predictions · next 24h
[1-3 forecasts with confidence + the caveat that they're forecasts not facts]
"""


# ── Parsing the Inspector's structured Markdown ──────────────────────
def _parse_recommendations(md: str) -> list[dict]:
    """Pull the `PATTERN: <name> · <rationale>` lines from the Would-do
    section. Returns [{pattern, rationale}]."""
    import re
    if not md or "## Would-do" not in md:
        return []
    try:
        section = md.split("## Would-do", 1)[1]
        section = section.split("\n## ", 1)[0]
    except Exception:
        return []
    out: list[dict] = []
    for line in section.split("\n"):
        m = re.match(r"\s*[-*+]\s*PATTERN:\s*([a-z0-9_:.\-]+)\s*[·\-:|]\s*(.*)$",
                     line.strip(), re.IGNORECASE)
        if m:
            out.append({
                "pattern":   m.group(1).strip(),
                "rationale": m.group(2).strip()[:240],
            })
    return out


def _parse_code_fix_candidates(md: str) -> list[dict]:
    """Pull the `RECIPE: <recipe> · <target> · <rationale>` lines from
    the Code-fix candidates section. Returns [{recipe, target, rationale}]."""
    import re
    if not md or "## Code-fix candidates" not in md:
        return []
    try:
        section = md.split("## Code-fix candidates", 1)[1]
        section = section.split("\n## ", 1)[0]
    except Exception:
        return []
    out: list[dict] = []
    valid = ("route_alias_404", "schema_drift_guard", "cron_if_mismatched")
    for line in section.split("\n"):
        m = re.match(r"\s*[-*+]\s*RECIPE:\s*([a-z0-9_]+)\s*[·\-:|]\s*([^·\-:|]+)\s*[·\-:|]\s*(.*)$",
                     line.strip(), re.IGNORECASE)
        if m and m.group(1).strip() in valid:
            # r80 (2026-06-04): citation_score=0 is an HONEST measurement (LLMs don't
            # organically cite us yet), NOT a schema bug — skip it so L22 never drafts a
            # wasted "fix" PR for reality. (Honest-numbers north star: don't fix the truth.)
            if "citation" in m.group(2).strip().lower():
                continue
            out.append({
                "recipe":    m.group(1).strip(),
                "target":    m.group(2).strip()[:200],
                "rationale": m.group(3).strip()[:240],
            })
    return out


# ── Apply recommendations (Inspector → L21 autopilot) ────────────────
def _fire_recommendation(pattern: str, rationale: str) -> dict:
    """Translate one Inspector recommendation into a real autopilot
    action. Looks up the pattern in _PATTERN_LIBRARY, builds a synthetic
    finding, gets (endpoint, payload), POSTs it.

    Honors the autopilot's rate-limit + cooldown — calls go through the
    same execution path as autonomous detector firings, so a misfire
    can't stack."""
    try:
        from routes.brain_autopilot import _PATTERN_LIBRARY
    except Exception as e:
        return {"pattern": pattern, "ok": False, "error": f"library_import: {e}"}

    entry = _PATTERN_LIBRARY.get(pattern)
    if not entry:
        # Try prefix match (e.g. brand_surface_dormant:power_totals)
        base = pattern.split(":", 1)[0]
        entry = _PATTERN_LIBRARY.get(base)
    if not entry:
        return {"pattern": pattern, "ok": False, "error": "pattern_not_in_library"}

    action_fn = entry.get("action")
    if not callable(action_fn):
        return {"pattern": pattern, "ok": False, "error": "no_action_callable"}

    synthetic = {
        "issue":  pattern,
        "url":    f"inspector://{pattern}",
        "count":  1,
        "detail": rationale or "inspector recommendation",
    }
    try:
        endpoint, payload = action_fn(synthetic)
    except Exception as e:
        return {"pattern": pattern, "ok": False,
                "error": f"action_eval: {str(e)[:200]}"}
    if not endpoint:
        return {"pattern": pattern, "ok": False,
                "error": "pattern_is_escalation_only"}

    # POST through the autopilot's _execute_action so the rate-limit +
    # logging behave identically to autonomous firings.
    try:
        from routes.brain_autopilot import _execute_action
        use_admin = bool(entry.get("use_admin"))
        http_code, body, error = _execute_action(endpoint, payload or {}, use_admin)
        return {
            "pattern":   pattern,
            "endpoint":  endpoint,
            "http_code": http_code,
            "ok":        bool(http_code and 200 <= http_code < 300),
            "body":      (body or "")[:200],
            "error":     error,
        }
    except Exception as e:
        return {"pattern": pattern, "ok": False,
                "error": f"execute: {str(e)[:200]}"}


@brain_inspector_bp.route("/api/v1/brain/brief/<int:bid>/apply",
                            methods=["POST"])
def brief_apply(bid: int):
    """Admin: fire the Inspector's Would-do recommendations through the
    autopilot. Each pattern goes through the standard rate-limit +
    cooldown — Inspector can't override safety guardrails."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("SELECT brief_md FROM brain_briefs WHERE id = %s",
                        (bid,))
            r = cur.fetchone()
            if not r: return jsonify(ok=False, error="not_found"), 404
            md = r[0] or ""
    finally:
        try: c.close()
        except Exception: pass

    recs = _parse_recommendations(md)
    results = [_fire_recommendation(r["pattern"], r["rationale"]) for r in recs]
    return jsonify(
        ok=True, brief_id=bid,
        recommendations_found=len(recs),
        fired=sum(1 for r in results if r.get("ok")),
        results=results,
    )


# ── Draft PRs via L22 (Inspector → auto-code) ────────────────────────
@brain_inspector_bp.route("/api/v1/brain/brief/<int:bid>/draft-prs",
                            methods=["POST"])
def brief_draft_prs(bid: int):
    """Admin: hand the Inspector's code-fix candidates to L22 so it can
    draft GitHub PRs. L22 has its own safety whitelist (only 3 recipes
    eligible for auto-PR; everything else gets a WIP label or refused)."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("SELECT brief_md FROM brain_briefs WHERE id = %s",
                        (bid,))
            r = cur.fetchone()
            if not r: return jsonify(ok=False, error="not_found"), 404
            md = r[0] or ""
    finally:
        try: c.close()
        except Exception: pass

    candidates = _parse_code_fix_candidates(md)
    if not candidates:
        return jsonify(ok=True, brief_id=bid, candidates_found=0,
                       note="No code-fix candidates in this brief.")

    # Hand off to L22. We don't reach into its internals — we just
    # POST its /run endpoint and let it pull from L14 + L21 findings
    # (which is its existing path). The candidates we found here are
    # logged so a human can correlate.
    # r33-Q+l22-port-fix (2026-05-21): PORT 8000 → 8080. The app runs on
    # 8080 (gunicorn binds $PORT which is 8080 on Railway). 8000 was
    # silently failing — connection refused — which is exactly why
    # /brain/innovation showed 0 L22 PR proposals despite the page
    # explicitly calling out the Inspector→L22 handoff as the next
    # autonomy frontier. ONE CHARACTER. Months of "wired but not firing"
    # was a port typo.
    try:
        import urllib.request, json as _json
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                      or "")
        req = urllib.request.Request(
            "http://localhost:8080/api/v1/brain/auto-code/run",
            data=_json.dumps({"trigger": "inspector",
                              "brief_id": bid}).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                      "X-Admin-Key": admin_key},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            l22_body = resp.read().decode("utf-8", errors="replace")[:1000]
            l22_status = resp.status
    except Exception as e:
        l22_body = ""
        l22_status = None
        l22_error = f"{type(e).__name__}: {str(e)[:200]}"
    else:
        l22_error = None

    return jsonify(
        ok=True, brief_id=bid,
        candidates_found=len(candidates),
        candidates=candidates,
        l22_status=l22_status,
        l22_body=l22_body[:500] if l22_body else None,
        l22_error=l22_error,
    )


def _call_opus(system: str, user: str, model: str,
                 max_tokens: int = 4000) -> tuple[str, dict]:
    """Call the Anthropic API. Returns (text, meta_dict). meta has
    tokens_in/out/duration_ms/error."""
    import urllib.request, urllib.error
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return "", {"error": "ANTHROPIC_API_KEY not set"}
    started = datetime.datetime.utcnow()
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        anthropic_messages_url(),
        data=payload, method="POST",
        headers={
            "x-api-key": key,
            "User-Agent": "dchub-brain/1.0",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return "", {"error": f"HTTPError {e.code}",
                    "detail": e.read().decode("utf-8", errors="replace")[:400]}
    except Exception as e:
        return "", {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    dur = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    text = ""
    for block in body.get("content", []) or []:
        if block.get("type") == "text":
            text += block.get("text", "")
    usage = body.get("usage") or {}
    return text, {
        "tokens_in":   usage.get("input_tokens"),
        "tokens_out":  usage.get("output_tokens"),
        "duration_ms": dur,
        "model_used":  body.get("model"),
    }


import re as _re_pii
_EMAIL_RE = _re_pii.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def _mask_emails_deep(obj):
    """Mask customer emails before they enter a brief (which is stored in
    brain_briefs + rendered). Keeps first char + domain (g•••@nlr.gov) so an admin
    still sees the org but the full address is never persisted in narrative text.
    Full PII stays available to admins via /api/v1/brain/conversions-audit.
    r-brain-gate (2026-06-18)."""
    if isinstance(obj, str):
        return _EMAIL_RE.sub(lambda m: m.group(1) + "•••@" + m.group(2), obj)
    if isinstance(obj, dict):
        return {k: _mask_emails_deep(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_mask_emails_deep(v) for v in obj]
    return obj


def _generate_brief() -> dict:
    """End-to-end: gather signals → ask Opus → persist → return."""
    from routes.brain_models import brain_model_for
    model = brain_model_for("inspector")
    signals = _mask_emails_deep(_gather_signals())
    user_msg = (
        "Signal block (JSON):\n\n```json\n"
        + json.dumps(signals, indent=2, default=str)[:18000]
        + "\n```\n\nWrite the brief now."
    )
    text, meta = _call_opus(_SYSTEM_PROMPT, user_msg, model)
    out = {
        "model":       model,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "brief_md":    text,
        "signals_count": len([k for k in signals if not k.endswith("_error")]),
        **meta,
    }

    # Heuristic section counters (so the API + UI can show summary
    # numbers without needing to parse the full Markdown).
    if text:
        def _count_after(header: str) -> int:
            try:
                section = text.split(f"## {header}", 1)[1]
                section = section.split("\n## ", 1)[0]
                return sum(1 for line in section.split("\n")
                           if line.strip().startswith(("-", "*", "+")))
            except Exception:
                return 0
        out["healthy_count"]   = _count_after("Healthy")
        out["degrading_count"] = _count_after("Degrading")
        out["attention_count"] = _count_after("Needs attention")
        # One-line take
        if "One-line take" in text:
            try:
                out["summary"] = (text.split("One-line take", 1)[1]
                                  .split("\n", 2)[0]
                                  .lstrip(":* ")
                                  .strip()[:300])
            except Exception:
                pass

    # r33-Q+empty-brief-guard (2026-05-22): do NOT persist an empty
    # brief. When the Opus call is throttled by L20 durability (>2
    # Claude calls/min) or errors, _call_opus returns text="" + an
    # error in meta. Persisting that creates a junk brain_briefs row
    # (observed: id=11, ok:False, 0 duration, null counts) that can
    # surface as "latest brief" and pollute the innovation page.
    # Bail early — the caller sees ok:False and can retry. The brief
    # table only ever holds real, content-bearing briefs.
    if out.get("error") or not (text or "").strip():
        out["ok"] = False
        out["skipped_persist"] = "empty_or_errored_brief"
        return out

    # Persist. FIX r10 (2026-05-20): explicit c.commit() — get_db()
    # returns connections in non-autocommit mode, so the INSERT was
    # rolling back on connection close. Same fix applied in
    # _ensure_brief_table above.
    _ensure_brief_table()
    c = _get_db()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO brain_briefs
                      (model, inputs, brief_md, summary,
                       healthy_count, degrading_count, attention_count,
                       tokens_in, tokens_out, duration_ms, error)
                    VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    RETURNING id, generated_at
                """, (model, json.dumps(signals, default=str), text,
                       out.get("summary"),
                       out.get("healthy_count"),
                       out.get("degrading_count"),
                       out.get("attention_count"),
                       out.get("tokens_in"), out.get("tokens_out"),
                       out.get("duration_ms"), out.get("error")))
                r = cur.fetchone()
                if r:
                    out["id"] = int(r[0])
                    out["generated_at"] = str(r[1])
            try: c.commit()
            except Exception: pass
        except Exception as e:
            try: c.rollback()
            except Exception: pass
            out["persist_error"] = str(e)[:200]
        finally:
            try: c.close()
            except Exception: pass

    # r33-Q+inspector-bridge (2026-05-21) — C + E together.
    #
    # C) Persist Inspector's Degrading + Needs-attention items to
    #    brain_findings so the consistency-radar page actually shows
    #    what Inspector sees. Before this hook, the radar showed
    #    "0 Open findings" even when Inspector flagged 8-10 degrading
    #    items per brief — the two were on different data planes.
    #
    # E) Auto-trigger the L22 draft-PR pipeline. The Inspector already
    #    identifies code-fix RECIPE candidates in every brief, but the
    #    handoff to L22 was only firing when an admin manually clicked
    #    "Draft PRs". After this hook, L22 gets the candidates the
    #    moment the brief lands. Combined with the port fix in
    #    brief_draft_prs, this is what unblocks the 0-L22-proposals
    #    embarrassment on /brain/innovation.
    #
    # Both wrapped in try/except so a failure doesn't taint the brief
    # itself — the brief is the canonical artifact; these are surfaces
    # on top of it.
    brief_id = out.get("id")
    if brief_id and out.get("brief_md"):
        # ── C: persist Degrading + Attention items to brain_findings ──
        try:
            md = out["brief_md"]
            findings = []
            for section in ("Degrading", "Needs attention"):
                try:
                    body = md.split(f"## {section}", 1)[1]
                    body = body.split("\n## ", 1)[0]
                except Exception:
                    continue
                # Each bullet becomes a finding. Detail = the bullet text.
                for line in body.split("\n"):
                    line = line.strip()
                    if not line.startswith(("-", "*", "+")):
                        continue
                    # Strip bullet + cleanup
                    bullet = line.lstrip("-*+ ").strip()
                    if len(bullet) < 8:
                        continue
                    # Extract issue key from leading **bold** segment if present;
                    # otherwise use first 80 chars
                    issue_key = ""
                    if bullet.startswith("**"):
                        try:
                            issue_key = bullet.split("**", 2)[1][:100]
                        except Exception:
                            pass
                    if not issue_key:
                        issue_key = bullet.split(":", 1)[0][:100]
                    findings.append({
                        "issue":  f"inspector:{section.lower().replace(' ', '_')}:{issue_key}",
                        "url":    f"/api/v1/brain/brief/{brief_id}",
                        "count":  1,
                        "detail": bullet[:2000],
                    })
            if findings:
                try:
                    from routes.brain_consistency_radar import _persist_findings_to_db
                    n = _persist_findings_to_db(findings)
                    out["inspector_findings_persisted"] = n
                    logger.info(
                        "Inspector → brain_findings: persisted %d items "
                        "from brief %d", n, brief_id,
                    )
                except Exception as _e:
                    out["inspector_findings_persist_error"] = str(_e)[:200]
        except Exception as _e:
            out["inspector_findings_persist_error"] = str(_e)[:200]

        # ── E: auto-fire L22 draft-PR pipeline ────────────────────────
        # Run in a background thread so the brief endpoint returns fast.
        # L22 will take 10-30s to draft; the brief caller shouldn't wait.
        #
        # LIVE BY DEFAULT (productive), with a kill-switch: the autonomous
        # arm routes to /auto-code/run unless BRAIN_L22_AUTOPR_LIVE=0, which
        # flips it to /auto-code/dry-run (records every proposal so
        # /brain/innovation still shows the pipeline, but opens nothing).
        # See _l22_autofire_mode().
        try:
            import threading as _th
            _l22_mode, _l22_path = _l22_autofire_mode()
            def _bg_l22(mode=_l22_mode, path=_l22_path):
                try:
                    import urllib.request, json as _json
                    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                                  or "")
                    req = urllib.request.Request(
                        "http://localhost:8080" + path,
                        data=_json.dumps({"trigger": "inspector_auto",
                                          "brief_id": brief_id}).encode(),
                        method="POST",
                        headers={"Content-Type": "application/json",
                                  "X-Admin-Key": admin_key},
                    )
                    with urllib.request.urlopen(req, timeout=45) as resp:
                        logger.info(
                            "Inspector → L22 auto-fire (%s) for brief %d: "
                            "HTTP %s", mode, brief_id, resp.status,
                        )
                except Exception as _e:
                    logger.warning(
                        "Inspector → L22 auto-fire (%s) failed: %s",
                        mode, str(_e)[:200],
                    )
            _th.Thread(
                target=_bg_l22, daemon=True,
                name=f"inspector-l22-brief-{brief_id}",
            ).start()
            out["l22_autofire_scheduled"] = True
            out["l22_autofire_mode"] = _l22_mode
        except Exception as _e:
            out["l22_autofire_error"] = str(_e)[:200]

    return out


# ── ENDPOINTS ────────────────────────────────────────────────────────
@brain_inspector_bp.route("/api/v1/brain/brief/latest", methods=["GET"])
def brief_latest():
    """Most recent brief — ADMIN-ONLY (it embeds customer emails + revenue)."""
    if not _admin_ok():
        return jsonify(error="admin only — internal brain dashboard"), 403
    _ensure_brief_table()
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, generated_at, model, summary,
                       healthy_count, degrading_count, attention_count,
                       brief_md, tokens_in, tokens_out, duration_ms
                  FROM brain_briefs
                 WHERE error IS NULL
                 ORDER BY generated_at DESC LIMIT 1
            """)
            r = cur.fetchone()
            if not r:
                return jsonify(
                    ok=False,
                    error="no_brief_yet",
                    hint="POST /api/v1/brain/brief/generate to trigger one. "
                         "Requires ANTHROPIC_API_KEY env var.",
                ), 404
            resp = jsonify(
                ok=True,
                id=int(r[0]),
                generated_at=str(r[1]),
                model=r[2],
                summary=r[3],
                healthy_count=r[4],
                degrading_count=r[5],
                attention_count=r[6],
                brief_md=r[7],
                tokens_in=r[8],
                tokens_out=r[9],
                duration_ms=r[10],
            )
            resp.headers["Cache-Control"] = "public, max-age=300"
            return resp
    finally:
        try: c.close()
        except Exception: pass


@brain_inspector_bp.route("/api/v1/brain/brief/<int:bid>", methods=["GET"])
def brief_specific(bid: int):
    if not _admin_ok():
        return jsonify(error="admin only — internal brain dashboard"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, generated_at, model, summary, brief_md,
                       healthy_count, degrading_count, attention_count
                  FROM brain_briefs WHERE id = %s
            """, (bid,))
            r = cur.fetchone()
            if not r: return jsonify(ok=False, error="not_found"), 404
            return jsonify(ok=True, id=int(r[0]),
                           generated_at=str(r[1]), model=r[2],
                           summary=r[3], brief_md=r[4],
                           healthy_count=r[5], degrading_count=r[6],
                           attention_count=r[7])
    finally:
        try: c.close()
        except Exception: pass


@brain_inspector_bp.route("/api/v1/brain/brief/list", methods=["GET"])
def brief_list():
    if not _admin_ok():
        return jsonify(error="admin only — internal brain dashboard"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, generated_at, model, summary,
                       healthy_count, degrading_count, attention_count
                  FROM brain_briefs
                 ORDER BY generated_at DESC LIMIT 50
            """)
            return jsonify(ok=True, briefs=[
                {"id": int(r[0]), "generated_at": str(r[1]),
                 "model": r[2], "summary": r[3],
                 "healthy": r[4], "degrading": r[5], "attention": r[6]}
                for r in cur.fetchall()
            ])
    finally:
        try: c.close()
        except Exception: pass


@brain_inspector_bp.route("/api/v1/brain/brief/generate", methods=["POST"])
def brief_generate():
    """Admin: trigger a fresh Inspector pass. Returns the new brief."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    out = _generate_brief()
    if out.get("error"):
        return jsonify(ok=False, **out), 503
    return jsonify(ok=True, **out)


@brain_inspector_bp.route("/api/v1/brain/models", methods=["GET"])
def brain_models_endpoint():
    """Show which model each tier currently uses. Useful for diagnostics
    when wondering 'is the brain really on Opus?' """
    try:
        from routes.brain_models import brain_model_summary
        return jsonify(ok=True, **brain_model_summary())
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


# ── HTML page at /brain/brief ────────────────────────────────────────
@brain_inspector_bp.route("/brain/brief", methods=["GET"])
def brief_html():
    """Render the latest brief — ADMIN-ONLY (customer PII + revenue + infra)."""
    if not _admin_ok():
        return Response(_ADMIN_ONLY_HTML, mimetype="text/html", status=403)
    _ensure_brief_table()
    summary = ""
    md = ""
    generated_at = ""
    model = ""
    counts = {"healthy": 0, "degrading": 0, "attention": 0}
    c = _get_db()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT generated_at, model, summary, brief_md,
                           healthy_count, degrading_count, attention_count
                      FROM brain_briefs
                     WHERE error IS NULL
                     ORDER BY generated_at DESC LIMIT 1
                """)
                r = cur.fetchone()
                if r:
                    generated_at = str(r[0])[:19]
                    model = r[1] or ""
                    summary = r[2] or ""
                    md = r[3] or ""
                    counts["healthy"]    = int(r[4] or 0)
                    counts["degrading"]  = int(r[5] or 0)
                    counts["attention"]  = int(r[6] or 0)
        finally:
            try: c.close()
            except Exception: pass

    body_inner = (md.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")) or (
        "<p style='color:#a1a1aa'>No brief yet. "
        "<code>POST /api/v1/brain/brief/generate</code> to trigger one.</p>"
    )

    # Extract latest brief id for the action buttons. Pulled separately
    # so the page can offer apply / draft-prs once the brief exists.
    latest_id = ""
    rec_count = 0
    fix_count = 0
    if md:
        c2 = None
        try:
            c2 = _get_db()
            if c2 is not None:
                with c2.cursor() as cur2:
                    cur2.execute("""
                        SELECT id FROM brain_briefs
                         WHERE error IS NULL
                         ORDER BY generated_at DESC LIMIT 1
                    """)
                    rid = cur2.fetchone()
                    if rid: latest_id = str(rid[0])
        except Exception: pass
        finally:
            if c2 is not None:
                try: c2.close()
                except Exception: pass
        rec_count = len(_parse_recommendations(md))
        fix_count = len(_parse_code_fix_candidates(md))

    # FIX r13 (2026-05-20): build the action-button HTML in a regular
    # string BEFORE the f-string render. The previous inline form had
    # backslash-escaped apostrophes inside an f-string, which Python
    # interprets as line-continuation + character — broke parse for the
    # whole module and 404'd every Inspector endpoint until this fix.
    if latest_id:
        btn_apply = (
            '<a class="regen" '
            'onclick="dchubApplyRecs(this)" '
            f'data-bid="{latest_id}" '
            'href="javascript:void(0)">'
            f'▶ Apply {rec_count} recommendations</a>'
        )
        btn_draft = (
            '<a class="regen" '
            'onclick="dchubDraftPrs(this)" '
            f'data-bid="{latest_id}" '
            'href="javascript:void(0)">'
            f'⌥ Draft {fix_count} PRs via L22</a>'
        )
        action_row = (
            '<div style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap">'
            + btn_apply + btn_draft + '</div>'
            + '<script>'
            + 'function dchubApplyRecs(el){var bid=el.dataset.bid;'
            + 'var k=prompt("admin key?");if(!k)return;'
            + 'fetch("/api/v1/brain/brief/"+bid+"/apply",{method:"POST",'
            + 'headers:{"X-Admin-Key":k}}).then(r=>r.json()).then('
            + 'd=>alert("Fired "+(d.fired||0)+" of "+(d.recommendations_found||0)));}'
            + 'function dchubDraftPrs(el){var bid=el.dataset.bid;'
            + 'var k=prompt("admin key?");if(!k)return;'
            + 'fetch("/api/v1/brain/brief/"+bid+"/draft-prs",{method:"POST",'
            + 'headers:{"X-Admin-Key":k}}).then(r=>r.json()).then('
            + 'd=>alert("Found "+(d.candidates_found||0)+" code-fix candidates"));}'
            + '</script>'
        )
    else:
        action_row = ''

    return Response(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<title>DC Hub · Brain Brief</title>
<meta name="description" content="DC Hub autonomous Inspector brief. {summary[:140]}">
<meta name="robots" content="noindex">
<link rel="icon" type="image/svg+xml" href="/icons/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script defer src="/js/dchub-brand.js"></script>
<style>
:root{{
  --bg:#0a0a0f;--surface:#131319;--border:rgba(255,255,255,.06);
  --border-strong:rgba(255,255,255,.1);--text:#f5f5f7;--text-dim:#a1a1aa;
  --text-faint:#71717a;--indigo:#6366f1;--violet:#a855f7;
  --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
  --grad-soft:linear-gradient(135deg,rgba(99,102,241,.10) 0%,rgba(168,85,247,.10) 100%);
  --font:'Instrument Sans',-apple-system,sans-serif;
  --mono:'JetBrains Mono','SF Mono',monospace;
}}
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:var(--font);background:var(--bg);color:var(--text);
     line-height:1.6;-webkit-font-smoothing:antialiased;
     position:relative;min-height:100vh}}
body::before{{content:'';position:fixed;top:-30%;left:50%;transform:translateX(-50%);
  width:1200px;height:1200px;z-index:0;pointer-events:none;
  background:radial-gradient(circle,rgba(99,102,241,.10) 0%,
                              rgba(168,85,247,.06) 30%,transparent 60%)}}
.wrap{{max-width:920px;margin:0 auto;padding:48px 24px 80px;
       position:relative;z-index:1}}
header.top{{display:flex;align-items:center;justify-content:space-between;
            margin-bottom:36px;flex-wrap:wrap;gap:14px}}
header.top a.brand{{display:inline-flex;align-items:center;gap:10px;
                    text-decoration:none;color:var(--text)}}
.meta{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
       letter-spacing:.1em;color:var(--text-faint)}}
.meta .dot{{display:inline-block;width:6px;height:6px;border-radius:50%;
            background:var(--violet);box-shadow:0 0 8px var(--violet);
            margin-right:6px;vertical-align:middle;
            animation:pulse 2s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.eyebrow{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
          letter-spacing:.16em;color:var(--violet);font-weight:600;
          margin-bottom:14px}}
h1.title{{font-size:clamp(1.8rem,3.6vw,2.4rem);font-weight:700;
          letter-spacing:-.025em;line-height:1.1;margin-bottom:12px}}
h1.title .grad{{background:var(--grad);-webkit-background-clip:text;
                background-clip:text;color:transparent}}
.lede{{color:var(--text-dim);font-size:1rem;line-height:1.55;
       max-width:680px;margin-bottom:28px}}
.counts{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;
         margin-bottom:32px}}
@media (max-width:640px){{.counts{{grid-template-columns:1fr}}}}
.count{{background:var(--surface);border:1px solid var(--border);
        border-radius:12px;padding:18px;text-align:center}}
.count-val{{font-size:1.6rem;font-weight:700;letter-spacing:-.02em;
            background:var(--grad);-webkit-background-clip:text;
            background-clip:text;color:transparent;display:block}}
.count-lbl{{font-family:var(--mono);font-size:10px;text-transform:uppercase;
            letter-spacing:.1em;color:var(--text-faint);margin-top:6px;
            display:block}}
pre.brief{{background:var(--surface);border:1px solid var(--border);
           border-radius:14px;padding:24px 28px;font-family:var(--font);
           font-size:14.5px;line-height:1.7;color:var(--text);
           white-space:pre-wrap;word-wrap:break-word;
           border-left:3px solid var(--violet)}}
.regen{{display:inline-flex;align-items:center;gap:8px;
        padding:8px 14px;border-radius:999px;background:var(--surface);
        border:1px solid var(--border-strong);font-size:12.5px;
        font-weight:600;color:var(--text);text-decoration:none;
        font-family:var(--mono);text-transform:uppercase;letter-spacing:.08em}}
.regen:hover{{border-color:var(--violet)}}
.foot{{margin-top:64px;padding-top:32px;border-top:1px solid var(--border);
       font-family:var(--mono);font-size:11px;color:var(--text-faint);
       text-align:center}}
.foot a{{color:var(--text-dim);margin:0 8px;text-decoration:none}}
.foot a:hover{{color:var(--text)}}
</style></head><body>
<div class="wrap">
  <header class="top">
    <a href="/" class="brand" data-dchub-brand></a>
    <span class="meta"><span class="dot"></span>{generated_at or 'No brief yet'}{' · ' + model if model else ''}</span>
  </header>
  <div class="eyebrow">Autonomous Inspector</div>
  <h1 class="title">What the brain <span class="grad">noticed today.</span></h1>
  <p class="lede">{summary or 'The Inspector reads every surface every few hours, synthesizes what changed, and writes this brief. Evidence-first, no invented numbers, no hype.'}</p>
  <div class="counts">
    <div class="count"><span class="count-val">{counts['healthy']}</span><span class="count-lbl">Healthy</span></div>
    <div class="count"><span class="count-val">{counts['degrading']}</span><span class="count-lbl">Degrading</span></div>
    <div class="count"><span class="count-val">{counts['attention']}</span><span class="count-lbl">Needs attention</span></div>
  </div>
  <pre class="brief">{body_inner}</pre>
  {action_row}
  <div class="foot">
    DC Hub · Inspector layer · model {model or '—'}<br>
    <a href="/">dchub.cloud</a> · <a href="/reports/monthly">monthly trend</a> · <a href="/cited-by">cited by</a> · <a href="/transparency">ops</a>
  </div>
</div>
</body></html>""",
        mimetype="text/html",
        headers={"Cache-Control": "public, max-age=300"})


def _smoke():
    logger.info("[brain-inspector] ready · GET /brain/brief + "
                 "/api/v1/brain/brief/{latest|<id>|list} + "
                 "POST /api/v1/brain/brief/generate")

_smoke()
