"""
watchlist.py — Real-time Watchlist + Verdict-Shift Alerts (2026-06-06).

Users pick DCPI markets to watch. When a market's DCPI verdict SHIFTS
(e.g. Phoenix CAUTION → AVOID), watchers get alerted.

  • FREE tier  → queued for the weekly digest (Monday 14:00 UTC).
  • PRO+ tier → alerted in near real-time (twice-daily cron sweep).

Channels: email (Resend) + browser push (pywebpush + VAPID). Browser
push is OPTIONAL — if VAPID_PRIVATE_KEY is unset, push degrades to a
no-op + the subscribe endpoint returns a clean 503 so the UI can hide
the button.

Endpoints
---------
POST /api/v1/watchlist/add        — public, IP rate-limited 10/day,
                                    email shape validated. Idempotent
                                    via (email_hash, market_slug, channel)
                                    UNIQUE.
POST /api/v1/watchlist/remove     — public, idempotent.
GET  /api/v1/watchlist/list       — requires ?email=…&token=… (HMAC
                                    of email + WATCHLIST_TOKEN_SECRET).
POST /api/v1/watchlist/push/subscribe
                                  — body: VAPID push-subscription JSON
                                    {endpoint, keys:{p256dh, auth}}.
                                    Stores under subscriber_email_hash;
                                    no-op-friendly if VAPID is unset.
GET  /watchlist                   — HTML page (form + current list).
                                    Email state lives in URL hash so the
                                    static page never leaks PII to the
                                    server's request log.

Safety
------
  • No raw email in logs — emails are SHA-256-hashed with the shared
    salt before persistence; we keep a masked form for the UI only.
  • IP rate-limited at 10 ADD calls per IP-hash per 24h.
  • Email shape validated (presence of '@', basic length cap).
  • Defensive on missing tables (lazy ensure on first call).
  • DB connection: psycopg2 / Neon (matches every sibling route).
"""
from __future__ import annotations

import os
import re
import sys
import hmac
import json
import time
import hashlib
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response

logger = logging.getLogger(__name__)

watchlist_bp = Blueprint("watchlist", __name__)


