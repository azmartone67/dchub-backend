"""
enterprise_leads_sweep.py — r47.37 (2026-05-26).

The 118 distinct users hitting `get_grid_intelligence` 5,544 times in
30 days are the highest-intent leads in the system — and 99% of them
are still on free tier because the only outreach is a generic CTA in
the paywall response.

This module:
  1. SWEEP: identifies free-tier developers with high paid-tool demand
     by JOINing mcp_call_log against mcp_dev_keys. Top N by
     (paid_tool_hits × distinct_tools_attempted).
  2. DRAFT: generates a personalized outreach email per lead that
     references their actual usage pattern ("I see your team's been
     pulling Cheyenne power data — would a $499/mo Pro key with raw
     exports + monthly briefings help?"). Stored in a drafts table.
  3. APPROVE: admin reviews drafts at /admin/enterprise/leads. Reject
     stays in DB for audit. Approve fires the email via Resend.

Cron-tickable. The sweep is safe to re-run — it dedupes against any
email contacted in the last 30 days so we don't spam.

CRITICAL: NEVER auto-sends. Draft-then-approve gate is the same
pattern as partnership_press_template + linkedin_partnership_weekly.
"""
import os
import json
import datetime
import logging
from contextlib import contextmanager
from flask import Blueprint, request, jsonify

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

