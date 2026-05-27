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


# Phase TT (2026-05-14): added `identify_shown` + `email_captured` to the
# event_type whitelist — the new anonymous->known stage of the funnel
# (value-moment email capture). The inline CHECK below covers fresh
# tables; the idempotent ALTER widens the constraint on tables that
# already exist with the old 6-value list.
MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS redeem_funnel_events (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL CHECK (event_type IN ('paywall_hit', 'click', 'view', 'submit', 'verified', 'upgrade', 'identify_shown', 'email_captured')),
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

-- Widen the event_type CHECK on already-existing tables. The original
-- inline CHECK is auto-named redeem_funnel_events_event_type_check;
-- drop it and re-add the full list under a stable name. Idempotent.
ALTER TABLE redeem_funnel_events DROP CONSTRAINT IF EXISTS redeem_funnel_events_event_type_check;
ALTER TABLE redeem_funnel_events DROP CONSTRAINT IF EXISTS redeem_funnel_events_event_type_v2;
ALTER TABLE redeem_funnel_events ADD CONSTRAINT redeem_funnel_events_event_type_v2
    CHECK (event_type IN ('paywall_hit', 'click', 'view', 'submit', 'verified', 'upgrade', 'identify_shown', 'email_captured'));
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


def record_funnel_event(event_type, *, tool=None, tier=None, source=None,
                        user_agent=None, ip=None, redeem_token=None,
                        metadata=None):
    """Phase MM (2026-05-14): context-INDEPENDENT funnel event recorder.

    `_record_event` above reads from Flask's `request` object — it only
    works inside a request handler for /click, /view, /submit. But the
    BIGGEST funnel stage — `paywall_hit` — happens deep in the MCP
    gating path (mcp_analytics_postgres.log_upgrade_signal), which has
    its own request context and none of the redeem-page query params.

    This version takes everything explicitly so the MCP gating layer
    can log a `paywall_hit` row for EVERY upgrade signal. Until now
    redeem_funnel_events had zero paywall_hit rows — the funnel had no
    top, so /funnel-stats conversion rates were meaningless.

    Best-effort — never raises. The caller's primary work (serving the
    MCP response) must never break because funnel logging hiccupped.
    """
    _ensure_table()
    import hashlib
    ip_hash = None
    if ip:
        ip_clean = str(ip).split(",")[0].strip()
        if ip_clean:
            ip_hash = hashlib.sha256(ip_clean.encode("utf-8")).hexdigest()[:16]
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO redeem_funnel_events
                       (event_type, source, tool, tier, user_agent,
                        ip_hash, referer, redeem_token, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    event_type,
                    source,
                    tool,
                    tier,
                    (user_agent or "")[:500],
                    ip_hash,
                    None,
                    redeem_token,
                    json.dumps(metadata or {}),
                ),
            )
            c.commit()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# Phase TT Increment 3 (2026-05-14): the nurture loop — welcome on capture.
#
# Increments 1 & 2 capture the email (via /keys/identify or /unlock).
# This is the first nurture touch: the moment a key is identified, its
# human gets a welcome email confirming what they unlocked, setting up
# the relationship, and planting the soft upgrade seed. It's what makes
# the "known" stage real — and the anchor the weekly digest + payment
# ask (Increment 3b/3c) will build on.
#
# Fire-and-forget (own daemon thread) so it never adds latency to the
# capture response. Deduped once-per-key via mcp_dev_keys.metadata.
# Best-effort: no Resend key, DB blip, send failure — all swallowed.
# ─────────────────────────────────────────────────────────────────────

def send_identify_welcome(email, api_key=None):
    """Fire-and-forget welcome email on email-capture. Returns
    immediately — the actual work runs in a daemon thread."""
    email = (email or "").strip().lower()
    if not email:
        return
    try:
        import threading
        threading.Thread(
            target=_send_identify_welcome_blocking,
            args=(email, api_key), daemon=True,
        ).start()
    except Exception:
        pass


