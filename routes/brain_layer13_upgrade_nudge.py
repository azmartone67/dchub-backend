"""
Brain L13 — Upgrade Nudge (2026-05-18).

Finds high-volume free-tier MCP users and sends a personalized,
non-spammy value-prop email showing what they'd unlock on the paid
tier. Targets the conversion-crisis the brain has been flagging:
10,641 search_facilities calls / 1 unique key / 0 paywall signals
fired / 0 conversions in 14 days.

The MCP gate apparently isn't surfacing the paywall in-response (that
needs a fix in the MCP server repo). Until then, L13 nudges out-of-band
via email — captured from MCP signup or upgrade-signal endpoints.

Targeting rules (conservative — never spam):
  - Free-tier key with >100 tool calls in last 14d
  - Has an email captured (from MCP signup or marketing pulse)
  - Never received an upgrade-nudge email (or last one was >30d ago)

Endpoints:
  GET  /api/v1/brain/upgrade-nudge/candidates  — preview who'd be targeted
  POST /api/v1/brain/upgrade-nudge/send-batch  — actually email (admin gate)
"""

import os
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer13_bp = Blueprint("brain_layer13", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
_FROM = "DC Hub <press@dchub.cloud>"
_MIN_CALLS_14D = 100
_NUDGE_COOLDOWN_DAYS = 30


def _ensure_table():
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn: return False
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brain_upgrade_nudges (
                id            SERIAL PRIMARY KEY,
                sent_at       TIMESTAMPTZ DEFAULT NOW(),
                api_key_hash  TEXT,
                email         TEXT,
                calls_14d     INTEGER,
                top_tool      TEXT,
                resend_id     TEXT,
                error         TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_nudges_email_time "
                    "ON brain_upgrade_nudges(email, sent_at DESC)")
        conn.commit()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return True
    except Exception as e:
        logger.warning(f"L13 table create failed: {e}")
        return False


def _candidates(limit: int = 25) -> list[dict]:
    """High-volume free-tier callers with a captured email who haven't
    been nudged recently."""
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn: return []
        cur = conn.cursor()
        # Schema: mcp_tool_calls.api_key_hash, api_keys.key_hash,
        # api_keys.user_id -> users.email, api_keys.plan (not tier).
        # Heavy callers in last 14d that have a captured email + are
        # on the free/identified tier and haven't been nudged in 30d.
        cur.execute("""
            WITH heavy AS (
                SELECT api_key_hash,
                       COUNT(*) AS calls_14d,
                       MODE() WITHIN GROUP (ORDER BY tool_name) AS top_tool
                FROM mcp_tool_calls
                WHERE called_at > NOW() - INTERVAL '14 days'
                  AND api_key_hash IS NOT NULL
                GROUP BY api_key_hash
                HAVING COUNT(*) >= %s
            ),
            with_email AS (
                SELECT h.api_key_hash, h.calls_14d, h.top_tool,
                       u.email, COALESCE(k.plan, 'free') AS plan
                FROM heavy h
                JOIN api_keys k ON k.key_hash = h.api_key_hash
                JOIN users u    ON u.id = k.user_id
                WHERE u.email IS NOT NULL AND u.email <> ''
                  AND COALESCE(k.plan, 'free') IN ('free', 'identified', 'developer')
            ),
            not_recently_nudged AS (
                SELECT w.* FROM with_email w
                LEFT JOIN (
                    SELECT email, MAX(sent_at) AS last_sent
                    FROM brain_upgrade_nudges
                    GROUP BY email
                ) n ON n.email = w.email
                WHERE n.last_sent IS NULL
                   OR n.last_sent < NOW() - INTERVAL %s
            )
            SELECT api_key_hash, calls_14d, top_tool, email, plan
            FROM not_recently_nudged
            ORDER BY calls_14d DESC
            LIMIT %s
        """, (_MIN_CALLS_14D, f"{_NUDGE_COOLDOWN_DAYS} days", limit))
        rows = [{"api_key_hash": r[0], "calls_14d": r[1], "top_tool": r[2],
                 "email": r[3], "tier": r[4]} for r in cur.fetchall()]
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
        return rows
    except Exception as e:
        logger.warning(f"L13 candidates query failed: {e}")
        return []


def _pitch_body(c: dict) -> tuple[str, str]:
    """Plain-text + HTML pitch body. Lead with their actual usage —
    proves we see them, not a generic blast."""
    calls = c.get("calls_14d", 0)
    tool = c.get("top_tool") or "the MCP tools"
    subj = f"You've made {calls:,} DC Hub calls in 14 days — here's what Pro would unlock"
    txt = f"""Hey —

Quick note: our backend flagged your DC Hub API key as a power user
({calls:,} calls in the last 14 days, mostly on `{tool}`).

You're already getting real value from the free tier, so I wanted to
share what the Pro tier would unlock for the way you're actually using
it:

  * Full row payloads (free tier truncates to first 25 records;
    Pro returns full result sets — relevant on `{tool}` because the
    interesting ranking signal sits past row 25)
  * `get_grid_intelligence` and `get_fiber_intel` full upgrade
    signals (currently you see "paid_only" — Pro sees the actual
    constraint scores, transmission corridors, dark-fiber proximity)
  * Direct CSV/JSON exports of any tool's result set
  * Slack/Discord webhook integration for new-facility alerts

Pricing's $9/mo (Developer) or $99/mo (Pro). If you'd like a free
2-week Pro upgrade to see the difference, reply with "yes" and I'll
flip the key.

— DC Hub team
https://dchub.cloud/pricing
"""
    html = txt.replace("\n", "<br>")
    return subj, txt, html


def _send_resend(to: str, subject: str, text: str, html: str) -> tuple[bool, str]:
    if not _RESEND_KEY:
        return False, "no_resend_key"
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_RESEND_KEY}",
                     "Content-Type": "application/json"},
            json={"from": _FROM, "to": [to], "subject": subject,
                  "text": text, "html": html},
            timeout=12,
        )
        if r.status_code in (200, 202):
            rid = (r.json() or {}).get("id", "")
            return True, rid
        return False, f"resend_{r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def _log_send(c: dict, ok: bool, rid_or_err: str):
    try:
        from main import get_db  # type: ignore
        conn = get_db()
        if not conn: return
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO brain_upgrade_nudges "
            "(api_key_hash, email, calls_14d, top_tool, resend_id, error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (c.get("api_key_hash"), c.get("email"), c.get("calls_14d"),
             c.get("top_tool"),
             rid_or_err if ok else None,
             None if ok else rid_or_err),
        )
        conn.commit()
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass
    except Exception as e:
        logger.warning(f"L13 log failed: {e}")


