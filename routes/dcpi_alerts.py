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
    _requested = [str(m).lower().strip() for m in markets if m]
    markets = _requested[:_ANON_MARKET_CAP]
    _over_cap = _requested[_ANON_MARKET_CAP:]
    token = secrets.token_urlsafe(16)

    c = _db()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        # ★ Which of these slugs can the daily check actually match? It reads
        # market_power_scores WHERE published = TRUE, so a slug absent from
        # that set can never produce a shift. Subscribing used to store it
        # anyway and answer ok=True naming it, so a typo -- or a retired twin
        # like "northern-virginia", which is what this endpoint's OWN 400 hint
        # suggests -- bought a confirmation for a watch that cannot fire.
        _known = set()
        try:
            with c.cursor() as _kc:
                _kc.execute("SELECT DISTINCT market_slug FROM market_power_scores "
                            "WHERE published = TRUE")
                _known = {r[0] for r in _kc.fetchall() if r and r[0]}
        except Exception:
            _known = set()          # cannot verify -> claim nothing below
        _verified = bool(_known)
        _unknown = [m for m in markets if m not in _known] if _verified else []
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
        _resp = dict(
            ok=True,
            subscription_id=sub_id,
            email=email,
            markets=markets,
            tier="free",
            cap_note=(f"Free tier: up to {_ANON_MARKET_CAP} markets. "
                     f"Upgrade to DC Hub Pro Alerts for unlimited + custom thresholds."),
            unsubscribe_url=f"https://dchub.cloud/alerts/unsubscribe?token={unsub}",
        )
        # Say what was NOT accepted. cap_note is an upsell, not a receipt --
        # it never named the markets silently dropped past the cap.
        if _over_cap:
            _resp["dropped_over_cap"] = _over_cap
        if not _verified:
            _resp["markets_verified"] = False
            _resp["note"] = ("market slugs could not be verified against "
                             "published scores on this request")
        elif _unknown:
            _resp["markets_verified"] = True
            _resp["unknown_markets"] = _unknown
            _resp["note"] = ("these slugs are not in the published DCPI score "
                             "set, so alerts for them cannot fire until they "
                             "are: " + ", ".join(_unknown))
        else:
            _resp["markets_verified"] = True
        return jsonify(**_resp), 200
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


def _persist_baseline(c, sub_id, markets, last_known, current, *, notified):
    """Record the verdicts just observed. Returns (changed, error_or_None).

    ★ MUST run on the no-shifts path too. Until 2026-09-06 the only call site
    sat below `if not shifts: continue` AND below the send, so the state that
    makes a shift detectable was written only after a shift had been detected
    and mailed. A subscription is born with '{}', so prev_v was None for every
    market, `shifts` was always empty, and the row could never leave that
    state. No subscriber has ever received a DCPI alert.

    `notified` also stamps last_notified_at -- seeding a baseline notifies
    nobody, so it must not claim to have.
    """
    new_known = dict(last_known or {})
    for slug in markets or []:
        if current.get(slug):
            new_known[slug] = current[slug]
    if new_known == (last_known or {}) and not notified:
        return False, None
    sql = ("""UPDATE dcpi_alert_subscriptions
                 SET last_known_verdicts = %s, last_notified_at = NOW()
               WHERE id = %s""" if notified else
           """UPDATE dcpi_alert_subscriptions
                 SET last_known_verdicts = %s
               WHERE id = %s""")
    try:
        with c.cursor() as cur2:
            cur2.execute(sql, (json.dumps(new_known), sub_id))
            c.commit()
        return True, None
    except Exception as _e:
        return False, "update %s: %s" % (sub_id, str(_e)[:80])


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
    skipped = 0        # had a baseline, genuinely did not move
    seeded = 0         # had none; one written now, comparison starts next run
    unresolvable = 0   # no tracked slug exists in `current` -- can never fire
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
                # Classify BEFORE writing, and name the condition. A single
                # "no_shifts" counter reported three different states, one of
                # which was "this comparison is structurally impossible".
                resolvable = [x for x in (markets or []) if current.get(x)]
                if not resolvable:
                    unresolvable += 1
                elif any(not last_known.get(x) for x in resolvable):
                    seeded += 1
                else:
                    skipped += 1
                _chg, _err = _persist_baseline(c, sub_id, markets, last_known,
                                               current, notified=False)
                if _err:
                    errors.append(_err)
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

            # Advance the baseline so the next call fires only on NEW shifts.
            _chg, _err = _persist_baseline(c, sub_id, markets, last_known,
                                           current, notified=True)
            if _err:
                errors.append(_err)

        return jsonify(
            ok=True,
            checked_at=datetime.datetime.utcnow().isoformat() + "Z",
            subscribers_checked=len(subs),
            emails_sent=sent,
            no_shifts=skipped,
            baseline_seeded=seeded,
            no_markets_resolvable=unresolvable,
            errors=errors[:10],
        ), 200
    finally:
        try: c.close()
        except Exception: pass
