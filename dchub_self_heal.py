"""
DC Hub Self-Healer · Phase 227
==============================
Runs INSIDE the Flask process via APScheduler. Every 5 min:
  1. Probes 10 critical endpoints
  2. Detects known error patterns
  3. Applies known fixes (SQL patches, cache invalidation, etc)
  4. Logs every action to self_heal_events table
  5. Uses Postgres advisory lock so only one worker runs heal cycle

The site is the living organism. This is its immune system.
"""
import os, json, time, traceback, logging
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

log = logging.getLogger("self_heal")
log.setLevel(logging.INFO)

DATABASE_URL = os.environ.get("DATABASE_URL")
BASE_URL = os.environ.get("SELF_HEAL_BASE_URL", "https://dchub.cloud")

# Critical endpoints to probe
PROBES = [
    ("/dcpi", "html"),
    ("/api/v1/markets/list", "json"),
    ("/api/v1/dcpi/scores", "json"),
    ("/api/v1/dcpi/movers", "json"),
    ("/api/v1/media/feed", "json"),
    ("/announcements", "html"),
    ("/markets/", "html"),
    ("/news/dcpi-v2-launch/", "html"),
    ("/dcpi/methodology/", "html"),
    ("/api/v1/health", "json"),
]

# Known error patterns and their healing strategies
PATTERNS = [
    {
        "name": "noneround",
        "match": ["NoneType", "__round__"],
        "fix": "sql_coalesce_market_scores",
    },
    {
        "name": "noneformat",
        "match": ["NoneType", "__format__"],
        "fix": "sql_coalesce_market_scores",
    },
    {
        "name": "nonegetitem",
        "match": ["NoneType", "__getitem__"],
        "fix": "log_only",
    },
    {
        "name": "media_feed_empty",
        "match": ['"items":[]', '"items": []'],
        "fix": "rerun_aggregation",
    },
    {
        "name": "media_feed_news_only",
        "match": [],  # custom detector
        "fix": "rerun_aggregation",
        "custom": "media_feed_only_news",
    },
    {
        "name": "http_500",
        "match": ['"error"', '"success":false'],
        "fix": "log_only",
    },
]


def _conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, connect_timeout=8)


def ensure_log_table():
    if not DATABASE_URL: return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS self_heal_events (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ DEFAULT NOW(),
                    cycle_id TEXT,
                    endpoint TEXT,
                    pattern TEXT,
                    fix_applied TEXT,
                    success BOOLEAN,
                    details TEXT
                );
                CREATE INDEX IF NOT EXISTS self_heal_events_ts_idx
                    ON self_heal_events (ts DESC);
            """)
            c.commit()
    except Exception as e:
        log.warning("ensure_log_table failed: %s", e)


def log_event(cycle_id, endpoint, pattern, fix, success, details=""):
    if not DATABASE_URL: return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO self_heal_events
                  (cycle_id, endpoint, pattern, fix_applied, success, details)
                VALUES (%s,%s,%s,%s,%s,%s);
            """, (cycle_id, endpoint, pattern, fix, success, details[:2000]))
            c.commit()
    except Exception as e:
        log.warning("log_event failed: %s", e)


def probe(path, kind="html", timeout=8):
    url = BASE_URL + path
    try:
        req = Request(url, headers={"User-Agent": "DCHub-SelfHeal/1.0"})
        with urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            return r.status, body
    except HTTPError as e:
        try: body = e.read().decode("utf-8", errors="ignore")
        except Exception: body = str(e)
        return e.code, body
    except URLError as e:
        return 0, str(e)
    except Exception as e:
        return -1, str(e)


def detect(body, status):
    """Return list of (pattern_name, fix_name) tuples that match."""
    hits = []
    for pat in PATTERNS:
        # custom detector?
        if pat.get("custom") == "media_feed_only_news":
            try:
                d = json.loads(body)
                items = d.get("items") or d.get("feed") or []
                if items:
                    cats = {i.get("category", i.get("type", "?")) for i in items}
                    if cats == {"news"} or cats == {"news", "?"}:
                        hits.append((pat["name"], pat["fix"]))
            except Exception:
                pass
            continue
        # standard pattern matcher
        matches = pat.get("match", [])
        if matches and all(m in body for m in matches):
            hits.append((pat["name"], pat["fix"]))
    if status >= 500 and not hits:
        hits.append(("http_5xx", "log_only"))
    return hits


# ---------- HEALING ACTIONS ----------

def fix_sql_coalesce_market_scores():
    """UPDATE market_power_scores to replace NULL numerics with 0."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE market_power_scores
                SET constraint_score    = COALESCE(constraint_score, 0),
                    excess_power_score  = COALESCE(excess_power_score, 0),
                    time_to_power_months= COALESCE(time_to_power_months, 0),
                    queue_wait_months   = COALESCE(queue_wait_months, 0),
                    reserve_margin_pct  = COALESCE(reserve_margin_pct, 0),
                    gen_additions_12mo_mw = COALESCE(gen_additions_12mo_mw, 0),
                    curtailment_pct     = COALESCE(curtailment_pct, 0),
                    stranded_capacity_mw= COALESCE(stranded_capacity_mw, 0)
                WHERE constraint_score IS NULL
                   OR excess_power_score IS NULL
                   OR time_to_power_months IS NULL
                   OR queue_wait_months IS NULL
                   OR reserve_margin_pct IS NULL
                   OR gen_additions_12mo_mw IS NULL
                   OR curtailment_pct IS NULL
                   OR stranded_capacity_mw IS NULL;
            """)
            n = cur.rowcount
            c.commit()
            return True, f"coalesced {n} rows"
    except Exception as e:
        return False, str(e)[:300]


