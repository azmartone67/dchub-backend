"""
Brain L6 — Predictive (2026-05-19).

L0-L3 detect-and-fix REACTIVELY. L4-L5 (brain_v2_*) propose fixes for
novel patterns. L6 watches METRIC VELOCITY across the brain's data
sources and predicts which findings WILL fire in 24-72h based on trend.

Examples of predictions L6 makes:
  • "tool_calls_7d down 3.5% per day for 7 days — predicted to cross
     pre-tightening floor in 14 days. Tighten/loosen choice incoming."
  • "auto_press queue depth growing 5/day — predicted to hit publisher
     daily cap in 8 days. Either bump cap or kill stale generators."
  • "DCPI markets_scored stable but DCPI movers per-week trending down
     → predicted source-data freshness issue in 5 days."
  • "Cache rate dropping 2% per day → predicted CF origin cost spike
     by next week."

The key insight: brain detectors are BINARY (fire or don't). L6 is
ANALOG — it watches the velocity of measurements and surfaces the
trajectory BEFORE the binary breach.

Endpoints:
  GET /api/v1/brain/predictions       JSON list of forecasted findings
  GET /api/v1/brain/predictions/page  HTML mini-dashboard

Velocity is computed from the freshness radar's table-age history +
the funnel/heartbeat snapshots persisted to a small new table:
brain_metric_snapshots (timestamp, metric_key, value).
"""

import os
import logging
import datetime as _dt
from flask import Blueprint, jsonify, Response

logger = logging.getLogger(__name__)
brain_layer6_bp = Blueprint("brain_layer6", __name__)


def _conn():
    try:
        from main import get_db
        return get_db()
    except Exception:
        import psycopg2
        return psycopg2.connect(os.environ.get("NEON_DATABASE_URL")
                                or os.environ.get("DATABASE_URL", ""))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_metric_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    metric_key    TEXT NOT NULL,
    value         DOUBLE PRECISION NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_bms_key_time
    ON brain_metric_snapshots(metric_key, recorded_at DESC);
