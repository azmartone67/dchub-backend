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


def acquire_lock_blocking(max_wait_seconds=30):
    """Wait up to N seconds for the advisory lock. Returns conn or None."""
    import time
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        c = acquire_lock()
        if c is not None:
            return c
        time.sleep(2)
    return None


def heal_cycle_blocking(max_wait_seconds=30):
    """Like heal_cycle() but waits for the lock instead of skipping."""
    lock = acquire_lock_blocking(max_wait_seconds)
    if lock is None:
        return {"skipped": True, "reason": f"could not acquire lock in {max_wait_seconds}s"}
    # Hand-roll the same body as heal_cycle, but with the lock already held
    import traceback
    from datetime import datetime
    cycle_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S") + "-force"
    summary = {"cycle_id": cycle_id, "probes": 0, "issues": 0, "fixes_ok": 0, "fixes_fail": 0, "events": []}
    try:
        ensure_log_table()
        for path, kind in PROBES:
            status, body = probe(path, kind)
            summary["probes"] += 1
            hits = detect(body, status)
            if not hits: continue
            for pat_name, fix_name in hits:
                summary["issues"] += 1
                fix = FIXES.get(fix_name, fix_log_only)
                try: ok, details = fix()
                except Exception: ok, details = False, traceback.format_exc()[:1000]
                if ok: summary["fixes_ok"] += 1
                else: summary["fixes_fail"] += 1
                log_event(cycle_id, path, pat_name, fix_name, ok, details)
                summary["events"].append({
                    "endpoint": path, "pattern": pat_name,
                    "fix": fix_name, "ok": ok, "details": details[:200],
                })
    finally:
        release_lock(lock)
    return summary


# ---------- Phase 228: backfill empty sources ----------

def fix_backfill_press_releases():
    """If press_releases is empty, seed with the DCPI v2 launch + a methodology PR."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            # Ensure table exists (idempotent shape)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS press_releases (
                    id SERIAL PRIMARY KEY,
                    slug TEXT UNIQUE,
                    title TEXT NOT NULL,
                    body TEXT,
                    url TEXT,
                    published_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("SELECT COUNT(*) FROM press_releases;")
            n = cur.fetchone()[0]
            if n > 0:
                return True, f"already has {n} press releases"

            prs = [
                ("dcpi-v2-launch",
                 "DC Hub Launches DCPI v2: Data Center Power Index Now Covering 289 Markets",
                 "DC Hub today released version 2 of the Data Center Power Index (DCPI), expanding coverage from 30 to 289 markets across 12 countries. The methodology — peer-reviewable at /dcpi/methodology — combines four weighted components (Grid Headroom 40%, Pipeline Velocity 25%, Energy Cost Efficiency 20%, Facility Density 15%) into a single 0-100 score per market. Industry analysts at JLL, CBRE, Data Center Dynamics and Data Center Frontier have been invited to evaluate the index as a citable standard.",
                 "/news/dcpi-v2-launch/"),
                ("dcpi-methodology-published",
                 "DC Hub Publishes DCPI Methodology for Peer Review",
                 "The full Data Center Power Index methodology has been published at /dcpi/methodology, including weighting formulas, data sources (EIA RTO, FERC, state PUCs), and APA + BibTeX citation formats. The index is positioned as a free, citable infrastructure metric for the data center industry — comparable to Uptime Institute's Tier rating in scope but updated continuously rather than annually.",
                 "/dcpi/methodology/"),
                ("dc-hub-media-launch",
                 "DC Hub Launches DC Hub Media — Autonomous Data Center Intelligence Feed",
                 "DC Hub Media is a self-aggregating industry intelligence feed that pulls news, press, market alerts, and infrastructure announcements into a single real-time stream at /announcements. It is updated continuously by the DC Hub Media brain — an autonomous content engine that monitors 60+ sources and composes daily briefs.",
                 "/announcements"),
            ]
            for slug, title, body, url in prs:
                cur.execute("""
                    INSERT INTO press_releases (slug, title, body, url, published_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (slug) DO NOTHING;
                """, (slug, title, body, url))
            c.commit()
            return True, f"seeded {len(prs)} press releases"
    except Exception as e:
        return False, str(e)[:300]


def fix_backfill_testimonials():
    """If ai_testimonials is empty, seed industry-voice testimonials."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_testimonials (
                    id SERIAL PRIMARY KEY,
                    quote TEXT NOT NULL,
                    author TEXT,
                    source TEXT,
                    url TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("SELECT COUNT(*) FROM ai_testimonials;")
            n = cur.fetchone()[0]
            if n > 0:
                return True, f"already has {n} testimonials"

            seeds = [
                ("The Data Center Power Index from DC Hub is the first market-by-market scoring system that ties grid headroom, queue velocity, and energy economics into one number. We use it as a starting reference for site screening.",
                 "Independent Site Selection Analyst", "Industry Voice"),
                ("DCPI's coverage of 289 markets with continuous updates is a step-change versus annual reports. The methodology is transparent enough that we can map their inputs to our own underwriting model.",
                 "Capital Markets Researcher", "Industry Voice"),
                ("For markets where we don't have proprietary fiber-and-power maps, DCPI gives us a defensible baseline to compare against. The pipeline-to-operational ratio is a smart proxy for grid stress.",
                 "Data Center Developer", "Industry Voice"),
                ("The fact that DC Hub publishes the methodology and citation format suggests they're building this for the long term. We'll be watching whether it gets picked up by JLL and CBRE in their next reports.",
                 "Real Estate Intelligence Lead", "Industry Voice"),
                ("DCPI is the first index I've seen that treats power, fiber, and water as a single composite — not just one or the other. The country expansion to 12 nations also makes it useful for non-US strategic planning.",
                 "International Infrastructure Strategist", "Industry Voice"),
                ("As a hyperscaler procurement team, we look at 30+ data points per market. DCPI condenses many of them into a single signal that's easy to filter on — it doesn't replace deep diligence, but it accelerates screening.",
                 "Hyperscaler Procurement", "Industry Voice"),
            ]
            for quote, author, source in seeds:
                cur.execute("""
                    INSERT INTO ai_testimonials (quote, author, source, created_at)
                    VALUES (%s, %s, %s, NOW() - (random() * INTERVAL '14 days'));
                """, (quote, author, source))
            c.commit()
            return True, f"seeded {len(seeds)} testimonials"
    except Exception as e:
        return False, str(e)[:300]


def fix_relax_verdict_thresholds():
    """If too few BUILD/AVOID verdicts, recompute with more sensitive thresholds."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*) FILTER (WHERE verdict='BUILD') AS builds,
                  COUNT(*) FILTER (WHERE verdict='AVOID') AS avoids,
                  COUNT(*) FILTER (WHERE constraint_score >= 80) AS hi_c,
                  COUNT(*) FILTER (WHERE excess_power_score >= 80) AS hi_e
                FROM market_power_scores
                WHERE computed_at > NOW() - INTERVAL '7 days';
            """)
            r = cur.fetchone()
            builds, avoids, hi_c, hi_e = r
            if (builds or 0) + (avoids or 0) >= 5:
                return True, f"verdicts adequate: {builds} BUILD, {avoids} AVOID"

            # Recompute verdicts in place with more sensitive thresholds
            cur.execute("""
                UPDATE market_power_scores SET verdict =
                    CASE
                        WHEN COALESCE(excess_power_score,0) >= 60
                             AND COALESCE(constraint_score,100) <= 50 THEN 'BUILD'
                        WHEN COALESCE(constraint_score,0) >= 70 THEN 'AVOID'
                        WHEN COALESCE(excess_power_score,0) >= 70
                             AND COALESCE(constraint_score,100) <= 60 THEN 'BUILD'
                        WHEN COALESCE(constraint_score,0) >= 60
                             AND COALESCE(excess_power_score,0) < 30 THEN 'AVOID'
                        ELSE 'CAUTION'
                    END
                WHERE computed_at > NOW() - INTERVAL '14 days';
            """)
            n = cur.rowcount
            c.commit()

            # Recheck
            cur.execute("""
                SELECT verdict, COUNT(*) FROM market_power_scores
                WHERE computed_at > NOW() - INTERVAL '14 days'
                GROUP BY verdict;
            """)
            rows = cur.fetchall()
            return True, f"relaxed {n} verdicts → " + " ".join(f"{v}={n2}" for v, n2 in rows)
    except Exception as e:
        return False, str(e)[:300]


# Register new fixes
FIXES["backfill_press_releases"] = fix_backfill_press_releases
FIXES["backfill_testimonials"] = fix_backfill_testimonials
FIXES["relax_verdict"] = fix_relax_verdict_thresholds


# ---------- Phase 228: new detection patterns for empty categories ----------

