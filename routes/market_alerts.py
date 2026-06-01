"""
market_alerts.py — Phase FF: the market-movement alerts primitive.

This is the shared spine the welcome email already promises ("alerts when
a tracked market moves") and the foundation both the buyer-facing surface
(Track 4: saved searches + email alerts) and the agent-facing surface
(Track 3: webhooks) build on. Build the detection + delivery once, expose
it through two channels.

How it works
------------
A "market" is a DCPI-scored metro (market_power_scores.market_slug).
"Movement" is a meaningful day-over-day change in that market's headline
signals — a verdict flip (BUILD <-> CAUTION <-> AVOID), or a large shift
in the constraint score or time-to-power.

Three tables:
  market_movement_snapshots — append-only time series, one row per market
      per detection pass. The diff baseline.
  market_movement_events    — append-only log, one row per detected
      movement. Also the audit trail of what got delivered.
  market_subscriptions      — who wants alerts for which market, on which
      channel (email | webhook). UNIQUE(market_slug, channel, destination)
      so re-subscribing reactivates rather than duplicates.

The detection pass (POST /api/v1/alerts/run, admin-gated, cron-driven):
  1. snapshot every market's current signals (skipped if the last
     snapshot for that market is < MIN_SNAPSHOT_GAP_HOURS old, so a
     manual re-run mid-day doesn't corrupt the day-over-day baseline)
  2. diff each market's two most-recent snapshots -> movement events
  3. for every market with new events, fan out to its active
     subscriptions: one grouped delivery per destination (email digest
     or webhook POST), best-effort
  4. stamp events notified, stamp subscriptions last_notified_at

Dry-run by default — ?send=true delivers. Snapshots + event rows are
still written on a dry run (so the baseline advances); only delivery is
gated. Best-effort throughout: one market's or one destination's hiccup
never aborts the pass.

Public endpoints (subscribe / unsubscribe / list) are intentionally
lightweight so both the web app and the MCP layer can call them.
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, request

market_alerts_bp = Blueprint("market_alerts", __name__)

ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
RESEND_KEY = os.environ.get("DCHUB_RESEND_API_KEY", "").strip()
FROM_EMAIL = os.environ.get("DCHUB_RESEND_FROM", "DC Hub <noreply@dchub.cloud>")

# Don't write a fresh snapshot if the last one for a market is younger
# than this — keeps the diff baseline a clean ~day-over-day cadence even
# if the run endpoint is hit manually between cron ticks.
MIN_SNAPSHOT_GAP_HOURS = float(os.environ.get("DCHUB_ALERTS_SNAPSHOT_GAP_HOURS", "20"))

# Movement thresholds — a numeric signal has to move at least this much
# (absolute) to count. Verdict flips always count regardless.
CONSTRAINT_THRESHOLD = float(os.environ.get("DCHUB_ALERTS_CONSTRAINT_DELTA", "8"))
TTP_THRESHOLD_MONTHS = float(os.environ.get("DCHUB_ALERTS_TTP_DELTA_MONTHS", "3"))

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS market_movement_snapshots (
    id                    BIGSERIAL PRIMARY KEY,
    market_slug           TEXT NOT NULL,
    verdict               TEXT,
    constraint_score      REAL,
    excess_power_score    REAL,
    time_to_power_months  REAL,
    captured_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mms_slug_at
    ON market_movement_snapshots (market_slug, captured_at DESC);

CREATE TABLE IF NOT EXISTS market_movement_events (
    id           BIGSERIAL PRIMARY KEY,
    market_slug  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    detail       JSONB,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_mme_slug_at
    ON market_movement_events (market_slug, detected_at DESC);
CREATE INDEX IF NOT EXISTS ix_mme_unnotified
    ON market_movement_events (detected_at) WHERE notified_at IS NULL;

CREATE TABLE IF NOT EXISTS market_subscriptions (
    id               BIGSERIAL PRIMARY KEY,
    market_slug      TEXT NOT NULL,
    channel          TEXT NOT NULL CHECK (channel IN ('email', 'webhook')),
    destination      TEXT NOT NULL,
    api_key          TEXT,
    source           TEXT,
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_notified_at TIMESTAMPTZ,
    UNIQUE (market_slug, channel, destination)
);
CREATE INDEX IF NOT EXISTS ix_msub_slug_active
    ON market_subscriptions (market_slug) WHERE active;
"""


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)


def _ensure_schema():
    if getattr(_ensure_schema, "_done", False):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
            c.commit()
        _ensure_schema._done = True
    except Exception as e:
        import sys
        print(f"[market_alerts] schema init failed: {e}", file=sys.stderr)


