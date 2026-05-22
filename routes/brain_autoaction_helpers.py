"""brain_autoaction_helpers.py
=================================
Phase r33-F (2026-05-21). Helper endpoints that power the three
new autopilot auto-actions:

  • POST /api/v1/admin/route-redirect/add
      Insert a (from_path, to_path) entry in route_redirects.
      Used by _action_404_spike_add_redirect when the autopilot
      finds a high-confidence target for a 404'd URL.

  • POST /api/stripe/webhook/replay
      Triggers a Stripe API call to resend recent failed events.
      Used by _action_stripe_webhook_replay when the lag detector
      fires. No-op if STRIPE_SECRET_KEY env var is missing.

  • POST /api/v1/brain/alerts/critical
      Logs a critical alert to brain_critical_alerts. Used by
      _action_neon_replication_paging for severity=critical cases
      (unreachable replica or URL misconfig).

Auto-bootstrap: each endpoint runs CREATE TABLE IF NOT EXISTS on
its target table the first time it's called, so we don't need a
separate migration. Tables are tiny — at most a few hundred rows
ever.

Auth: all admin endpoints require X-Admin-Key (same pattern as
other admin routes). The Stripe replay endpoint accepts X-Internal-
Key (used by the brain autopilot's _post_json helper).
"""
from __future__ import annotations

import os
import time
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

brain_autoaction_helpers_bp = Blueprint(
    "brain_autoaction_helpers", __name__)


def _admin_authorized() -> bool:
    """Match the brain-autopilot _post_json admin-key contract. Either
    X-Admin-Key matches DCHUB_ADMIN_KEY OR X-Internal-Key matches
    DCHUB_INTERNAL_KEY. Brain calls both during a single action."""
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY"))
    if not admin_key:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "")
    return provided and provided == admin_key


def _db():
    """Returns a Neon connection or None. Tries main.get_pg_connection
    first (uses the existing pool), falls back to direct connect."""
    try:
        from main import get_pg_connection
        return get_pg_connection()
    except Exception:
        pass
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not url: return None
    try:
        return psycopg2.connect(url, sslmode="require",
                                connect_timeout=5)
    except Exception:
        return None


def _db_return(conn) -> None:
    """Return to pool or close."""
    if conn is None: return
    try:
        from main import return_pg_connection
        return_pg_connection(conn)
    except Exception:
        try: conn.close()
        except Exception: pass


