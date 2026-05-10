"""
routes/_freshness.py — Brain v2 · Layer 3 support

Returns age-in-seconds for each ingestion pipeline. Used by /api/v1/health
so Layer 3's qa-stale-remediate.py can probe staleness.

Defensive: every query is wrapped to return None on any error (missing
table, connection issue, schema mismatch). Never raises — health endpoint
must stay snappy.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

def _age_seconds(conn, sql: str) -> float | None:
    """Run a query expected to return a single timestamp. Convert to age-seconds."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            ts = row[0]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except Exception as e:
        log.debug(f"freshness query failed: {e}")
        return None

def freshness_dict(conn) -> dict:
    """Return {field: age_seconds_or_None} for every monitored pipeline.
    Pass an already-open psycopg2 connection. Adapt the SQL to whatever
    tables actually exist in your schema.
    """
    out = {}

    # ISO ingest — try both common table names
    iso_sql_candidates = [
        "SELECT MAX(captured_at) FROM eia_lmp_snapshots",
        "SELECT MAX(captured_at) FROM iso_snapshots",
        "SELECT MAX(snapshot_at) FROM iso_data",
        "SELECT MAX(ingested_at) FROM grid_snapshots",
    ]
    age = None
    for sql in iso_sql_candidates:
        age = _age_seconds(conn, sql)
        if age is not None:
            break
    out["iso_ingest_age_seconds"] = age

    # News
    news_sql_candidates = [
        "SELECT MAX(published_at) FROM news_articles",
        "SELECT MAX(fetched_at) FROM news",
        "SELECT MAX(created_at) FROM news_articles",
        "SELECT MAX(published_at) FROM articles",
    ]
    age = None
    for sql in news_sql_candidates:
        age = _age_seconds(conn, sql)
        if age is not None:
            break
    out["news_age_seconds"] = age

    # Testimonials
    test_sql_candidates = [
        "SELECT MAX(captured_at) FROM testimonials",
        "SELECT MAX(created_at) FROM testimonials",
        "SELECT MAX(captured_at) FROM ai_validations",
    ]
    age = None
    for sql in test_sql_candidates:
        age = _age_seconds(conn, sql)
        if age is not None:
            break
    out["testimonials_age_seconds"] = age

    # Stats snapshot
    stats_sql_candidates = [
        "SELECT MAX(snapshot_at) FROM stats_snapshots",
        "SELECT MAX(captured_at) FROM stats_snapshots",
        "SELECT MAX(generated_at) FROM stats_daily",
    ]
    age = None
    for sql in stats_sql_candidates:
        age = _age_seconds(conn, sql)
        if age is not None:
            break
    out["stats_snapshot_age_seconds"] = age

    return out

def freshness_dict_from_url(database_url: str | None = None) -> dict:
    """Convenience wrapper if the caller doesn't already have a connection."""
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        return {k: None for k in [
            "iso_ingest_age_seconds", "news_age_seconds",
            "testimonials_age_seconds", "stats_snapshot_age_seconds",
        ]}
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            return freshness_dict(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"freshness connection failed: {e}")
        return {k: None for k in [
            "iso_ingest_age_seconds", "news_age_seconds",
            "testimonials_age_seconds", "stats_snapshot_age_seconds",
        ]}