try:
    _ensure_schema()
except Exception:
    pass


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        provided = (request.headers.get("X-Admin-Key")
                    or request.args.get("admin_key") or "").strip()
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        return fn(*a, **kw)
    return w


# ─────────────────────────────────────────────────────────────────────
# Subscribe / unsubscribe / list — the public surface both web + MCP use
# ─────────────────────────────────────────────────────────────────────

def _known_slugs():
    """Distinct market slugs that DCPI actually scores — the valid
    subscription targets. Returns a set; empty on any DB hiccup."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT DISTINCT market_slug FROM market_power_scores")
            return {r[0] for r in cur.fetchall() if r[0]}
    except Exception:
        return set()


def _norm_slug(s):
    return re.sub(r"[^a-z0-9-]+", "-", (s or "").strip().lower()).strip("-")


@market_alerts_bp.route("/api/v1/alerts/subscribe", methods=["POST"])
def subscribe():
    """Subscribe a destination to movement alerts for a market.

    Body: {market, channel, destination}
      channel     'email' | 'webhook'
      destination email address, or an https:// webhook URL
    An X-API-Key header (if present) is recorded for attribution. Re-
    subscribing the same (market, channel, destination) reactivates the
    existing row rather than creating a duplicate.
    """
    _ensure_schema()
    body = request.get_json(silent=True) or {}
    slug = _norm_slug(body.get("market") or body.get("market_slug") or "")
    channel = (body.get("channel") or "").strip().lower()
    destination = (body.get("destination") or "").strip()
    api_key = (request.headers.get("X-API-Key") or body.get("api_key") or "").strip() or None
    source = (body.get("source") or ("mcp" if api_key else "web")).strip()[:40]

    if not slug:
        return jsonify(ok=False, error="missing market"), 400
    if channel not in ("email", "webhook"):
        return jsonify(ok=False, error="channel must be 'email' or 'webhook'"), 400
    if channel == "email":
        if not _EMAIL_RE.match(destination) or len(destination) > 254:
            return jsonify(ok=False, error="invalid email destination"), 400
        destination = destination.lower()
    else:
        if not destination.startswith("https://") or len(destination) > 500:
            return jsonify(ok=False, error="webhook destination must be an https:// URL"), 400

    known = _known_slugs()
    if known and slug not in known:
        return jsonify(ok=False, error="unknown market",
                       hint="market must be a DCPI-scored slug",
                       sample_markets=sorted(known)[:12]), 404

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO market_subscriptions
                       (market_slug, channel, destination, api_key, source, active)
                   VALUES (%s, %s, %s, %s, %s, TRUE)
                   ON CONFLICT (market_slug, channel, destination)
                   DO UPDATE SET active = TRUE,
                                 api_key = COALESCE(EXCLUDED.api_key,
                                                    market_subscriptions.api_key),
                                 source = EXCLUDED.source
                   RETURNING id, created_at""",
                (slug, channel, destination, api_key, source),
            )
            row = cur.fetchone()
            c.commit()
        return jsonify(ok=True, subscription_id=row[0], market=slug,
                       channel=channel, message="subscribed to market-movement alerts"), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


@market_alerts_bp.route("/api/v1/alerts/unsubscribe", methods=["POST"])
def unsubscribe():
    """Deactivate a subscription. Body: {market, channel, destination}.
    Omit `market` to unsubscribe a destination from ALL markets."""
    _ensure_schema()
    body = request.get_json(silent=True) or {}
    slug = _norm_slug(body.get("market") or body.get("market_slug") or "")
    channel = (body.get("channel") or "").strip().lower()
    destination = (body.get("destination") or "").strip().lower()
    if not destination or channel not in ("email", "webhook"):
        return jsonify(ok=False, error="channel + destination required"), 400
    try:
        with _conn() as c, c.cursor() as cur:
            if slug:
                cur.execute(
                    """UPDATE market_subscriptions SET active = FALSE
                        WHERE market_slug = %s AND channel = %s
                          AND LOWER(destination) = %s""",
                    (slug, channel, destination))
            else:
                cur.execute(
                    """UPDATE market_subscriptions SET active = FALSE
                        WHERE channel = %s AND LOWER(destination) = %s""",
                    (channel, destination))
            n = cur.rowcount
            c.commit()
        return jsonify(ok=True, deactivated=n), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