# ──────────────────────────────────────────────────────────────────
# 1. /api/v1/admin/route-redirect/add
# ──────────────────────────────────────────────────────────────────
_REDIRECT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS route_redirects (
    id            SERIAL PRIMARY KEY,
    from_path     TEXT NOT NULL UNIQUE,
    to_path       TEXT NOT NULL,
    status_code   INTEGER NOT NULL DEFAULT 301,
    created_by    TEXT NOT NULL DEFAULT 'autopilot',
    confidence    NUMERIC(5,2),
    hit_count     BIGINT NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_hit_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS route_redirects_from_idx
    ON route_redirects (from_path);
"""


@brain_autoaction_helpers_bp.route(
    "/api/v1/admin/route-redirect/add", methods=["POST"])
def admin_route_redirect_add():
    """Autonomously register a 301 redirect.

    Body: {"from": "/old", "to": "/new", "confidence": 0.92,
           "created_by": "autopilot"}

    Cap: max 5 autopilot-created redirects per 24h. Manual entries
    (created_by != 'autopilot') bypass the cap."""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    src = (body.get("from") or "").strip()
    dst = (body.get("to") or "").strip()
    conf = body.get("confidence")
    by   = (body.get("created_by") or "autopilot").strip()
    status = int(body.get("status_code") or 301)
    if not src or not dst:
        return jsonify(error="from and to required"), 400
    if not src.startswith("/") or not dst.startswith("/"):
        return jsonify(error="paths must start with /"), 400
    if src == dst:
        return jsonify(error="from == to is a loop"), 400

    conn = _db()
    if conn is None:
        return jsonify(error="no_database"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(_REDIRECT_TABLE_DDL)
            # 24h autopilot cap
            if by == "autopilot":
                cur.execute("""
                    SELECT COUNT(*) FROM route_redirects
                     WHERE created_by = 'autopilot'
                       AND created_at > NOW() - INTERVAL '24 hours'
                """)
                recent = (cur.fetchone() or [0])[0]
                if recent >= 5:
                    return jsonify(
                        error="autopilot_rate_limit",
                        detail=("autopilot has already created 5+ "
                                "redirects in the last 24h; "
                                "escalate to a human"),
                        recent_count=int(recent),
                    ), 429
            cur.execute("""
                INSERT INTO route_redirects
                    (from_path, to_path, status_code, created_by, confidence)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (from_path) DO UPDATE
                   SET to_path = EXCLUDED.to_path,
                       status_code = EXCLUDED.status_code,
                       confidence = EXCLUDED.confidence,
                       created_by = EXCLUDED.created_by
                RETURNING id
            """, (src, dst, status, by, conf))
            new_id = (cur.fetchone() or [None])[0]
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.error("route-redirect/add failed: %s", e)
        return jsonify(error=str(e)[:200]), 500
    finally:
        _db_return(conn)
    return jsonify(success=True, id=new_id,
                   from_path=src, to_path=dst,
                   status_code=status, created_by=by), 200


@brain_autoaction_helpers_bp.route(
    "/api/v1/admin/route-redirect/list", methods=["GET"])
def admin_route_redirect_list():
    """List active redirects. Public read (the destinations are
    already user-visible in 30x responses)."""
    conn = _db()
    if conn is None:
        return jsonify(error="no_database"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(_REDIRECT_TABLE_DDL)
            cur.execute("""
                SELECT id, from_path, to_path, status_code, created_by,
                       confidence, hit_count, created_at, last_hit_at
                  FROM route_redirects
                 ORDER BY created_at DESC LIMIT 200
            """)
            rows = cur.fetchall()
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    finally:
        _db_return(conn)
    out = []
    for r in rows:
        out.append({
            "id": r[0], "from_path": r[1], "to_path": r[2],
            "status_code": r[3], "created_by": r[4],
            "confidence": float(r[5]) if r[5] is not None else None,
            "hit_count": int(r[6] or 0),
            "created_at": r[7].isoformat() if r[7] else None,
            "last_hit_at": r[8].isoformat() if r[8] else None,
        })
    return jsonify(redirects=out, count=len(out)), 200


# ──────────────────────────────────────────────────────────────────
# 2. /api/stripe/webhook/replay
# ──────────────────────────────────────────────────────────────────
@brain_autoaction_helpers_bp.route(
    "/api/stripe/webhook/replay", methods=["POST"])
def stripe_webhook_replay():
    """Replays recent Stripe events that should have hit our
    webhook but haven't been received in the last 2h.

    Strategy:
      1. Read MAX(received_at) from stripe_webhooks (or fallback
         tables) to get the gap.
      2. Hit Stripe's /v1/events?created[gte]={since} to list
         events since then.
      3. For each event, log it as 'replay_pending' to a
         stripe_webhook_replay_log so a follow-up cron can re-
         process them via our normal webhook handler. We don't
         actually call Stripe's re-deliver endpoint because that's
         throttled and the event_id is what matters — we have full
         payloads from the Events list call.

    Requires STRIPE_SECRET_KEY env. Returns 503 if missing."""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401

    stripe_key = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe_key:
        return jsonify(
            error="stripe_not_configured",
            detail="STRIPE_SECRET_KEY env missing — replay no-op",
        ), 503

    # Find the gap
    conn = _db()
    if conn is None:
        return jsonify(error="no_database"), 503
    last_seen_iso = None
    try:
        with conn.cursor() as cur:
            for tbl, col in (("stripe_webhooks", "received_at"),
                             ("stripe_webhook_log", "received_at"),
                             ("stripe_events", "created_at")):
                cur.execute("SELECT to_regclass(%s)",
                            (f"public.{tbl}",))
                if (cur.fetchone() or [None])[0]:
                    cur.execute(f"SELECT MAX({col}) FROM {tbl}")
                    last = (cur.fetchone() or [None])[0]
                    if last:
                        last_seen_iso = (
                            last if isinstance(last, str)
                            else last.isoformat())
                    break
    except Exception:
        pass
    finally:
        _db_return(conn)

    # Use a default 4h window if we can't find a last-seen
    since_epoch = int(time.time()) - 14400  # 4h
    if last_seen_iso:
        try:
            dt = _dt.datetime.fromisoformat(
                last_seen_iso.replace("Z", "+00:00"))
            since_epoch = int(dt.timestamp())
        except Exception:
            pass

    # Hit Stripe API
    import urllib.request as _ur, json as _json
    url = (
        f"https://api.stripe.com/v1/events"
        f"?created[gte]={since_epoch}&limit=50"
    )
    req = _ur.Request(url, headers={
        "Authorization": f"Bearer {stripe_key}",
        "Accept": "application/json",
        "User-Agent": "DCHub-StripeReplay/1.0",
    })
    try:
        with _ur.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return jsonify(
            error="stripe_api_error",
            detail=str(e)[:200],
        ), 502

    events = (data.get("data") or [])
    # Log to replay table for cron pickup
    conn = _db()
    if conn is None:
        return jsonify(
            success=True,
            note="found events but cannot log — DB unavailable",
            event_count=len(events),
        ), 200
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stripe_webhook_replay_log (
                    id          SERIAL PRIMARY KEY,
                    event_id    TEXT NOT NULL UNIQUE,
                    event_type  TEXT,
                    created_at  TIMESTAMPTZ,
                    queued_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ,
                    status      TEXT NOT NULL DEFAULT 'replay_pending'
                )
            """)
            queued = 0
            for e in events:
                eid = e.get("id")
                if not eid: continue
                created_at = _dt.datetime.fromtimestamp(
                    int(e.get("created") or 0), tz=_dt.timezone.utc)
                try:
                    cur.execute("""
                        INSERT INTO stripe_webhook_replay_log
                            (event_id, event_type, created_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                    """, (eid, e.get("type") or "", created_at))
                    if cur.rowcount > 0:
                        queued += 1
                except Exception:
                    continue
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(error=str(e)[:200]), 500
    finally:
        _db_return(conn)
    return jsonify(
        success=True,
        event_count=len(events),
        queued=queued,
        since_epoch=since_epoch,
    ), 200


