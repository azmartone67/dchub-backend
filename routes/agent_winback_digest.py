"""
agent_winback_digest.py — the weekly "what changed in your markets" push-back
digest (2026-06-21). THE retention pull-channel.

Verified problem: external MCP agents arrive, make ~1,365 calls to finish one
task in one day, and ~95% never return (4.5% multi-day return). Nothing pulls
them back on a later day. This is the PUSH that converts a one-off into a habit:
a weekly email — to agents/humans who EXPLICITLY opted in via bind_email — that
says "here's what moved in the markets you queried; reconnect to pull the delta."

SAFETY (this is customer-facing mail):
  • Audience = ONLY marketing_recipients_from_keys() — mcp_dev_keys with
    metadata.marketing_opt_in='true', suppression-excluded. No opt-in → no mail.
  • Every send routes through marketing_send.send_marketing_email() — the
    suppression-gated, List-Unsubscribe-compliant choke-point. We never POST
    Resend directly here.
  • DRY-RUN by default (DCHUB_AGENT_DIGEST_DRY_RUN=1): renders + logs intended
    recipients, sends NOTHING, until the operator flips it off after review.
  • Idempotent: one send per (email, ISO-week) via agent_digest_log.
  • Kill switch: DCHUB_AGENT_DIGEST_DISABLE=1.

Endpoints:
  GET  /api/v1/admin/agent-digest/preview?email=<addr>  — render only, no send
  POST /api/v1/admin/agent-digest/send                  — run (admin; dry-run default)
"""
from __future__ import annotations
import os, datetime, logging
from flask import Blueprint, request, jsonify

log = logging.getLogger("agent_winback_digest")
agent_winback_digest_bp = Blueprint("agent_winback_digest", __name__)

_FROM_NAME = "DC Hub"


def _disabled() -> bool:
    return str(os.environ.get("DCHUB_AGENT_DIGEST_DISABLE", "")).lower() in ("1", "true", "yes")


def _dry_run() -> bool:
    # default ON — sends nothing until the operator explicitly flips it off
    return str(os.environ.get("DCHUB_AGENT_DIGEST_DRY_RUN", "1")).lower() in ("1", "true", "yes")


def _conn():
    try:
        from main import get_pg_connection
        return get_pg_connection()
    except Exception:
        try:
            import psycopg2
            return psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=8)
        except Exception:
            return None


def _release(c):
    try:
        from main import return_pg_connection
        return_pg_connection(c)
    except Exception:
        try:
            c.close()
        except Exception:
            pass


