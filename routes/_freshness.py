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
    # r-newsdead (2026-08-13): was MAX(published_date) FROM `news` — a table
    # NOTHING writes any more. Its only writer is news_aggregator.py; the live
    # loader (/api/jobs/news-refresh -> auto_sync.sync_news) writes
    # `news_articles`. So this probe measured a secondary table and reported the
    # news feed stale while it was fetching every few hours.
    #
    # ★ CORRECTION 2026-09-03: "invoked from no workflow or cron" was too strong
    # and is now removed. crawler_scheduler.py:1366 DOES run news_aggregator
    # ("Run news sync once. Uses news_aggregator (proven working path)"), and
    # `news` was last written 06:33Z on 2026-09-03 — 3,561 rows against
    # news_articles' 13,032. So `news` is a SMALLER, LIVE, secondary pipeline,
    # not an abandoned table. The probe's move to news_articles + fetched_at
    # remains right; the reason recorded for it was not. This matters because
    # routes/data_freshness_radar.py::_DOMAINS still watches `news`, and anyone
    # reading the old wording would "fix" that by repointing a live domain at a
    # different pipeline on the strength of a claim that is not true. That false alarm cost brain investigation #100046 and
    # three earlier "heartbeat_surfaces_stale" fixes, all aimed at a working
    # pipeline.
    #
    # fetched_at, NOT published_at: feeds publish ahead (max published_at is
    # currently 2026-09-21, five weeks out), so published_at would read fresh
    # for weeks after the loader died — false calm, the same class of bug
    # pointing the wrong way. fetched_at is the loader's own heartbeat.
    "news_age_seconds":         "SELECT MAX(fetched_at) FROM news_articles",
    "testimonials_age_seconds": "SELECT MAX(created_at) FROM ai_testimonials",
    "stats_snapshot_age_seconds":"SELECT MAX(snapshot_at) FROM db_health_snapshots",
    # customer white-glove loop heartbeat — last time the tick touched a
    # paying customer's lifecycle_stage. Goes stale iff the loop stops; the
    # authoritative alarm is brain check_cron_freshness (cron_last_run), this
    # is the data-layer mirror on the freshness board.
    "customer_lifecycle_age_seconds":
        "SELECT MAX(last_touched_at) FROM users WHERE last_touch_by='customer_white_glove'",
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


def diag_for_brain_v2():
    """One-shot diagnostic — returns everything Brain v2 needs to write fixes.
    Press table info, facilities schema + status, eia_electricity_rates, markets endpoint shape.
    """
    import os
    url = os.environ.get("DATABASE_URL")
    out = {"db_url_set": bool(url)}
    if not url:
        out["error"] = "DATABASE_URL not set"
        return out
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=8)
        with conn.cursor() as cur:
            # Press tables
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='public' AND table_name ~* '(press|release)' ORDER BY 1;
            """)
            press_tables = []
            for r in cur.fetchall():
                t = r[0]
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                cnt = cur.fetchone()[0]
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position LIMIT 12", (t,))
                cols = [c[0] for c in cur.fetchall()]
                press_tables.append({"table": t, "rows": cnt, "cols": cols})
            out["press_tables"] = press_tables

            # Facilities
            cur.execute("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name='facilities' ORDER BY ordinal_position;
            """)
            fac_cols = [{"col": r[0], "type": r[1]} for r in cur.fetchall()]
            out["facilities_cols"] = fac_cols
            # lint: legacy-facilities-ok — intentional audit of legacy table
            cur.execute("SELECT COUNT(*) FROM facilities")
            out["facilities_total"] = cur.fetchone()[0]
            cur.execute("SELECT status, COUNT(*) FROM facilities GROUP BY status ORDER BY 2 DESC LIMIT 12")
            out["facilities_status_dist"] = [{"status": r[0], "count": r[1]} for r in cur.fetchall()]

            # eia_electricity_rates
            cur.execute("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name='eia_electricity_rates' ORDER BY ordinal_position;
            """)
            out["eia_cols"] = [{"col": r[0], "type": r[1]} for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM eia_electricity_rates")
            out["eia_total"] = cur.fetchone()[0]
            cur.execute("SELECT * FROM eia_electricity_rates ORDER BY retrieved_at DESC LIMIT 1")
            row = cur.fetchone()
            out["eia_last_row"] = str(row)[:300] if row else None

            # Markets table
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='markets' ORDER BY ordinal_position;
            """)
            out["markets_cols"] = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM markets")
            out["markets_total"] = cur.fetchone()[0]

            # News — the LIVE table. r-newsdead (2026-08-13): this introspection
            # described `news`, which nothing writes, so the diagnostic a human
            # reaches for when the board says "news stale" confirmed the wrong
            # table and sent three fixes at a working pipeline.
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='news_articles' ORDER BY ordinal_position LIMIT 12;
            """)
            out["news_cols"] = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM news_articles")
            out["news_total"] = cur.fetchone()[0]

        conn.close()
    except Exception as e:
        out["error"] = str(e)[:200]
    return out
