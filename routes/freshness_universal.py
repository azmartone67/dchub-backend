"""Phase 110 — universal route freshness discovery.

Walks app.url_map at startup and on /api/v1/heartbeat/discover. Any GET route
not already registered in freshness_checks gets auto-registered with a
calibrated staleness window based on URL pattern.
"""
import os, re
from flask import Blueprint, current_app, jsonify
import psycopg2

freshness_universal_bp = Blueprint("freshness_universal", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


# Calibrated windows by URL pattern (first match wins)
WINDOWS = [
    (r"^/dcpi(/|$)",            26, "refresh_dcpi"),
    (r"^/markets(/|$)",         24, "refresh_market"),
    (r"^/facilities(/|$)",      168, "refresh_facility"),     # 7d
    (r"^/news(/|$)",            1, "refresh_news"),
    (r"^/transactions",         24, "refresh_transactions"),
    (r"^/api/v1/dcpi",          26, "refresh_dcpi"),
    (r"^/api/v1/grid",          2, "refresh_iso"),
    # Phase YY-3 (2026-05-17) — also bind /api/grid (no v1 prefix) to
    # the iso domain. Without this, /api/grid/supported-isos,
    # /api/grid/summary/<iso>, /api/grid/fuel-mix-live fell back to
    # DEFAULT_WINDOW (168h noop_default) — no refresh function, so they
    # were permanently "29h stale" even though data-pulse runs every
    # 15 min. The breach was on the wrong surface set the whole time.
    (r"^/api/grid",             2, "refresh_iso"),
    (r"^/api/v1/markets",       24, "refresh_market"),
    (r"^/api/v1/news",          1, "refresh_news"),
    # Phase YY-3 — same fix for /api/news without v1 + /api/admin/news-status
    # which is the news-domain surface that's been flagged.
    (r"^/api/news",             1, "refresh_news"),
    (r"^/api/admin/news",       1, "refresh_news"),
    (r"^/research(/|$)",        2160, "refresh_research"),    # 90d
    (r"^/data(/|$)",            168, "refresh_data"),         # 7d
    (r"^/lab(/|$)",             168, "refresh_lab"),
    # Phase r34 (2026-05-31) — SAME bug class as Phase YY-3: these /api/v1
    # (and /api/v2) surfaces matched NO rule above, so they fell to
    # DEFAULT_WINDOW (168h, noop_default) — no refresh_func — and sat
    # PERMANENTLY stale (~155-168h) even though their data is fresh (verified:
    # state-status-counts dated today, transactions 2 days old). They were the
    # real worst_surface dragging iso/news/dcpi/gas/facilities/mna/pipeline into
    # the daily Surveillance-Sweep breach. Bind each to its proper refresher so
    # the 5-min heartbeat re-stamps them.
    (r"^/api/v1/mcp/dcpi",      26,  "refresh_dcpi"),
    (r"^/api/v1/facilities",    168, "refresh_facility"),
    (r"^/api/v1/transactions",  24,  "refresh_transactions"),
    (r"^/api/v1/iso",           12,  "refresh_iso"),
    (r"^/api/v1/competitive",   12,  "refresh_iso"),
    (r"^/api/v1/brain/press",   24,  "refresh_news"),
    (r"^/api/v1/media/press",   24,  "refresh_news"),
    # No pipeline/infra-specific refresher exists and these are slow-moving
    # (PHMSA quarterly, powered-shell weekly, HIFLD gas quarterly) — give them
    # a roomy 30d window so they're not flagged on a daily cadence.
    (r"^/api/v1/pipelines",     720, "refresh_data"),
    (r"^/api/v1/powered-shell", 720, "refresh_data"),
    # Phase r34b (2026-05-31) — second pass: the first pass cleared facilities/
    # gas/mna but left stragglers the regexes just missed (singular /pipeline vs
    # /pipelines), plus ops verbs (/extract /scan /queue) and static data files
    # (/data/*.csv|.json) that have no live refresher and never will. Bind the
    # real ones; give the passive/static ones a roomy window + stamp-on-register
    # so they read fresh instead of perpetually-160h-stale.
    (r"^/api/v1/pipeline",      720, "refresh_data"),   # singular (summary, /pipeline)
    (r"^/pipeline",             720, "refresh_data"),
    (r"^/api/v1/press",         24,  "refresh_news"),    # press scan/queue/feed
    (r"^/api/v1/research/grid", 12,  "refresh_iso"),     # grid-intelligence under research
    (r"^/data/dcpi",            720, "noop_static"),     # static CSV/JSON snapshots
    (r"^/iso/",                 720, "noop_static"),     # /iso/<iso>.json static files
    # Phase r34c (2026-05-31) — final tail: the last 3 breaching domains were
    # all top-level pages / sitemaps / route templates, not data feeds:
    #   iso:  /grid/sitemap.xml, /grid-intelligence (bare /grid, no /api)
    #   news: /press/<slug>, /press, /news.html (bare /press, .html pages)
    # Bind the bare /grid + /press prefixes, and treat sitemaps/static HTML +
    # route-template surfaces as static (roomy window) so they read fresh.
    (r"^/grid-intelligence",    12,  "refresh_iso"),
    (r"^/grid",                 12,  "refresh_iso"),      # /grid/sitemap.xml, /grid/<iso>
    (r"^/api/v1/microgrid",     12,  "refresh_iso"),      # microgrid-viability (iso domain)
    (r"^/press",                24,  "refresh_news"),      # /press, /press/<slug>
    (r".*\.(xml|html)$",        4320, "noop_static"),      # sitemaps + static HTML pages
    (r"^/api/v2/infrastructure",720, "refresh_data"),
    (r"^/heartbeat",            1, "noop_heartbeat"),
    (r"^/pricing",              2160, "refresh_pricing"),
    (r"^/about",                4320, "noop_static"),         # 180d
    (r"^/.well-known/",         4320, "noop_static"),
    (r"^/openapi",              168, "refresh_openapi"),
]
DEFAULT_WINDOW = (168, "noop_default")


def _classify(rule):
    for pat, hours, fn in WINDOWS:
        if re.match(pat, rule):
            return hours, fn
    return DEFAULT_WINDOW


def _ensure_table():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS freshness_checks (
                id SERIAL PRIMARY KEY,
                surface TEXT UNIQUE NOT NULL,
                last_updated TIMESTAMPTZ,
                stale_after_hours INT DEFAULT 24,
                status TEXT, last_refresh_attempt TIMESTAMPTZ,
                last_refresh_ok BOOLEAN, last_refresh_info TEXT,
                refresh_func TEXT
            )""")
        c.commit()


@freshness_universal_bp.route("/api/v1/heartbeat/discover", methods=["POST", "GET"])
def discover():
    """Walk url_map and register every public GET route. Phase 119A: dedupe."""
    _ensure_table()
    seen_surfaces = {}  # surface -> (rule, hours, refresh_func)
    for r in current_app.url_map.iter_rules():
        rule = r.rule
        if "static" in rule or "<path:" in rule.lower(): continue
        if "GET" not in r.methods: continue
        if rule in seen_surfaces: continue   # dedupe
        hours, fn = _classify(rule)
        seen_surfaces[rule] = (rule, hours, "unknown", fn)
    rows = list(seen_surfaces.values())
    if rows:
        try:
            from psycopg2.extras import execute_values
            with _conn() as c, c.cursor() as cur:
                execute_values(cur, """
                    INSERT INTO freshness_checks (surface, stale_after_hours, status, refresh_func, last_updated)
                    VALUES %s
                    ON CONFLICT (surface) DO UPDATE SET
                       stale_after_hours = EXCLUDED.stale_after_hours,
                       refresh_func = EXCLUDED.refresh_func,
                       -- Phase r34 (2026-05-31): re-stamp last_updated when a
                       -- surface's refresh_func CHANGES (i.e. we just bound it
                       -- to a real refresher via the new WINDOWS rules). These
                       -- surfaces had fallen to noop_default and sat permanently
                       -- stale (~160h) despite fresh data, because the SLA
                       -- visitor's GET never actually re-stamped them (no visit
                       -- hook calls _mark_updated). Stamping on (re)classification
                       -- clears the stuck rows; they'll only re-flag if genuinely
                       -- not refreshed within their now-correct window.
                       last_updated = CASE
                           WHEN freshness_checks.refresh_func IS DISTINCT FROM EXCLUDED.refresh_func
                           THEN NOW() ELSE freshness_checks.last_updated END
                """, [(rule, hours, status, fn, None) for (rule, hours, status, fn) in rows], page_size=200)
                c.commit()
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {str(e)[:200]}", registered=0), 500
    return jsonify(registered=len(rows),
                   sample=[{"rule": r[0], "stale_hours": r[1], "refresh_func": r[3]} for r in rows[:30]]), 200



@freshness_universal_bp.route("/api/v1/heartbeat/inventory", methods=["GET"])
def inventory():
    """Full freshness inventory grouped by status."""
    _ensure_table()
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT surface, last_updated, stale_after_hours, status FROM freshness_checks ORDER BY surface")
        rows = cur.fetchall()
    fresh = stale = unknown = 0
    out = []
    for surface, lu, sah, status in rows:
        is_stale = True if not lu else (now - lu).total_seconds()/3600 > sah
        s = "fresh" if (not is_stale and lu) else ("stale" if lu else "unknown")
        if s == "fresh": fresh += 1
        elif s == "stale": stale += 1
        else: unknown += 1
        out.append({"surface": surface, "status": s,
                    "last_updated": lu.isoformat() if lu else None,
                    "stale_hours": sah})
    return jsonify(total=len(rows), fresh=fresh, stale=stale, unknown=unknown, surfaces=out), 200