# ──────────────────────────────────────────────────────────────────
# 3. /api/v1/brain/alerts/critical
# ──────────────────────────────────────────────────────────────────
_ALERTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS brain_critical_alerts (
    id            SERIAL PRIMARY KEY,
    severity      TEXT NOT NULL,
    issue         TEXT NOT NULL,
    finding_url   TEXT,
    detail        TEXT,
    source        TEXT NOT NULL DEFAULT 'autopilot',
    acknowledged  BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS brain_alerts_unack_idx
    ON brain_critical_alerts (acknowledged, created_at DESC);
"""


@brain_autoaction_helpers_bp.route(
    "/api/v1/brain/alerts/critical", methods=["POST"])
def brain_alerts_critical_create():
    """Log a critical alert. Body:
       {"severity": "critical|high|medium",
        "issue": "neon_replication_lag",
        "finding_url": "neon:read_replica",
        "detail": "..."}"""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True) or {}
    severity = (body.get("severity") or "medium").lower()
    if severity not in ("critical", "high", "medium", "low"):
        severity = "medium"
    issue = (body.get("issue") or "").strip()
    if not issue:
        return jsonify(error="issue required"), 400
    detail = (body.get("detail") or "")[:2000]
    url    = (body.get("finding_url") or "")
    source = (body.get("source") or "autopilot")

    conn = _db()
    if conn is None:
        return jsonify(error="no_database"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(_ALERTS_TABLE_DDL)
            # Dedup: don't insert if same (issue, finding_url,
            # severity) is already unacknowledged from the last 6h
            cur.execute("""
                SELECT id FROM brain_critical_alerts
                 WHERE issue = %s AND finding_url = %s
                   AND severity = %s AND acknowledged = FALSE
                   AND created_at > NOW() - INTERVAL '6 hours'
                 ORDER BY created_at DESC LIMIT 1
            """, (issue, url, severity))
            existing = cur.fetchone()
            if existing:
                return jsonify(success=True,
                               id=existing[0],
                               dedup="existing_unacknowledged"), 200
            cur.execute("""
                INSERT INTO brain_critical_alerts
                    (severity, issue, finding_url, detail, source)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (severity, issue, url, detail, source))
            new_id = (cur.fetchone() or [None])[0]
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(error=str(e)[:200]), 500
    finally:
        _db_return(conn)

    # Optional: notify via Slack/PagerDuty if env var set. Keep
    # this thin — no network call inside the request unless cheap.
    slack_url = os.environ.get("BRAIN_ALERT_WEBHOOK_URL")
    if slack_url and severity in ("critical", "high"):
        try:
            import urllib.request as _ur, json as _json
            payload = _json.dumps({
                "text": (
                    f":rotating_light: [{severity.upper()}] {issue}\n"
                    f"`{url}`\n{detail[:500]}"
                ),
            }).encode("utf-8")
            req = _ur.Request(slack_url, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": "DCHub-BrainAlerts/1.0",
            })
            _ur.urlopen(req, timeout=4).read()
        except Exception:
            pass  # Don't block the alert insert on Slack hiccups

    return jsonify(success=True, id=new_id,
                   severity=severity, issue=issue), 201