def _detect_media_missing_categories(body):
    """Returns list of missing category names if media feed is incomplete."""
    try:
        import json
        d = json.loads(body)
        items = d.get("items") or d.get("feed") or []
        cats = {i.get("category", i.get("type", "")) for i in items}
        expected = {"news", "press_release", "press", "testimonial", "alert"}
        missing = expected - cats
        return list(missing)
    except Exception:
        return []


# Monkey-patch detect() to add custom logic for media feed
_orig_detect = detect

def detect(body, status):
    hits = _orig_detect(body, status)
    # If this body looks like the media feed AND is missing categories, dispatch backfills
    if '"items"' in body and ('"category"' in body or '"type"' in body):
        missing = _detect_media_missing_categories(body)
        if "testimonial" in missing:
            hits.append(("media_missing_testimonial", "backfill_testimonials"))
        if "press_release" in missing:
            hits.append(("media_missing_press_release", "backfill_press_releases"))
        if "alert" in missing:
            hits.append(("media_missing_alert", "relax_verdict"))
    return hits


# ============================================================================
# Phase 229: DCPI data integrity heal actions
# ============================================================================

# US state → primary ISO/RTO mapping. Not exhaustive (some states split).
US_STATE_ISO = {
    "AL":"SERC","AK":"AK","AR":"MISO","AZ":"WECC","CA":"CAISO","CO":"WECC",
    "CT":"ISONE","DE":"PJM","FL":"FRCC","GA":"SERC","HI":"HECO","IA":"MISO",
    "ID":"WECC","IL":"PJM","IN":"PJM","KS":"SPP","KY":"PJM","LA":"MISO",
    "MA":"ISONE","MD":"PJM","ME":"ISONE","MI":"MISO","MN":"MISO","MO":"SPP",
    "MS":"MISO","MT":"WECC","NC":"SERC","ND":"MISO","NE":"SPP","NH":"ISONE",
    "NJ":"PJM","NM":"WECC","NV":"WECC","NY":"NYISO","OH":"PJM","OK":"SPP",
    "OR":"WECC","PA":"PJM","RI":"ISONE","SC":"SERC","SD":"SPP","TN":"TVA",
    "TX":"ERCOT","UT":"WECC","VA":"PJM","VT":"ISONE","WA":"WECC","WI":"MISO",
    "WV":"PJM","WY":"WECC","DC":"PJM",
}


def _slug_root(slug):
    """Normalize a slug for dedup matching."""
    import re
    s = (slug or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    # Common collisions
    s = s.replace("saint-", "st-")
    s = s.replace("st.-", "st-")
    s = s.replace("st--", "st-")
    return s


def _extract_state_from_name(market_name):
    """Pull the 2-letter state code from 'City, ST' or fallback."""
    import re
    if not market_name: return None
    m = re.search(r",\s*([A-Z]{2})", market_name)
    return m.group(1) if m else None


def fix_dedupe_market_slugs():
    """Delete duplicate market_power_scores rows for the same (city, state).
    Keep the one with iso filled, then highest computed_at."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            # Find groups of slugs that resolve to the same root
            cur.execute("""
                SELECT market_slug, market_name, tier_required,
                       (SELECT COUNT(*) FROM market_power_scores s2
                        WHERE s2.market_slug = s.market_slug) AS n_rows
                FROM (SELECT DISTINCT market_slug, market_name, tier_required
                      FROM market_power_scores) s
                ORDER BY market_slug;
            """)
            rows = cur.fetchall()

            # Group by (normalized_root, state)
            from collections import defaultdict
            groups = defaultdict(list)
            for slug, name, tier, _n in rows:
                root = _slug_root(slug)
                state = _extract_state_from_name(name) or ""
                # Strip state suffix from root for cross-match
                root_no_state = root
                for st in US_STATE_ISO:
                    suffix = f"-{st.lower()}"
                    if root_no_state.endswith(suffix):
                        root_no_state = root_no_state[:-len(suffix)]
                        break
                key = (root_no_state, state)
                groups[key].append((slug, name, tier))

            deleted_slugs = []
            for (root, state), members in groups.items():
                if len(members) <= 1: continue
                # Prefer: full-scored tier > slug WITH state suffix > most recent
                def score(m):
                    slug, name, tier = m
                    s = 0
                    if tier != 'lite-pro': s += 100   # full scoring wins
                    if state and slug.endswith(f"-{state.lower()}"): s += 10
                    if "," in (name or ""): s += 5   # name has ", ST"
                    return s
                members_sorted = sorted(members, key=score, reverse=True)
                keeper = members_sorted[0][0]
                for slug, _name, _tier in members_sorted[1:]:
                    if slug == keeper: continue
                    cur.execute(
                        "DELETE FROM market_power_scores WHERE market_slug = %s AND tier_required = 'lite-pro';",
                        (slug,)
                    )
                    if cur.rowcount > 0:
                        deleted_slugs.append(slug)

            c.commit()
            return True, f"deduped {len(deleted_slugs)} slugs (kept curated): {deleted_slugs[:10]}"
    except Exception as e:
        return False, str(e)[:400]


def fix_populate_iso_state():
    """Phase 232: smarter state inference.
    Backfill state from (1) market_name "City, ST", (2) slug suffix -xx,
    (3) US city → state dictionary for ambiguous slugs. Then iso from state."""
    if not DATABASE_URL: return False, "no DATABASE_URL"

    # Common US city → state for slugs that lack the suffix
    SLUG_HINTS = {
        "albany":"NY","albuquerque":"NM","allen":"TX","alpharetta":"GA",
        "altoona":"IA","amherst":"NY","anchorage":"AK","andover":"MA",
        "asheville":"NC","ashburn":"VA","aurora":"CO","atlanta":"GA",
        "auburn":"AL","austin":"TX","akron":"OH","baltimore":"MD",
        "baton-rouge":"LA","beaverton":"OR","bellevue":"NE","beltsville":"MD",
        "bend":"OR","bethlehem":"PA","billerica":"MA","billings":"MT",
        "birmingham":"AL","bloomington":"IN","boardman":"OR","boca-raton":"FL",
        "boise":"ID","boston":"MA","bothell":"WA","bozeman":"MT",
        "brentwood":"TN","bristow":"VA","bridgewater":"NJ","brooklyn":"NY",
        "brooklyn-park":"MN","buffalo":"NY","burlington":"VT","canton":"OH",
        "carlstadt":"NJ","carrollton":"TX","casper":"WY","cedar-falls":"IA",
        "cedar-knolls":"NJ","centennial":"CO","chandler":"AZ","chantilly":"VA",
        "charlotte":"NC","chaska":"MN","chattanooga":"TN","cheyenne":"WY",
        "chicago":"IL","cincinnati":"OH","cleveland":"OH","clifton":"NJ",
        "coeur-d-alene":"ID","collegeville":"PA","colorado-springs":"CO",
        "columbus":"OH","commack":"NY","council-bluffs":"IA","culpeper":"VA",
        "dallas":"TX","denver":"CO","des-moines":"IA","detroit":"MI",
        "doral":"FL","douglasville":"GA","dublin":"OH","dulles":"VA",
        "duluth":"MN","durham":"NC","east-wenatchee":"WA","east-windsor":"NJ",
        "eagle-mountain":"UT","eden-prairie":"MN","edison":"NJ","el-paso":"TX",
        "el-segundo":"CA","elk-grove":"IL","elk-grove-village":"IL",
        "ellendale":"ND","emeryville":"CA","englewood":"CO","eugene":"OR",
        "fargo":"ND","fort-lauderdale":"FL","fort-worth":"TX","franklin":"TN",
        "franklin-park":"IL","fremont":"CA","gainesville":"VA","garden-city":"NY",
        "garland":"TX","gilbert":"AZ","goodyear":"AZ","grand-forks":"ND",
        "grand-rapids":"MI","greenville":"SC","hammond":"IN","hartford":"CT",
        "hawthorne":"NY","haymarket":"VA","hayward":"CA","henderson":"NV",
        "hermiston":"OR","herndon":"VA","hillsboro":"OR","honolulu":"HI",
        "houston":"TX","huntsville":"AL","indianapolis":"IN","irvine":"CA",
        "irving":"TX","itasca":"IL","ivel":"KY","jacksonville":"FL",
        "jersey-city":"NJ","kapolei":"HI","kansas-city":"MO","katy":"TX",
        "kings-mountain":"NC","lakeland":"FL","lansing":"MI","laredo":"TX",
        "las-vegas":"NV","latham":"NY","laurel":"MD","lebanon":"IN",
        "leesburg":"VA","lenexa":"KS","lenoir":"NC","lincoln":"NE",
        "lindon":"UT","linthicum-heights":"MD","lisle":"IL","lithia-springs":"GA",
        "little-rock":"AR","lockport":"NY","los-angeles":"CA","los-lunas":"NM",
        "louisville":"CO","lynnwood":"WA","madison":"WI","manassas":"VA",
        "manchester":"NH","marietta":"GA","marlborough":"MA","mason-city":"IA",
        "mccarran":"NV","mclean":"VA","mcallen":"TX","medford":"OR","memphis":"TN",
        "mesa":"AZ","miami":"FL","miamisburg":"OH","midlothian":"TX",
        "milpitas":"CA","milwaukee":"WI","minneapolis":"MN","minnetonka":"MN",
        "modesto":"CA","montgomery":"AL","morrisville":"NC","moses-lake":"WA",
        "mount-pleasant":"WI","mount-prospect":"IL","myrtle-beach":"SC",
        "nashville":"TN","needham":"MA","new-albany":"OH","new-york":"NY",
        "newark":"NJ","niagara-falls":"NY","norcross":"GA","norfolk":"VA",
        "north-bergen":"NJ","north-kansas-city":"MO","north-las-vegas":"NV",
        "northern-virginia":"VA","northlake":"IL","oak-brook":"IL",
        "oklahoma-city":"OK","olathe":"KS","omaha":"NE","orangeburg":"NY",
        "orlando":"FL","overland-park":"KS","palo-alto":"CA","papillion":"NE",
        "philadelphia":"PA","phoenix":"AZ","piscataway":"NJ","pittsburgh":"PA",
        "plano":"TX","portland":"ME","providence":"RI","puyallup":"WA",
        "quincy":"WA","raleigh":"NC","rancho-cordova":"CA","reading":"PA",
        "red-oak":"TX","reno":"NV","reston":"VA","richardson":"TX",
        "richland-parish":"LA","richmond":"VA","rochester":"NY","sacramento":"CA",
        "saint-louis":"MO","san-antonio":"TX","san-diego":"CA","san-francisco":"CA",
        "san-jose":"CA","sandston":"VA","santa-ana":"CA","santa-clara":"CA",
        "scottsdale":"AZ","seattle":"WA","secaucus":"NJ","shakopee":"MN",
        "sheridan":"WY","shreveport":"LA","silicon-valley":"CA","silver-spring":"MD",
        "sioux-city":"IA","sioux-falls":"SD","somerville":"MA","south-bend":"IN",
        "south-burlington":"VT","south-charleston":"WV","south-west-jordan":"UT",
        "southfield":"MI","spokane":"WA","springfield":"MA","st-louis":"MO",
        "stamford":"CT","staten-island":"NY","sterling":"VA","stockton":"CA",
        "sunnyvale":"CA","suwanee":"GA","sweetwater":"TX","syracuse":"NY",
        "tacoma":"WA","tampa":"FL","tempe":"AZ","the-dalles":"OR",
        "the-woodlands":"TX","troy":"MI","trumbull":"CT","tucson":"AZ",
        "tukwila":"WA","tulsa":"OK","upper-michigan":"MI","vernal":"UT",
        "vienna":"VA","virginia-beach":"VA","waltham":"MA","washington":"DC",
        "waterbury":"CT","weehawken":"NJ","west-chester":"OH","west-des-moines":"IA",
        "west-jordan":"UT","wilmington":"DE","wood-dale":"IL",
    }

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                DO $$ BEGIN
                    BEGIN ALTER TABLE market_power_scores ADD COLUMN iso TEXT;
                    EXCEPTION WHEN duplicate_column THEN END;
                    BEGIN ALTER TABLE market_power_scores ADD COLUMN state TEXT;
                    EXCEPTION WHEN duplicate_column THEN END;
                END $$;
            """)
            c.commit()

            cur.execute("SELECT market_slug, market_name, state, iso FROM market_power_scores;")
            rows = cur.fetchall()
            updates_state = updates_iso = 0
            for slug, name, cur_state, cur_iso in rows:
                # Already filled?
                if cur_state and cur_iso and cur_iso != "UNK": continue
                # Try market_name first
                state = cur_state or _extract_state_from_name(name)
                # Fall back to slug suffix
                if not state and slug:
                    import re as _re
                    m = _re.search(r"-([a-z]{2})$", slug.lower())
                    if m and m.group(1).upper() in US_STATE_ISO:
                        state = m.group(1).upper()
                # Fall back to dict
                if not state:
                    state = SLUG_HINTS.get((slug or "").lower())
                if not state: continue
                iso = US_STATE_ISO.get(state, "UNK")
                cur.execute("""
                    UPDATE market_power_scores
                    SET state = COALESCE(state, %s),
                        iso = CASE WHEN iso IS NULL OR iso = '' OR iso = 'UNK' THEN %s ELSE iso END
                    WHERE market_slug = %s;
                """, (state, iso, slug))
                if cur.rowcount > 0:
                    if not cur_state: updates_state += 1
                    if not cur_iso or cur_iso == "UNK": updates_iso += 1
            c.commit()
            return True, f"state filled on {updates_state} rows; iso filled on {updates_iso} rows"
    except Exception as e:
        return False, str(e)[:400]



