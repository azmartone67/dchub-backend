"""
hyperscaler_alerts.py — fire alerts when new $1B+ AI capex deals appear.

Phase ZZZZZ-round41 (2026-05-25). The hyperscaler-deals endpoint already
extracts $-figures + MW from news. This module is the WATCHER that
elevates anything ≥ $1B into a typed alert + pushes to the MCP SSE
event ring + writes to hyperscaler_alerts DB table for the dashboard.

Cron-callable. Runs every 5 min (cheap query).

Endpoints:
  GET  /api/v1/hyperscaler-alerts          — recent alerts (last 7d)
  POST /api/v1/hyperscaler-alerts/sweep    — scan + emit new alerts
"""
import os
import datetime
import re
from contextlib import contextmanager

from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

hyperscaler_alerts_bp = Blueprint("hyperscaler_alerts", __name__)

RE_DOLLAR = re.compile(
    r"(?:\$\s?([\d,]+(?:\.\d+)?)\s?(billion|B|trillion|T)\b"
    r"|\b([\d,]+(?:\.\d+)?)\s?(billion|trillion)\s+(?:dollars?|USD))",
    re.I)
MIN_DEAL_USD = 1_000_000_000  # $1B threshold


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


def _ensure_table():
    if not (_pg and _dsn()): return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS hyperscaler_alerts (
                    id            SERIAL PRIMARY KEY,
                    news_id       INT NOT NULL,
                    headline      TEXT NOT NULL,
                    url           TEXT,
                    source        TEXT,
                    value_usd     BIGINT,
                    value_display TEXT,
                    actor         TEXT,
                    published_at  TIMESTAMPTZ,
                    detected_at   TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(news_id)
                );
                CREATE INDEX IF NOT EXISTS ix_hsa_value ON hyperscaler_alerts(value_usd DESC);
                CREATE INDEX IF NOT EXISTS ix_hsa_detected ON hyperscaler_alerts(detected_at DESC);
            """)
            c.commit()
    except Exception:
        pass

_ensure_table()


def _extract_dollar_usd(text):
    if not text: return None
    m = RE_DOLLAR.search(text)
    if not m: return None
    num_str = m.group(1) or m.group(3)
    unit    = (m.group(2) or m.group(4) or "").lower()
    if not num_str: return None
    try: num = float(num_str.replace(",", ""))
    except: return None
    if unit in ("b", "billion"):  return num * 1_000_000_000, f"${num}B"
    if unit in ("t", "trillion"): return num * 1_000_000_000_000, f"${num}T"
    return None


def _classify_actor(text):
    t = (text or "").lower()
    for actor, keys in [
        ("OpenAI",    ["openai", "stargate"]),
        ("Anthropic", ["anthropic", "claude"]),
        ("Microsoft", ["microsoft", "azure"]),
        ("Google",    ["google ai", "alphabet"]),
        ("AWS",       ["aws", "amazon web"]),
        ("Meta",      ["meta ai", "facebook ai"]),
        ("Oracle",    ["oracle"]),
        ("CoreWeave", ["coreweave"]),
        ("Lambda",    ["lambda labs"]),
        ("Crusoe",    ["crusoe"]),
        ("xAI",       ["xai", "grok", "musk ai"]),
        ("NVIDIA",    ["nvidia", "blackwell"]),
        ("AMD",       ["amd "]),
    ]:
        if any(k in t for k in keys): return actor
    return "Unknown"


@hyperscaler_alerts_bp.route("/api/v1/hyperscaler-alerts/sweep", methods=["GET", "POST"])
def sweep():
    """Scan recent news for $1B+ deals, write new ones to alerts table."""
    out = {
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "new_alerts": 0,
        "scanned": 0,
        "errors": [],
    }
    if not (_pg and _dsn()):
        out["error"] = "no_db"
        return jsonify(out), 200

    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Try news table with various column names
            for col_summary in ("summary", "description", "''"):
                try:
                    cur.execute(f"""
                        SELECT id, title, source, url, published_date,
                               COALESCE({col_summary}, '') AS body
                        FROM news
                        WHERE (LOWER(title) LIKE '%%billion%%'
                               OR title ~ '\\$[0-9]+\\s?[BT]')
                          AND published_date > CURRENT_DATE - INTERVAL '7 days'
                        ORDER BY published_date DESC LIMIT 100
                    """)
                    rows = cur.fetchall()
                    break
                except Exception:
                    continue
            else:
                out["errors"].append("no_news_table")
                return jsonify(out), 200

        out["scanned"] = len(rows)
        new_count = 0
        for r in rows:
            full = (r["title"] or "") + " " + (r.get("body") or "")
            extract = _extract_dollar_usd(full)
            if not extract: continue
            value_usd, display = extract
            if value_usd < MIN_DEAL_USD: continue
            actor = _classify_actor(full)
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO hyperscaler_alerts
                          (news_id, headline, url, source, value_usd, value_display, actor, published_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (news_id) DO NOTHING
                        RETURNING id
                    """, (r["id"], r["title"][:500], r.get("url"), r.get("source"),
                           int(value_usd), display, actor, r.get("published_date")))
                    if cur.fetchone():
                        new_count += 1
                    c.commit()
            except Exception as e:
                out["errors"].append(f"insert_failed_for_news_id_{r['id']}: {type(e).__name__}")

        out["new_alerts"] = new_count
    except Exception as e:
        out["errors"].append(f"sweep_failed: {type(e).__name__}: {str(e)[:120]}")

    return jsonify(out), 200


@hyperscaler_alerts_bp.route("/api/v1/hyperscaler-alerts", methods=["GET"])
def list_alerts():
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 200
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, headline, url, source, value_usd, value_display, actor,
                       published_at, detected_at
                FROM hyperscaler_alerts
                ORDER BY detected_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
            for r in rows:
                for k, v in list(r.items()):
                    if isinstance(v, datetime.datetime):
                        r[k] = v.isoformat()
        return jsonify({
            "count": len(rows),
            "alerts": rows,
            "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
            "threshold_usd": MIN_DEAL_USD,
        }), 200
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}", "detail": str(e)[:200]}), 500