logger = logging.getLogger(__name__)
enterprise_leads_bp = Blueprint("enterprise_leads_sweep", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS enterprise_lead_drafts (
    id             BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    email          TEXT NOT NULL,
    api_key        TEXT,
    paid_hits_30d  INTEGER,
    top_tools      JSONB,
    score          REAL,
    subject        TEXT,
    body           TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | sent
    approved_at    TIMESTAMPTZ,
    sent_at        TIMESTAMPTZ,
    notes          TEXT,
    UNIQUE (email, created_at)
);
CREATE INDEX IF NOT EXISTS enterprise_lead_drafts_status_idx
  ON enterprise_lead_drafts (status, created_at DESC);
"""


def _ensure_schema():
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception as e:
        logger.warning(f"[enterprise_leads_sweep] schema init failed: {e}")


def _is_admin(req):
    provided = req.headers.get("X-Admin-Key") or req.headers.get("X-Internal-Key")
    if not provided: return False
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    try:
        from internal_auth import is_valid_internal_key
        return bool(is_valid_internal_key(provided))
    except Exception:
        return False


# Tools that mean "this caller wants paid intelligence" — used to score leads
_PAID_TOOLS = (
    'get_grid_intelligence', 'get_fiber_intel', 'analyze_site', 'compare_sites',
    'get_market_intel', 'get_pipeline', 'get_infrastructure', 'get_facility',
)


def _tool_to_pitch(tool: str) -> str:
    """Map a tool name to a 1-line pitch about why they'd want it on Pro."""
    return {
        'get_grid_intelligence':  "live ISO-level grid headroom & interconnect queues",
        'get_fiber_intel':        "carrier-network fiber routes + dark/lit segments",
        'analyze_site':           "12-factor site scoring with raw analyst exports",
        'compare_sites':          "side-by-side 5-site comparison + ranking explainer",
        'get_market_intel':       "286-market DCPI with daily delta + Excess/Constraint scores",
        'get_pipeline':           "369 GW construction pipeline w/ operator + capacity",
        'get_infrastructure':     "substations / transmission lines / gas pipelines layered geo",
        'get_facility':           "21K+ facility specs w/ PUE, fiber, power, M&A history",
    }.get(tool, "the data your team has been repeatedly hitting")


def _generate_draft(lead: dict) -> dict:
    """Generate subject + body for a personalized outreach email."""
    name_guess = (lead['email'].split('@')[0] or '').replace('.', ' ').replace('_', ' ').title()
    domain     = lead['email'].split('@')[-1] if '@' in lead['email'] else ''
    top_tools  = lead.get('top_tools') or []
    top_tool   = top_tools[0] if top_tools else 'get_grid_intelligence'
    pitch      = _tool_to_pitch(top_tool)
    paid_hits  = int(lead.get('paid_hits_30d') or 0)
    tool_list  = ", ".join(top_tools[:3]) if top_tools else top_tool

    subject = (f"Your DC Hub usage — {paid_hits} hits on paid intelligence in 30d "
               f"(quick offer)")

    body = (
        f"Hi {name_guess.split()[0] if name_guess else 'there'},\n\n"
        f"DC Hub's analytics flagged your account: {paid_hits} calls "
        f"on paid-tier tools in the last 30 days, primarily {tool_list}. "
        f"That's heavy usage for a free key.\n\n"
        f"The Pro tier ($499/mo, https://dchub.cloud/pricing) unlocks "
        f"{pitch} — same endpoints you're already hitting, full data instead "
        f"of teaser responses. Most {domain or 'firms'} that pull at this volume "
        f"are using it for {'site selection' if 'site' in top_tool else 'market intelligence'}.\n\n"
        f"If you'd rather a custom data feed (DCPI parquet, M&A tracker, "
        f"interconnect queue exports), we also do enterprise tiers from "
        f"$25K/yr — https://dchub.cloud/enterprise.\n\n"
        f"Either way, want a 20-min call this week to scope what'd be most "
        f"useful? I can show you exactly which tools are paywalled vs free "
        f"and what your usage pattern would cost on Pro.\n\n"
        f"— Jonathan\n"
        f"  Founder, DC Hub\n"
        f"  https://dchub.cloud\n\n"
        f"P.S. If your team has separate users, the Pro tier covers up to "
        f"5 seats. Enterprise covers unlimited.\n"
    )
    return {"subject": subject, "body": body, "top_tool": top_tool, "pitch": pitch}


@enterprise_leads_bp.route("/api/v1/admin/enterprise/leads/sweep",
                            methods=["POST"], strict_slashes=False)
def run_sweep():
    """Admin — run the lead-detection sweep + generate drafts.

    Query params:
      ?top=N        max leads to generate drafts for (default 10)
      ?min_hits=N   min paid-tool hits in 30d to qualify (default 5)
      ?dedupe_days=N  skip emails contacted/drafted in last N days (default 30)

    Idempotent. Safe cron target — re-running won't double-draft."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    _ensure_schema()
    top         = max(1, min(int(request.args.get('top', 10)), 50))
    min_hits    = max(1, int(request.args.get('min_hits', 5)))
    dedupe_days = max(1, int(request.args.get('dedupe_days', 30)))

    paid_tools_sql = ",".join(f"'{t}'" for t in _PAID_TOOLS)
    drafted = []
    skipped_dupe = 0
    candidates = 0

    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Build the candidate list: free-tier email × paid-tool hits in 30d
            cur.execute(f"""
                WITH paid_hits AS (
                  SELECT m.api_key, m.tool, COUNT(*) AS n
                    FROM mcp_call_log m
                   WHERE m.timestamp > NOW() - INTERVAL '30 days'
                     AND m.tool IN ({paid_tools_sql})
                   GROUP BY m.api_key, m.tool
                ),
                key_totals AS (
                  SELECT api_key, SUM(n) AS total_hits,
                         ARRAY_AGG(tool ORDER BY n DESC) AS top_tools_arr
                    FROM paid_hits GROUP BY api_key
                )
                SELECT k.email, k.api_key, k.tier, kt.total_hits, kt.top_tools_arr
                  FROM key_totals kt
                  JOIN mcp_dev_keys k ON k.api_key = kt.api_key
                 WHERE k.tier IN ('free', 'developer')
                   AND k.email IS NOT NULL
                   AND k.email <> ''
                   AND kt.total_hits >= %s
                 ORDER BY kt.total_hits DESC
                 LIMIT %s
            """, (min_hits, top * 3))   # over-fetch so we can skip dupes
            rows = cur.fetchall() or []
            candidates = len(rows)

            for r in rows:
                if len(drafted) >= top:
                    break

                # Dedupe by email — skip if we have a recent draft / inquiry
                cur.execute("""
                    SELECT 1 FROM enterprise_lead_drafts
                     WHERE email = %s
                       AND created_at > NOW() - INTERVAL %s
                     LIMIT 1
                """, (r['email'], f'{dedupe_days} days'))
                if cur.fetchone():
                    skipped_dupe += 1
                    continue

                lead = {
                    "email":         r['email'],
                    "api_key":       r['api_key'],
                    "paid_hits_30d": int(r['total_hits'] or 0),
                    "top_tools":     list(r['top_tools_arr'] or []),
                }
                draft = _generate_draft(lead)
                score = float(lead['paid_hits_30d']) * (1 + len(lead['top_tools']) / 8.0)

                cur.execute("""
                    INSERT INTO enterprise_lead_drafts
                        (email, api_key, paid_hits_30d, top_tools, score,
                         subject, body, status)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, 'pending')
                    RETURNING id
                """, (
                    lead['email'], lead['api_key'], lead['paid_hits_30d'],
                    json.dumps(lead['top_tools']), score,
                    draft['subject'], draft['body']
                ))
                new_id = int(cur.fetchone()[0])
                drafted.append({
                    "id":            new_id,
                    "email":         lead['email'],
                    "paid_hits_30d": lead['paid_hits_30d'],
                    "top_tool":      draft['top_tool'],
                    "score":         round(score, 1),
                })
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

    # r47.37.1: when 0 candidates, run a diagnostic so the operator
    # understands WHY. Most paywall demand is anonymous (no key, no email)
    # and our sweep only finds registered free-tier users — that's by
    # design, but it's surprising the first time you run it on a young
    # platform where most callers haven't signed up yet.
    diagnostic = {}
    if candidates == 0:
        try:
            with _conn() as c, c.cursor() as cur:
                # Total paid-tool hits in window
                cur.execute(f"""
                    SELECT COUNT(*) FROM mcp_call_log
                     WHERE timestamp > NOW() - INTERVAL '30 days'
                       AND tool IN ({paid_tools_sql})
                """)
                total_paid_hits = int((cur.fetchone() or [0])[0])

                # By tier (NULL key = anon)
                cur.execute(f"""
                    SELECT COALESCE(k.tier, '_anonymous') AS tier_bucket,
                           COUNT(*) AS hits,
                           COUNT(DISTINCT m.api_key) AS distinct_keys
                      FROM mcp_call_log m
                      LEFT JOIN mcp_dev_keys k ON k.api_key = m.api_key
                     WHERE m.timestamp > NOW() - INTERVAL '30 days'
                       AND m.tool IN ({paid_tools_sql})
                     GROUP BY COALESCE(k.tier, '_anonymous')
                     ORDER BY hits DESC
                """)
                by_tier = [{
                    "tier":          r[0],
                    "paid_hits_30d": int(r[1]),
                    "distinct_keys": int(r[2] or 0),
                } for r in cur.fetchall()]

                # Free-tier keys with ANY paid-tool hit (even just 1)
                cur.execute(f"""
                    SELECT COUNT(DISTINCT m.api_key)
                      FROM mcp_call_log m
                      JOIN mcp_dev_keys k ON k.api_key = m.api_key
                     WHERE m.timestamp > NOW() - INTERVAL '30 days'
                       AND m.tool IN ({paid_tools_sql})
                       AND k.tier IN ('free', 'developer')
                """)
                free_with_paid_hits = int((cur.fetchone() or [0])[0])

            diagnostic = {
                "total_paid_tool_hits_30d":     total_paid_hits,
                "free_keys_with_any_paid_hit":  free_with_paid_hits,
                "hits_by_tier":                 by_tier,
                "interpretation":                (
                    "Sweep returned 0 because no registered free-tier "
                    "user made enough paid-tool calls to qualify. "
                    "Most demand is anonymous (_anonymous bucket in "
                    "hits_by_tier) — those callers have no email, so "
                    "this sweep can't reach them. The /enterprise "
                    "inquiry form is the right surface for inbound. "
                    "The sweep will start finding candidates once free "
                    "tier sign-ups start using the platform heavily."
                ),
            }
        except Exception:
            pass

    return jsonify({
        "ok":           True,
        "candidates":   candidates,
        "drafted":      len(drafted),
        "skipped_dupe": skipped_dupe,
        "leads":        drafted,
        "diagnostic":   diagnostic,
        "review_url":   "/admin/partnerships/review",
        "hint":         "Review drafts then POST /approve/<id> to send via Resend.",
    }), 200


@enterprise_leads_bp.route("/api/v1/admin/enterprise/leads/drafts",
                            methods=["GET"], strict_slashes=False)
def list_drafts():
    """Admin — drafts queue. Filter by ?status=pending|approved|sent|rejected."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db", "drafts": []}), 503

    status_f = (request.args.get('status') or 'pending').strip().lower()
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM enterprise_lead_drafts
                 WHERE status = %s
                 ORDER BY score DESC NULLS LAST, created_at DESC
                 LIMIT 100
            """, (status_f,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                for k in ("created_at", "approved_at", "sent_at"):
                    if r.get(k): r[k] = r[k].isoformat()
        return jsonify({"count": len(rows), "status": status_f, "drafts": rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


def _send_email_resend(to: str, subject: str, body: str, reply_to: str = None) -> bool:
    """Fire via Resend SMTP API. Returns True on success."""
    try:
        import urllib.request as _req
        api_key = os.environ.get("RESEND_API_KEY", "")
        if not api_key:
            logger.warning("[enterprise_leads_sweep] RESEND_API_KEY missing — cannot send")
            return False
        html_body = body.replace('\n', '<br>')
        payload = json.dumps({
            "from":     "Jonathan Martone <jonathan@dchub.cloud>",
            "to":       [to],
            "subject":  subject,
            "html":     html_body,
            "text":     body,
            "reply_to": reply_to or "jonathan@dchub.cloud",
        }).encode()
        req = _req.Request("https://api.resend.com/emails", data=payload, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        })
        with _req.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        logger.warning(f"[enterprise_leads_sweep] resend failed: {e}")
        return False


@enterprise_leads_bp.route("/api/v1/admin/enterprise/leads/approve/<int:draft_id>",
                            methods=["POST"], strict_slashes=False)
def approve_draft(draft_id):
    """Admin — approve + fire the email. Marks status='sent' on success."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT email, subject, body, status FROM enterprise_lead_drafts
                 WHERE id = %s
            """, (draft_id,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "not_found"}), 404
            email, subject, body, status = r
            if status not in ('pending', 'approved'):
                return jsonify({"error": f"draft is {status}, cannot send"}), 400

            ok = _send_email_resend(email, subject, body)
            new_status = 'sent' if ok else 'approved'  # 'approved' = ready to retry
            cur.execute("""
                UPDATE enterprise_lead_drafts
                   SET status      = %s,
                       approved_at = COALESCE(approved_at, NOW()),
                       sent_at     = CASE WHEN %s THEN NOW() ELSE sent_at END
                 WHERE id = %s
            """, (new_status, ok, draft_id))
        return jsonify({
            "ok":      True,
            "id":      draft_id,
            "status":  new_status,
            "sent":    ok,
            "to":      email,
            "subject": subject,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@enterprise_leads_bp.route("/api/v1/admin/enterprise/leads/reject/<int:draft_id>",
                            methods=["POST"], strict_slashes=False)
def reject_draft(draft_id):
    """Admin — mark draft as rejected (kept in DB for audit)."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    notes = (request.args.get('notes') or '').strip()[:500]
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE enterprise_lead_drafts
                   SET status = 'rejected',
                       notes  = COALESCE(NULLIF(%s,''), notes)
                 WHERE id = %s AND status = 'pending'
            """, (notes, draft_id))
            n = cur.rowcount
        return jsonify({"ok": True, "id": draft_id, "rejected": n > 0}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@enterprise_leads_bp.route("/api/v1/admin/enterprise/leads/anonymous-demand",
                            methods=["GET"], strict_slashes=False)
def anonymous_demand():
    """Admin — where the unaddressable paywall demand is concentrated.

    Anonymous callers (no api_key OR not in mcp_dev_keys) can't be
    emailed, but we can see WHICH tools they hit and from WHAT IPs
    (hashed). If a single hashed-IP shows up with hundreds of paid-tool
    hits, that's a real prospect — they're just not signed up yet.

    Use this to:
      - Spot organizations doing heavy due-diligence anonymously
      - Reach out via the tools they're hitting (which signals intent)
      - Improve free-tier teasers for tools where anon demand is high

    Returns aggregated counts, never raw IPs."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503

    paid_tools_sql = ",".join(f"'{t}'" for t in _PAID_TOOLS)
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Top tools by anonymous paid-tool hits
            cur.execute(f"""
                SELECT m.tool, COUNT(*) AS hits,
                       COUNT(DISTINCT m.session_id) AS distinct_sessions
                  FROM mcp_call_log m
                  LEFT JOIN mcp_dev_keys k ON k.api_key = m.api_key
                 WHERE m.timestamp > NOW() - INTERVAL '30 days'
                   AND m.tool IN ({paid_tools_sql})
                   AND k.api_key IS NULL    -- no matching dev key = anonymous
                 GROUP BY m.tool
                 ORDER BY hits DESC LIMIT 12
            """)
            top_tools = [dict(r) for r in cur.fetchall()]

            # Top hashed sessions / IPs (anonymous heavies)
            cur.execute(f"""
                SELECT
                  LEFT(COALESCE(m.session_id, ''), 12) AS session_prefix,
                  COUNT(*) AS hits,
                  COUNT(DISTINCT m.tool) AS distinct_tools,
                  ARRAY_AGG(DISTINCT m.tool ORDER BY m.tool) AS tools_attempted,
                  MIN(m.timestamp) AS first_seen,
                  MAX(m.timestamp) AS last_seen
                  FROM mcp_call_log m
                  LEFT JOIN mcp_dev_keys k ON k.api_key = m.api_key
                 WHERE m.timestamp > NOW() - INTERVAL '30 days'
                   AND m.tool IN ({paid_tools_sql})
                   AND k.api_key IS NULL
                   AND m.session_id IS NOT NULL AND m.session_id <> ''
                 GROUP BY LEFT(COALESCE(m.session_id, ''), 12)
                HAVING COUNT(*) >= 5
                 ORDER BY hits DESC LIMIT 20
            """)
            heavy_anon = [dict(r) for r in cur.fetchall()]
            for h in heavy_anon:
                for k in ('first_seen', 'last_seen'):
                    if h.get(k): h[k] = h[k].isoformat()
                if isinstance(h.get('tools_attempted'), list):
                    h['tools_attempted'] = list(h['tools_attempted'])[:6]

            # Platform breakdown for the anon traffic
            cur.execute(f"""
                SELECT COALESCE(NULLIF(m.platform,''), 'unknown') AS platform,
                       COUNT(*) AS hits
                  FROM mcp_call_log m
                  LEFT JOIN mcp_dev_keys k ON k.api_key = m.api_key
                 WHERE m.timestamp > NOW() - INTERVAL '30 days'
                   AND m.tool IN ({paid_tools_sql})
                   AND k.api_key IS NULL
                 GROUP BY platform
                 ORDER BY hits DESC LIMIT 10
            """)
            platforms = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500

    return jsonify({
        "window":              "30 days",
        "top_anon_tools":      top_tools,
        "heavy_anon_sessions": heavy_anon,
        "anon_by_platform":    platforms,
        "hint":                ("Heavy anonymous sessions on paid tools = real "
                                 "prospects who haven't signed up. Add a free-key "
                                 "CTA into the response payload of the top tools "
                                 "they hit (already done for fiber/intel in r47.34). "
                                 "Convert them by sign-up, then they enter the "
                                 "weekly /sweep candidate pool."),
    }), 200


