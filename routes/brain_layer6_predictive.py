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
from routes._swallowed_writes import note_swallowed_write

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
                "INSERT INTO brain_metric_snapshots (metric_key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (key, float(value)))
            c.commit()
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        note_swallowed_write("brain_metric_snapshots", where="brain_layer6_predictive._record_metric")
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
                  AND recorded_at >= NOW() - %s * INTERVAL '1 day'
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
    # 2026-07-02 — north-star GROWTH metrics: the numbers the whole flywheel
    # exists to move. Read from the canonical /api/v1/reach endpoint (which
    # reads mcp_calls_identity / mcp_agent_retention_30d — never session_id)
    # so L6's velocity/forecast machinery + the forecast→finding bridge (#7)
    # operate on REAL external growth, not internal proxies. Before this the
    # brain snapshotted tool_calls/signals/queue — all self-traffic-inflatable.
    try:
        reach = requests.get("http://localhost:8080/api/v1/reach",
                             timeout=8).json()
    except Exception:
        reach = {}
    _day2 = None
    try:
        _day2 = (reach.get("retention_30d") or {}).get("day2_return_rate_pct")
    except Exception:
        pass
    _binds_7d = None
    try:
        _bc = _conn()
        try:
            _bcur = _bc.cursor()
            _bcur.execute(
                "SELECT COUNT(email) FROM mcp_dev_keys "
                "WHERE email IS NOT NULL AND created_at > NOW() - INTERVAL '7 days'")
            _binds_7d = (_bcur.fetchone() or [None])[0]
        finally:
            try: _bc.close()
            except Exception: pass
    except Exception:
        pass

    # Persist current values
    for k, v in [
        ("tool_calls_7d",       funnel.get("tool_calls_7d", 0)),
        ("upgrade_signals_7d",  funnel.get("upgrade_signals_7d", 0)),
        ("conversions_30d",     funnel.get("conversions_30d", 0)),
        ("publisher_queue",     (ws.get("distribution") or {}).get("queued_unpublished", 0)),
        ("linkedin_published_7d", ((ws.get("distribution") or {}).get("published_7d") or {}).get("linkedin", 0)),
        # north-star growth (None when /api/v1/reach unavailable → skipped,
        # never a fabricated 0)
        ("real_agents_7d",      reach.get("real_agents_7d")),
        ("real_calls_7d",       reach.get("real_calls_7d")),
        ("citations_7d",        reach.get("citations_7d")),
        ("day2_retention_pct",  _day2),
        ("email_binds_7d",      _binds_7d),
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
        # 2026-07-02 north-star growth: thresholds sized to the real baseline
        # (16 agents / 6 citations the week this shipped) — a forecast below
        # them means the reach constraint is actively tightening.
        ("real_agents_7d",       "Real external agents (7d)", 8),
        ("real_calls_7d",        "Real external MCP calls (7d)", None),
        ("citations_7d",         "AI citations (7d)",    3),
        ("day2_retention_pct",   "Agent day-2 retention %", None),
        ("email_binds_7d",       "Email binds (7d)",     None),
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
            # Additive keys used by the (dark-by-default) forecast→finding
            # bridge below. Harmless to existing consumers.
            "threshold":   threshold,
            "samples":     v.get("samples", 0),
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


# ── Forecast → finding bridge + opportunity arm (Feature #7) ───────────
# DARK-BY-DEFAULT: emission gated behind BRAIN_FORECAST_FINDINGS_ENABLED.
# Read-only / disabled path is the current behavior. When ON, a forecast
# that crosses a threshold N days out files an ADDITIVE brain_findings row
# (deduped on (issue,url) by the canonical writer's UPDATE-then-INSERT).

# Minimum snapshots required before a forecast is trustworthy enough to
# file a finding. The velocity fit itself needs 3; we want a touch more
# signal before acting on the extrapolation.
_FORECAST_MIN_SAMPLES = int(os.environ.get("BRAIN_FORECAST_MIN_SAMPLES", "5") or 5)


def _forecast_findings_enabled() -> bool:
    return os.environ.get(
        "BRAIN_FORECAST_FINDINGS_ENABLED", "").lower() in ("1", "true", "yes")


def _emit_forecast_findings(preds: list[dict]) -> dict:
    """Bridge predictions → brain_findings (gated, fail-safe, deduped).

    For each forecasted metric:
      (a) FALLING through a downside threshold within the forecast window
          → issue='forecast_breach:<metric>'.
      (b) RISING through a positive threshold within the forecast window
          → issue='forecast_opportunity:<metric>'.

    Gated on a min-confidence sample check. Deduped via the canonical
    writer (constraint-agnostic upsert on issue+url). Never raises — any
    failure degrades to today's behavior (no findings emitted)."""
    out = {"enabled": False, "emitted": 0, "results": []}
    if not _forecast_findings_enabled():
        return out
    out["enabled"] = True

    # Build the candidate findings first (pure, side-effect-free).
    candidates: list[dict] = []
    try:
        for p in preds or []:
            if p.get("status") == "waiting":
                continue
            threshold = p.get("threshold")
            if threshold is None:
                continue
            samples = int(p.get("samples", 0) or 0)
            if samples < _FORECAST_MIN_SAMPLES:
                continue  # min-confidence gate
            metric = p.get("metric")
            current = p.get("current")
            forecast = p.get("forecast_7d")
            slope = p.get("slope_per_day")
            trend = p.get("trend")
            if metric is None or forecast is None or current is None:
                continue

            # (a) Downside breach: falling AND projected below the floor,
            #     but not already below it (the binary detector owns that).
            if trend == "falling" and forecast < threshold and current >= threshold:
                candidates.append({
                    "issue":  f"forecast_breach:{metric}",
                    "url":    "/api/v1/brain/predictions",
                    "detail": (
                        f"L6 forecast: {p.get('label', metric)} trending toward "
                        f"{forecast:.0f} within 7d (current {current:.0f}, floor "
                        f"{threshold}, slope {slope}/day, {samples} samples). "
                        f"Projected to breach BEFORE the binary detector fires."),
                })
            # (b) Opportunity: rising AND projected above the positive
            #     threshold, but not already above it.
            elif trend == "rising" and forecast > threshold and current <= threshold:
                candidates.append({
                    "issue":  f"forecast_opportunity:{metric}",
                    "url":    "/api/v1/brain/predictions",
                    "detail": (
                        f"L6 forecast: {p.get('label', metric)} rising toward "
                        f"{forecast:.0f} within 7d (current {current:.0f}, "
                        f"threshold {threshold}, slope {slope}/day, {samples} "
                        f"samples). Positive-trajectory opportunity to lean in."),
                })
    except Exception as e:
        logger.warning(f"L6 forecast-finding candidate build failed: {e}")
        return out

    if not candidates:
        return out

    # Persist (deduped) via the canonical writer. Single connection/tx.
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception as e:
        logger.warning(f"L6 forecast-finding writer import failed: {e}")
        return out

    try:
        c = _conn()
        try:
            cur = c.cursor()
            for cand in candidates:
                try:
                    res = upsert_brain_finding(
                        cur,
                        issue=cand["issue"],
                        url=cand["url"],
                        count=1,
                        detail=cand["detail"],
                        detector="brain_l6_forecast",
                        status="open")
                    out["results"].append({"issue": cand["issue"], "result": res})
                    if res in ("inserted", "updated"):
                        out["emitted"] += 1
                except Exception as ie:
                    logger.warning(f"L6 forecast-finding upsert failed: {ie}")
            try:
                c.commit()
            except Exception:
                pass
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"L6 forecast-finding persist failed: {e}")
    return out


def forecast_findings_tick() -> dict:
    """Master-tick entrypoint: gather predictions then (gated) emit
    forecast findings. Safe to call unconditionally — does nothing unless
    BRAIN_FORECAST_FINDINGS_ENABLED is set. Never raises."""
    try:
        preds = _gather_predictions()
        return _emit_forecast_findings(preds)
    except Exception as e:
        logger.warning(f"L6 forecast_findings_tick failed: {e}")
        return {"enabled": _forecast_findings_enabled(), "emitted": 0,
                "error": str(e)[:200]}


@brain_layer6_bp.route("/api/v1/brain/predictions", methods=["GET"])
def predictions_json():
    preds = _gather_predictions()
    # Forecast→finding bridge (dark-by-default; no-op unless flag is on).
    try:
        _emit_forecast_findings(preds)
    except Exception:
        pass
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