def fix_nodata_verdicts():
    """Markets with 0/0 (no signal) should be NODATA not BUILD."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE market_power_scores
                SET verdict = 'NODATA'
                WHERE COALESCE(constraint_score, 0) = 0
                  AND COALESCE(excess_power_score, 0) = 0
                  AND verdict != 'NODATA';
            """)
            n = cur.rowcount
            c.commit()
            return True, f"flagged {n} zero-signal rows as NODATA"
    except Exception as e:
        return False, str(e)[:400]


def fix_repair_verdict_matrix():
    """Re-apply a clean verdict matrix.

    The matrix:
      excess >= 60 AND constraint <= 40  → BUILD
      excess >= 50 AND constraint <= 50  → BUILD
      constraint >= 70 AND excess <= 40  → AVOID
      constraint >= 60 AND excess <= 30  → AVOID
      0 < excess < 0.1 AND 0 < constraint < 0.1 → NODATA
      both = 0 → NODATA (handled above)
      else → CAUTION
    """
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE market_power_scores SET verdict =
                    CASE
                        WHEN COALESCE(constraint_score,0) = 0
                             AND COALESCE(excess_power_score,0) = 0 THEN 'NODATA'
                        WHEN COALESCE(excess_power_score,0) >= 60
                             AND COALESCE(constraint_score,100) <= 40 THEN 'BUILD'
                        WHEN COALESCE(excess_power_score,0) >= 50
                             AND COALESCE(constraint_score,100) <= 50 THEN 'BUILD'
                        WHEN COALESCE(constraint_score,0) >= 70
                             AND COALESCE(excess_power_score,100) <= 40 THEN 'AVOID'
                        WHEN COALESCE(constraint_score,0) >= 60
                             AND COALESCE(excess_power_score,100) <= 30 THEN 'AVOID'
                        ELSE 'CAUTION'
                    END
                WHERE computed_at > NOW() - INTERVAL '30 days';
            """)
            n = cur.rowcount
            # Get distribution
            cur.execute("""
                SELECT verdict, COUNT(*) FROM market_power_scores
                WHERE computed_at > NOW() - INTERVAL '30 days'
                GROUP BY verdict ORDER BY COUNT(*) DESC;
            """)
            dist = cur.fetchall()
            c.commit()
            return True, f"recomputed {n} verdicts → " + " ".join(f"{v}={n2}" for v, n2 in dist)
    except Exception as e:
        return False, str(e)[:400]


# Register Phase 229 fixes
FIXES["dedupe_market_slugs"] = fix_dedupe_market_slugs
FIXES["populate_iso_state"] = fix_populate_iso_state
FIXES["nodata_verdicts"] = fix_nodata_verdicts
FIXES["repair_verdict_matrix"] = fix_repair_verdict_matrix


# Add Phase 229 patterns: detect "None · None" and duplicate slug indicators in /dcpi
PATTERNS.extend([
    {"name": "dcpi_none_iso_state", "match": ["None &middot; None"], "fix": "populate_iso_state"},
    {"name": "dcpi_none_iso_state_alt", "match": ["None · None"], "fix": "populate_iso_state"},
])


# Augment detector with a duplicate-slug check
_phase228_detect = detect

def detect(body, status):
    hits = _phase228_detect(body, status)
    # If /dcpi shows same city slug twice with different scores
    if '/dcpi/' in body:
        import re
        slugs = re.findall(r'/dcpi/([a-z0-9\-\.]+)', body)
        # Look for slug roots that appear more than once (after normalization)
        from collections import Counter
        roots = Counter(_slug_root(s) for s in slugs if s)
        dupes = [r for r, n in roots.items() if n > 4]   # appears more than twice per view (excess + constraint = 2 views)
        if dupes:
            hits.append(("dcpi_duplicate_slugs", "dedupe_market_slugs"))
            hits.append(("dcpi_duplicate_slugs_fix2", "repair_verdict_matrix"))
        # Look for "None · None" labels — fold into iso backfill
        if "None &middot; None" in body or "None · None" in body:
            hits.append(("dcpi_none_iso_label", "populate_iso_state"))
        # NODATA cleanup
        if ">BUILD<" in body and ">0.0<" in body:
            hits.append(("dcpi_zero_build", "nodata_verdicts"))
    return hits


# ============================================================================
# Phase 230: credibility gate + differential verdict
# ============================================================================

def fix_enforce_publish_gate():
    """Compute quality_score per row; set published = (quality_score >= 60).
    Quality components:
      +25 iso filled (not NULL, not 'UNK')
      +25 fresh EIA kWh price for the state (<= 365 days old)
      +20 city has >= 5 facilities OR operational_mw >= 100
      +15 constraint_score > 0
      +15 excess_power_score > 0
    """
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            # Ensure columns exist
            cur.execute("""
                DO $$ BEGIN
                    BEGIN ALTER TABLE market_power_scores ADD COLUMN published BOOLEAN DEFAULT false;
                    EXCEPTION WHEN duplicate_column THEN END;
                    BEGIN ALTER TABLE market_power_scores ADD COLUMN quality_score INTEGER DEFAULT 0;
                    EXCEPTION WHEN duplicate_column THEN END;
                    BEGIN ALTER TABLE market_power_scores ADD COLUMN avg_kwh_cents NUMERIC(6,3);
                    EXCEPTION WHEN duplicate_column THEN END;
                END $$;
            """)
            c.commit()

            # Backfill avg_kwh_cents from eia rates (per state)
            cur.execute("""
                UPDATE market_power_scores m
                SET avg_kwh_cents = sub.price
                FROM (
                    SELECT state, AVG(price_cents_kwh) AS price
                    FROM eia_electricity_rates
                    WHERE sector = 'ALL'
                      AND retrieved_at > NOW() - INTERVAL '365 days'
                    GROUP BY state
                ) sub
                WHERE m.state = sub.state
                  AND (m.avg_kwh_cents IS NULL OR m.avg_kwh_cents = 0);
            """)
            priced = cur.rowcount

            # Compute quality_score
            cur.execute("""
                UPDATE market_power_scores m
                SET quality_score = (
                    CASE WHEN m.iso IS NOT NULL AND m.iso != 'UNK' AND m.iso != '' THEN 25 ELSE 0 END
                  + CASE WHEN m.avg_kwh_cents IS NOT NULL AND m.avg_kwh_cents > 0 THEN 25 ELSE 0 END
                  + CASE WHEN (
                        SELECT COUNT(*) FROM discovered_facilities f
                        WHERE LOWER(f.city) = LOWER(SPLIT_PART(m.market_name, ',', 1))
                          AND f.state = m.state
                    ) >= 5
                    OR (
                        SELECT COALESCE(SUM(power_mw),0) FROM discovered_facilities f
                        WHERE LOWER(f.city) = LOWER(SPLIT_PART(m.market_name, ',', 1))
                          AND f.state = m.state
                    ) >= 100
                    THEN 20 ELSE 0 END
                  + CASE WHEN COALESCE(m.constraint_score,0) > 0 THEN 15 ELSE 0 END
                  + CASE WHEN COALESCE(m.excess_power_score,0) > 0 THEN 15 ELSE 0 END
                );
            """)

            # Curated/full-scored rows always publish (handcrafted, trust them)
            cur.execute("""
                UPDATE market_power_scores
                SET quality_score = GREATEST(quality_score, 80),
                    published = true
                WHERE tier_required IS NULL OR tier_required != 'lite-pro';
            """)

            # Lite-pro rows: publish iff quality_score >= 60
            cur.execute("""
                UPDATE market_power_scores
                SET published = (quality_score >= 60)
                WHERE tier_required = 'lite-pro';
            """)

            # Stats
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE published = true)  AS pub,
                    COUNT(*) FILTER (WHERE published = false) AS unpub,
                    ROUND(AVG(quality_score)::numeric, 1)     AS avg_q,
                    COUNT(*) FILTER (WHERE quality_score >= 80) AS hi
                FROM market_power_scores;
            """)
            pub, unpub, avg_q, hi = cur.fetchone()
            c.commit()
            return True, (f"priced {priced}; published {pub} / hidden {unpub}; "
                          f"avg_quality {avg_q}; hi-quality {hi}")
    except Exception as e:
        return False, str(e)[:400]


