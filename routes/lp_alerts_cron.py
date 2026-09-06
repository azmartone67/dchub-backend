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
from routes._swallowed_writes import note_swallowed_write


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


# Phase 3 — CAN-SPAM suppression + one-click unsubscribe helpers. Guarded so
# a missing/broken module degrades gracefully and never blocks a send.
def _suppression():
    """Return (is_suppressed, unsub_link, list_unsubscribe_headers) or (None,)*3."""
    try:
        from routes.email_suppression import (
            is_suppressed, unsub_link, list_unsubscribe_headers)
        return is_suppressed, unsub_link, list_unsubscribe_headers
    except Exception:
        try:
            from email_suppression import (
                is_suppressed, unsub_link, list_unsubscribe_headers)
            return is_suppressed, unsub_link, list_unsubscribe_headers
        except Exception:
            return None, None, None


def _send_resend_email(to_email, subject, body_html, unsub_headers=None):
    """Returns (ok, info). Silent in dry-run mode."""
    if not _RESEND_KEY:
        return False, "no_resend_api_key"
    try:
        import requests
        _json = {
            "from": f"{_FROM_NAME} <{_FROM_EMAIL}>",
            "to":   [to_email],
            "subject": subject,
            "html":    body_html,
        }
        # RFC 2369 + RFC 8058 one-click unsubscribe headers (Resend "headers").
        if unsub_headers:
            _json["headers"] = {str(k): str(v) for k, v in unsub_headers.items()}
        r = requests.post(
            "https://api.resend.com/emails",
            json=_json,
            headers={"Authorization": f"Bearer {_RESEND_KEY}"},
            timeout=10,
        )
        if r.status_code < 300:
            return True, f"sent_status_{r.status_code}"
        return False, f"status_{r.status_code}_{r.text[:80]}"
    except Exception as e:
        return False, f"{type(e).__name__}:{str(e)[:60]}"


def _current_dcpi_for_market(cur, market: str | None, lat: float,
                             lon: float) -> tuple[float | None, str | None]:
    """(score, why_not). `why_not` is None on success and otherwise NAMES the
    condition — a caller that only ever saw None could not tell a swallowed
    exception from a missing market from a NULL score."""
    if market:
        try:
            cur.execute("""
                SELECT score AS v FROM market_power_scores
                 WHERE LOWER(market_name) = LOWER(%s)
                    OR LOWER(market_slug) = LOWER(%s)
                 ORDER BY computed_at DESC LIMIT 1
            """, (market, market.replace(" ", "-")))
            r = cur.fetchone()
            # ★ 2026-09-06 — KEY, NOT POSITION. `cur` is passed in from
            # fire_pending_alerts, where it is a RealDictCursor, so r[0] raised
            # KeyError(0) into the bare `except: pass` below and this returned
            # None on EVERY call. The caller reported that as "no_current_value"
            # — a plausible-sounding reason that was never true.
            if r is None:
                return None, "market_not_in_scores:%s" % (market or "")[:40]
            if r.get("v") is None:
                return None, "score_is_null:%s" % (market or "")[:40]
            return float(r["v"]), None
        except Exception as e:
            # ★ AND THE REASON NOW NAMES THE CAUSE. Returning a bare None made
            # THREE different conditions — a swallowed exception, no matching
            # market row, and a NULL score — indistinguishable at the only
            # surface anyone reads. That is what made the KeyError invisible for
            # as long as it was, and it would have hidden the next one too.
            return None, "dcpi_lookup_failed:%s" % type(e).__name__
    return None, "alert_has_no_market"


# Phase LLLL (2026-05-16) — capacity_change + new_facility_nearby
# trigger implementations. JJJJ shipped these as safe-skip stubs;
# this fills them in so PRO subscribers get the full alert suite.

def _current_capacity_for_market(cur, market: str | None) -> float | None:
    """Sum of operating MW for facilities in the given market. Best-
    effort — tolerates missing columns / market mismatches."""
    if not market:
        return None
    try:
        cur.execute("""
            SELECT COALESCE(SUM(power_mw), 0) AS v FROM discovered_facilities
             WHERE LOWER(COALESCE(market, '')) = LOWER(%s)
               AND COALESCE(is_duplicate, 0) = 0
               AND LOWER(COALESCE(status, '')) IN
                   ('operational','operating','live','active','running','in-service')
        """, (market,))
        r = cur.fetchone()
        # Same RealDictCursor, same defect: reported as "no_market_capacity".
        if r and r.get("v") is not None: return float(r["v"])
    except Exception: pass
    return None