@enterprise_leads_bp.route("/api/v1/admin/enterprise/leads/pipeline-stats",
                            methods=["GET"], strict_slashes=False)
def pipeline_stats():
    """Admin — at-a-glance numbers for the dashboard."""
    if not _is_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT status, COUNT(*) FROM enterprise_lead_drafts
                 GROUP BY status
            """)
            drafts_by_status = {r[0]: int(r[1]) for r in cur.fetchall()}

            cur.execute("""SELECT COUNT(*) FROM enterprise_lead_drafts
                            WHERE created_at > NOW() - INTERVAL '7 days'""")
            new_7d = int(cur.fetchone()[0])

            cur.execute("""SELECT COUNT(*) FROM enterprise_lead_drafts
                            WHERE sent_at > NOW() - INTERVAL '7 days'""")
            sent_7d = int(cur.fetchone()[0])

            # Inquiries (the inbound counterpart)
            try:
                cur.execute("""SELECT status, COUNT(*) FROM enterprise_inquiries
                                GROUP BY status""")
                inquiries_by_status = {r[0]: int(r[1]) for r in cur.fetchall()}
            except Exception:
                inquiries_by_status = {}
        return jsonify({
            "drafts_by_status":     drafts_by_status,
            "inquiries_by_status":  inquiries_by_status,
            "new_drafts_7d":        new_7d,
            "sent_drafts_7d":       sent_7d,
            "review_url":           "/admin/partnerships/review",
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
