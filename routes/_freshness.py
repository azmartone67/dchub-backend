"""routes/_freshness.py — Brain v2 · Layer 3 support (real schema)"""
import os, logging
from datetime import datetime, timezone
log = logging.getLogger(__name__)

def _age(conn, sql):
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if not row or row[0] is None: return None
            ts = row[0]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z","+00:00"))
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            elif not hasattr(ts, "tzinfo"):
                # Date only
                ts = datetime.combine(ts, datetime.min.time()).replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except Exception as e:
        log.debug(f"freshness err: {e}")
        return None

# Real schema mapping discovered via phase 178 introspection:
#   ISO   → eia_electricity_rates (retrieved_at)  — 49d stale, 2000 rows
#   News  → $NEWS_TABLE
#   Tests → ai_testimonials (created_at)  — 12d stale, 1198 rows
#   Stats → db_health_snapshots (snapshot_at)  — fresh, 323 rows
QUERIES = {
    "iso_ingest_age_seconds":   "SELECT MAX(retrieved_at) FROM eia_electricity_rates",
    "news_age_seconds":         "SELECT MAX(published_date) FROM news",
    "testimonials_age_seconds": "SELECT MAX(created_at) FROM ai_testimonials",
    "stats_snapshot_age_seconds":"SELECT MAX(snapshot_at) FROM db_health_snapshots",
}

def freshness_dict(conn):
    out = {}
    for field, sql in QUERIES.items():
        if not sql:
            out[field] = None
            continue
        out[field] = _age(conn, sql)
    return out

def freshness_dict_from_url(database_url=None):
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        return {k: None for k in QUERIES.keys()}
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5)
        try: return freshness_dict(conn)
        finally: conn.close()
    except Exception as e:
        log.warning(f"freshness conn err: {e}")
        return {k: None for k in QUERIES.keys()}

def introspect_freshness_candidates():
    url = os.environ.get("DATABASE_URL")
    out = {"tables": [], "error": None}
    if not url:
        out["error"] = "DATABASE_URL not set"
        return out
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name ~* '(iso|news|test|stats|grid|article|eia|snap|capture|ingest|fetch|publi|monitor|heartbeat)'
                    ORDER BY table_name LIMIT 80;
                """)
                table_names = [r[0] for r in cur.fetchall()]
                for t in table_names:
                    cur.execute("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema='public' AND table_name=%s
                          AND data_type IN ('timestamp without time zone','timestamp with time zone','date')
                        ORDER BY ordinal_position LIMIT 5;
                    """, (t,))
                    cols = [r[0] for r in cur.fetchall()]
                    entry = {"table": t, "timestamp_columns": cols, "max_ts": None, "row_count": None}
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {t};")
                        entry["row_count"] = cur.fetchone()[0]
                    except: pass
                    if cols:
                        try:
                            cur.execute(f"SELECT MAX({cols[0]}) FROM {t};")
                            v = cur.fetchone()[0]
                            entry["max_ts"] = v.isoformat() if v else None
                        except Exception as e:
                            entry["max_ts_error"] = str(e)[:100]
                    out["tables"].append(entry)
        finally:
            conn.close()
    except Exception as e:
        out["error"] = str(e)
    return out
