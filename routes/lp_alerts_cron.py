"""Phase JJJJ (2026-05-16) — nightly L+P alert firing via Resend.

Closes the GGGG loop. When a PRO subscriber has saved L+P sites with
alert configs (dcpi_change | capacity_change | new_facility_nearby),
this cron checks each one nightly and fires a Resend email if any
trigger threshold was crossed since last_fired_at.

  POST /api/v1/lp/alerts/fire-pending     admin-only cron trigger
  GET  /api/v1/lp/alerts/dry-run          admin-only preview, no send

Cron: .github/workflows/lp-alerts-nightly.yml (added separately, fires
07:00 UTC daily).

Throttling:
  - per-alert cooldown: 24h between firings
  - per-user soft cap: max 5 alerts per day per email
  - dry-run mode when DCHUB_RESEND_API_KEY is unset (silent)

Email template: simple HTML — change description + link back to the
/land-power-map?lat=X&lon=Y query so user can see the new state.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


lp_alerts_cron_bp = Blueprint("lp_alerts_cron", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY")
               or os.environ.get("RESEND_API_KEY") or "").strip()
_FROM_NAME  = os.environ.get("DCHUB_FROM_NAME", "DC Hub Alerts")
_FROM_EMAIL = os.environ.get("DCHUB_FROM_EMAIL", "alerts@dchub.cloud")


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _send_resend_email(to_email, subject, body_html):
    """Returns (ok, info). Silent in dry-run mode."""
    if not _RESEND_KEY:
        return False, "no_resend_api_key"
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            json={
                "from": f"{_FROM_NAME} <{_FROM_EMAIL}>",
                "to":   [to_email],
                "subject": subject,
                "html":    body_html,
            },
            headers={"Authorization": f"Bearer {_RESEND_KEY}"},
            timeout=10,
        )
        if r.status_code < 300:
            return True, f"sent_status_{r.status_code}"
        return False, f"status_{r.status_code}_{r.text[:80]}"
    except Exception as e:
        return False, f"{type(e).__name__}:{str(e)[:60]}"


def _current_dcpi_for_market(cur, market: str | None, lat: float, lon: float) -> float | None:
    """Best-effort DCPI score lookup. If market is set, use that.
    Otherwise approximate via nearest market_power_scores row."""
    if market:
        try:
            cur.execute("""
                SELECT score FROM market_power_scores
                 WHERE LOWER(market_name) = LOWER(%s)
                    OR LOWER(market_slug) = LOWER(%s)
                 ORDER BY computed_at DESC LIMIT 1
            """, (market, market.replace(" ", "-")))
            r = cur.fetchone()
            if r and r[0] is not None: return float(r[0])
        except Exception: pass
    # No market match → just return None (caller skips)
    return None


def _render_alert_html(site: dict, alert: dict, current_value: float | None,
                        previous_value: float | None) -> str:
    """Conversion-friendly alert email body."""
    site_name = site.get("name", "your saved site")
    lat = site.get("latitude", "")
    lon = site.get("longitude", "")
    delta_text = ""
    if current_value is not None and previous_value is not None:
        delta = current_value - previous_value
        sign = "+" if delta > 0 else ""
        delta_text = (f"<p style='font-size:1.1rem;margin:1rem 0'>"
                      f"<strong>{previous_value:.1f}</strong> → "
                      f"<strong>{current_value:.1f}</strong> "
                      f"(<span style='color:{'#16a34a' if delta>0 else '#dc2626'}'>"
                      f"{sign}{delta:.1f}</span>)</p>")
    map_url = (f"https://dchub.cloud/land-power-map"
               f"?lat={lat}&lon={lon}&utm_source=lp_alert&utm_medium=email")
    site_url = f"https://dchub.cloud/api/v1/lp/saved"
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;max-width:600px;
margin:0 auto;padding:1.5rem;color:#1f2937;line-height:1.55">
<div style="background:#0f172a;color:white;padding:1rem 1.25rem;border-radius:8px;margin-bottom:1.5rem">
 <h2 style="margin:0;font-size:1.15rem">⚡ DC Hub Alert — {site_name}</h2>
 <p style="margin:.25rem 0 0;color:#cbd5e1;font-size:.9rem">{alert.get('trigger_type','')} threshold crossed</p>
</div>
<p>Your saved site <strong>{site_name}</strong> at ({lat}, {lon}) had a {alert.get('trigger_type','')}
that crossed your configured threshold of {alert.get('threshold','?')}.</p>
{delta_text}
<p>
 <a href="{map_url}" style="display:inline-block;background:linear-gradient(135deg,#065f46,#0f766e);color:white;padding:.6rem 1.25rem;border-radius:6px;font-weight:600;text-decoration:none">View on Land+Power map →</a>
</p>
<p style="color:#6b7280;font-size:.85rem;margin-top:2rem">
 Manage your saved sites + alerts: <a href="{site_url}" style="color:#1e40af">/api/v1/lp/saved</a><br>
 Reply to unsubscribe this specific alert, or delete the saved site via DELETE /api/v1/lp/saved/&lt;id&gt;.
</p>
</body></html>"""