def _new_facilities_within_radius(cur, lat: float, lon: float,
                                    since, radius_km: float = 50.0) -> int:
    """Count discovered_facilities with first_seen >= `since` whose
    great-circle distance from (lat, lon) is within `radius_km`.
    Uses cheap lat/lon bbox + haversine in SQL — no PostGIS required."""
    if since is None:
        # First-time alert — define `since` as 30 days ago so we don't
        # backfill the user with every facility ever indexed nearby.
        since = datetime.datetime.utcnow() - datetime.timedelta(days=30)
    try:
        # Crude bbox first (~111 km per degree latitude); haversine
        # narrows to the actual radius. This avoids a full table scan.
        deg_lat = radius_km / 111.0
        deg_lon = radius_km / (111.0 * max(0.01, abs((90.0 - abs(lat)) / 90.0) + 0.1))
        cur.execute("""
            SELECT COUNT(*) AS n FROM discovered_facilities
             WHERE first_seen >= %s
               AND COALESCE(is_duplicate, 0) = 0
               AND latitude  BETWEEN %s AND %s
               AND longitude BETWEEN %s AND %s
               AND (
                 6371.0 * acos(
                   LEAST(1.0, GREATEST(-1.0,
                     cos(radians(%s)) * cos(radians(latitude)) *
                     cos(radians(longitude) - radians(%s)) +
                     sin(radians(%s)) * sin(radians(latitude))
                   ))
                 )
               ) <= %s
        """, (since, lat - deg_lat, lat + deg_lat,
              lon - deg_lon, lon + deg_lon,
              lat, lon, lat, radius_km))
        r = cur.fetchone()
        # ★ The worst of the three. `(r or [0])[0]` reads as a null guard and is
        # not one: a non-empty RealDictRow is TRUTHY, so the fallback never
        # substituted and [0] raised KeyError(0) — caught below and returned 0.
        # The caller then reported "only_0_new_within_50km", which is a COUNT
        # this function never performed.
        return int((r and r.get("n")) or 0)
    except Exception:
        return 0