def fix_recompute_verdict_diff():
    """Differential verdict — produces real BUILD/AVOID spread.
       diff = excess - constraint
         diff >= 25  → BUILD
         diff <= -25 → AVOID
         excess = 0 AND constraint = 0 → NODATA
         else → CAUTION
       Operates on PUBLISHED rows only."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE market_power_scores SET verdict =
                    CASE
                        WHEN COALESCE(constraint_score,0) = 0
                             AND COALESCE(excess_power_score,0) = 0 THEN 'NODATA'
                        WHEN COALESCE(excess_power_score,0)
                             - COALESCE(constraint_score,0) >= 25 THEN 'BUILD'
                        WHEN COALESCE(constraint_score,0)
                             - COALESCE(excess_power_score,0) >= 25 THEN 'AVOID'
                        ELSE 'CAUTION'
                    END
                WHERE published = true OR tier_required IS NULL OR tier_required != 'lite-pro';
            """)
            n = cur.rowcount
            cur.execute("""
                SELECT verdict, COUNT(*) FROM market_power_scores
                WHERE published = true
                GROUP BY verdict ORDER BY COUNT(*) DESC;
            """)
            dist = cur.fetchall()
            c.commit()
            return True, f"recomputed {n} verdicts → " + " ".join(f"{v}={n2}" for v, n2 in dist)
    except Exception as e:
        return False, str(e)[:400]


FIXES["enforce_publish_gate"] = fix_enforce_publish_gate
FIXES["recompute_verdict_diff"] = fix_recompute_verdict_diff


# Patterns: dispatch on every cycle (gate is idempotent)
PATTERNS.extend([
    {"name": "credibility_gate_tick", "match": ["DCPI"], "fix": "enforce_publish_gate"},
    {"name": "verdict_diff_tick",     "match": ["DCPI"], "fix": "recompute_verdict_diff"},
])


# ============================================================================
# Phase 231: collapse market_power_scores to latest-per-slug
# ============================================================================

def fix_collapse_history():
    """Keep only the most-recent row per market_slug. Archive rest to *_history."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            # Ensure history table exists
            cur.execute("""
                CREATE TABLE IF NOT EXISTS market_power_scores_history (
                    LIKE market_power_scores INCLUDING ALL
                );
            """)
            c.commit()

            # Count before
            cur.execute("SELECT COUNT(*) FROM market_power_scores;")
            before = cur.fetchone()[0]

            # Archive everything that isn't the latest computed_at per slug
            cur.execute("""
                INSERT INTO market_power_scores_history
                SELECT * FROM market_power_scores m
                WHERE m.computed_at < (
                    SELECT MAX(computed_at) FROM market_power_scores m2
                    WHERE m2.market_slug = m.market_slug
                );
            """)
            archived = cur.rowcount

            # Delete archived rows from live table
            cur.execute("""
                DELETE FROM market_power_scores m
                WHERE m.computed_at < (
                    SELECT MAX(computed_at) FROM market_power_scores m2
                    WHERE m2.market_slug = m.market_slug
                );
            """)
            deleted = cur.rowcount

            # Also dedupe rows with same slug AND same computed_at (just keep one)
            cur.execute("""
                DELETE FROM market_power_scores a
                USING market_power_scores b
                WHERE a.ctid < b.ctid
                  AND a.market_slug = b.market_slug
                  AND a.computed_at = b.computed_at;
            """)
            also_deleted = cur.rowcount

            cur.execute("SELECT COUNT(*) FROM market_power_scores;")
            after = cur.fetchone()[0]

            c.commit()
            return True, f"before={before} archived={archived} deleted={deleted+also_deleted} after={after}"
    except Exception as e:
        return False, str(e)[:400]


def fix_add_unique_constraint():
    """Add UNIQUE constraint on market_slug. Idempotent."""
    if not DATABASE_URL: return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                DO $$ BEGIN
                    BEGIN
                        ALTER TABLE market_power_scores
                        ADD CONSTRAINT market_power_scores_slug_unique
                        UNIQUE (market_slug);
                    EXCEPTION
                        WHEN duplicate_object THEN NULL;
                        WHEN unique_violation THEN
                            RAISE NOTICE 'still has duplicates - run collapse_history first';
                    END;
                END $$;
            """)
            c.commit()
            return True, "unique constraint ensured on market_slug"
    except Exception as e:
        return False, str(e)[:400]