@brain_autoaction_helpers_bp.route(
    "/api/v1/brain/alerts/critical", methods=["GET"])
def brain_alerts_critical_list():
    """List unacknowledged critical alerts. Public read."""
    only_unack = (request.args.get("unack") or "1") != "0"
    conn = _db()
    if conn is None:
        return jsonify(error="no_database"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(_ALERTS_TABLE_DDL)
            q = """
                SELECT id, severity, issue, finding_url, detail,
                       source, acknowledged, created_at
                  FROM brain_critical_alerts
            """
            params = ()
            if only_unack:
                q += " WHERE acknowledged = FALSE"
            q += " ORDER BY created_at DESC LIMIT 100"
            cur.execute(q, params)
            rows = cur.fetchall()
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    finally:
        _db_return(conn)
    out = []
    for r in rows:
        out.append({
            "id": r[0], "severity": r[1], "issue": r[2],
            "finding_url": r[3], "detail": r[4],
            "source": r[5], "acknowledged": bool(r[6]),
            "created_at": r[7].isoformat() if r[7] else None,
        })
    return jsonify(alerts=out, count=len(out)), 200


# ──────────────────────────────────────────────────────────────────
# Phase r33-J+sweep (2026-05-21) — one-shot data hygiene endpoints.
# Operator-callable cleanup for state corruption detected by the
# brain that doesn't fit into the autopilot's per-pattern actions.
# ──────────────────────────────────────────────────────────────────


@brain_autoaction_helpers_bp.route(
    "/api/v1/admin/news/clamp-future-dates", methods=["POST"])
def admin_news_clamp_future_dates():
    """Clamp any news_articles row where published_at > NOW() to
    NOW(). Returns count of rows touched.

    Triggered by the user noting age=-960h on /system-status — a
    bad upstream feed wrote a row dated 40 days in the future.
    Combined with the news_engine.py ingest-side defensive clamp
    (r33-J+sweep), this prevents the issue both retroactively and
    prospectively."""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401
    conn = _db()
    if conn is None: return jsonify(error="no_database"), 503
    try:
        with conn.cursor() as cur:
            # Show what we'll touch before touching
            cur.execute("""
                SELECT id, title, published_at
                  FROM news_articles
                 WHERE published_at > NOW()
                 ORDER BY published_at DESC LIMIT 50
            """)
            preview = []
            for r in cur.fetchall():
                preview.append({
                    "id": r[0],
                    "title": (r[1] or "")[:80],
                    "published_at": r[2].isoformat() if r[2] else None,
                })
            # Now clamp them
            cur.execute("""
                UPDATE news_articles
                   SET published_at = NOW()
                 WHERE published_at > NOW()
            """)
            touched = cur.rowcount
            # r33-Q+news-table (2026-05-22): ALSO clamp the `news` table.
            # The freshness radar (routes/_freshness.py) reads `news`,
            # not news_articles — that's the table that showed the
            # "955h ahead" alarm. Different ingester (news_aggregator.py),
            # different column name (published_date). Clamp both so the
            # one admin call fixes the surface the radar actually checks.
            touched_news = 0
            try:
                cur.execute("""
                    UPDATE news
                       SET published_date = NOW()
                     WHERE published_date > NOW()
                """)
                touched_news = cur.rowcount
            except Exception:
                pass  # `news` table may not exist in all envs
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(error=str(e)[:200]), 500
    finally:
        _db_return(conn)
    return jsonify(success=True, touched=touched,
                   touched_news_table=touched_news,
                   preview=preview), 200


@brain_autoaction_helpers_bp.route(
    "/api/v1/admin/dcpi/trigger-recompute", methods=["POST"])
def admin_dcpi_trigger_recompute():
    """Trigger a DCPI recompute via the standard /api/v1/dcpi/recompute
    endpoint, with admin auth wrapped here so the autopilot doesn't
    need to also juggle DCHUB_DCPI_KEY (if separate)."""
    if not _admin_authorized():
        return jsonify(error="unauthorized"), 401
    import urllib.request as _ur, json as _json
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY"))
    if not admin_key:
        return jsonify(error="no_admin_key_env"), 503
    target = "http://localhost:8080/api/v1/dcpi/recompute"
    req = _ur.Request(target, method="POST")
    req.add_header("X-Admin-Key", admin_key)
    req.add_header("Content-Type", "application/json")
    try:
        with _ur.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return (body, resp.status,
                    {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify(error=str(e)[:200]), 502
