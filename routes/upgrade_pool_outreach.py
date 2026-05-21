"""Phase r32-conv (2026-05-20) — MCP upgrade-pool outreach engine.
==========================================================================

User flagged 9 conversions / 30d against ~18K paywall hits + 96-98
distinct users hitting paid grid-intel + fiber-intel from free tier.
That's an addressable list, not a paywall problem.

This module turns the existing /api/v1/mcp/power-users query into a
fire-able campaign:

  GET  /api/v1/admin/upgrade-pool/preview
       Returns candidates: identified users with paywall signals,
       not yet outreached, not yet converted. Same query shape as
       /power-users but with the per-user outreach body baked in so
       you can audit what each person would receive.

  POST /api/v1/admin/upgrade-pool/send
       Send Resend emails to the candidates. Body params:
         ?dry=1                  — preview, no send
         ?limit=N                — cap (default 25, max 100)
         ?min_signals=N          — minimum paywall signal count (default 3)
       Each successful send sets mcp_upgrade_signals.outreach_sent=true
       so we never email the same person twice.

The email is hand-tuned for the actual highest-signal cohort:
get_grid_intelligence + get_fiber_intel + analyze_site. Each recipient
sees their own tool list + signal count, with a free dev-key offer
that unlocks the exact tools they were hitting.

Conversion math (industry benchmark):
  - Paywall passive: ~0.05% (what we have now)
  - Direct outreach with named tool + free trial: 5-15%
  - Even at 5% on 96 users = 4-5 new $49/mo Developer customers
    = $200-250 MRR from one outreach batch.
"""
import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
upgrade_pool_outreach_bp = Blueprint("upgrade_pool_outreach", __name__)


# r32-conv-fix (2026-05-20): match the EXACT auth pattern that
# /api/v1/admin/schema/repair uses — multi-env-var set with header
# fallback chain. User reported 401 even though their X-Admin-Key
# worked for schema/repair. Root cause: my earlier check looked at
# DCHUB_ADMIN_KEY only, but Railway has DCHUB_INTERNAL_KEY set (which
# schema/repair also accepts). Now both modules accept the same key.
_INTERNAL_KEYS: set = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY",
           "ADMIN_API_KEY", "ADMIN_SECRET"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    """Match /schema/repair's auth pattern exactly: accept any header
    in {X-Internal-Key, X-Admin-Key} or ?admin_key= query param that
    matches ANY of the configured internal keys."""
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception as e:
        logger.warning(f"upgrade_pool: pg connect failed: {e}")
        return None