"""

_SCHEMA_INIT = False

def _ensure_schema():
    global _SCHEMA_INIT
    if _SCHEMA_INIT: return
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute(_SCHEMA)
            try: c.commit()
            except Exception: pass
            _SCHEMA_INIT = True
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"L6 schema init failed: {e}")


def _record_metric(key: str, value: float):
    _ensure_schema()
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute(
                "INSERT INTO brain_metric_snapshots (metric_key, value) VALUES (%s, %s)",
                (key, float(value)))
            c.commit()
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        pass


def _velocity(key: str, days: int = 7) -> dict:
    """Linear regression on the last N days of values for a metric.
    Returns slope_per_day + days_to_zero (extrapolation) + recent_count."""
    _ensure_schema()
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("""
                SELECT EXTRACT(EPOCH FROM recorded_at)::float, value
                FROM brain_metric_snapshots
                WHERE metric_key = %s
                  AND recorded_at >= NOW() - INTERVAL '%s days'
                ORDER BY recorded_at ASC
            """ % (cur.mogrify("%s", (key,)).decode(), days))
            rows = cur.fetchall() or []
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        return {"insufficient_data": True}

    if len(rows) < 3:
        return {"insufficient_data": True, "samples": len(rows)}

    # Simple linear regression
    n = len(rows)
    xs = [r[0] for r in rows]
    ys = [r[1] for r in rows]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n)) or 1e-9
    slope_per_sec = num / den
    slope_per_day = slope_per_sec * 86400.0
    current = ys[-1]
    days_to_zero = None
    if slope_per_day < 0 and current > 0:
        days_to_zero = current / abs(slope_per_day)
    elif slope_per_day > 0 and current > 0:
        days_to_zero = float("inf")
    return {
        "samples": n,
        "current": current,
        "slope_per_day": slope_per_day,
        "days_to_zero": days_to_zero,
        "trend": ("rising" if slope_per_day > 0.01 * abs(my)
                  else "falling" if slope_per_day < -0.01 * abs(my)
                  else "flat"),
    }


def _gather_predictions() -> list[dict]:
    """For each tracked metric, compute velocity + predict any breaches."""
    # First, pull current values + persist snapshots
    try:
        import requests
        f = requests.get("http://localhost:8080/api/v1/mcp/funnel", timeout=5)
        funnel = f.json() if f.ok else {}
    except Exception:
        funnel = {}
    try:
        ws = requests.get("http://localhost:8080/api/v1/marketing/worker-status",
                           timeout=5).json()
    except Exception:
        ws = {}

    # Persist current values
    for k, v in [
        ("tool_calls_7d",       funnel.get("tool_calls_7d", 0)),
        ("upgrade_signals_7d",  funnel.get("upgrade_signals_7d", 0)),
        ("conversions_30d",     funnel.get("conversions_30d", 0)),
        ("publisher_queue",     (ws.get("distribution") or {}).get("queued_unpublished", 0)),
        ("linkedin_published_7d", ((ws.get("distribution") or {}).get("published_7d") or {}).get("linkedin", 0)),
    ]:
        if v is not None:
            _record_metric(k, v)

    # Compute velocities + predictions
    predictions: list[dict] = []
    for key, label, threshold in [
        ("tool_calls_7d",        "MCP tool calls (7d)",  20000),  # below 20K = real concern
        ("upgrade_signals_7d",   "Paywall signals (7d)", 1000),
        ("conversions_30d",      "Conversions (30d)",    None),
        ("publisher_queue",      "Publisher queue depth",100),
        ("linkedin_published_7d","LinkedIn posts (7d)",  None),
    ]:
        v = _velocity(key, days=7)
        if v.get("insufficient_data"):
            predictions.append({
                "metric":      key,
                "label":       label,
                "status":      "waiting",
                "note":        f"Need 3+ snapshots. Currently {v.get('samples',0)}.",
            })
            continue
        slope = v["slope_per_day"]
        current = v["current"]
        trend = v["trend"]
        forecast_7d = current + (slope * 7)

        p = {
            "metric":      key,
            "label":       label,
            "current":     current,
            "trend":       trend,
            "slope_per_day": round(slope, 3),
            "forecast_7d": round(forecast_7d, 1),
        }
        # Alert logic
        if threshold and forecast_7d < threshold and trend == "falling":
            p["alert"] = "FALLING"
            p["alert_detail"] = (
                f"Trending toward {forecast_7d:.0f} in 7d (current {current:.0f}, "
                f"threshold {threshold}). Slope {slope:.1f}/day.")
        elif key == "publisher_queue" and forecast_7d > 200:
            p["alert"] = "RISING"
            p["alert_detail"] = (
                f"Queue trending toward {forecast_7d:.0f} in 7d (current {current:.0f}). "
                f"Publisher daily cap is the constraint; either lift cap or kill generators.")
        elif key == "conversions_30d" and current == 0 and slope == 0:
            p["alert"] = "FLAT_AT_ZERO"
            p["alert_detail"] = (
                f"Zero conversions, zero velocity. Trial-tightening hasn't moved the "
                f"needle yet; consider next-step (3-day trial or remove auto-trial entirely).")
        predictions.append(p)
    return predictions


@brain_layer6_bp.route("/api/v1/brain/predictions", methods=["GET"])
def predictions_json():
    preds = _gather_predictions()
    alerts = [p for p in preds if p.get("alert")]
    return jsonify(
        ok=True,
        generated_at=_dt.datetime.utcnow().isoformat() + "Z",
        prediction_count=len(preds),
        alert_count=len(alerts),
        predictions=preds,
        note=("L6 brain — analog velocity predictions. Reads recent "
              "metric snapshots (recorded each scan), fits a 7d linear "
              "trend, surfaces alerts when trajectory crosses a threshold. "
              "Snapshots accumulate as this endpoint is called (or via "
              "cron). After 7 days of data, predictions are meaningful."),
    ), 200


@brain_layer6_bp.route("/api/v1/brain/predictions/page", methods=["GET"])
def predictions_page():
    preds = _gather_predictions()
    rows = ""
    for p in preds:
        if p.get("status") == "waiting":
            rows += f"<tr><td>{p['label']}</td><td colspan=4 style='color:#666'>{p['note']}</td></tr>"
            continue
        alert = p.get("alert", "")
        cls = "warn" if alert else ""
        rows += (f"<tr class='{cls}'><td><b>{p['label']}</b></td>"
                 f"<td class='num'>{p.get('current','—')}</td>"
                 f"<td>{p.get('trend','—')}</td>"
                 f"<td class='num'>{p.get('slope_per_day','—')}</td>"
                 f"<td class='num'>{p.get('forecast_7d','—')}</td>"
                 f"<td>{alert}</td></tr>")
        if p.get("alert_detail"):
            rows += f"<tr><td></td><td colspan=5 class='detail'>{p['alert_detail']}</td></tr>"

    html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>DC Hub Brain — L6 Predictions</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:1100px;margin:0 auto;padding:2rem 1rem;color:#1f2937}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:.6rem;border-bottom:1px solid #e5e7eb;text-align:left}}
.num{{font-family:'JetBrains Mono',monospace}}
.warn{{background:rgba(239,68,68,.08)}}
.detail{{color:#6b7280;font-size:.85rem;padding-left:1rem}}</style></head><body>
<h1>Brain L6 — Predictive</h1>
<p>Analog velocity predictions. Watches metric trajectory; surfaces alerts before binary breaches fire.</p>
<table><tr><th>Metric</th><th>Current</th><th>Trend</th><th>Slope/day</th><th>Forecast 7d</th><th>Alert</th></tr>
{rows}
</table>
<p style="color:#6b7280;font-size:.85rem;margin-top:2rem">
JSON: <a href="/api/v1/brain/predictions">/api/v1/brain/predictions</a> ·
Snapshots accumulate on every page/JSON hit + via cron.
</p></body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300"})