def _render_alert_html(site: dict, alert: dict, current_value: float | None,
                        previous_value: float | None,
                        unsub_url: str | None = None) -> str:
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
    # Real tokenized one-click unsubscribe (replaces bare "Reply to unsubscribe").
    if unsub_url:
        unsub_line = (f'<a href="{unsub_url}" style="color:#1e40af">Unsubscribe</a> '
                      f'from all DC Hub alert email, or delete the saved site via '
                      f'DELETE /api/v1/lp/saved/&lt;id&gt;.')
    else:
        unsub_line = ("Reply to unsubscribe this specific alert, or delete the "
                      "saved site via DELETE /api/v1/lp/saved/&lt;id&gt;.")
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
 {unsub_line}
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
                # ★ 2026-09-05 — KEY, NOT POSITION. This cursor is a
                # RealDictCursor, so a row is dict-like: `(row or [None])[0]`
                # never took the [None] fallback (a non-empty dict is TRUTHY)
                # and [0] was a key lookup -> KeyError(0). The except below
                # recorded "schema_probe_failed" and RETURNED, so this cron has
                # never fired an alert — while reporting a missing table that
                # exists. The wrong reason is the worse half: it sends whoever
                # reads the output hunting for a schema problem.
                cur.execute("SELECT to_regclass('public.saved_lp_alerts') AS reg")
                _probe = cur.fetchone()
                if not (_probe and _probe.get("reg")):
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
            _is_sup, _ulink, _lheaders = _suppression()

            for a in alerts:
                out["checked"] += 1
                email = (a["notify_email"] or "").strip().lower()
                if not email or "@" not in email:
                    out["skipped"].append({"alert_id": int(a["alert_id"]), "reason": "no_email"})
                    continue
                # Phase 3 — honor CAN-SPAM suppression list (opted-out addresses).
                if _is_sup:
                    try:
                        if _is_sup(cur, email):
                            out["skipped"].append({"alert_id": int(a["alert_id"]),
                                                    "reason": "suppressed"})
                            continue
                    except Exception:
                        pass
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

                dcpi_why = None
                if trigger == "dcpi_change":
                    curr, dcpi_why = _current_dcpi_for_market(
                        cur, a["market"], site["latitude"], site["longitude"])
                    if curr is None and a["dcpi_score_at_save"] is not None:
                        # Fall back: compare against initial score at save
                        prev = prev if prev is not None else float(a["dcpi_score_at_save"])
                elif trigger == "capacity_change":
                    # Phase LLLL (2026-05-16): operational MW in the
                    # saved site's market. Compare against last_value.
                    curr = _current_capacity_for_market(cur, a["market"])
                    if curr is None:
                        out["skipped"].append({"alert_id": int(a["alert_id"]),
                                                "reason": "no_market_capacity"})
                        continue
                elif trigger == "new_facility_nearby":
                    # Phase LLLL (2026-05-16): count of facilities first
                    # seen after our last_fired_at within 50km. Threshold
                    # is interpreted as MINIMUM new facilities (default 1).
                    new_count = _new_facilities_within_radius(
                        cur, site["latitude"], site["longitude"],
                        since=a.get("last_fired_at"),
                        radius_km=50.0)
                    curr = float(new_count)
                    # For this trigger, "prev" is implicit zero — fire
                    # whenever count >= threshold (treated as min, not
                    # delta — that's what users actually want).
                    if new_count < max(1, int(threshold)):
                        out["skipped"].append({"alert_id": int(a["alert_id"]),
                                                "reason": f"only_{new_count}_new_within_50km"})
                        # Still update last_value so the next compare
                        # starts from the current count
                        try:
                            cur.execute("UPDATE saved_lp_alerts SET last_value=%s WHERE id=%s",
                                        (curr, a["alert_id"]))
                        except Exception:
                            note_swallowed_write("saved_lp_alerts", where="lp_alerts_cron.fire_pending_alerts")
                            pass
                        continue
                    # Force fire by treating prev as None (first-time semantics)
                    prev = None
                else:
                    out["skipped"].append({"alert_id": int(a["alert_id"]),
                                            "reason": f"unknown_trigger:{trigger}"})
                    continue

                if curr is None:
                    # Carry the helper's specific reason when it gave one.
                    out["skipped"].append({
                        "alert_id": int(a["alert_id"]),
                        "reason": dcpi_why or "no_current_value"})
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
                    except Exception:
                        note_swallowed_write("saved_lp_alerts", where="lp_alerts_cron.fire_pending_alerts")
                        pass
                    continue

                # Fire (or pretend to in dry-run)
                # Tokenized one-click unsubscribe link + List-Unsubscribe headers.
                unsub_url = None
                unsub_headers = None
                if _ulink:
                    try:
                        unsub_url = _ulink(email)
                    except Exception:
                        unsub_url = None
                if _lheaders:
                    try:
                        unsub_headers = _lheaders(email)
                    except Exception:
                        unsub_headers = None

                subject = (f"DC Hub Alert: {site['name']} — "
                           f"{trigger.replace('_', ' ')} crossed {threshold}")
                body = _render_alert_html(site, alert, curr, prev, unsub_url=unsub_url)

                if dry_run:
                    out["fired"].append({"alert_id": int(a["alert_id"]),
                                          "to": email, "dry_run": True,
                                          "curr": curr, "prev": prev,
                                          "subject": subject})
                    continue

                ok, info = _send_resend_email(email, subject, body,
                                              unsub_headers=unsub_headers)
                if ok:
                    try:
                        cur.execute("""
                            UPDATE saved_lp_alerts
                               SET last_fired_at = NOW(),
                                   last_value = %s
                             WHERE id = %s
                        """, (curr, a["alert_id"]))
                    except Exception:
                        note_swallowed_write("saved_lp_alerts", where="lp_alerts_cron.fire_pending_alerts")
                        pass
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
