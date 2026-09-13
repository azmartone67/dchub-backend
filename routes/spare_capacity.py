"""Phase CCCC (2026-05-16) Spare Capacity Marketplace, folded into Capacity Source.

★ 2026-09-13, owner decision: the Spare Capacity Marketplace is part of DC Hub
Capacity Source (routes/exclusive_listings.py, served at /listings). Two
supply-side listing products meant two inventories, two intakes and two stories
for an operator; now there is one of each, under one name.

Measured before the fold: spare_capacity_listings held no rows in any status
(GET /api/v1/spare-capacity/listings?status=all answered total 0), so there was
nothing to migrate. The table is left in place, and this module no longer runs
DDL against it.

  GET  /spare-capacity                   301 -> /listings#list-capacity
  GET  /spare-capacity/<ref>             301 -> /listings (no referral code was ever issued)
  POST /api/v1/spare-capacity/submit     410 Gone, pointing at the Capacity Source intake
  GET  /api/v1/spare-capacity/listings   read-only legacy feed, kept for what still
                                         reads it (the /transparency page did)

The feed keeps its response keys and adds `moved_to`. It serves live rows only
and no contact fields; moderation, if rows ever appear, belongs in the Capacity
Source admin API.
"""

from __future__ import annotations

import datetime
import os

from flask import Blueprint, jsonify, redirect, request


spare_capacity_bp = Blueprint("spare_capacity", __name__)

SITE = "https://dchub.cloud"
CAPACITY_SOURCE_URL = f"{SITE}/listings"
LIST_CAPACITY_URL = f"{SITE}/listings#list-capacity"
MOVED_MESSAGE = (
    "The Spare Capacity Marketplace is now part of DC Hub Capacity Source. "
    "List capacity, one site or a whole portfolio, at "
    f"{LIST_CAPACITY_URL}, or email hello@dchub.cloud."
)


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


@spare_capacity_bp.route("/api/v1/spare-capacity/submit", methods=["POST", "OPTIONS"])
def submit_listing():
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST",
        })
    return jsonify(ok=False, error="moved", status="moved",
                   message=MOVED_MESSAGE, moved_to=LIST_CAPACITY_URL), 410


@spare_capacity_bp.route("/api/v1/spare-capacity/listings", methods=["GET"])
def listings():
    """Legacy read-only feed: live rows, no contact fields, plus moved_to."""
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (ValueError, TypeError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page

    # r33 (2026-05-24): a public LIST endpoint degrades to an empty 200, not a
    # 503 — the underlying database issue surfaces via /api/health.
    c = _conn()
    if c is None:
        return jsonify(
            listings=[], total=0, page=1, per_page=per_page, moved_to=CAPACITY_SOURCE_URL,
            note="database transiently unreachable — listings empty",
        ), 200
    rows: list = []
    total = 0
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT referral_code, operator_name, location, state, market,
                       mw_available, ready_date, description, status,
                       tenant_inquiries, created_at
                  FROM spare_capacity_listings
                 WHERE status = %s
                 ORDER BY created_at DESC LIMIT %s OFFSET %s
            """, ("live", per_page, offset))
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) AS n FROM spare_capacity_listings WHERE status = %s",
                        ("live",))
            # RealDictCursor: fetchone() is a dict (r48, 2026-05-25).
            total = int((cur.fetchone() or {}).get("n", 0) or 0)
    except Exception as _qe:
        return jsonify(
            listings=[], total=0, page=1, per_page=per_page, moved_to=CAPACITY_SOURCE_URL,
            note=f"query failed gracefully: {type(_qe).__name__}",
        ), 200
    finally:
        try:
            c.close()
        except Exception:
            pass

    out = []
    for r in rows:
        out.append({
            "referral_code":    r["referral_code"],
            "operator_name":    r["operator_name"],
            "location":         r["location"],
            "state":            r["state"],
            "market":           r["market"],
            "mw_available":     float(r["mw_available"] or 0),
            "ready_date":       r["ready_date"],
            "description":      r["description"],
            "status":           r["status"],
            "tenant_inquiries": int(r["tenant_inquiries"] or 0),
            "created_at":       r["created_at"].isoformat() if r["created_at"] else None,
            "tracker_url":      f"{SITE}/spare-capacity/{r['referral_code']}",
        })

    resp = jsonify(listings=out, count=len(out), total=total, page=page,
                   per_page=per_page, status_filter="live", moved_to=CAPACITY_SOURCE_URL,
                   generated_at=datetime.datetime.now(datetime.timezone.utc)
                   .isoformat().replace("+00:00", "Z"))
    resp.headers["Cache-Control"] = "public, max-age=180"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@spare_capacity_bp.route("/spare-capacity", methods=["GET"], strict_slashes=False)
def spare_capacity_page():
    """The marketplace page now lives inside Capacity Source."""
    return redirect(LIST_CAPACITY_URL, code=301)


@spare_capacity_bp.route("/spare-capacity/<ref>", methods=["GET"], strict_slashes=False)
def spare_capacity_tracker(ref):
    """No referral code was ever issued (the table was empty at the fold), so
    every tracker URL lands on the Capacity Source catalog."""
    return redirect(CAPACITY_SOURCE_URL, code=301)