def _fetch_candidates(min_signals: int = 3, limit: int = 200) -> list[dict]:
    """Returns identified users with paywall signals worth outreaching:
    not yet outreached, not yet converted, ≥ min_signals hits in 30d."""
    conn = _get_db()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_email,
                       COUNT(*) AS signal_count,
                       array_agg(DISTINCT tool_requested
                                  ORDER BY tool_requested) AS tools,
                       MAX(created_at) AS most_recent,
                       array_agg(DISTINCT mcp_client) AS clients,
                       MAX(tier_current) AS current_tier
                  FROM mcp_upgrade_signals
                 WHERE created_at > NOW() - INTERVAL '30 days'
                   AND user_email IS NOT NULL
                   AND user_email != ''
                   AND COALESCE(converted, false) = false
                   AND COALESCE(outreach_sent, false) = false
                 GROUP BY user_email
                HAVING COUNT(*) >= %s
                 ORDER BY signal_count DESC
                 LIMIT %s
            """, (min_signals, limit))
            rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"upgrade_pool: query failed: {e}")
        return []
    finally:
        try: conn.close()
        except Exception: pass

    out = []
    for r in rows:
        out.append({
            "email":         r[0],
            "signal_count":  int(r[1] or 0),
            "tools":         [t for t in (r[2] or []) if t],
            "most_recent":   r[3].isoformat() if r[3] else None,
            "mcp_clients":   [c for c in (r[4] or []) if c],
            "current_tier":  r[5] or "free",
        })
    return out


# Hand-tuned descriptions per tool — used in personalized outreach body.
# Pulled from the existing tool registry so language stays consistent
# with what callers see in the MCP tool list.
_TOOL_BLURBS = {
    "get_grid_intelligence":   "ISO-level grid headroom + congestion + curtailment overlays",
    "get_fiber_intel":         "fiber backbone routes + latency map across 106 carrier paths",
    "get_market_intel":        "DCPI market scoring across 276 markets",
    "get_grid_data":           "real-time ISO grid demand + generation mix",
    "get_water_risk":          "facility-level water-risk overlay (FEMA + USGS)",
    "get_energy_prices":       "EIA retail rates by state, residential/commercial/industrial",
    "get_renewable_energy":    "renewable mix + clean-energy supply by market",
    "analyze_site":            "composite site score across power/fiber/risk/carbon",
    "compare_sites":           "side-by-side market comparison",
    "get_dchub_recommendation":"DC Hub agent-facing recommendation",
    "get_facility":            "full facility detail (vs free's 5-result list)",
    "get_infrastructure":      "substation + fiber + gas pipeline overlay",
    "get_pipeline":            "construction pipeline + MW under build",
    "list_transactions":       "M&A transaction history with value + MW",
}


def _draft_outreach(user: dict) -> dict:
    """Build the personalized subject + HTML body for one user."""
    email = user["email"]
    name_guess = email.split("@")[0].split(".")[0].title()
    sig = user["signal_count"]
    tools = [t for t in (user["tools"] or []) if t]
    top_tools = tools[:3]

    # Tool-aware subject — names the specific paid tool they hit most.
    primary = top_tools[0] if top_tools else "DC Hub MCP"
    subject = (
        f"You hit {primary} {sig}x last month — here's an unlock"
    )

    tool_list_html = "".join(
        f'<li><b>{t}</b> — {_TOOL_BLURBS.get(t, "DC Hub MCP tool")}</li>'
        for t in top_tools
    ) or '<li><b>DC Hub MCP tools</b></li>'

    body = f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif;color:#1a1a1a;line-height:1.55;max-width:560px">
<p>Hi {name_guess},</p>
<p>I run DC Hub (<a href="https://dchub.cloud">dchub.cloud</a>). Our backend pings me when someone hits a paid MCP tool from a free tier — and you've shown up {sig} times in the last 30 days.</p>
<p>The tools you've been hitting:</p>
<ul>{tool_list_html}</ul>
<p>I'd rather get those into your hands than have you bounce on the paywall. Two ways to unlock:</p>
<p><b>1. Free Developer dev key</b> (no card, 60-second signup):<br>
<a href="https://dchub.cloud/redeem?utm_source=outreach&utm_campaign=upgrade_pool&email={email}" style="color:#6366f1;font-weight:600">https://dchub.cloud/redeem</a><br>
Unlocks 50 calls/day on the tools you're hitting — enough to actually evaluate the dataset.</p>
<p><b>2. Direct to Pro</b> with founding-customer pricing while we still have it:<br>
$49/mo Developer or $99/mo (50% off Pro) for the first 90 days. Reply to this email and I'll send a link.</p>
<p>If neither works because of what you're trying to do, tell me what would — I read every reply.</p>
<p>— Jonathan<br>
DC Hub · <a href="https://dchub.cloud">dchub.cloud</a></p>
<p style="margin-top:32px;font-size:11px;color:#6b7280;border-top:1px solid #e5e7eb;padding-top:12px">You're receiving this because your dev key has hit DC Hub's MCP server {sig} times in the last 30 days. <a href="https://dchub.cloud/unsubscribe?email={email}" style="color:#6b7280">Unsubscribe</a></p>
</div>"""
    return {
        "email":   email,
        "subject": subject,
        "body":    body,
        "tools":   top_tools,
        "signals": sig,
    }


@upgrade_pool_outreach_bp.route(
    "/api/v1/admin/upgrade-pool/backfill-emails", methods=["POST"])