@market_alerts_bp.route("/api/v1/alerts/list", methods=["GET"])
def list_subscriptions():
    """List active subscriptions for a destination or an api_key.
    Query: ?destination=... or ?api_key=... (X-API-Key header also works)."""
    _ensure_schema()
    destination = (request.args.get("destination") or "").strip().lower()
    api_key = (request.headers.get("X-API-Key")
               or request.args.get("api_key") or "").strip()
    if not destination and not api_key:
        return jsonify(ok=False, error="destination or api_key required"), 400
    try:
        with _conn() as c, c.cursor() as cur:
            if destination:
                cur.execute(
                    """SELECT market_slug, channel, destination, created_at,
                              last_notified_at
                         FROM market_subscriptions
                        WHERE active AND LOWER(destination) = %s
                        ORDER BY market_slug""",
                    (destination,))
            else:
                cur.execute(
                    """SELECT market_slug, channel, destination, created_at,
                              last_notified_at
                         FROM market_subscriptions
                        WHERE active AND api_key = %s
                        ORDER BY market_slug""",
                    (api_key,))
            subs = [{"market": r[0], "channel": r[1], "destination": r[2],
                     "created_at": r[3].isoformat() if r[3] else None,
                     "last_notified_at": r[4].isoformat() if r[4] else None}
                    for r in cur.fetchall()]
        return jsonify(ok=True, count=len(subs), subscriptions=subs), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


# ─────────────────────────────────────────────────────────────────────
# Detection — snapshot, diff, build events
# ─────────────────────────────────────────────────────────────────────

def _latest_market_signals():
    """Current headline signals per market, latest row per slug."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (market_slug)
                      market_slug, market_name, verdict,
                      constraint_score, excess_power_score, time_to_power_months
                 FROM market_power_scores
                ORDER BY market_slug, computed_at DESC""")
        return [
            {"slug": r[0], "name": r[1], "verdict": r[2],
             "constraint_score": r[3], "excess_power_score": r[4],
             "time_to_power_months": r[5]}
            for r in cur.fetchall()
        ]


def _snapshot_and_diff(cur, sig):
    """For one market: write a snapshot if due, then diff the two most
    recent snapshots. Returns a list of movement-event dicts (may be empty).
    `cur` is an open cursor inside the caller's transaction."""
    slug = sig["slug"]

    # Most recent prior snapshot — the diff baseline + the gap check.
    cur.execute(
        """SELECT verdict, constraint_score, time_to_power_months, captured_at
             FROM market_movement_snapshots
            WHERE market_slug = %s
            ORDER BY captured_at DESC LIMIT 1""",
        (slug,))
    prev = cur.fetchone()

    due = True
    if prev and prev[3]:
        age_h = (datetime.now(timezone.utc) - prev[3]).total_seconds() / 3600.0
        due = age_h >= MIN_SNAPSHOT_GAP_HOURS

    if due:
        cur.execute(
            """INSERT INTO market_movement_snapshots
                   (market_slug, verdict, constraint_score,
                    excess_power_score, time_to_power_months)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (slug, sig["verdict"], sig["constraint_score"],
             sig["excess_power_score"], sig["time_to_power_months"]))

    # Nothing to diff against on the very first snapshot for a market.
    if not prev:
        return []

    events = []
    prev_verdict, prev_constraint, prev_ttp, _ = prev

    cur_verdict = sig["verdict"]
    if cur_verdict and prev_verdict and cur_verdict != prev_verdict:
        events.append({
            "kind": "verdict_flip",
            "summary": f"{sig['name']}: outlook changed {prev_verdict} → {cur_verdict}",
            "detail": {"from": prev_verdict, "to": cur_verdict},
        })

    cur_constraint = sig["constraint_score"]
    if cur_constraint is not None and prev_constraint is not None:
        delta = float(cur_constraint) - float(prev_constraint)
        if abs(delta) >= CONSTRAINT_THRESHOLD:
            direction = "tightened" if delta > 0 else "eased"
            events.append({
                "kind": "constraint_shift",
                "summary": (f"{sig['name']}: grid constraint {direction} "
                            f"{abs(delta):.0f} pts "
                            f"({prev_constraint:.0f} → {cur_constraint:.0f})"),
                "detail": {"from": round(float(prev_constraint), 1),
                           "to": round(float(cur_constraint), 1),
                           "delta": round(delta, 1)},
            })

    cur_ttp = sig["time_to_power_months"]
    if cur_ttp is not None and prev_ttp is not None:
        delta = float(cur_ttp) - float(prev_ttp)
        if abs(delta) >= TTP_THRESHOLD_MONTHS:
            direction = "slipped" if delta > 0 else "improved"
            events.append({
                "kind": "time_to_power_shift",
                "summary": (f"{sig['name']}: time-to-power {direction} "
                            f"{abs(delta):.0f} months "
                            f"({prev_ttp:.0f} → {cur_ttp:.0f} mo)"),
                "detail": {"from": round(float(prev_ttp), 1),
                           "to": round(float(cur_ttp), 1),
                           "delta": round(delta, 1)},
            })

    for ev in events:
        ev["market_slug"] = slug
        ev["market_name"] = sig["name"]
    return events


# ─────────────────────────────────────────────────────────────────────
# Delivery
# ─────────────────────────────────────────────────────────────────────

def _deliver_email(destination, events):
    """One grouped digest email for all of a destination's moved markets."""
    if not RESEND_KEY:
        return False
    rows = "".join(
        f'<li style="margin:6px 0"><strong>{e["market_name"]}</strong> &mdash; '
        f'{e["summary"].split(": ", 1)[-1]}</li>'
        for e in events)
    html = f"""<!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:28px;color:#1a1a1a">
<div style="font-size:11px;color:#888;letter-spacing:.05em;text-transform:uppercase;margin-bottom:10px">DC Hub &middot; market movement alert</div>
<h2 style="margin:0 0 12px;font-size:21px">{len(events)} market{'s' if len(events) != 1 else ''} you track moved</h2>
<ul style="color:#444;font-size:15px;line-height:1.6;padding-left:20px">{rows}</ul>
<p style="margin:22px 0"><a href="https://dchub.cloud/dcpi" style="background:#1976d2;color:#fff;padding:11px 22px;border-radius:6px;text-decoration:none;font-weight:600;display:inline-block">See the full DCPI breakdown &rarr;</a></p>
<hr style="border:0;border-top:1px solid #eee;margin:28px 0">
<p style="font-size:12px;color:#888">You subscribed to market-movement alerts on dchub.cloud. <a href="https://dchub.cloud/dcpi" style="color:#888">Manage alerts</a></p>
</body></html>"""
    try:
        import requests as _rq
        resp = _rq.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_KEY}",
                     "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": [destination],
                  "subject": f"DC Hub alert: {len(events)} tracked market"
                             f"{'s' if len(events) != 1 else ''} moved",
                  "html": html},
            timeout=12)
        return resp.status_code in (200, 201)
    except Exception:
        return False


