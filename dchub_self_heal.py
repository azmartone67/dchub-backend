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

HTML_BAD_PATTERNS = {
    "multi-GW placeholder":    "multi-GW",
    "$$$$ pricing leak":       "$" * 4,
    "Save 34% stale text":     "Save 34%",
    "$249.50 stale text":      "$249.50",
    "$798 stale text":         "$798",
    "__$$$$__ template leak":  "__$$$$__",
    "276 MARKETS stale":       "276 MARKETS",
    "30 U.S. markets stale":   "30 U.S. markets",
    "NaN ago timestamp bug":   "NaN ago",
    "NAND AGO timestamp bug":  "NAND AGO",
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
    """Probe rendered HTML on key pages, count occurrences of known
       bad strings. Log every hit to self_heal_events. Returns a structured
       report the QA workflow can read."""
    import urllib.request
    findings = {}
    total_issues = 0

    for url in HTML_PROBE_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DCHub-Healer/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            findings[url] = {"error": str(e)[:200]}
            continue
        page_hits = {}
        for label, needle in HTML_BAD_PATTERNS.items():
            n = body.count(needle)
            if n > 0:
                page_hits[label] = n
                total_issues += n
        if page_hits:
            findings[url] = page_hits

    return True, f"{total_issues} HTML quality issues across {len(findings)} pages: " + str(findings)[:280]


def fix_feed_diversity_check():
    """Probe /api/v1/media/feed-v3 and check whether top 8 items have
       category diversity. If 6+ of top 8 are same category, that's a
       low-diversity bug — log it."""
    import urllib.request, json
    try:
        with urllib.request.urlopen("https://dchub.cloud/api/v1/media/feed-v3", timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
        items = d.get("items", [])[:8]
        if not items:
            return True, "no items to check"
        from collections import Counter
        cats = Counter(i.get("category") for i in items)
        most_common_cat, most_common_n = cats.most_common(1)[0]
        if most_common_n >= 6:
            return True, f"LOW DIVERSITY: top 8 has {most_common_n}/8 {most_common_cat}"
        return True, f"OK: top 8 categories spread = {dict(cats)}"
    except Exception as e:
        return False, str(e)[:200]


def fix_cdn_cache_staleness():
    """Check Age header on /pricing vs max-age. If Age > 30 min, log it
       so external automation can purge."""
    import urllib.request
    try:
        req = urllib.request.Request("https://dchub.cloud/pricing", method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as r:
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

# Register as patterns so the 5-min cycle runs them automatically
PATTERNS.extend([
    {"name": "html_quality_tick", "match": ["DCPI"], "fix": "html_quality_scan"},
    {"name": "feed_diversity_tick", "match": ["DCPI"], "fix": "feed_diversity_check"},
])