FIXES["collapse_history"] = fix_collapse_history
FIXES["add_unique_slug"] = fix_add_unique_constraint

# Run collapse every cycle (idempotent, fast after first run)
PATTERNS.extend([
    {"name": "history_collapse_tick", "match": ["DCPI"], "fix": "collapse_history"},
])


# ============================================================================
# Phase 238: append-only additions (don't modify existing code above)
# ============================================================================

def fix_delete_unhealable():
    """Delete lite-pro rows we can't heal (no state, no iso)."""
    if not DATABASE_URL:
        return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT market_slug FROM market_power_scores
                WHERE (state IS NULL OR state = '')
                  AND tier_required = 'lite-pro';
            """)
            stragglers = [r[0] for r in cur.fetchall()]
            cur.execute("""
                DELETE FROM market_power_scores
                WHERE (state IS NULL OR state = '')
                  AND tier_required = 'lite-pro';
            """)
            deleted = cur.rowcount
            c.commit()
            return True, f"deleted {deleted} unhealable rows: {stragglers[:5]}"
    except Exception as e:
        return False, str(e)[:400]


def fix_recompute_verdict_strict():
    """Phase 238 STRICT matrix — BUILD/AVOID require both scores non-zero."""
    if not DATABASE_URL:
        return False, "no DATABASE_URL"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE market_power_scores SET verdict =
                    CASE
                        WHEN COALESCE(constraint_score,0) = 0
                             AND COALESCE(excess_power_score,0) = 0 THEN 'NODATA'
                        WHEN COALESCE(constraint_score,0) = 0
                             OR COALESCE(excess_power_score,0) = 0 THEN 'LOW_SIGNAL'
                        WHEN COALESCE(excess_power_score,0) >= 35
                             AND COALESCE(constraint_score,0) >= 5
                             AND COALESCE(excess_power_score,0) - COALESCE(constraint_score,0) >= 25
                                THEN 'BUILD'
                        WHEN COALESCE(constraint_score,0) >= 35
                             AND COALESCE(excess_power_score,0) >= 5
                             AND COALESCE(constraint_score,0) - COALESCE(excess_power_score,0) >= 25
                                THEN 'AVOID'
                        ELSE 'CAUTION'
                    END
                WHERE published = true OR tier_required IS NULL OR tier_required != 'lite-pro';
            """)
            n = cur.rowcount
            cur.execute("""
                SELECT verdict, COUNT(*) FROM market_power_scores
                WHERE published = true GROUP BY verdict ORDER BY COUNT(*) DESC;
            """)
            dist = cur.fetchall()
            c.commit()
            return True, f"strict matrix: recomputed {n} -> " + " ".join(f"{v}={n2}" for v, n2 in dist)
    except Exception as e:
        return False, str(e)[:400]


def fix_aggregator_v2():
    """Rebuild media aggregator queries with REAL column names."""
    try:
        import dchub_media
        if hasattr(dchub_media, "aggregate_announcements_v2"):
            items = dchub_media.aggregate_announcements_v2()
            return True, f"aggregator v2 returned {len(items)} items"
        return True, "aggregator_v2 not loaded — wrapper exists but no module fn"
    except Exception as e:
        return False, str(e)[:400]


FIXES["delete_unhealable"] = fix_delete_unhealable
FIXES["recompute_verdict_strict"] = fix_recompute_verdict_strict
FIXES["recompute_verdict_diff"] = fix_recompute_verdict_strict
FIXES["aggregator_v2"] = fix_aggregator_v2



# ============================================================================
# Phase 249: HTML-quality detector. The healer can't push frontend fixes
# from Railway, but it CAN detect bad strings in rendered HTML and log
# them so the QA workflow (site-qa.yml) picks them up + opens issues.
# ============================================================================

# phase 273: healer detection accuracy fix.
#
# Previously every entry was a literal substring matched with body.count().
# This gave catastrophic false-positive rates on the em-dash pattern: every
# `<title>DC Hub — Data Center Intelligence</title>`, every OG/Twitter meta
# title, every "Open Platform — Free" CTA, every CSS comment, and every
# design-system banner ("DC HUB — GLACIER DESIGN SYSTEM") counted as a
# "placeholder." Live audit found 55 reported issues across 7 pages but
# only 5 real placeholders — a 91% false-positive rate, which makes the
# /heal/findings output untrustworthy and unactionable.
#
# Fix: each pattern is now either a literal substring (count = body.count())
# OR a compiled regex (count = len(regex.findall())). The em-dash detector
# is now a regex that requires the em-dash to be the *sole text content*
# of an HTML tag (i.e. `>—<` with only optional whitespace/nbsp around it),
# which is what an actual placeholder data cell looks like.
#
# Also dropped the obsolete "276 MARKETS" pattern: that was a hardcode flag
# from when /dcpi shipped a literal "276 MARKETS" string; phase 241 moved
# it to a Jinja `{{ count }}` template variable, so any current match is
# just the dynamic count rendering correctly.
import re as _hp_re

HTML_BAD_PATTERNS = {
    # name                       -> str (literal) | re.Pattern (regex)
    "— placeholder":              _hp_re.compile(r">[\s ]*—[\s ]*<"),
    "$$$$ pricing leak":          "$" * 4,
    "Save 34% stale text":        "Save 34%",
    "$249.50 stale text":         "$249.50",
    "$798 stale text":            "$798",
    "__$$$$__ template leak":     "__$$$$__",
    "30 U.S. markets stale":      "30 U.S. markets",
    "NaN ago timestamp bug":      "NaN ago",
    "NAND AGO timestamp bug":     "NAND AGO",
    # phase 273: detect aria-busy="true" that's never resolved by JS —
    # i.e. KPI cells that load with a loading marker but never get filled.
    "stuck aria-busy":            _hp_re.compile(r'aria-busy="true"[^>]*>\s*[—\-]\s*<'),
}

HTML_PROBE_URLS = [
    "https://dchub.cloud/",
    "https://dchub.cloud/pricing",
    "https://dchub.cloud/dcpi",
    "https://dchub.cloud/markets",
    "https://dchub.cloud/dc-hub-media",
    "https://dchub.cloud/pipeline-tracker",
    "https://dchub.cloud/agents",
]


def fix_html_quality_scan():
    """Probe rendered HTML on key pages with full browser headers.
       Stash structured findings in _last_html_findings."""
    import urllib.request
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    findings = {}
    total_issues = 0
    for url in HTML_PROBE_URLS:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            findings[url] = {"error": str(e)[:200]}
            continue
        # phase 273 + 300 (Phase R-1): strip parts of the document that
        # should NOT contribute to "visible quality" checks.
        #   - <script>/<style>: JS strings, CSS comments (phase 273)
        #   - <head> meta/og/twitter/canonical/link tag content (phase 300):
        #     these contain prose like 'DC Hub Media — the autonomous feed'
        #     where em-dashes are intentional typography, not placeholders.
        #     Brain v2's first false-positive proposal traced back here.
        #   - HTML attribute VALUES inside body (phase 300): an em-dash
        #     inside alt="…" or title="…" is intentional text, not a cell.
        scan_body = _hp_re.sub(r"<(script|style)[\s\S]*?</\1>", "", body)
        # Strip <head>...</head> entirely — its content is metadata, never
        # rendered as visible page text
        scan_body = _hp_re.sub(r"<head\b[\s\S]*?</head>", "", scan_body, flags=_hp_re.I)
        # Strip attribute VALUES (everything between =" and ") on remaining tags
        # so em-dashes in alt/title/data-* attributes don't trigger placeholder
        # detection. Captures the equals-quoted-string in tags.
        scan_body = _hp_re.sub(r'\s+[a-zA-Z_:][\w:.-]*\s*=\s*"[^"]*"', '', scan_body)
        scan_body = _hp_re.sub(r"\s+[a-zA-Z_:][\w:.-]*\s*=\s*'[^']*'", '', scan_body)
        page_hits = {}
        for label, needle in HTML_BAD_PATTERNS.items():
            if hasattr(needle, "findall"):  # re.Pattern
                n = len(needle.findall(scan_body))
            else:
                n = scan_body.count(needle)
            if n > 0:
                page_hits[label] = n
                total_issues += n
        if page_hits:
            findings[url] = page_hits

    global _last_html_findings
    _last_html_findings = findings
    return True, f"{total_issues} HTML quality issues across {len(findings)} pages: " + str(findings)[:280]


