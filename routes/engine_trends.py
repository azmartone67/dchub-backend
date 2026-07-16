"""
engine_trends.py — trend tracking for the optimization engines.

The engines showed a SNAPSHOT (40.6) but couldn't answer "are we IMPROVING?".
This records each engine's headline score over time (rate-limited to ~hourly) and
exposes the delta vs 1d / 7d ago, so the dashboard can show ↑/flat/↓ per engine.

Self-contained, fail-soft (a DB blip never breaks an engine — record() is a no-op
on error). Used by mcp_leadership_engine + agent_utilization_engine via record(),
read by the dashboard via GET /api/v1/engines/trend.
"""
from __future__ import annotations
import json, datetime
from flask import Blueprint, jsonify

engine_trends_bp = Blueprint("engine_trends", __name__)
_ENSURED = False


def _conn():
    try:
        from main import get_pg_connection
        return get_pg_connection()
    except Exception:
        return None


def _release(c):
    try:
        from main import return_pg_connection
        return_pg_connection(c)
    except Exception:
        try:
            c.close()
        except Exception:
            pass


def _ensure(cur):
    global _ENSURED
    if _ENSURED:
        return
    cur.execute("""CREATE TABLE IF NOT EXISTS engine_snapshots (
        id BIGSERIAL PRIMARY KEY,
        engine     TEXT NOT NULL,
        snapped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        score      NUMERIC,
        detail     JSONB
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_engine_snapshots ON engine_snapshots(engine, snapped_at DESC)")
    _ENSURED = True


def record(engine: str, score, detail: dict | None = None, min_gap_min: int = 55) -> None:
    """Insert a snapshot, rate-limited to ~hourly per engine. Fail-soft no-op on error."""
    if score is None:
        return
    c = _conn()
    if c is None:
        return
    try:
        cur = c.cursor()
        _ensure(cur)
        cur.execute("SELECT snapped_at FROM engine_snapshots WHERE engine=%s ORDER BY snapped_at DESC LIMIT 1", (engine,))
        r = cur.fetchone()
        if r and r[0]:
            age_min = (datetime.datetime.now(datetime.timezone.utc) - r[0]).total_seconds() / 60.0
            if age_min < min_gap_min:
                cur.close()
                c.commit()
                return
        cur.execute("INSERT INTO engine_snapshots (engine, score, detail) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (engine, float(score), json.dumps(detail or {})))
        c.commit()
        cur.close()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
    finally:
        _release(c)


def _trend(engine: str) -> dict:
    c = _conn()
    if c is None:
        return {}
    try:
        cur = c.cursor()
        _ensure(cur)
        cur.execute("SELECT score FROM engine_snapshots WHERE engine=%s ORDER BY snapped_at DESC LIMIT 1", (engine,))
        nowr = cur.fetchone()
        now = float(nowr[0]) if nowr and nowr[0] is not None else None

        def at(days):
            cur.execute("""SELECT score FROM engine_snapshots
                            WHERE engine=%s AND snapped_at <= NOW() - make_interval(days => %s)
                            ORDER BY snapped_at DESC LIMIT 1""", (engine, days))
            x = cur.fetchone()
            return float(x[0]) if x and x[0] is not None else None

        d1, d7 = at(1), at(7)
        cur.execute("SELECT COUNT(*), MIN(snapped_at) FROM engine_snapshots WHERE engine=%s", (engine,))
        cnt = cur.fetchone()
        cur.close()
        c.commit()
        return {
            "now": now,
            "delta_1d": round(now - d1, 1) if (now is not None and d1 is not None) else None,
            "delta_7d": round(now - d7, 1) if (now is not None and d7 is not None) else None,
            "points": int(cnt[0] or 0),
            "since": cnt[1].isoformat() if cnt and cnt[1] else None,
        }
    except Exception:
        return {}
    finally:
        _release(c)


@engine_trends_bp.route("/api/v1/engines/trend", methods=["GET"])
def engines_trend():
    """Trend (Δ vs 1d/7d ago) for both engines, for the dashboard."""
    resp = jsonify({
        "leadership": _trend("leadership"),
        "utilization": _trend("utilization"),
        "note": "Δ vs 1d / 7d ago. Builds over time — null deltas mean not enough history yet.",
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200