def upgrade_pool_backfill_emails():
    """Phase r32-conv-2 (2026-05-20): backfill user_email on the 15,826
    anonymous-but-actually-identifiable signals.

    Root cause uncovered: live /api/v1/mcp/email-distribution shows
    0.0% email capture rate (1 of 15,827 signals has email). That's
    why /upgrade-pool/preview returned 0 candidates — the WHERE
    user_email != '' filter eats everything. But api_key prefixes
    leak into the user_agent column (via the same pattern
    _daily_call_count uses), so we can resolve them retroactively.

    This endpoint walks every email-less signal, extracts a dchub_
    prefix from user_agent, joins api_keys → users to find the email,
    and UPDATEs the row. After it runs, /upgrade-pool/preview will
    show the real addressable pool.

    Idempotent — only touches rows where user_email IS NULL OR ''."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401

    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
    except (ValueError, TypeError):
        days = 30

    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503

    updated = 0
    examined = 0
    distinct_emails = set()
    try:
        with conn.cursor() as cur:
            # Single SQL statement: regex-extract dchub_<prefix> from
            # user_agent, hash check against api_keys.key_prefix, join
            # to users for the email, UPDATE in place. SUBSTRING the
            # 16-char prefix that _daily_call_count uses so it matches.
            cur.execute("""
                WITH candidates AS (
                  SELECT s.id,
                         substring(s.user_agent FROM 'dchub_[A-Za-z0-9_]{1,30}') AS api_prefix
                    FROM mcp_upgrade_signals s
                   WHERE s.created_at > NOW() - INTERVAL %s
                     AND (s.user_email IS NULL OR s.user_email = '')
                     AND s.user_agent IS NOT NULL
                     AND s.user_agent ~ 'dchub_'
                ),
                resolved AS (
                  SELECT c.id, u.email
                    FROM candidates c
                    JOIN api_keys ak
                      ON c.api_prefix LIKE ak.key_prefix || '%%'
                       OR ak.key_prefix LIKE substring(c.api_prefix FROM 1 FOR 12) || '%%'
                    JOIN users u ON ak.user_id = u.id
                   WHERE u.email IS NOT NULL AND u.email != ''
                )
                UPDATE mcp_upgrade_signals s
                   SET user_email = r.email
                  FROM resolved r
                 WHERE s.id = r.id
                RETURNING s.id, s.user_email
            """, (f"{days} days",))
            rows = cur.fetchall()
            updated = len(rows)
            distinct_emails = {r[1] for r in rows if r[1]}
            conn.commit()

            # Also count how many email-less rows remain (for next-step
            # diagnosis — these need session_id or IP-based resolution).
            cur.execute("""
                SELECT COUNT(*) FROM mcp_upgrade_signals
                 WHERE created_at > NOW() - INTERVAL %s
                   AND (user_email IS NULL OR user_email = '')
            """, (f"{days} days",))
            remaining_anonymous = int((cur.fetchone() or [0])[0] or 0)

            # Count addressable pool now.
            cur.execute("""
                SELECT COUNT(DISTINCT user_email)
                  FROM mcp_upgrade_signals
                 WHERE created_at > NOW() - INTERVAL %s
                   AND user_email IS NOT NULL AND user_email != ''
                   AND COALESCE(converted, false) = false
                   AND COALESCE(outreach_sent, false) = false
                 GROUP BY user_email
                HAVING COUNT(*) >= 1
            """, (f"{days} days",))
            addressable = len(cur.fetchall())
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(ok=False, error="backfill_failed",
                       detail=str(e)[:300]), 500
    finally:
        try: conn.close()
        except Exception: pass

    return jsonify(
        ok=True,
        days=days,
        signals_updated=updated,
        distinct_emails_resolved=len(distinct_emails),
        remaining_anonymous_signals=remaining_anonymous,
        addressable_users_now=addressable,
        message=(
            f"Resolved emails for {updated} signals "
            f"({len(distinct_emails)} distinct users). "
            f"{remaining_anonymous} signals still anonymous "
            f"(no api_key prefix in user_agent — likely Claude/ChatGPT "
            f"calling MCP without an unlocked dev key)."
        ),
    ), 200


@upgrade_pool_outreach_bp.route(
    "/api/v1/admin/upgrade-pool/preview", methods=["GET"])
def upgrade_pool_preview():
    """List the addressable upgrade pool with the outreach body that
    would be sent to each. Audit before firing /send."""
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401

    try:
        min_signals = max(1, int(request.args.get("min_signals", 3)))
        limit       = min(200, int(request.args.get("limit", 50)))
    except (ValueError, TypeError):
        min_signals, limit = 3, 50

    users = _fetch_candidates(min_signals=min_signals, limit=limit)
    drafts = [_draft_outreach(u) for u in users]

    return jsonify(
        ok=True,
        as_of=datetime.utcnow().isoformat() + "Z",
        min_signals=min_signals,
        candidate_count=len(users),
        candidates=[
            {**u, **{
                "subject":       d["subject"],
                "body_preview":  d["body"][:300].replace("\n", " "),
                "body_full":     d["body"],
            }}
            for u, d in zip(users, drafts)
        ],
    ), 200


@upgrade_pool_outreach_bp.route(
    "/api/v1/admin/upgrade-pool/send", methods=["POST"])
def upgrade_pool_send():
    """Send personalized Resend emails to the upgrade-pool candidates.

    Query:
      ?dry=1            preview, no send (default ?dry=0 fires)
      ?limit=N          cap (default 25, max 100) — start small
      ?min_signals=N    minimum paywall signal count (default 3)

    Each successful send marks mcp_upgrade_signals.outreach_sent=true
    for that email so the campaign is idempotent across re-runs.
    """
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401

    try:
        dry         = (request.args.get("dry", "0") == "1")
        limit       = min(100, int(request.args.get("limit", 25)))
        min_signals = max(1, int(request.args.get("min_signals", 3)))
    except (ValueError, TypeError):
        dry, limit, min_signals = False, 25, 3

    users = _fetch_candidates(min_signals=min_signals, limit=limit)
    if not users:
        return jsonify(ok=True, sent=0, recipients=0,
                       message="No candidates match — pool exhausted or thresholds too strict"), 200

    drafts = [_draft_outreach(u) for u in users]

    if dry:
        return jsonify(
            ok=True, dry_run=True,
            recipients=len(drafts),
            sample=drafts[:3],
        ), 200

    resend_key = (os.environ.get("DCHUB_RESEND_API_KEY") or
                  os.environ.get("RESEND_API_KEY", "")).strip()
    if not resend_key:
        return jsonify(ok=False, error="resend_not_configured",
                       message="Set DCHUB_RESEND_API_KEY on Railway"), 500

    from_email = os.environ.get(
        "DCHUB_FROM_EMAIL",
        "Jonathan @ DC Hub <jonathan@dchub.cloud>",
    )

    sent, failed = 0, 0
    sent_emails = []
    try:
        import requests as _rq
        for d in drafts:
            try:
                r = _rq.post(
                    "https://api.resend.com/emails",
                    json={
                        "from":    from_email,
                        "to":      [d["email"]],
                        "subject": d["subject"],
                        "html":    d["body"],
                        "reply_to": "jonathan@dchub.cloud",
                        "tags": [
                            {"name": "campaign", "value": "upgrade_pool"},
                            {"name": "signal_count", "value": str(d["signals"])},
                        ],
                    },
                    headers={
                        "Authorization": f"Bearer {resend_key}",
                        "Content-Type":  "application/json",
                    },
                    timeout=15,
                )
                if 200 <= r.status_code < 300:
                    sent += 1
                    sent_emails.append(d["email"])
                else:
                    failed += 1
                    logger.warning(
                        f"upgrade_pool send failed for {d['email']}: "
                        f"{r.status_code} {r.text[:120]}"
                    )
            except Exception as ex:
                failed += 1
                logger.warning(f"upgrade_pool exception for {d['email']}: {ex}")
    except Exception as e:
        return jsonify(ok=False, error="resend_exception",
                       detail=str(e)[:200], sent=sent, failed=failed), 500

    # Mark outreach_sent=true for emails that landed. Idempotent — the
    # query above filters WHERE outreach_sent=false, so a re-run after
    # this update simply finds zero candidates for these users.
    if sent_emails:
        conn = _get_db()
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE mcp_upgrade_signals
                           SET outreach_sent = true,
                               outreach_sent_at = NOW()
                         WHERE user_email = ANY(%s)
                    """, (sent_emails,))
                    conn.commit()
            except Exception as e:
                logger.warning(f"upgrade_pool flag update failed: {e}")
                try: conn.rollback()
                except Exception: pass
            finally:
                try: conn.close()
                except Exception: pass

        # Log to brain_findings for observability.
        conn = _get_db()
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO brain_findings
                            (issue, url, count, detail, detector, created_at)
                           VALUES ('upgrade_pool_outreach_sent', %s, %s, %s,
                                   'upgrade_pool_outreach', NOW())""",
                        ("/api/v1/admin/upgrade-pool/preview", sent,
                         f"sent={sent} failed={failed} "
                         f"sample={','.join(sent_emails[:3])}"),
                    )
                    conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            finally:
                try: conn.close()
                except Exception: pass

    return jsonify(
        ok=True,
        sent=sent,
        failed=failed,
        recipients=len(drafts),
        sample_sent=sent_emails[:5],
    ), 200