# ── Tunables ──────────────────────────────────────────────────────────
_RATE_LIMIT_PER_DAY = 10  # ADD calls per IP-hash per 24h
_VALID_CHANNELS = {"email", "push", "sms"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")

# Shared salt — same value as feedback_forum + every other hash-of-PII
# in the backend (we deliberately share so admin queries can join hashes
# across tables). Fall back to a stable default for local dev.
_IP_SALT = os.environ.get("DCHUB_HASH_SALT", "dchub-2026-salt-v1")

# Token secret for the GET /list endpoint — required so callers must
# prove they own the email before we hand back its watchlist.
_TOKEN_SECRET = (os.environ.get("WATCHLIST_TOKEN_SECRET")
                 or os.environ.get("DCHUB_SESSION_SECRET")
                 or os.environ.get("DCHUB_HASH_SALT")
                 or "watchlist-token-dev-secret")


# ── Plumbing ──────────────────────────────────────────────────────────
def _db_conn():
    """psycopg2 / Neon, short connect timeout. Returns None on failure
    so callers can degrade gracefully (the public ADD endpoint never
    500s — it returns a clean error dict)."""
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return None
        return psycopg2.connect(url, connect_timeout=5)
    except Exception as e:
        _log(f"db_conn_failed: {e}")
        return None


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[watchlist] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _client_ip() -> str:
    for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        v = request.headers.get(h, "").strip()
        if v:
            return v.split(",")[0].strip()
    return (request.remote_addr or "0.0.0.0").strip()


def _hash_email(email: str) -> str:
    return hashlib.sha256(
        ((email or "").lower().strip() + "|" + _IP_SALT).encode("utf-8")
    ).hexdigest()


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(((ip or "") + "|" + _IP_SALT).encode("utf-8")).hexdigest()


def _mask_email(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e:
        return ""
    local, _, domain = e.partition("@")
    if not local:
        return "*@" + domain
    if len(local) == 1:
        return local + "*@" + domain
    return local[0] + "*" * max(3, min(8, len(local) - 1)) + "@" + domain


def _valid_email(email: str) -> bool:
    e = (email or "").strip().lower()
    return bool(e) and len(e) <= 200 and bool(_EMAIL_RE.match(e))


def _valid_slug(slug: str) -> bool:
    s = (slug or "").strip().lower()
    return bool(s) and bool(_SLUG_RE.match(s))


def _valid_channel(channel: str) -> bool:
    return (channel or "").strip().lower() in _VALID_CHANNELS


def _email_token(email: str) -> str:
    """HMAC-sign the lowercased email with _TOKEN_SECRET so the user
    must possess the matching token to read their own watchlist via GET.
    A future /watchlist/verify endpoint can email this token; right now
    we accept it via query string (frontend stashes it in URL hash)."""
    e = (email or "").lower().strip()
    return hmac.new(_TOKEN_SECRET.encode("utf-8"),
                    e.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _verify_token(email: str, token: str) -> bool:
    if not email or not token:
        return False
    expected = _email_token(email)
    # constant-time compare
    return hmac.compare_digest(expected, (token or "").strip().lower())


# ── Schema ────────────────────────────────────────────────────────────
_TABLES_READY = False


def init_watchlist_tables() -> None:
    """Public boot-init hook. Wired from content_publisher.init_content_tables()
    so the tables exist before the FIRST API call. Idempotent CREATE
    TABLE IF NOT EXISTS + defensive ALTER ADD COLUMN IF NOT EXISTS.

    Defensive: never raises — a missing DB never takes down boot."""
    conn = _db_conn()
    if conn is None:
        _log("init_tables: no DB; skipping")
        return
    try:
        with conn.cursor() as cur:
            # user_watchlists ----------------------------------------
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_watchlists (
                        id                SERIAL PRIMARY KEY,
                        owner_email_hash  TEXT NOT NULL,
                        owner_email_masked TEXT,
                        market_slug       TEXT NOT NULL,
                        channel           TEXT NOT NULL DEFAULT 'email',
                        tier_at_signup    TEXT,
                        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        unsubscribed_at   TIMESTAMPTZ
                    )
                """)
                conn.commit()
            except Exception as e:
                _log(f"user_watchlists CREATE skipped: {e}")
                try: conn.rollback()
                except Exception: pass
            for col_def in [
                "owner_email_masked TEXT",
                "tier_at_signup TEXT",
                "unsubscribed_at TIMESTAMPTZ",
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE user_watchlists "
                        f"ADD COLUMN IF NOT EXISTS {col_def}"
                    )
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            for ix in [
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_watchlists_owner_slug_channel "
                "ON user_watchlists (owner_email_hash, market_slug, channel) "
                "WHERE unsubscribed_at IS NULL",
                "CREATE INDEX IF NOT EXISTS ix_user_watchlists_slug "
                "ON user_watchlists (market_slug) WHERE unsubscribed_at IS NULL",
                "CREATE INDEX IF NOT EXISTS ix_user_watchlists_owner "
                "ON user_watchlists (owner_email_hash, created_at DESC)",
            ]:
                try:
                    cur.execute(ix)
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass

            # watchlist_alerts_sent ----------------------------------
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS watchlist_alerts_sent (
                        id           SERIAL PRIMARY KEY,
                        watchlist_id INT NOT NULL,
                        market_slug  TEXT,
                        shift_from   TEXT,
                        shift_to     TEXT,
                        channel      TEXT,
                        status       TEXT NOT NULL DEFAULT 'sent',
                        sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        opened_at    TIMESTAMPTZ
                    )
                """)
                conn.commit()
            except Exception as e:
                _log(f"watchlist_alerts_sent CREATE skipped: {e}")
                try: conn.rollback()
                except Exception: pass
            for col_def in [
                "market_slug TEXT",
                "status TEXT NOT NULL DEFAULT 'sent'",
                "opened_at TIMESTAMPTZ",
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE watchlist_alerts_sent "
                        f"ADD COLUMN IF NOT EXISTS {col_def}"
                    )
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            for ix in [
                "CREATE INDEX IF NOT EXISTS ix_was_watchlist "
                "ON watchlist_alerts_sent (watchlist_id, sent_at DESC)",
                "CREATE INDEX IF NOT EXISTS ix_was_status_sent "
                "ON watchlist_alerts_sent (status, sent_at DESC)",
                "CREATE INDEX IF NOT EXISTS ix_was_slug_shift_day "
                "ON watchlist_alerts_sent (market_slug, shift_to, (sent_at::date))",
            ]:
                try:
                    cur.execute(ix)
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass

            # browser_push_subscriptions -----------------------------
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS browser_push_subscriptions (
                        id                   SERIAL PRIMARY KEY,
                        subscriber_email_hash TEXT,
                        endpoint             TEXT NOT NULL,
                        p256dh               TEXT,
                        auth                 TEXT,
                        user_agent           TEXT,
                        created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_used_at         TIMESTAMPTZ,
                        revoked_at           TIMESTAMPTZ
                    )
                """)
                conn.commit()
            except Exception as e:
                _log(f"browser_push_subscriptions CREATE skipped: {e}")
                try: conn.rollback()
                except Exception: pass
            for col_def in [
                "subscriber_email_hash TEXT",
                "last_used_at TIMESTAMPTZ",
                "revoked_at TIMESTAMPTZ",
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE browser_push_subscriptions "
                        f"ADD COLUMN IF NOT EXISTS {col_def}"
                    )
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            for ix in [
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_bps_endpoint "
                "ON browser_push_subscriptions (endpoint) "
                "WHERE revoked_at IS NULL",
                "CREATE INDEX IF NOT EXISTS ix_bps_email_hash "
                "ON browser_push_subscriptions (subscriber_email_hash) "
                "WHERE revoked_at IS NULL",
            ]:
                try:
                    cur.execute(ix)
                    conn.commit()
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
        global _TABLES_READY
        _TABLES_READY = True
        logger.info("watchlist: tables initialized")
    finally:
        try: conn.close()
        except Exception: pass


def _ensure_tables(cur=None) -> None:
    """Lazy-init guard. Called from every public endpoint just in case
    boot-init was skipped (e.g. the route file got imported before
    content_publisher.init_content_tables ran)."""
    if _TABLES_READY:
        return
    init_watchlist_tables()


# ── Rate limit ────────────────────────────────────────────────────────
def _ip_rate_limit_ok(ip_hash: str) -> bool:
    """10 ADD calls per IP-hash per 24h. Fail-OPEN if the DB is down
    (better to accept a few rows than to 503 every signup)."""
    conn = _db_conn()
    if conn is None:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM user_watchlists "
                "WHERE owner_email_hash IN ("
                "  SELECT owner_email_hash FROM user_watchlists "
                "  WHERE created_at > NOW() - INTERVAL '24 hours' LIMIT 1) "
                "AND created_at > NOW() - INTERVAL '24 hours'"
            )
            # The above is a rough budget. Refined: count rows where the
            # request's IP-hash matches one we stored — but we don't store
            # IP-hash to keep PII out of the table. Use a side table for
            # the rate counter so the watchlist table stays clean.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlist_ratelimit (
                    ip_hash    TEXT NOT NULL,
                    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_wlrl_ip_ts "
                "ON watchlist_ratelimit (ip_hash, ts DESC)"
            )
            try: conn.commit()
            except Exception: pass
            cur.execute(
                "SELECT COUNT(*) FROM watchlist_ratelimit "
                "WHERE ip_hash = %s AND ts > NOW() - INTERVAL '24 hours'",
                (ip_hash,)
            )
            row = cur.fetchone()
            n = int((row[0] if row else 0) or 0)
            if n >= _RATE_LIMIT_PER_DAY:
                return False
            cur.execute(
                "INSERT INTO watchlist_ratelimit (ip_hash) VALUES (%s) ON CONFLICT DO NOTHING",
                (ip_hash,)
            )
            conn.commit()
            return True
    except Exception as e:
        _log(f"rate_limit_check_failed: {e}")
        return True  # fail-open
    finally:
        try: conn.close()
        except Exception: pass


# ── Tier lookup ───────────────────────────────────────────────────────
def _lookup_tier(email: str) -> str:
    """Best-effort: look up the email's tier in mcp_dev_keys (canonical
    paid-tier table). Returns UPPER tier or 'FREE'. Never raises."""
    if not email:
        return "FREE"
    conn = _db_conn()
    if conn is None:
        return "FREE"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tier FROM mcp_dev_keys "
                "WHERE LOWER(email) = LOWER(%s) "
                "ORDER BY created_at DESC LIMIT 1",
                (email.strip(),)
            )
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0]).upper()
    except Exception as e:
        _log(f"lookup_tier_failed: {e}")
    finally:
        try: conn.close()
        except Exception: pass
    return "FREE"


# ── Endpoints ─────────────────────────────────────────────────────────
@watchlist_bp.route("/api/v1/watchlist/add", methods=["POST"])
def add_watchlist_entry():
    """Public. Add a market to the caller's watchlist.

    Body: {email, market_slug, channel}. Idempotent — re-adding the
    same (email, market, channel) triple no-ops + returns existing_id.
    Rate-limited 10/IP/24h."""
    _ensure_tables()
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    slug  = (body.get("market_slug") or "").strip().lower()
    channel = (body.get("channel") or "email").strip().lower()

    if not _valid_email(email):
        return jsonify(error="invalid_email", hint="Provide a valid email address."), 400
    if not _valid_slug(slug):
        return jsonify(error="invalid_market_slug",
                       hint="Pick a market from the autocomplete (e.g. 'phoenix')."), 400
    if not _valid_channel(channel):
        return jsonify(error="invalid_channel",
                       hint=f"Channel must be one of: {sorted(_VALID_CHANNELS)}"), 400

    ip_hash = _hash_ip(_client_ip())
    if not _ip_rate_limit_ok(ip_hash):
        return jsonify(error="rate_limited",
                       hint=f"Max {_RATE_LIMIT_PER_DAY} adds per IP per day."), 429

    email_hash = _hash_email(email)
    masked = _mask_email(email)
    tier = _lookup_tier(email)

    conn = _db_conn()
    if conn is None:
        return jsonify(error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            # Resurrect prior unsubscribe if it exists, else insert fresh.
            cur.execute("""
                SELECT id, unsubscribed_at
                  FROM user_watchlists
                 WHERE owner_email_hash = %s
                   AND market_slug      = %s
                   AND channel          = %s
                 ORDER BY id DESC LIMIT 1
            """, (email_hash, slug, channel))
            existing = cur.fetchone()
            if existing:
                wid, unsub = existing
                if unsub is not None:
                    cur.execute(
                        "UPDATE user_watchlists "
                        "SET unsubscribed_at = NULL, tier_at_signup = %s "
                        "WHERE id = %s", (tier, wid))
                    conn.commit()
                    return jsonify(ok=True, id=int(wid), resurrected=True,
                                   tier=tier, masked_email=masked), 200
                return jsonify(ok=True, id=int(wid), idempotent=True,
                               tier=tier, masked_email=masked), 200

            cur.execute("""
                INSERT INTO user_watchlists
                       (owner_email_hash, owner_email_masked, market_slug,
                        channel, tier_at_signup)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                RETURNING id
            """, (email_hash, masked, slug, channel, tier))
            new_id = cur.fetchone()[0]
            conn.commit()
            logger.info("watchlist add: slug=%s channel=%s tier=%s id=%s",
                        slug, channel, tier, new_id)
            return jsonify(
                ok=True, id=int(new_id), tier=tier, masked_email=masked,
                token=_email_token(email),
                cadence=("realtime" if tier not in ("FREE", "")
                         else "weekly_digest"),
            ), 200
    except Exception as e:
        _log(f"add_failed: {e}")
        try: conn.rollback()
        except Exception: pass
        return jsonify(error="add_failed", detail=str(e)[:140]), 500
    finally:
        try: conn.close()
        except Exception: pass


@watchlist_bp.route("/api/v1/watchlist/remove", methods=["POST"])
def remove_watchlist_entry():
    """Public + idempotent. Mark (email, market_slug, channel) as
    unsubscribed. Re-removing a row is a no-op."""
    _ensure_tables()
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    slug  = (body.get("market_slug") or "").strip().lower()
    channel = (body.get("channel") or "email").strip().lower()

    if not _valid_email(email):
        return jsonify(error="invalid_email"), 400
    if not _valid_slug(slug):
        return jsonify(error="invalid_market_slug"), 400
    if not _valid_channel(channel):
        return jsonify(error="invalid_channel"), 400

    email_hash = _hash_email(email)

    conn = _db_conn()
    if conn is None:
        return jsonify(error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_watchlists
                   SET unsubscribed_at = NOW()
                 WHERE owner_email_hash = %s
                   AND market_slug      = %s
                   AND channel          = %s
                   AND unsubscribed_at IS NULL
                RETURNING id
            """, (email_hash, slug, channel))
            row = cur.fetchone()
            conn.commit()
            return jsonify(ok=True, removed=bool(row),
                           id=int(row[0]) if row else None), 200
    except Exception as e:
        _log(f"remove_failed: {e}")
        try: conn.rollback()
        except Exception: pass
        return jsonify(error="remove_failed", detail=str(e)[:140]), 500
    finally:
        try: conn.close()
        except Exception: pass


