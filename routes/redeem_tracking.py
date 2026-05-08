"""
redeem_tracking.py — track redeem URL clicks + form views + redemptions.

Three tracking events:
  1. /api/v1/redeem/click  — fires when user opens the redeem URL (page load)
  2. /api/v1/redeem/view   — fires when redemption form renders
  3. /api/v1/redeem/submit — fires on successful redemption

Each event creates a row in redeem_funnel_events table. The funnel page
then queries this table to surface real conversion rates per stage:
  paywall_hit -> click -> form_view -> redeem_complete -> upgrade_paid

Without this, we can't tell where the funnel leaks. With it, we know.
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request


redeem_tracking_bp = Blueprint("redeem_tracking", __name__, url_prefix="/api/v1/redeem")


def _dsn(): return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS redeem_funnel_events (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL CHECK (event_type IN ('paywall_hit', 'click', 'view', 'submit', 'verified', 'upgrade')),
    event_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source          TEXT,
    tool            TEXT,
    tier            TEXT,
    user_agent      TEXT,
    ip_hash         TEXT,
    referer         TEXT,
    redeem_token    TEXT,
    metadata        JSONB
);

CREATE INDEX IF NOT EXISTS ix_redeem_funnel_event_type_at ON redeem_funnel_events (event_type, event_at DESC);
CREATE INDEX IF NOT EXISTS ix_redeem_funnel_tool ON redeem_funnel_events (tool);
"""


def _ensure_table():
    if getattr(_ensure_table, "_done", False): return
    with _conn() as c, c.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        c.commit()
    _ensure_table._done = True


def _record_event(event_type, **fields):
    """Insert a funnel event row. Best-effort — swallow DB errors."""
    _ensure_table()
    import hashlib
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()[:16] if ip else None

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO redeem_funnel_events
                       (event_type, source, tool, tier, user_agent,
                        ip_hash, referer, redeem_token, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event_type,
                    fields.get("source") or request.args.get("source"),
                    fields.get("tool") or request.args.get("tool"),
                    fields.get("tier") or request.args.get("tier"),
                    request.headers.get("User-Agent", "")[:500],
                    ip_hash,
                    request.headers.get("Referer", "")[:500],
                    fields.get("redeem_token"),
                    json.dumps(fields.get("metadata") or {}),
                ),
            )
            c.commit()
    except Exception:
        pass


@redeem_tracking_bp.route("/click", methods=["GET", "POST"])
def track_click():
    """Fire when redeem URL is opened. Beacon-style — usually a 1px GIF or 204."""
    _record_event("click")
    return ("", 204)


@redeem_tracking_bp.route("/view", methods=["GET", "POST"])
def track_view():
    """Fire when redemption form renders fully (JS onload)."""
    _record_event("view")
    return ("", 204)


@redeem_tracking_bp.route("/submit", methods=["POST"])
def track_submit():
    """Fire from the submit button — JS sends this BEFORE network call."""
    p = request.get_json(silent=True) or {}
    _record_event("submit", metadata=p)
    return ("", 204)


@redeem_tracking_bp.route("/funnel-stats", methods=["GET"])
def funnel_stats():
    """Roll-up: per-stage conversion rates over rolling 30 days."""
    _ensure_table()

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT event_type, COUNT(*) AS n, COUNT(DISTINCT ip_hash) AS distinct_users
               FROM redeem_funnel_events
               WHERE event_at > NOW() - INTERVAL '30 days'
               GROUP BY event_type"""
        )
        rows = cur.fetchall()

        cur.execute(
            """SELECT tool, event_type, COUNT(*) AS n
               FROM redeem_funnel_events
               WHERE event_at > NOW() - INTERVAL '30 days'
                 AND tool IS NOT NULL
               GROUP BY tool, event_type
               ORDER BY n DESC"""
        )
        per_tool = cur.fetchall()

    by_event = {r[0]: {"events": int(r[1]), "distinct_users": int(r[2])} for r in rows}

    # Compute conversion rates between stages
    paywall = by_event.get("paywall_hit", {}).get("events", 0)
    click = by_event.get("click", {}).get("events", 0)
    view = by_event.get("view", {}).get("events", 0)
    submit = by_event.get("submit", {}).get("events", 0)
    verified = by_event.get("verified", {}).get("events", 0)
    upgrade = by_event.get("upgrade", {}).get("events", 0)

    rates = {}
    if paywall > 0: rates["paywall_to_click"] = round(click / paywall, 4)
    if click > 0: rates["click_to_view"] = round(view / click, 4)
    if view > 0: rates["view_to_submit"] = round(submit / view, 4)
    if submit > 0: rates["submit_to_verified"] = round(verified / submit, 4)
    if verified > 0: rates["verified_to_upgrade"] = round(upgrade / verified, 4)
    if paywall > 0: rates["overall_paywall_to_upgrade"] = round(upgrade / paywall, 6)

    return jsonify({
        "by_event": by_event,
        "conversion_rates": rates,
        "per_tool": [{"tool": r[0], "event": r[1], "count": int(r[2])} for r in per_tool[:50]],
        "windowed_30d": True,
    }), 200


@redeem_tracking_bp.route("/health", methods=["GET"])
def health():
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(event_at) FROM redeem_funnel_events")
        total, latest = cur.fetchone()
    return jsonify(
        status="ok",
        total_events=int(total or 0),
        latest_event=latest.isoformat() if latest else None,
    ), 200