def _ensure(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS agent_digest_log (
        id BIGSERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        week_of DATE NOT NULL,
        status TEXT,
        sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (email, week_of))""")


def _week_of():
    today = datetime.date.today()
    return today - datetime.timedelta(days=today.weekday())  # Monday of this ISO week


def _safe1(cur, sql, params=None, default=None):
    """Single-value read isolated by a SAVEPOINT — a failing query (e.g. a bad
    ::timestamptz cast) rolls back to the savepoint instead of poisoning the
    whole digest transaction."""
    try:
        cur.execute("SAVEPOINT _awd")
        cur.execute(sql, params or ())
        r = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT _awd")
        return r[0] if r else default
    except Exception:
        try: cur.execute("ROLLBACK TO SAVEPOINT _awd")
        except Exception: pass
        return default


def _safeall(cur, sql, params=None):
    try:
        cur.execute("SAVEPOINT _awd")
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT _awd")
        return rows
    except Exception:
        try: cur.execute("ROLLBACK TO SAVEPOINT _awd")
        except Exception: pass
        return []


def _changes(cur) -> dict:
    """The 'what changed this week' payload. NOTE: deals.created_at and
    discovered_facilities.discovered_at are TEXT — must cast ::timestamptz."""
    out = {"new_deals": 0, "deal_titles": [], "new_facilities": 0, "markets_tracked": 232}
    out["new_deals"] = int(_safe1(cur, "SELECT count(*) FROM deals WHERE created_at::timestamptz > NOW() - INTERVAL '7 days'", default=0) or 0)
    out["deal_titles"] = [r[0] for r in _safeall(cur,
        "SELECT COALESCE(title, company, target) FROM deals WHERE created_at::timestamptz > NOW() - INTERVAL '7 days' ORDER BY created_at::timestamptz DESC LIMIT 3") if r and r[0]]
    out["new_facilities"] = int(_safe1(cur, "SELECT count(*) FROM discovered_facilities WHERE discovered_at::timestamptz > NOW() - INTERVAL '7 days'", default=0) or 0)
    out["markets_tracked"] = int(_safe1(cur, "SELECT count(DISTINCT market) FROM dcpi_scores", default=232) or 232)
    return out


def _interests(cur, email: str) -> list:
    """Top markets/ISOs this email's keys actually queried — personalizes the digest."""
    rows = _safeall(cur, """SELECT lower(coalesce(l.params->>'market', l.params->>'iso', l.params->>'slug')) AS m, count(*)
                       FROM mcp_call_log l
                       WHERE l.api_key IN (SELECT api_key FROM mcp_dev_keys WHERE lower(email)=lower(%s))
                         AND l.timestamp > NOW() - INTERVAL '60 days'
                         AND coalesce(l.params->>'market', l.params->>'iso', l.params->>'slug') IS NOT NULL
                       GROUP BY 1 ORDER BY 2 DESC LIMIT 4""", (email,))
    return [r[0] for r in rows if r and r[0]]


def _html(email: str, interests: list, ch: dict) -> str:
    pretty = ", ".join(i.replace("-", " ").title() for i in interests[:4]) if interests else None
    yours = (f'<p style="margin:0 0 14px">The markets you looked at on DC Hub — '
             f'<strong>{pretty}</strong> — may have moved. Reconnect and call '
             f'<code>get_changes</code> to pull just the delta.</p>') if pretty else (
             '<p style="margin:0 0 14px">Reconnect and call <code>get_changes</code> to pull only '
             'what shifted since your last session — DCPI market movers, new facilities, new deals.</p>')
    deals = ""
    if ch.get("deal_titles"):
        items = "".join(f"<li style='margin:4px 0'>{t}</li>" for t in ch["deal_titles"])
        deals = f'<p style="margin:14px 0 4px"><strong>{ch["new_deals"]} new M&amp;A deals</strong> tracked this week, including:</p><ul style="margin:0 0 14px;padding-left:20px;color:#444">{items}</ul>'
    elif ch.get("new_deals"):
        deals = f'<p style="margin:14px 0"><strong>{ch["new_deals"]} new M&amp;A deals</strong> tracked this week.</p>'
    fac = f'<p style="margin:0 0 14px"><strong>{ch["new_facilities"]} newly discovered facilities</strong> added across {ch.get("markets_tracked",232)} tracked power markets.</p>' if ch.get("new_facilities") else ""
    return (f'<div style="font-family:-apple-system,Segoe UI,Inter,sans-serif;max-width:560px;margin:0 auto;color:#1a1a2e">'
            f'<h2 style="font-size:20px;margin:0 0 6px">What changed on DC Hub this week</h2>'
            f'<p style="color:#888;font-size:13px;margin:0 0 18px">The live data layer for AI agents — dchub.cloud</p>'
            f'{yours}{deals}{fac}'
            f'<p style="margin:18px 0 6px"><a href="https://dchub.cloud/mcp" style="background:#4F8FFF;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600">Reconnect → call get_changes</a></p>'
            f'<p style="color:#999;font-size:12px;margin:16px 0 0">You\'re getting this because you bound this email to a DC Hub MCP key for change alerts.</p>'
            f'</div>')


def run_digest(dry_run: bool = True, limit: int = 500) -> dict:
    """Build + send (or, in dry-run, just log) the weekly digest to the opted-in audience."""
    if _disabled():
        return {"ok": False, "reason": "disabled"}
    c = _conn()
    if c is None:
        return {"ok": False, "reason": "no_db"}
    week = _week_of()
    sent = skipped = errors = rendered = 0
    sample = None
    try:
        cur = c.cursor()
        _ensure(cur)
        try:
            from routes.marketing_send import marketing_recipients_from_keys, send_marketing_email
        except Exception:
            from marketing_send import marketing_recipients_from_keys, send_marketing_email  # type: ignore
        audience = marketing_recipients_from_keys(cur)[:limit]
        ch = _changes(cur)
        for email in audience:
            # idempotent: one per (email, week)
            cur.execute("SELECT 1 FROM agent_digest_log WHERE email=%s AND week_of=%s", (email, week))
            if cur.fetchone():
                skipped += 1
                continue
            html = _html(email, _interests(cur, email), ch)
            rendered += 1
            if sample is None:
                sample = {"email": email, "html_len": len(html)}
            if dry_run:
                continue
            res = send_marketing_email(email, "What changed on DC Hub this week",
                                       html, source="agent_winback_digest", from_name=_FROM_NAME)
            if res.get("sent"):
                sent += 1
                cur.execute("INSERT INTO agent_digest_log (email, week_of, status) VALUES (%s,%s,%s) ON CONFLICT (email, week_of) DO NOTHING",
                            (email, week, "sent"))
            elif res.get("suppressed"):
                skipped += 1
            else:
                errors += 1
        c.commit()
        cur.close()
        return {"ok": True, "dry_run": dry_run, "week_of": str(week), "audience": len(audience),
                "rendered": rendered, "sent": sent, "skipped": skipped, "errors": errors, "sample": sample,
                "changes": ch}
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return {"ok": False, "reason": str(e)[:200]}
    finally:
        _release(c)


@agent_winback_digest_bp.route("/api/v1/admin/agent-digest/preview", methods=["GET"])
def preview():
    """Render the digest for one email (no send, no consent gate — render only)."""
    email = (request.args.get("email") or "preview@example.com").strip().lower()
    c = _conn()
    interests, ch = [], {"new_deals": 0, "deal_titles": [], "new_facilities": 0, "markets_tracked": 232}
    if c is not None:
        try:
            cur = c.cursor()
            interests = _interests(cur, email)
            ch = _changes(cur)
            cur.close()
        except Exception:
            pass
        finally:
            _release(c)
    from flask import Response
    return Response(_html(email, interests, ch), mimetype="text/html")


@agent_winback_digest_bp.route("/api/v1/admin/agent-digest/send", methods=["POST"])
def send():
    """Run the digest. Admin-gated. DRY-RUN by default; pass ?live=1 AND env
    DCHUB_AGENT_DIGEST_DRY_RUN=0 to actually send."""
    admin = os.environ.get("DCHUB_ADMIN_KEY", "")
    if admin and request.headers.get("X-Admin-Key") != admin:
        return jsonify({"ok": False, "reason": "unauthorized"}), 403
    # live send requires BOTH the env flag off AND an explicit ?live=1
    want_live = request.args.get("live") == "1"
    dry = _dry_run() or not want_live
    resp = jsonify(run_digest(dry_run=dry))
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200