def fix_rerun_aggregation():
    """Touch aggregator — invalidates any in-memory cache."""
    try:
        import dchub_media
        if hasattr(dchub_media, "aggregate_announcements"):
            items = dchub_media.aggregate_announcements()
            return True, f"re-aggregated {len(items)} items"
        return False, "aggregator not found"
    except Exception as e:
        return False, str(e)[:300]


def fix_log_only():
    return True, "logged for human review"


FIXES = {
    "sql_coalesce_market_scores": fix_sql_coalesce_market_scores,
    "rerun_aggregation": fix_rerun_aggregation,
    "log_only": fix_log_only,
}


def acquire_lock():
    """Postgres advisory lock so only ONE worker runs heal cycle."""
    if not DATABASE_URL: return None
    try:
        c = _conn()
        cur = c.cursor()
        cur.execute("SELECT pg_try_advisory_lock(7727227);")
        got = cur.fetchone()[0]
        if got:
            return c
        c.close()
        return None
    except Exception:
        return None


def release_lock(c):
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(7727227);")
        c.commit()
        c.close()
    except Exception:
        pass


def heal_cycle():
    """One full cycle: probe → detect → fix → log."""
    lock = acquire_lock()
    if lock is None:
        log.info("self_heal: another worker holds lock, skipping")
        return {"skipped": True}
    cycle_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    summary = {"cycle_id": cycle_id, "probes": 0, "issues": 0, "fixes_ok": 0, "fixes_fail": 0, "events": []}
    try:
        ensure_log_table()
        for path, kind in PROBES:
            status, body = probe(path, kind)
            summary["probes"] += 1
            hits = detect(body, status)
            if not hits:
                continue
            for pat_name, fix_name in hits:
                summary["issues"] += 1
                fix = FIXES.get(fix_name, fix_log_only)
                try:
                    ok, details = fix()
                except Exception as e:
                    ok, details = False, traceback.format_exc()[:1000]
                if ok: summary["fixes_ok"] += 1
                else: summary["fixes_fail"] += 1
                log_event(cycle_id, path, pat_name, fix_name, ok, details)
                summary["events"].append({
                    "endpoint": path, "pattern": pat_name,
                    "fix": fix_name, "ok": ok, "details": details[:200],
                })
    finally:
        release_lock(lock)
    log.info("self_heal cycle %s: %s probes, %s issues, %s fixed, %s failed",
             cycle_id, summary["probes"], summary["issues"],
             summary["fixes_ok"], summary["fixes_fail"])
    return summary


def get_recent_events(limit=50):
    if not DATABASE_URL: return []
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, ts, cycle_id, endpoint, pattern, fix_applied, success, details
                FROM self_heal_events ORDER BY ts DESC LIMIT %s;
            """, (limit,))
            rows = cur.fetchall()
            return [{
                "id": r[0],
                "ts": r[1].isoformat() if r[1] else None,
                "cycle_id": r[2], "endpoint": r[3], "pattern": r[4],
                "fix_applied": r[5], "success": r[6], "details": r[7],
            } for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def get_status():
    """Public-safe status snapshot."""
    if not DATABASE_URL:
        return {"healer": "disabled", "reason": "no DATABASE_URL"}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE success = true),
                       COUNT(*) FILTER (WHERE success = false),
                       MAX(ts)
                FROM self_heal_events
                WHERE ts > NOW() - INTERVAL '24 hours';
            """)
            total, ok, fail, last = cur.fetchone()
            cur.execute("""
                SELECT pattern, COUNT(*) FROM self_heal_events
                WHERE ts > NOW() - INTERVAL '24 hours'
                GROUP BY pattern ORDER BY COUNT(*) DESC LIMIT 5;
            """)
            patterns = [{"pattern": p, "count": n} for p, n in cur.fetchall()]
        return {
            "healer": "alive",
            "window": "24h",
            "total_events": total or 0,
            "successful_fixes": ok or 0,
            "failed_fixes": fail or 0,
            "last_event": last.isoformat() if last else None,
            "top_patterns": patterns,
            "probe_count": len(PROBES),
            "next_cycle_minutes": 5,
        }
    except Exception as e:
        return {"healer": "degraded", "error": str(e)[:200]}


_scheduler = None

def start_scheduler():
    """Called from main.py at app startup."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        log.warning("APScheduler not installed — self-heal scheduler NOT started")
        return None
    _scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
    _scheduler.add_job(heal_cycle, "interval", minutes=5, id="heal_cycle",
                       max_instances=1, coalesce=True, misfire_grace_time=120)
    # Also run once 60 seconds after boot
    from datetime import datetime, timedelta
    _scheduler.add_job(heal_cycle, "date",
                       run_date=datetime.utcnow() + timedelta(seconds=60),
                       id="heal_warmup", max_instances=1)
    _scheduler.start()
    log.info("self_heal scheduler STARTED — heal_cycle every 5 min")
    return _scheduler