def _send_identify_welcome_blocking(email, api_key):
    resend_key = os.environ.get("DCHUB_RESEND_API_KEY", "").strip()
    if not resend_key:
        return  # no email provider configured — best-effort, skip quietly

    # Dedup: one welcome per key. A re-identify (idempotent) must not
    # re-send.
    if api_key:
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT metadata->>'welcome_sent_at' FROM mcp_dev_keys WHERE api_key = %s",
                    (api_key,))
                row = cur.fetchone()
                if row and row[0]:
                    return
        except Exception:
            pass  # dedup check failed — a rare duplicate is harmless

    ident_limit = int(os.environ.get("MCP_IDENTIFIED_DAILY_LIMIT", "100"))
    free_limit = int(os.environ.get("MCP_FREE_DAILY_LIMIT", "25"))
    sender = os.environ.get("DCHUB_RESEND_FROM", "DC Hub <noreply@dchub.cloud>")
    html = f"""<!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:540px;margin:0 auto;padding:28px;color:#1a1a1a">
<div style="font-size:11px;color:#888;letter-spacing:.05em;text-transform:uppercase;margin-bottom:10px">DC Hub &middot; key identified</div>
<h2 style="margin:0 0 12px;font-size:22px">You're in — your DC Hub key is unlocked</h2>
<p style="color:#555;font-size:15px;line-height:1.55">Your AI assistant's DC Hub key is now tied to this email, which means:</p>
<ul style="color:#555;font-size:15px;line-height:1.7">
<li><strong>{ident_limit} MCP calls/day</strong> (up from {free_limit})</li>
<li>A weekly digest of the data-center markets your assistant queries</li>
<li>Alerts when a market you've looked at moves</li>
</ul>
<p style="color:#555;font-size:15px;line-height:1.55">Nothing else to do — your assistant already has the higher limit. The first market digest lands within a week.</p>
<p style="margin:22px 0"><a href="https://dchub.cloud/pricing" style="background:#1976d2;color:#fff;padding:11px 22px;border-radius:6px;text-decoration:none;font-weight:600;display:inline-block">Need 1,000/day + full data? See Developer &rarr;</a></p>
<hr style="border:0;border-top:1px solid #eee;margin:28px 0">
<p style="font-size:12px;color:#888">You're getting this because your AI assistant identified its DC Hub key with this email. <a href="https://dchub.cloud" style="color:#888">dchub.cloud</a></p>
</body></html>"""

    sent_ok = False
    try:
        import requests as _rq
        resp = _rq.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}",
                     "Content-Type": "application/json"},
            json={"from": sender, "to": [email],
                  "subject": "Your DC Hub key is unlocked — 100 calls/day + market digest",
                  "html": html},
            timeout=12,
        )
        sent_ok = resp.status_code in (200, 201)
    except Exception:
        sent_ok = False

    # Stamp the dedup marker only on a confirmed send, so a transient
    # failure is retried on the next capture touch rather than lost.
    if sent_ok and api_key:
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    """UPDATE mcp_dev_keys
                          SET metadata = COALESCE(metadata, '{}'::jsonb)
                                         || jsonb_build_object('welcome_sent_at', %s::text)
                        WHERE api_key = %s""",
                    (datetime.now(timezone.utc).isoformat(), api_key))
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

    # paywall_hit / verified / upgrade come from the authoritative
    # mcp_upgrade_signals + mcp_conversions tables (2026-05-14): the
    # MCP gating layer writes those directly — Phase MM's attempt to
    # mirror them into redeem_funnel_events hooked a code path the live
    # MCP server doesn't actually run, so the funnel top stayed empty
    # at 0 while 8K+ real signals/week landed in mcp_upgrade_signals.
    # Source the top + bottom of the funnel from where the data really
    # is; redeem_funnel_events still owns the click/view/submit middle.
    paywall_signals = 0
    verified_n = 0
    upgrade_n = 0
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

        # Authoritative top-of-funnel: every paywall the MCP gate fired.
        try:
            cur.execute(
                """SELECT COUNT(*) FROM mcp_upgrade_signals
                   WHERE created_at > NOW() - INTERVAL '30 days'"""
            )
            paywall_signals = int((cur.fetchone() or [0])[0] or 0)
        except Exception:
            c.rollback()

        # Authoritative bottom-of-funnel: actual paid conversions.
        try:
            cur.execute(
                """SELECT COUNT(*) FROM mcp_conversions
                   WHERE created_at > NOW() - INTERVAL '30 days'"""
            )
            upgrade_n = int((cur.fetchone() or [0])[0] or 0)
        except Exception:
            c.rollback()

    by_event = {r[0]: {"events": int(r[1]), "distinct_users": int(r[2])} for r in rows}

    # Compute conversion rates between stages. paywall_hit is sourced
    # from mcp_upgrade_signals (real data) rather than redeem_funnel_events.
    paywall = paywall_signals or by_event.get("paywall_hit", {}).get("events", 0)
    click = by_event.get("click", {}).get("events", 0)
    view = by_event.get("view", {}).get("events", 0)
    submit = by_event.get("submit", {}).get("events", 0)
    verified = by_event.get("verified", {}).get("events", 0) or submit
    upgrade = upgrade_n or by_event.get("upgrade", {}).get("events", 0)

    rates = {}
    if paywall > 0: rates["paywall_to_click"] = round(click / paywall, 4)
    if click > 0: rates["click_to_view"] = round(view / click, 4)
    if view > 0: rates["view_to_submit"] = round(submit / view, 4)
    if submit > 0: rates["submit_to_verified"] = round(verified / submit, 4)
    if verified > 0: rates["verified_to_upgrade"] = round(upgrade / verified, 4)
    if paywall > 0: rates["overall_paywall_to_upgrade"] = round(upgrade / paywall, 6)

    # Surface the headline leak explicitly so the dashboard doesn't have
    # to derive it. With paywall_hit now real (8K+/30d) and click ~0,
    # this pinpoints that the funnel leaks at the very first step:
    # the MCP paywall message isn't getting agents to open the redeem URL.
    biggest_leak = None
    _stages = [("paywall_hit", paywall), ("click", click), ("view", view),
               ("submit", submit), ("verified", verified), ("upgrade", upgrade)]
    for i in range(len(_stages) - 1):
        _from_n, _to_n = _stages[i][1], _stages[i + 1][1]
        if _from_n > 0 and (_to_n / _from_n) < 0.02:
            biggest_leak = {"between": f"{_stages[i][0]} -> {_stages[i+1][0]}",
                            "from": _from_n, "to": _to_n,
                            "rate": round(_to_n / _from_n, 5)}
            break

    return jsonify({
        "by_event": by_event,
        "funnel_counts": {"paywall_hit": paywall, "click": click, "view": view,
                          "submit": submit, "verified": verified, "upgrade": upgrade},
        "conversion_rates": rates,
        "biggest_leak": biggest_leak,
        "per_tool": [{"tool": r[0], "event": r[1], "count": int(r[2])} for r in per_tool[:50]],
        "windowed_30d": True,
    }), 200


# AUTO-REPAIR: duplicate route '/health' also in main.py:3566 — review and remove one
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
