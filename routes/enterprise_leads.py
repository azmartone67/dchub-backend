"""
enterprise_leads.py — Phase ZZZZZ-round23 (2026-05-23).

Tools-roadmap Item 3: per-company enterprise leads pipeline.

When a whale matches `is_hosting=False AND company is not None AND
total_calls > 1000`, that's a high-confidence enterprise lead — a
real company hitting our MCP server hard. This module:

  1. Materializes those leads into an `enterprise_leads` table
     (idempotent — ON CONFLICT (company) DO UPDATE).
  2. Exposes admin endpoints to list, mark contacted, mark closed.
  3. Provides a daily cron POST that the autonomous brain can fire
     to refresh the leads from the latest whale data.

Companion to:
  - routes/bot_outreach.py (_compute_whales) — feeds in the candidates
  - routes/visitor_intelligence.py (_ipinfo_enrich) — supplies the IPinfo enrichment
  - dchub_outreach (lead outreach) — consumes the lead rows downstream
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, make_response

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

enterprise_leads_bp = Blueprint("enterprise_leads", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db or psycopg2 is None:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_table():
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS enterprise_leads (
                    id              SERIAL PRIMARY KEY,
                    company         TEXT NOT NULL UNIQUE,
                    asn             TEXT,
                    domain          TEXT,
                    country         TEXT,
                    city            TEXT,
                    first_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    total_calls     BIGINT DEFAULT 0,
                    days_active     INT DEFAULT 0,
                    distinct_ips    INT DEFAULT 1,
                    top_tools       TEXT,
                    suggested_action TEXT DEFAULT 'outreach',
                    status          TEXT DEFAULT 'new',
                    notes           TEXT,
                    contacted_at    TIMESTAMPTZ,
                    closed_at       TIMESTAMPTZ,
                    closed_outcome  TEXT,
                    last_refreshed  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_enterprise_leads_status
                  ON enterprise_leads(status, total_calls DESC)
            """)
    finally:
        try: c.close()
        except Exception: pass


def _refresh_leads_from_whales() -> dict:
    """Pull current whales, filter to non-hosting business IPs with
    a resolvable company name, upsert into enterprise_leads."""
    _ensure_table()
    try:
        from routes.bot_outreach import _compute_whales
        from routes.visitor_intelligence import _ipinfo_enrich
        from routes.brain_security_detectors import _is_hosting_ip
    except Exception as e:
        return {"ok": False, "error": f"module_import: {str(e)[:120]}"}

    whales = _compute_whales(min_days=3, min_calls_per_day=100) or []
    inserted = updated = skipped = 0
    leads_out = []
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db"}
    try:
        for w in whales:
            ip = (w.get("ip_address") or "").strip()
            if not ip:
                # _compute_whales returns hashed IPs only, so we lose
                # the raw value. Skip — leads need company resolution
                # via ip_company already present in the whale row.
                pass
            company = (w.get("ip_company") or "").strip()
            ip_type = (w.get("ip_type") or "").lower()
            total = int(w.get("total_calls_14d") or 0)
            # Skip if: no company name, hosting IP, low volume, or
            # action is throttle/block.
            if not company or ip_type == "hosting" or total < 1000:
                skipped += 1
                continue
            if w.get("suggested_action") == "block_or_throttle":
                skipped += 1
                continue
            top_tools = w.get("top_tools") or []
            top_tools_str = ", ".join(
                t.get("tool", "?") for t in top_tools[:3] if isinstance(t, dict)
            )[:300]
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO enterprise_leads
                        (company, country, city, total_calls, days_active,
                         top_tools, suggested_action, last_seen,
                         last_refreshed)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING, NOW())
                    ON CONFLICT (company) DO UPDATE SET
                        last_seen = EXCLUDED.last_seen,
                        total_calls = EXCLUDED.total_calls,
                        days_active = EXCLUDED.days_active,
                        top_tools = EXCLUDED.top_tools,
                        suggested_action = EXCLUDED.suggested_action,
                        last_refreshed = NOW()
                    RETURNING (xmax = 0) AS inserted
                """, (
                    company,
                    w.get("ip_country"),
                    w.get("ip_city"),
                    total,
                    int(w.get("days_active") or 0),
                    top_tools_str,
                    w.get("suggested_action") or "outreach",
                ))
                row = cur.fetchone()
                if row and row[0]:
                    inserted += 1
                else:
                    updated += 1
                leads_out.append({
                    "company": company,
                    "country": w.get("ip_country"),
                    "city": w.get("ip_city"),
                    "total_calls": total,
                    "action": w.get("suggested_action"),
                })
    finally:
        try: c.close()
        except Exception: pass
    return {"ok": True, "inserted": inserted, "updated": updated,
            "skipped": skipped, "leads_count": len(leads_out),
            "sample": leads_out[:10]}


@enterprise_leads_bp.route(
    "/api/v1/admin/enterprise-leads/refresh", methods=["POST"])
def admin_refresh_leads():
    """Admin-triggered: refresh enterprise_leads from current whales."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key"))
    try:
        from internal_auth import is_valid_internal_key
        if not is_valid_internal_key(provided):
            resp = make_response(jsonify(ok=False, error="unauthorized"), 401)
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp
    except Exception:
        return jsonify(ok=False, error="auth_module_unavailable"), 500
    result = _refresh_leads_from_whales()
    return jsonify(**result, as_of=datetime.now(timezone.utc).isoformat())