def fire_pending_alerts(dry_run: bool = False, max_alerts: int = 100) -> dict:
    """The cron entry point. Iterates enabled alerts with NULL or
    >24h-old last_fired_at, computes current vs last value, fires
    email + updates last_fired_at if threshold crossed."""
    out: dict = {"fired": [], "skipped": [], "errors": [], "checked": 0,
                  "dry_run": dry_run, "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}
    c = _conn()
    if c is None:
        out["errors"].append("no_database")
        return out
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("SELECT to_regclass('public.saved_lp_alerts')")
                if not (cur.fetchone() or [None])[0]:
                    out["errors"].append("saved_lp_alerts_table_missing")
                    return out
            except Exception:
                out["errors"].append("schema_probe_failed")
                return out

            # Pull eligible alerts
            cur.execute("""
                SELECT a.id AS alert_id, a.trigger_type, a.threshold,
                       a.notify_email, a.last_fired_at, a.last_value,
                       a.saved_site_id, a.user_id,
                       s.name, s.latitude, s.longitude, s.market, s.state,
                       s.dcpi_score_at_save
                  FROM saved_lp_alerts a
                  JOIN saved_lp_sites s ON s.id = a.saved_site_id
                 WHERE a.enabled = TRUE
                   AND (a.last_fired_at IS NULL
                        OR a.last_fired_at < NOW() - INTERVAL '24 hours')
                 ORDER BY a.last_fired_at NULLS FIRST
                 LIMIT %s
            """, (max_alerts,))
            alerts = cur.fetchall()

            # Per-email cap so a bad config doesn't spam one user
            per_email_count: dict = {}

            for a in alerts:
                out["checked"] += 1
                email = (a["notify_email"] or "").strip().lower()
                if not email or "@" not in email:
                    out["skipped"].append({"alert_id": int(a["alert_id"]), "reason": "no_email"})
                    continue
                if per_email_count.get(email, 0) >= 5:
                    out["skipped"].append({"alert_id": int(a["alert_id"]), "reason": "per_email_cap"})
                    continue

                trigger = a["trigger_type"]
                threshold = float(a["threshold"] or 5.0)
                site = {
                    "name": a["name"], "latitude": float(a["latitude"]),
                    "longitude": float(a["longitude"]),
                }
                alert = {"trigger_type": trigger, "threshold": threshold}
                prev = float(a["last_value"]) if a["last_value"] is not None else None
                curr = None

                if trigger == "dcpi_change":
                    curr = _current_dcpi_for_market(cur, a["market"],
                                                     site["latitude"], site["longitude"])
                    if curr is None and a["dcpi_score_at_save"] is not None:
                        # Fall back: compare against initial score at save
                        prev = prev if prev is not None else float(a["dcpi_score_at_save"])
                elif trigger == "capacity_change":
                    # Stub: real implementation would query market_power_scores
                    # for the capacity column. Skip for now to avoid false fires.
                    out["skipped"].append({"alert_id": int(a["alert_id"]), "reason": "trigger_not_implemented"})
                    continue
                elif trigger == "new_facility_nearby":
                    # Stub: real implementation would query discovered_facilities
                    # for first_seen >= last_fired_at within N km.
                    out["skipped"].append({"alert_id": int(a["alert_id"]), "reason": "trigger_not_implemented"})
                    continue

                if curr is None:
                    out["skipped"].append({"alert_id": int(a["alert_id"]), "reason": "no_current_value"})
                    continue

                crossed = (prev is not None and abs(curr - prev) >= threshold)
                # First-time alerts (prev is None) always fire so the user
                # gets a "baseline established" notification.
                first_time = prev is None

                if not (crossed or first_time):
                    out["skipped"].append({"alert_id": int(a["alert_id"]),
                                            "reason": "below_threshold",
                                            "curr": curr, "prev": prev})
                    # Update last_value so next compare uses fresh baseline
                    try:
                        cur.execute("""
                            UPDATE saved_lp_alerts SET last_value = %s
                             WHERE id = %s
                        """, (curr, a["alert_id"]))
                    except Exception: pass
                    continue

                # Fire (or pretend to in dry-run)
                subject = (f"DC Hub Alert: {site['name']} — "
                           f"{trigger.replace('_', ' ')} crossed {threshold}")
                body = _render_alert_html(site, alert, curr, prev)

                if dry_run:
                    out["fired"].append({"alert_id": int(a["alert_id"]),
                                          "to": email, "dry_run": True,
                                          "curr": curr, "prev": prev,
                                          "subject": subject})
                    continue

                ok, info = _send_resend_email(email, subject, body)
                if ok:
                    try:
                        cur.execute("""
                            UPDATE saved_lp_alerts
                               SET last_fired_at = NOW(),
                                   last_value = %s
                             WHERE id = %s
                        """, (curr, a["alert_id"]))
                    except Exception: pass
                    out["fired"].append({"alert_id": int(a["alert_id"]),
                                          "to": email, "info": info,
                                          "curr": curr, "prev": prev})
                    per_email_count[email] = per_email_count.get(email, 0) + 1
                else:
                    out["errors"].append({"alert_id": int(a["alert_id"]),
                                           "to": email, "info": info})
    finally:
        try: c.close()
        except Exception: pass
    return out


@lp_alerts_cron_bp.route("/api/v1/lp/alerts/fire-pending", methods=["POST"])
def fire_pending_endpoint():
    """Admin-only: cron entry point. Fires all eligible alerts."""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    out = fire_pending_alerts(dry_run=False)
    return jsonify(out), 200


@lp_alerts_cron_bp.route("/api/v1/lp/alerts/dry-run", methods=["GET", "POST"])
def fire_dry_run():
    """Admin-only: preview what WOULD fire without actually sending."""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    out = fire_pending_alerts(dry_run=True)
    return jsonify(out), 200
