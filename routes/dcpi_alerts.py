"""
Phase r43-A (2026-05-27) — DCPI verdict-shift email alerts.

Sticky distribution: users subscribe a list of markets, get an email
when any of those markets changes BUILD/CAUTION/AVOID. The signal is
already firing daily (autonomous-brain-power-plants triggers a DCPI
recompute, brain_consistency_radar tracks the diff). This module just
exposes a subscription surface + the cron hook to send the emails.

Endpoints:
  POST /api/v1/alerts/dcpi/subscribe   email + markets[] → row, free
  POST /api/v1/alerts/dcpi/unsubscribe email + token → soft-delete
  GET  /api/v1/alerts/dcpi/check       cron-fired (admin-gated); for
                                        each subscriber, find verdict
                                        shifts since last_notified_at,
                                        send digest, update last_notified_at
  GET  /api/v1/alerts/dcpi/stats       public; subscribers + last shift count

Free tier (any email): 5 markets per subscription.
Paid tier ($99/mo "DCPI Alerts"): unlimited + per-market threshold tuning.
"""

import os
import json
import secrets
import datetime
import logging
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
dcpi_alerts_bp = Blueprint("dcpi_alerts", __name__)

_ANON_MARKET_CAP = 5  # free tier cap


def _db():
    """Reuse the global pg connection helper."""
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_table():
    c = _db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dcpi_alert_subscriptions (
                    id              SERIAL PRIMARY KEY,
                    email           TEXT NOT NULL,
                    market_slugs    TEXT[] NOT NULL,
                    unsub_token     TEXT NOT NULL UNIQUE,
                    tier            TEXT DEFAULT 'free',
                    active          BOOLEAN DEFAULT TRUE,
                    last_notified_at TIMESTAMPTZ,
                    last_known_verdicts JSONB DEFAULT '{}'::jsonb,
                    created_at      TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dcpi_alert_email "
                         "ON dcpi_alert_subscriptions(email) "
                         "WHERE active = TRUE")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dcpi_alert_active "
                         "ON dcpi_alert_subscriptions(active) "
                         "WHERE active = TRUE")
            c.commit()
    except Exception as e:
        logger.warning(f"dcpi_alert_subscriptions table ensure failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


@dcpi_alerts_bp.route("/api/v1/alerts/dcpi/subscribe", methods=["POST"])
def subscribe():
    """email + markets[] → subscription row. Idempotent on email."""
    _ensure_table()
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    markets = data.get("markets") or []
    if not email or "@" not in email or len(email) > 200:
        return jsonify(ok=False, error="invalid_email"), 400
    if not isinstance(markets, list) or not markets:
        return jsonify(ok=False, error="markets_required",
                       hint="POST {\"email\": \"...\", \"markets\": [\"northern-virginia\", \"phoenix\"]}"), 400
    markets = [str(m).lower().strip() for m in markets if m][:_ANON_MARKET_CAP]
    token = secrets.token_urlsafe(16)

    c = _db()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            # Upsert by email — replace markets list
            cur.execute("""
                INSERT INTO dcpi_alert_subscriptions
                    (email, market_slugs, unsub_token, tier, active)
                VALUES (%s, %s, %s, 'free', TRUE)
                ON CONFLICT DO NOTHING
                RETURNING id, unsub_token
            """, (email, markets, token))
            row = cur.fetchone()
            if not row:
                # Already exists — update markets
                cur.execute("""
                    UPDATE dcpi_alert_subscriptions
                       SET market_slugs = %s, active = TRUE
                     WHERE email = %s
                     RETURNING id, unsub_token
                """, (markets, email))
                row = cur.fetchone()
            c.commit()
            sub_id, unsub = row
        return jsonify(
            ok=True,
            subscription_id=sub_id,
            email=email,
            markets=markets,
            tier="free",
            cap_note=(f"Free tier: up to {_ANON_MARKET_CAP} markets. "
                     f"Upgrade to DC Hub Pro Alerts for unlimited + custom thresholds."),
            unsubscribe_url=f"https://dchub.cloud/alerts/unsubscribe?token={unsub}",
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@dcpi_alerts_bp.route("/api/v1/alerts/dcpi/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    token = (request.args.get("token") or
              (request.get_json(force=True, silent=True) or {}).get("token") or "").strip()
    if not token:
        return jsonify(ok=False, error="token_required"), 400
    c = _db()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""UPDATE dcpi_alert_subscriptions
                              SET active = FALSE
                            WHERE unsub_token = %s
                            RETURNING email""", (token,))
            row = cur.fetchone()
            c.commit()
        if not row:
            return jsonify(ok=False, error="token_not_found"), 404
        return jsonify(ok=True, email=row[0], unsubscribed=True), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@dcpi_alerts_bp.route("/api/v1/alerts/dcpi/stats", methods=["GET"])
def stats():
    _ensure_table()
    c = _db()
    if c is None:
        return jsonify(subscriptions=0, error="no_database"), 200
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dcpi_alert_subscriptions WHERE active = TRUE")
            subs = int((cur.fetchone() or [0])[0])
            cur.execute("SELECT SUM(cardinality(market_slugs)) "
                         "FROM dcpi_alert_subscriptions WHERE active = TRUE")
            slot_total = int((cur.fetchone() or [0])[0] or 0)
        return jsonify(
            ok=True,
            active_subscriptions=subs,
            total_market_subscriptions=slot_total,
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@dcpi_alerts_bp.route("/api/v1/alerts/dcpi/check", methods=["POST", "GET"])
def check_and_send():
    """Cron-fired. For each active subscription, diff current verdicts
    against last_known_verdicts. If any market shifted, send digest +
    update. Admin-gated to prevent random callers triggering email blasts."""
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    if expected and provided != expected:
        return jsonify(ok=False, error="unauthorized"), 401

    _ensure_table()
    c = _db()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503

    # Pull current verdicts for ALL markets at once
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON (market_slug) market_slug, verdict
                            FROM market_power_scores
                           WHERE published = TRUE
                           ORDER BY market_slug, computed_at DESC""")
            current = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify(ok=False, error=f"verdict_fetch: {str(e)[:120]}"), 500

    sent = 0
    skipped = 0
    errors = []
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT id, email, market_slugs, last_known_verdicts, unsub_token
                            FROM dcpi_alert_subscriptions
                           WHERE active = TRUE""")
            subs = cur.fetchall() or []

        for sub_id, email, markets, last_known, unsub_token in subs:
            last_known = last_known or {}
            shifts = []
            for slug in (markets or []):
                cur_v = current.get(slug)
                prev_v = (last_known or {}).get(slug)
                if cur_v and prev_v and cur_v != prev_v:
                    shifts.append({"market": slug, "from": prev_v, "to": cur_v})
            if not shifts:
                skipped += 1
                continue

            # Build digest email
            def _shift_line(s):
                slug = s['market']
                return (f"  • {slug}: <strong>{s['from']}</strong> → "
                        f"<strong>{s['to']}</strong> "
                        f'(<a href="https://dchub.cloud/dcpi/{slug}">view →</a>)')
            shift_lines = "\n".join(_shift_line(s) for s in shifts)
            subject = f"DC Hub · {len(shifts)} DCPI verdict shift{'s' if len(shifts) > 1 else ''}"
            html = (
                f"<h2>DCPI verdict shifts in your tracked markets</h2>"
                f"<p>{shift_lines}</p>"
                f"<hr>"
                f"<p><small>Sent by DC Hub. <a href='https://dchub.cloud/api/v1/alerts/dcpi/"
                f"unsubscribe?token={unsub_token}'>Unsubscribe</a> · "
                f"<a href='https://dchub.cloud/dcpi'>Full DCPI</a></small></p>"
            )

            try:
                from email_service import send_email
                send_email(email, subject, html)
                sent += 1
            except Exception as _e:
                errors.append(f"{email}: {str(_e)[:100]}")
                continue

            # Update last_known_verdicts to reflect all current verdicts
            # for the tracked markets (so next call only fires on NEW shifts)
            new_known = dict(last_known)
            for slug in markets or []:
                if current.get(slug):
                    new_known[slug] = current[slug]
            try:
                with c.cursor() as cur2:
                    cur2.execute("""UPDATE dcpi_alert_subscriptions
                                      SET last_known_verdicts = %s,
                                          last_notified_at = NOW()
                                    WHERE id = %s""",
                                  (json.dumps(new_known), sub_id))
                    c.commit()
            except Exception as _e:
                errors.append(f"update {sub_id}: {str(_e)[:80]}")

        return jsonify(
            ok=True,
            checked_at=datetime.datetime.utcnow().isoformat() + "Z",
            subscribers_checked=len(subs),
            emails_sent=sent,
            no_shifts=skipped,
            errors=errors[:10],
        ), 200
    finally:
        try: c.close()
        except Exception: pass