@watchlist_bp.route("/api/v1/watchlist/list", methods=["GET"])
def list_watchlist():
    """Return the caller's watchlist. Requires ?email=…&token=… where
    token = HMAC(email, WATCHLIST_TOKEN_SECRET). The token is handed
    back by /add, so the typical UX is: user adds first market →
    stash token in localStorage → load on revisit."""
    _ensure_tables()
    email = (request.args.get("email") or "").strip().lower()
    token = (request.args.get("token") or "").strip()

    if not _valid_email(email):
        return jsonify(error="invalid_email"), 400
    if not _verify_token(email, token):
        return jsonify(error="invalid_token",
                       hint="Pass the token returned by /add for this email."), 403

    email_hash = _hash_email(email)
    conn = _db_conn()
    if conn is None:
        return jsonify(error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, market_slug, channel, tier_at_signup, created_at
                  FROM user_watchlists
                 WHERE owner_email_hash = %s
                   AND unsubscribed_at IS NULL
                 ORDER BY created_at DESC
            """, (email_hash,))
            items = []
            for r in cur.fetchall() or []:
                items.append({
                    "id":            int(r[0]),
                    "market_slug":   r[1],
                    "channel":       r[2],
                    "tier_at_signup": r[3],
                    "created_at":    r[4].isoformat() if r[4] else None,
                })
            tier = _lookup_tier(email)
            return jsonify(
                ok=True,
                masked_email=_mask_email(email),
                current_tier=tier,
                cadence=("realtime" if tier not in ("FREE", "") else "weekly_digest"),
                items=items,
                count=len(items),
            ), 200
    except Exception as e:
        _log(f"list_failed: {e}")
        return jsonify(error="list_failed", detail=str(e)[:140]), 500
    finally:
        try: conn.close()
        except Exception: pass


@watchlist_bp.route("/api/v1/watchlist/push/subscribe", methods=["POST"])
def push_subscribe():
    """Store a VAPID browser-push subscription so the dispatcher can
    fire push notifications at PRO+ watchers.

    Body: {endpoint, keys:{p256dh, auth}, email (optional)}.
    Returns 503 if VAPID isn't configured so the UI can hide the button.
    Idempotent on `endpoint` (revoked rows are resurrected)."""
    _ensure_tables()
    body = request.get_json(silent=True) or {}
    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth   = (keys.get("auth")   or "").strip()
    email  = (body.get("email")  or "").strip().lower()
    ua     = request.headers.get("User-Agent", "")[:300]

    # Defensive on VAPID env vars — degrade gracefully.
    if not (os.environ.get("VAPID_PRIVATE_KEY")
            and os.environ.get("VAPID_PUBLIC_KEY")):
        return jsonify(error="push_unavailable",
                       hint="VAPID is not configured on this server."), 503

    if not endpoint or not endpoint.startswith(("https://", "http://")):
        return jsonify(error="invalid_endpoint"), 400
    if not p256dh or not auth:
        return jsonify(error="missing_keys"), 400

    email_hash = _hash_email(email) if _valid_email(email) else None

    conn = _db_conn()
    if conn is None:
        return jsonify(error="db_unavailable"), 503
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, revoked_at FROM browser_push_subscriptions
                 WHERE endpoint = %s ORDER BY id DESC LIMIT 1
            """, (endpoint,))
            existing = cur.fetchone()
            if existing:
                sid, revoked = existing
                cur.execute(
                    "UPDATE browser_push_subscriptions "
                    "SET p256dh = %s, auth = %s, user_agent = %s, "
                    "    subscriber_email_hash = COALESCE(%s, subscriber_email_hash), "
                    "    last_used_at = NOW(), revoked_at = NULL "
                    "WHERE id = %s",
                    (p256dh, auth, ua, email_hash, sid))
                conn.commit()
                return jsonify(ok=True, id=int(sid), resurrected=bool(revoked)), 200
            cur.execute("""
                INSERT INTO browser_push_subscriptions
                       (subscriber_email_hash, endpoint, p256dh, auth,
                        user_agent, last_used_at)
                VALUES (%s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                RETURNING id
            """, (email_hash, endpoint, p256dh, auth, ua))
            new_id = cur.fetchone()[0]
            conn.commit()
            return jsonify(ok=True, id=int(new_id)), 200
    except Exception as e:
        _log(f"push_subscribe_failed: {e}")
        try: conn.rollback()
        except Exception: pass
        return jsonify(error="subscribe_failed", detail=str(e)[:140]), 500
    finally:
        try: conn.close()
        except Exception: pass


# ── HTML page ─────────────────────────────────────────────────────────
@watchlist_bp.route("/watchlist", methods=["GET"])
def watchlist_page():
    """Serve the static /watchlist.html from the frontend (Cloudflare
    Pages serves the canonical copy via _routes.json). This Flask route
    is the fallback for the rare case the request reaches the backend
    (e.g. local dev or CF outage). It re-fetches and returns the static
    HTML so we never diverge from the canonical UI."""
    try:
        # Best-effort: serve a minimal stub here. The canonical page is
        # at dchub-frontend/watchlist.html (CF Pages); _routes.json keeps
        # /watchlist OUTSIDE include so CF serves it directly. This
        # function exists only as a backend safety net.
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Watchlist — DC Hub</title>"
            "<meta http-equiv='refresh' content='0; url=/watchlist.html'>"
            "</head><body>Loading <a href='/watchlist.html'>"
            "/watchlist</a>…</body></html>"
        )
        return Response(html, mimetype="text/html")
    except Exception as e:
        return Response(f"<!doctype html><body>watchlist: {e}</body>",
                        mimetype="text/html", status=500)