def _deliver_webhook(destination, events):
    """One POST per destination carrying all its markets' events."""
    payload = {
        "source": "dchub-market-alerts",
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "event_count": len(events),
        "events": [
            {"market_slug": e["market_slug"], "market_name": e["market_name"],
             "kind": e["kind"], "summary": e["summary"], "detail": e["detail"]}
            for e in events
        ],
    }
    try:
        import requests as _rq
        resp = _rq.post(destination, json=payload, timeout=8,
                        headers={"User-Agent": "dchub-market-alerts/1"})
        return 200 <= resp.status_code < 300
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# The pass — POST /api/v1/alerts/run (admin-gated, cron-driven)
# ─────────────────────────────────────────────────────────────────────

@market_alerts_bp.route("/api/v1/alerts/run", methods=["POST"])
@_require_admin
def run_alerts():
    """Snapshot every market, detect movement, fan out to subscribers.

    Dry-run by default — ?send=true delivers. Snapshots + event rows are
    written either way (the baseline must advance); only the email/webhook
    delivery is gated on ?send=true.
    """
    _ensure_schema()
    send = (request.args.get("send") or "").lower() in ("1", "true", "yes")

    try:
        signals = _latest_market_signals()
    except Exception as e:
        return jsonify(mode="error", error=str(e)[:300]), 200

    # ── Phase 1: snapshot + diff every market in one transaction ──
    # Each market runs inside its own SAVEPOINT so a single bad row
    # rolls back only that market — not every snapshot taken before it.
    all_events = []
    try:
        with _conn() as c, c.cursor() as cur:
            for sig in signals:
                try:
                    cur.execute("SAVEPOINT mkt")
                    evs = _snapshot_and_diff(cur, sig)
                    cur.execute("RELEASE SAVEPOINT mkt")
                    all_events.extend(evs)
                except Exception:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT mkt")
                    except Exception:
                        pass
            # Persist the detected events, collecting their ids back.
            for ev in all_events:
                cur.execute(
                    """INSERT INTO market_movement_events
                           (market_slug, kind, summary, detail)
                       VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING
                       RETURNING id""",
                    (ev["market_slug"], ev["kind"], ev["summary"],
                     json.dumps(ev["detail"])))
                ev["id"] = cur.fetchone()[0]
            c.commit()
    except Exception as e:
        return jsonify(mode="error", error=str(e)[:300],
                       markets_scanned=len(signals)), 200

    result = {
        "mode": "sent" if (send and RESEND_KEY) else (
            "dry_run" if not send else "no_provider"),
        "markets_scanned": len(signals),
        "movements_detected": len(all_events),
        "events": [{"market": e["market_slug"], "kind": e["kind"],
                    "summary": e["summary"]} for e in all_events],
        "deliveries": {"email": 0, "webhook": 0, "failed": 0},
        "notified_subscriptions": 0,
        "provider_configured": bool(RESEND_KEY),
    }

    if not all_events:
        return jsonify(result), 200

    # ── Phase 2: fan out to subscribers ──
    moved_slugs = sorted({e["market_slug"] for e in all_events})
    events_by_slug = defaultdict(list)
    for e in all_events:
        events_by_slug[e["market_slug"]].append(e)

    by_dest = {}  # populated inside the try; pre-init so the dry-run
                  # summary below is safe even if the fanout query fails.
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT id, market_slug, channel, destination
                     FROM market_subscriptions
                    WHERE active AND market_slug = ANY(%s)""",
                (moved_slugs,))
            subs = cur.fetchall()

            # Group subscriptions by (channel, destination) so a
            # destination tracking 3 moved markets gets ONE delivery.
            by_dest = defaultdict(lambda: {"sub_ids": [], "events": []})
            for sub_id, slug, channel, destination in subs:
                key = (channel, destination)
                by_dest[key]["sub_ids"].append(sub_id)
                by_dest[key]["events"].extend(events_by_slug.get(slug, []))

            notified_sub_ids = []
            for (channel, destination), grp in by_dest.items():
                evs = grp["events"]
                if not evs:
                    continue
                if not send:
                    continue  # dry run — count nothing as delivered
                ok = (_deliver_email(destination, evs) if channel == "email"
                      else _deliver_webhook(destination, evs))
                if ok:
                    result["deliveries"][channel] += 1
                    notified_sub_ids.extend(grp["sub_ids"])
                else:
                    result["deliveries"]["failed"] += 1

            if notified_sub_ids:
                cur.execute(
                    """UPDATE market_subscriptions
                          SET last_notified_at = NOW()
                        WHERE id = ANY(%s)""",
                    (notified_sub_ids,))
                result["notified_subscriptions"] = len(notified_sub_ids)

            # Stamp every event processed this pass — keeps the
            # unnotified-events index clean regardless of send mode.
            event_ids = [e["id"] for e in all_events if e.get("id")]
            if event_ids:
                cur.execute(
                    """UPDATE market_movement_events
                          SET notified_at = NOW()
                        WHERE id = ANY(%s)""",
                    (event_ids,))
            c.commit()
    except Exception as e:
        result["fanout_error"] = str(e)[:200]

    # Dry-run: show who WOULD have been notified.
    if not send:
        result["would_notify_destinations"] = len(
            {dest for (_ch, dest) in by_dest.keys()})

    return jsonify(result), 200


@market_alerts_bp.route("/api/v1/alerts/health", methods=["GET"])
def alerts_health():
    """Cohort + recent-activity visibility for the alerts primitive."""
    _ensure_schema()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM market_subscriptions WHERE active")
            active_subs = int((cur.fetchone() or [0])[0] or 0)
            cur.execute(
                """SELECT channel, COUNT(*) FROM market_subscriptions
                    WHERE active GROUP BY channel""")
            by_channel = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute(
                """SELECT COUNT(*), MAX(detected_at)
                     FROM market_movement_events
                    WHERE detected_at > NOW() - INTERVAL '7 days'""")
            ev_row = cur.fetchone() or (0, None)
            cur.execute("SELECT COUNT(*), MAX(captured_at) FROM market_movement_snapshots")
            snap_row = cur.fetchone() or (0, None)
        return jsonify(
            status="ok",
            active_subscriptions=active_subs,
            subscriptions_by_channel=by_channel,
            movements_last_7d=int(ev_row[0] or 0),
            latest_movement=ev_row[1].isoformat() if ev_row[1] else None,
            total_snapshots=int(snap_row[0] or 0),
            latest_snapshot=snap_row[1].isoformat() if snap_row[1] else None,
            provider_configured=bool(RESEND_KEY),
        ), 200
    except Exception as e:
        return jsonify(status="error", error=str(e)[:200]), 200