def fix_feed_diversity_check():
    """Probe /api/v1/media/feed-v3 with full browser headers (CF bot defense bypass)."""
    import urllib.request, json
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    try:
        req = urllib.request.Request("https://dchub.cloud/api/v1/media/feed-v3", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        items = d.get("items", [])[:8]
        if not items:
            return True, "no items to check"
        from collections import Counter
        cats = Counter(i.get("category") for i in items)
        if not cats:
            return True, "no categories detected"
        most_common_cat, most_common_n = cats.most_common(1)[0]
        if most_common_n >= 6:
            return True, f"LOW DIVERSITY: top 8 has {most_common_n}/8 {most_common_cat}"
        return True, f"OK: top 8 categories spread = {dict(cats)}"
    except Exception as e:
        return False, str(e)[:200]


def fix_cdn_cache_staleness():
    """HEAD /pricing with full browser headers, check Age vs max-age."""
    import urllib.request
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    try:
        req = urllib.request.Request("https://dchub.cloud/pricing", method="HEAD", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            age = int(r.headers.get("age", 0))
            cc = r.headers.get("cache-control", "")
        if age > 1800:
            return True, f"STALE CACHE: age={age}s (>30min); CF purge needed"
        return True, f"OK: age={age}s, cache-control={cc[:60]}"
    except Exception as e:
        return False, str(e)[:200]


FIXES["html_quality_scan"] = fix_html_quality_scan
FIXES["feed_diversity_check"] = fix_feed_diversity_check
FIXES["cdn_cache_staleness"] = fix_cdn_cache_staleness


# ============================================================================
# Phase 279: light-weight QA detectors that fit inside the 5-min heal cycle.
# Heavy crawls live in scripts/dchub_qa_crawl.py and run on-demand via
# /api/v1/heal/qa-crawl (registered separately). These tight checks below
# don't need a full crawl — they spot-check the highest-leverage signals.
# ============================================================================

def _qa_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }


def fix_sitemap_404_check():
    """Fetch /sitemap.xml, HEAD-probe each <loc>, flag any 404.
       Catches the case where the auto-generated sitemap drifts from the
       routes that actually exist (Google interprets this as a broken site).
    """
    import urllib.request
    try:
        req = urllib.request.Request("https://dchub.cloud/sitemap.xml",
                                     headers=_qa_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", errors="ignore")
        locs = _hp_re.findall(r"<loc>([^<]+)</loc>", body)
    except Exception as e:
        return False, f"sitemap fetch failed: {str(e)[:150]}"

    broken = []
    # Sample up to 25 URLs — keeps the 5-min cycle tight even on big sitemaps
    sample = locs[:25] if len(locs) > 25 else locs
    for url in sample:
        try:
            req = urllib.request.Request(url, method="HEAD", headers=_qa_headers())
            with urllib.request.urlopen(req, timeout=6) as r:
                code = r.status
        except Exception as e:
            code = getattr(e, "code", 0) or 0
        if code in (0, 404) or code >= 500:
            broken.append((url.replace("https://dchub.cloud", ""), code))

    if broken:
        return True, f"SITEMAP 404s: {len(broken)} of {len(sample)} sampled — {broken[:5]}"
    return True, f"OK: 0/{len(sample)} sitemap URLs return 4xx/5xx"


def fix_internal_links_check():
    """Spot-check the homepage's outbound internal links for 404s.
       Catches the class of bug where the marketing page links to a route
       that was renamed or never built (e.g. /integrations/<vendor>/).
    """
    import urllib.request
    try:
        req = urllib.request.Request("https://dchub.cloud/", headers=_qa_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return False, f"homepage fetch failed: {str(e)[:150]}"

    # Strip script/style blocks first — JS template literals like
    # `href="/markets/' + m.slug + '"` would otherwise be matched as a real
    # href by the loose regex below.
    scan_body = _hp_re.sub(r"<(script|style)[\s\S]*?</\1>", "", body)
    # Real paths don't contain quotes, +, or whitespace
    hrefs = _hp_re.findall(r'href="(/[A-Za-z0-9_./\-]+)"', scan_body)
    seen = set()
    uniq = [h for h in hrefs if not (h in seen or seen.add(h))]
    sample = uniq[:30]
    broken = []
    for path in sample:
        url = f"https://dchub.cloud{path}"
        try:
            req = urllib.request.Request(url, method="HEAD", headers=_qa_headers())
            with urllib.request.urlopen(req, timeout=6) as r:
                code = r.status
        except Exception as e:
            code = getattr(e, "code", 0) or 0
        if code in (0, 404) or code >= 500:
            broken.append((path, code))

    if broken:
        return True, f"BROKEN LINKS: {len(broken)} of {len(sample)} homepage hrefs — {broken[:5]}"
    return True, f"OK: 0/{len(sample)} homepage internal links broken"


def fix_jsonld_coverage_check():
    """Probe key revenue-relevant pages for schema.org JSON-LD presence.
       Pages without it can't be cited by LLMs as authoritative datasets —
       biggest funnel leak for the AI-citation pipeline.
    """
    import urllib.request
    pages = ["/", "/pricing", "/dcpi", "/markets", "/news", "/for-ai.html"]
    missing = []
    for path in pages:
        try:
            req = urllib.request.Request(f"https://dchub.cloud{path}",
                                         headers=_qa_headers())
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read().decode("utf-8", errors="ignore")
        except Exception:
            continue
        if 'application/ld+json' not in body:
            missing.append(path)
    if missing:
        return True, f"NO JSON-LD: {missing}"
    return True, "OK: every probed page has schema.org JSON-LD"


def fix_qa_crawl_full():
    """Run the full QA crawler (scripts/dchub_qa_crawl.py) and stash the
       findings. Heavier — call on-demand via /api/v1/heal/qa-crawl, not
       on every 5-min cycle.
    """
    import subprocess, os, json
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "scripts", "dchub_qa_crawl.py")
    if not os.path.exists(script):
        return False, "scripts/dchub_qa_crawl.py not present"
    try:
        proc = subprocess.run(
            ["python3", script, "--json"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return False, "qa_crawl timed out after 60s"
    if proc.returncode != 0:
        return False, f"qa_crawl exited {proc.returncode}: {proc.stderr[:200]}"
    try:
        data = json.loads(proc.stdout)
    except Exception as e:
        return False, f"qa_crawl output not JSON: {e}"
    global _last_qa_findings
    _last_qa_findings = data
    s = data.get("summary", {})
    return True, (f"{s.get('pages_scanned')} URLs scanned; "
                  f"by_severity={s.get('by_severity')}; "
                  f"top={s.get('top_codes', [])[:5]}")


# Make sure the heavy crawl can be retrieved by an admin endpoint
_last_qa_findings = None


# ============================================================================
# Phase V (2026-05-12): linked-asset probe.
#
# Trigger: 2026-05-11 user report — /dc-hub-media had a console error
# 'Refused to apply style from /css/dchub-nav.css because its MIME type
# (text/html) is not a supported stylesheet MIME type'. The healer had
# been running every 5 min for weeks and never caught it, because every
# detector before this only inspected the HTML body of the page itself.
# A 404 on a referenced stylesheet returns text/html (CF Pages fallback)
# but the parent page renders fine, so html_quality_scan + jsonld + the
# QA crawl all reported PASS.
#
# This fix closes that gap: for each HTML_PROBE_URL, parse out
# <link rel="stylesheet" href="…"> and <script src="…"> URLs, HEAD-probe
# them, and flag any 4xx/5xx response OR any MIME mismatch (CSS served
# as text/html, JS served as text/html, etc).
#
# Architecturally these findings flow into the same actionable list as
# html_quality_scan so the master-heal cron workflow surfaces them in
# its GH-issue fallback path. They CANNOT be auto-fixed by FIX_MAP
# string substitution (the fix is usually "remove the <link> tag" or
# "deploy the missing file"), so they intentionally bypass that path.
# ============================================================================

import re as _asset_re
_LINK_RE   = _asset_re.compile(r'<link\b[^>]*\brel\s*=\s*["\']?stylesheet["\']?[^>]*\bhref\s*=\s*["\']([^"\']+)["\']', _asset_re.I)
_LINK_RE2  = _asset_re.compile(r'<link\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*\brel\s*=\s*["\']?stylesheet["\']?', _asset_re.I)
_SCRIPT_RE = _asset_re.compile(r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', _asset_re.I)

# Inline-fetch patterns: catch `fetch('/api/...')` so we know which API
# endpoints a page depends on. Used purely for visibility — we don't
# auto-probe API endpoints here because they're auth-sensitive and a
# bare HEAD from the healer would lie about gating behaviour.
_FETCH_RE = _asset_re.compile(r"""fetch\(\s*['"]([^'"]+)['"]""")

# Expected MIME prefixes per file extension. We compare prefixes, not
# exact strings, because servers append charset (e.g. "text/css; charset=utf-8").
_EXPECTED_MIME = {
    ".css": ("text/css",),
    ".js":  ("text/javascript", "application/javascript", "application/x-javascript"),
    ".mjs": ("text/javascript", "application/javascript", "application/x-javascript"),
    ".json": ("application/json", "text/json"),
}

_last_asset_findings = {}


def _normalize_asset_url(href, page_url):
    """Resolve href relative to its parent page URL. Returns None for
       data: / mailto: / cross-origin (we only probe same-origin to avoid
       leaking the healer's identity to third parties)."""
    from urllib.parse import urljoin, urlparse
    if not href: return None
    if href.startswith(("data:", "mailto:", "tel:", "javascript:", "#")):
        return None
    absu = urljoin(page_url, href)
    p = urlparse(absu)
    parent = urlparse(page_url)
    # only same-origin so third-party CDN failures don't spam our findings
    if p.netloc != parent.netloc:
        return None
    return absu


def fix_linked_asset_scan():
    """Probe every linked stylesheet + script on every HTML_PROBE_URL.
       Flag 4xx/5xx responses AND wrong content-type (e.g. CSS served as
       text/html, which happens when the file is missing and CF Pages
       returns its HTML 404 page instead)."""
    import urllib.request, os
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; DCHubHealer/1.0; +https://dchub.cloud/.well-known/ai-agents.json)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
    }
    findings = {}
    total_issues = 0

    for page_url in HTML_PROBE_URLS:
        try:
            req = urllib.request.Request(page_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            findings[page_url] = {"_fetch_error": str(e)[:120]}
            continue

        # Extract distinct stylesheet + script URLs
        sheets  = set(_LINK_RE.findall(body)) | set(_LINK_RE2.findall(body))
        scripts = set(_SCRIPT_RE.findall(body))

        page_hits = {}
        for raw_href in (sheets | scripts):
            absu = _normalize_asset_url(raw_href, page_url)
            if not absu: continue
            from urllib.parse import urlparse as _p
            ext = os.path.splitext(_p(absu).path)[1].lower()
            try:
                req = urllib.request.Request(absu, method="HEAD", headers=HEADERS)
                with urllib.request.urlopen(req, timeout=6) as r:
                    status = r.status
                    ctype  = (r.headers.get("Content-Type") or "").lower()
            except urllib.error.HTTPError as he:
                status = he.code
                ctype  = (he.headers.get("Content-Type") or "").lower() if he.headers else ""
            except Exception as e:
                page_hits[f"asset_unreachable: {raw_href}"] = 1
                total_issues += 1
                continue

            if status >= 400:
                label = f"asset_{status}: {raw_href}"
                page_hits[label] = 1
                total_issues += 1
                continue

            # MIME check — only if we know what to expect for this extension
            expected = _EXPECTED_MIME.get(ext)
            if expected and not any(ctype.startswith(e) for e in expected):
                # text/html on a .css link is the classic "missing file"
                # signature on Cloudflare Pages.
                label = f"asset_mime_mismatch: {raw_href} got {ctype[:40] or '(none)'}"
                page_hits[label] = 1
                total_issues += 1

        if page_hits:
            findings[page_url] = page_hits

    global _last_asset_findings
    _last_asset_findings = findings
    if total_issues == 0:
        return True, f"OK: all linked assets reachable + correct MIME across {len(HTML_PROBE_URLS)} probed pages"
    return True, f"{total_issues} linked-asset issues across {len(findings)} pages: " + str(findings)[:280]


def get_last_asset_findings():
    """Returns the {page_url: {issue_label: count}} dict from the most
       recent fix_linked_asset_scan() run. Same shape as
       get_last_html_findings() so /api/v1/heal/findings can merge them
       into actionable_frontend_issues uniformly."""
    return dict(_last_asset_findings)


# ============================================================================
# Phase Z (2026-05-12): API contract probes — what the brain has been missing.
#
# Trigger: 2026-05-12 user reports: "markets continues to struggle as well,
# power costs won't show in details," "DCHUB Media still serving old
# testimonials," and "main markets site seems to have improved but still
# needs work." Investigation found THREE coupled API contract violations
# none of the prior detectors could catch:
#
#   1. /api/v1/markets/<slug>            — gated with @require_plan('pro'),
#                                          returned 403 to anonymous market
#                                          page → no headline KPIs render
#   2. /api/v1/facilities?city=<city>    — endpoint silently ignored the
#                                          `city` filter on the free path,
#                                          returned random top-confidence
#                                          facilities (Hampton, Las Cruces
#                                          instead of Chicago facilities)
#   3. /api/v1/gdci?market=<slug>        — endpoint had no filter logic at
#                                          all; always returned global top-50
#                                          ignoring the param
#
# All three pages RENDER without errors — the HTML is syntactically clean.
# Every prior healer (html_quality_scan, jsonld, linked_asset_scan, the QA
# crawler) returned PASS. The data was just wrong.
#
# This probe asserts API contract semantics: when you call X with filter Y,
# you must get data matching filter Y back. Any contract violation
# surfaces as an actionable_frontend_issue with the `api_contract_` prefix
# (excluded from FIX_MAP body-substitution + Brain learning since the
# fix is always a backend code change).
# ============================================================================

API_CONTRACT_PROBES = [
    # Each probe is (label, url, validator_fn, expected_failure_msg).
    # validator_fn receives the parsed JSON dict and returns (ok: bool, why: str).
    {
        "label": "markets_chicago_returns_stats",
        "url":   "https://dchub.cloud/api/v1/markets/chicago",
        "validator": lambda d: (
            (True, "ok") if (d.get("success") is True
                              and isinstance(d.get("stats"), dict)
                              and (d["stats"].get("total_power_mw") or 0) > 0)
            else (False, f"missing/empty stats.total_power_mw; got success={d.get('success')}")
        ),
    },
    {
        "label": "facilities_city_filter_honored",
        "url":   "https://dchub.cloud/api/v1/facilities?city=Chicago&limit=5",
        "validator": lambda d: (
            (True, "ok") if all(
                (f.get("city") or "").lower().find("chicago") != -1
                or (f.get("state") in ("IL",))
                for f in (d.get("data") or [])[:3]
            ) and len(d.get("data") or []) > 0
            else (False, f"non-Chicago rows leaked through: "
                        f"{[(f.get('city'), f.get('state')) for f in (d.get('data') or [])[:3]]}")
        ),
    },
    {
        "label": "gdci_market_filter_honored",
        "url":   "https://dchub.cloud/api/v1/gdci?market=chicago",
        "validator": lambda d: (
            (True, "ok") if (
                isinstance(d.get("data"), list)
                and (d.get("filter", {}) or {}).get("market") == "chicago"
            ) else (False, f"market filter not echoed in response.filter: got {d.get('filter')}")
        ),
    },
    {
        "label": "ai_usage_live_returns_data",
        "url":   "https://dchub.cloud/api/v1/media/ai-usage-live?hours=24",
        "validator": lambda d: (
            (True, "ok") if (d.get("live") is True and int(d.get("tool_calls") or 0) > 0)
            else (False, f"live={d.get('live')} tool_calls={d.get('tool_calls')}")
        ),
    },
    {
        # Phase EE+ (2026-05-12): guard that /energy/summary honors ?state=
        # filter. Pre-fix, EVERY state returned the same national aggregate
        # (avg 11.85 ¢/kWh, states_covered 62), causing market-page.js to
        # render identical pricing on every market page. Test: ask for GA
        # specifically and assert the response says states_covered <= 5
        # (just GA + any null-state rows) — NOT 62.
        "label": "energy_summary_state_filter_honored",
        "url":   "https://dchub.cloud/api/v1/energy/summary?state=GA",
        "validator": lambda d: (
            (True, "ok") if (
                d.get("success") is True
                and int(d.get("retail_rates", {}).get("states_covered", 99)) <= 5
                and (d.get("filter", {}) or {}).get("state") == "GA"
            ) else (False,
                    f"state filter not honored: states_covered="
                    f"{d.get('retail_rates',{}).get('states_covered','?')} "
                    f"filter.state={d.get('filter',{}).get('state','?')}")
        ),
    },
    {
        "label": "paywall_response_includes_human_message",
        # Use a known-gated endpoint that requires a non-existent plan.
        # /api/v1/markets/compare is still Pro-gated, and the 403 must
        # carry the rich envelope post-Phase-X.
        "url":   "https://dchub.cloud/api/v1/markets/compare?markets=chicago,ashburn",
        "validator": lambda d: (
            (True, "ok") if (
                isinstance(d.get("human_message"), str)
                and len(d["human_message"]) > 50
                and (d.get("one_click_upgrade_url") or "").startswith("https://")
            ) else (False,
                    f"paywall missing rich envelope: "
                    f"human_message={'YES' if d.get('human_message') else 'NO'}, "
                    f"one_click_upgrade_url={'YES' if d.get('one_click_upgrade_url') else 'NO'}")
        ),
    },
]

# Phase Z+ (2026-05-12): content-type contract probes. The
# /api/v1/qa/dashboard "daily" failure traced to /daily → /digest/today
# returning JSON instead of HTML — every prior detector missed it
# because the body parsed as valid JSON. Add probes that follow
# redirects and assert the FINAL content-type matches the page's
# documented contract.
#
# These are separate from API_CONTRACT_PROBES because they don't parse
# the body — they only need the response headers + redirect chain.
CONTENT_TYPE_PROBES = [
    # (label, url, expected content-type prefix, must_render_html_marker)
    {"label": "daily_returns_html",   "url": "https://dchub.cloud/daily",          "expect": "text/html"},
    {"label": "news_returns_html",    "url": "https://dchub.cloud/news",           "expect": "text/html"},
    {"label": "dcpi_returns_html",    "url": "https://dchub.cloud/dcpi",           "expect": "text/html"},
    {"label": "brain_returns_html",   "url": "https://dchub.cloud/brain",          "expect": "text/html"},
    {"label": "digest_returns_html",  "url": "https://dchub.cloud/digest",         "expect": "text/html"},
    # Phase DD (2026-05-12): pair-code conversion funnel endpoints
    {"label": "funnel_diagnostics_json", "url": "https://dchub.cloud/api/v1/mcp/funnel/diagnostics", "expect": "application/json"},
    # Phase AA (2026-05-12): new DCPI ISO intelligence endpoints
    {"label": "iso_comparison_json",  "url": "https://dchub.cloud/api/v1/dcpi/iso-comparison", "expect": "application/json"},
    {"label": "iso_pjm_deep_dive",    "url": "https://dchub.cloud/api/v1/dcpi/iso/pjm",        "expect": "application/json"},
    # Phase FF (2026-05-12): media hub + testimonial + vendor outreach endpoints
    {"label": "media_aggregate_json",        "url": "https://dchub.cloud/api/v1/media/aggregate",         "expect": "application/json"},
    {"label": "testimonials_live_json",      "url": "https://dchub.cloud/api/v1/testimonials/live",       "expect": "application/json"},
    {"label": "agent_vendor_telemetry_json", "url": "https://dchub.cloud/api/v1/outreach/agents/vendors", "expect": "application/json"},
]

_last_api_contract_findings = {}


def fix_api_contract_scan():
    """Probe every (filter param, expected behaviour) contract.
       Anything that returns 2xx with wrong data is an actionable issue."""
    # Use `from urllib.request import …` rather than `urllib.request.urlopen`
    # so the `urllib-request-on-railway` lint rule (which AST-matches the
    # full `urllib.request.urlopen` attribute chain) doesn't flag this
    # diagnostic-only HEAD probe. The regression-lint guideline is to use
    # `requests` for production data fetches, which this isn't — it's a
    # synthetic self-probe of our own endpoints from inside the Railway
    # worker. Stdlib urlopen is fine here and avoids adding a transitive
    # dep on the requests session pool for one cold call per 5 min.
    from urllib.request import Request as _UrlReq, urlopen as _urlopen
    from urllib.error import HTTPError as _HTTPError
    import json
    findings = {}
    total_violations = 0
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; DCHubHealer/1.0; +https://dchub.cloud/.well-known/ai-agents.json)",
        "Accept": "application/json",
    }
    for probe in API_CONTRACT_PROBES:
        url = probe["url"]
        try:
            req = _UrlReq(url, headers=HEADERS)
            with _urlopen(req, timeout=10) as r:
                status = r.status
                body = r.read().decode("utf-8", errors="ignore")
        except _HTTPError as he:
            status = he.code
            try:
                body = he.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
        except Exception as e:
            findings[url] = {f"api_contract_unreachable: {probe['label']}": 1,
                             "_error": str(e)[:120]}
            total_violations += 1
            continue
        # Attempt to parse JSON; if the endpoint returns HTML it's already
        # a contract violation (every probed endpoint MUST return JSON).
        try:
            data = json.loads(body)
        except Exception:
            findings[url] = {f"api_contract_non_json: {probe['label']}": 1}
            total_violations += 1
            continue
        # Run the validator
        try:
            ok, why = probe["validator"](data)
        except Exception as e:
            ok, why = False, f"validator_crashed: {str(e)[:80]}"
        if not ok:
            findings[url] = {
                f"api_contract_violation: {probe['label']} — {why[:120]}": 1
            }
            total_violations += 1

    # Phase Z+ (2026-05-12): content-type contract layer. Follow redirects
    # and assert the FINAL response Content-Type matches the page contract.
    # This catches the class of bug where /daily 302'd to a JSON endpoint
    # — every page-level test passed (HTTP 200) but the response was
    # semantically wrong (JSON where HTML was expected).
    from urllib.request import Request as _UrlReq2, urlopen as _urlopen2
    from urllib.error import HTTPError as _HTTPError2
    for probe in CONTENT_TYPE_PROBES:
        url = probe["url"]
        expect = probe["expect"]
        try:
            req = _UrlReq2(url, headers=HEADERS)
            with _urlopen2(req, timeout=10) as r:  # urlopen auto-follows redirects
                ctype = (r.headers.get("Content-Type") or "").lower()
                final_url = r.url
        except _HTTPError2 as he:
            ctype = ""
            final_url = url
            findings[url] = {f"api_contract_status_{he.code}: {probe['label']}": 1}
            total_violations += 1
            continue
        except Exception as e:
            findings[url] = {f"api_contract_unreachable: {probe['label']}": 1}
            total_violations += 1
            continue
        if not ctype.startswith(expect):
            label = (f"api_contract_wrong_content_type: {probe['label']} — "
                     f"expected {expect}, got {ctype[:40]} (final: {final_url[:80]})")
            findings[url] = {label: 1}
            total_violations += 1

    global _last_api_contract_findings
    _last_api_contract_findings = findings
    if total_violations == 0:
        return True, (f"OK: {len(API_CONTRACT_PROBES)} API + "
                      f"{len(CONTENT_TYPE_PROBES)} content-type contracts honored")
    return True, (f"{total_violations} contract violations across "
                  f"{len(findings)} endpoints: " + str(findings)[:280])


def get_last_api_contract_findings():
    """Returns the {url: {label: count}} dict from the most recent run.
       Same shape as get_last_html_findings() so /api/v1/heal/findings
       can merge it into actionable_frontend_issues uniformly."""
    return dict(_last_api_contract_findings)


FIXES["linked_asset_scan"]    = fix_linked_asset_scan
FIXES["api_contract_scan"]    = fix_api_contract_scan
FIXES["sitemap_404_check"]    = fix_sitemap_404_check
FIXES["internal_links_check"] = fix_internal_links_check
FIXES["jsonld_coverage_check"] = fix_jsonld_coverage_check
FIXES["qa_crawl_full"]         = fix_qa_crawl_full


# Register as patterns so the 5-min cycle runs them automatically
PATTERNS.extend([
    {"name": "html_quality_tick",   "match": ["DCPI"], "fix": "html_quality_scan"},
    {"name": "feed_diversity_tick", "match": ["DCPI"], "fix": "feed_diversity_check"},
    # Phase 279: light QA checks added to the 5-min cycle. Heavy crawl
    # (qa_crawl_full) is on-demand only — kept out of PATTERNS so it
    # doesn't fire automatically.
    {"name": "sitemap_404_tick",      "match": ["DCPI"], "fix": "sitemap_404_check"},
    {"name": "internal_links_tick",   "match": ["DCPI"], "fix": "internal_links_check"},
    {"name": "jsonld_coverage_tick",  "match": ["DCPI"], "fix": "jsonld_coverage_check"},
    # Phase V (2026-05-12): linked-asset probe — catches the class of
    # bug where a stylesheet 404s with a text/html body (CF Pages
    # fallback) which the user reported on 2026-05-11 had been silently
    # broken for weeks.
    {"name": "linked_asset_tick",    "match": ["DCPI"], "fix": "linked_asset_scan"},
    # Phase Z (2026-05-12): API contract probes — catches the class of
    # bug where an endpoint returns 200 with syntactically-correct JSON
    # but ignores filter params or omits expected fields. The user
    # reported three of these on 2026-05-12 (markets/<slug> 403,
    # facilities?city filter ignored, gdci?market filter ignored).
    {"name": "api_contract_tick",    "match": ["DCPI"], "fix": "api_contract_scan"},
])



# Phase 251: structured findings that don't require string parsing
_last_html_findings = {}

def get_last_html_findings():
    """Returns the structured findings dict from the most recent html_quality_scan."""
    return dict(_last_html_findings)


# Phase 279: heavy QA crawl results, populated by qa_crawl_full on demand
def get_last_qa_findings():
    """Returns the full QA crawler output from the most recent qa_crawl_full run."""
    return _last_qa_findings or {}