@enterprise_leads_bp.route(
    "/api/v1/admin/enterprise-leads", methods=["GET"])
def admin_list_leads():
    """Admin: list all enterprise leads. Optional filter ?status=new."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key"))
    try:
        from internal_auth import is_valid_internal_key
        if not is_valid_internal_key(provided):
            resp = make_response(jsonify(ok=False, error="unauthorized"), 401)
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp
    except Exception:
        return jsonify(ok=False, error="auth_module_unavailable"), 500
    _ensure_table()
    status_filter = (request.args.get("status") or "").strip()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    rows = []
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status_filter:
                cur.execute("""
                    SELECT * FROM enterprise_leads
                     WHERE status = %s
                     ORDER BY total_calls DESC LIMIT 100
                """, (status_filter,))
            else:
                cur.execute("""
                    SELECT * FROM enterprise_leads
                     ORDER BY total_calls DESC LIMIT 100
                """)
            rows = [dict(r) for r in cur.fetchall()]
            # Convert timestamps to ISO strings
            for r in rows:
                for k, v in list(r.items()):
                    if isinstance(v, datetime):
                        r[k] = v.isoformat()
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, count=len(rows), rows=rows)


@enterprise_leads_bp.route(
    "/api/v1/admin/enterprise-leads/<int:lead_id>/mark", methods=["POST"])
def admin_mark_lead(lead_id):
    """Update a lead's status. Body: {status: 'contacted'|'closed',
                                       outcome: '...', notes: '...'}."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key"))
    try:
        from internal_auth import is_valid_internal_key
        if not is_valid_internal_key(provided):
            resp = make_response(jsonify(ok=False, error="unauthorized"), 401)
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp
    except Exception:
        return jsonify(ok=False, error="auth_module_unavailable"), 500
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    outcome = (body.get("outcome") or "").strip() or None
    notes = (body.get("notes") or "").strip() or None
    if status not in ("new", "contacted", "qualified", "closed", "blocked"):
        return jsonify(ok=False, error="invalid_status",
                       allowed=["new", "contacted", "qualified", "closed", "blocked"]), 400
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE enterprise_leads
                   SET status = %s,
                       notes = COALESCE(%s, notes),
                       contacted_at = CASE WHEN %s = 'contacted'
                                            AND contacted_at IS NULL
                                            THEN NOW()
                                            ELSE contacted_at END,
                       closed_at = CASE WHEN %s IN ('closed','blocked')
                                         AND closed_at IS NULL
                                         THEN NOW()
                                         ELSE closed_at END,
                       closed_outcome = COALESCE(%s, closed_outcome)
                 WHERE id = %s
                RETURNING company, status, contacted_at, closed_at
            """, (status, notes, status, status, outcome, lead_id))
            r = cur.fetchone()
        if not r:
            return jsonify(ok=False, error="lead_not_found"), 404
        return jsonify(ok=True, id=lead_id, company=r[0], status=r[1],
                       contacted_at=r[2].isoformat() if r[2] else None,
                       closed_at=r[3].isoformat() if r[3] else None)
    finally:
        try: c.close()
        except Exception: pass