@brain_layer13_bp.route("/api/v1/brain/upgrade-nudge/candidates",
                        methods=["GET"])
def candidates():
    """Preview who'd be nudged — read-only, no admin gate."""
    _ensure_table()
    limit = int(request.args.get("limit", 25))
    rows = _candidates(limit)
    return jsonify(
        ok=True,
        threshold={"min_calls_14d": _MIN_CALLS_14D,
                    "cooldown_days": _NUDGE_COOLDOWN_DAYS},
        count=len(rows),
        candidates=[{"calls_14d": r["calls_14d"], "top_tool": r["top_tool"],
                      "email_domain": (r["email"] or "?").split("@")[-1],
                      "tier": r["tier"]} for r in rows],
    )


@brain_layer13_bp.route("/api/v1/brain/upgrade-nudge/send-batch",
                        methods=["POST"])
def send_batch():
    """Actually email the batch. Admin-gated."""
    if _ADMIN_KEY:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
    if not _RESEND_KEY:
        return jsonify(ok=False, error="DCHUB_RESEND_API_KEY not set"), 503
    _ensure_table()
    body = request.get_json(silent=True) or {}
    limit = int(body.get("limit", request.args.get("limit", 5)))
    dry = bool(body.get("dry", request.args.get("dry") == "1"))
    rows = _candidates(limit)
    sent, failed = [], []
    for c in rows:
        subj, txt, html = _pitch_body(c)
        if dry:
            sent.append({"email": c["email"], "calls_14d": c["calls_14d"],
                          "subject": subj, "dry": True})
            continue
        ok, rid_or_err = _send_resend(c["email"], subj, txt, html)
        _log_send(c, ok, rid_or_err)
        (sent if ok else failed).append({"email": c["email"],
                                          "calls_14d": c["calls_14d"],
                                          "result": rid_or_err})
    return jsonify(
        ok=True,
        dry=dry,
        attempted=len(rows),
        sent_count=len(sent),
        failed_count=len(failed),
        sent=sent[:10],
        failed=failed[:10],
        sent_at=_dt.datetime.utcnow().isoformat() + "Z",
    )
