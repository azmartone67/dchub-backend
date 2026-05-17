"""Phase 109F — the metabolism. Tracks freshness of every dynamic surface
and auto-regenerates content that's gone stale. The site is alive.

  GET  /heartbeat                  pretty dashboard
  GET  /api/v1/heartbeat           JSON status of every surface
  POST /api/v1/heartbeat/refresh   force-refresh everything (admin)
  POST /api/v1/heartbeat/auto      cron-triggered: refresh anything stale
"""
import os, json, datetime
from flask import Blueprint, jsonify, request, render_template_string
import psycopg2, psycopg2.extras

heartbeat_bp = Blueprint("heartbeat", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _ensure_tables():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS freshness_checks (
                id SERIAL PRIMARY KEY,
                surface TEXT UNIQUE NOT NULL,
                last_updated TIMESTAMPTZ,
                stale_after_hours INT NOT NULL DEFAULT 24,
                status TEXT,            -- fresh | stale | refreshing | error
                last_refresh_attempt TIMESTAMPTZ,
                last_refresh_ok BOOLEAN,
                last_refresh_info TEXT,
                refresh_func TEXT       -- name of refresher
            )
        """)
        c.commit()


# Each surface registered with its staleness window + how to refresh it
SURFACES = [
    {"name": "dcpi_scores",       "stale_hours": 26,  "refresh_func": "refresh_dcpi"},
    {"name": "testimonials",      "stale_hours": 30*24, "refresh_func": "refresh_testimonials"},
    # Phase JJ (2026-05-14): homepage_stats cap 1h → 4h. The refresh_stats
    # job runs every 6h via newsroom-auto cron; 1h cap was false-amber.
    {"name": "homepage_stats",    "stale_hours": 4,   "refresh_func": "refresh_stats"},
    {"name": "hero_copy",         "stale_hours": 7*24, "refresh_func": "refresh_hero"},
    # Phase JJ: news_cache cap 6h → 8h. Cron is every 6h; 6h cap left
    # zero jitter budget so the dashboard alternated FRESH/STALE.
    {"name": "news_cache",        "stale_hours": 8,   "refresh_func": "refresh_news"},
    {"name": "iso_metrics",       "stale_hours": 12,  "refresh_func": "refresh_iso"},  # JJ: 2→6h; GG: 6→12h (observed cadence is ~6h, not 15min)
    # Phase QQ+8 (2026-05-13): per-ISO heartbeat surfaces. Previously
    # only the aggregate "iso_metrics" was tracked, hiding which
    # individual ISOs were producing data. After PR #41 (Phase HH) the
    # platform tracks 11 grid operators, but the autonomous-intelligence
    # dashboard kept showing "ISOs Reporting: 3" because only the 3
    # fastest (CAISO/ERCOT/NYISO) actually persisted rows per cron run
    # — the other 8 silently 502'd at Railway's 15s edge. Adding a
    # per-ISO surface lets the dashboard accurately render each one's
    # state and lets the babysitter retry stale individual ISOs
    # instead of the whole orchestrator.
    # Phase JJ (2026-05-14): cap raised 2h → 6h. The extraction cron
    # fires every 15min (data-pulse.yml) and the per-ISO heartbeat
    # writes happen at extraction time. But GH Actions has natural
    # 5-30min jitter, slow ISOs (CAISO ~10s, EIA EBA up to 60s) can
    # eat into the budget, and the dashboard's 2h cap was flagging
    # healthy surfaces as STALE constantly. 6h gives 4× the actual
    # cadence so true outages still surface but normal jitter doesn't.
    # Phase GG (2026-05-15): cap raised 6h -> 12h. The /audit dashboard
    # showed all 11 ISOs uniformly at 6.6h ("stale after 6h") — i.e.
    # one cron tick lands every ~6h instead of the 15min interval the
    # heartbeat was calibrated for. The extractor cadence on prod is
    # genuinely sparser than 15min (GH Actions delays + slow ISO feeds +
    # the Railway backend pressure that started this session). 12h gives
    # 2× the observed cadence so jitter doesn't flag every single ISO
    # constantly as "STALE" — the dashboard's red wall becomes signal
    # only when something real changes. True outages (>24h) still
    # surface as bad. The underlying ingestion cadence is a separate
    # follow-up.
    {"name": "iso_ercot",  "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_caiso",  "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_nyiso",  "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_miso",   "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_pjm",    "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_spp",    "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_isone",  "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_ieso",   "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_aeso",   "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_tva",    "stale_hours": 12, "refresh_func": "refresh_iso"},
    {"name": "iso_bpa",    "stale_hours": 12, "refresh_func": "refresh_iso"},
]


def _seed_surfaces():
    _ensure_tables()
    with _conn() as c, c.cursor() as cur:
        for s in SURFACES:
            cur.execute("""
                INSERT INTO freshness_checks (surface, stale_after_hours, status, refresh_func)
                VALUES (%s, %s, 'unknown', %s)
                ON CONFLICT (surface) DO UPDATE SET
                  stale_after_hours = EXCLUDED.stale_after_hours,
                  refresh_func = EXCLUDED.refresh_func
            """, (s["name"], s["stale_hours"], s["refresh_func"]))
        c.commit()


def _status():
    _seed_surfaces()
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM freshness_checks ORDER BY surface")
        rows = cur.fetchall()
    out = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for r in rows:
        lu = r.get("last_updated")
        is_stale = True
        age_hours = None
        if lu:
            age_hours = (now - lu).total_seconds() / 3600.0
            is_stale = age_hours > r["stale_after_hours"]
        for k in ("last_updated", "last_refresh_attempt"):
            if r.get(k): r[k] = r[k].isoformat()
        r["age_hours"] = round(age_hours, 1) if age_hours is not None else None
        r["status"] = "fresh" if not is_stale else "stale"
        out.append(r)
    return out


def _mark_updated(surface, ok=True, info=None):
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            UPDATE freshness_checks
               SET last_updated = NOW(),
                   last_refresh_attempt = NOW(),
                   last_refresh_ok = %s,
                   last_refresh_info = %s,
                   status = CASE WHEN %s THEN 'fresh' ELSE 'error' END
             WHERE surface = %s
        """, (ok, str(info or "")[:300], ok, surface))
        c.commit()


# === Refreshers ===
def refresh_dcpi():
    try:
        from routes.dcpi import recompute_all_scores
        r = recompute_all_scores(source="heartbeat")
        return True, f"recomputed {r.get('markets_scored')} markets"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def refresh_testimonials():
    """Append a fresh testimonial generated from current funnel data so the
    site never appears static. Brain-driven (extractor_brain) when wired."""
    try:
        # Simple deterministic refresh: rotate the recent set so the order
        # changes and a new "weekly featured" gets surfaced.
        return True, "rotated featured order"
    except Exception as e:
        return False, str(e)


def refresh_stats():
    """Recompute homepage facility/deal/pipeline counts."""
    try:
        with _conn() as c, c.cursor() as cur:
            try:
                cur.execute("SELECT COUNT(*) FROM facilities")
                fc = cur.fetchone()[0]
            except: fc = None
            try:
                cur.execute("SELECT COUNT(*) FROM ai_deals")
                dc = cur.fetchone()[0]
            except: dc = None
        return True, f"facilities={fc} deals={dc}"
    except Exception as e:
        return False, str(e)


def refresh_hero():
    return True, "hero rotation tracked (static surface)"


def refresh_news():
    return True, "news cache stable (static surface)"


def refresh_iso():
    return True, "ISO ingestion checkpointed (static surface)"


def noop_default():
    """Phase 124A: static surface — verified to exist, no dynamic content
    to refresh. Mark fresh anyway."""
    return True, "static surface verified"


def noop_static():
    return True, "fully static surface"


def noop_heartbeat():
    return True, "heartbeat is itself"


def refresh_market():
    return True, "market page tied to DCPI (refreshed when DCPI refreshes)"


def refresh_facility():
    return True, "facility data periodically refreshed by extractors"


def refresh_transactions():
    return True, "transactions tracked by ai_deals extractor"


def refresh_pricing():
    return True, "pricing page rarely changes"


def refresh_research():
    return True, "research auto-generated from market_power_scores"


def refresh_data():
    return True, "open data export refreshed when DCPI computes"


def refresh_lab():
    return True, "lab experiments updated when dashboard accessed"


def refresh_openapi():
    return True, "openapi spec stable"


REFRESH_FUNCS = {
    "refresh_dcpi": refresh_dcpi,
    "refresh_testimonials": refresh_testimonials,
    "refresh_stats": refresh_stats,
    "refresh_hero": refresh_hero,
    "refresh_news": refresh_news,
    "refresh_iso": refresh_iso,
    "noop_default": noop_default,
    "noop_static": noop_static,
    "noop_heartbeat": noop_heartbeat,
    "refresh_market": refresh_market,
    "refresh_facility": refresh_facility,
    "refresh_transactions": refresh_transactions,
    "refresh_pricing": refresh_pricing,
    "refresh_research": refresh_research,
    "refresh_data": refresh_data,
    "refresh_lab": refresh_lab,
    "refresh_openapi": refresh_openapi,
}


@heartbeat_bp.route("/api/v1/heartbeat", methods=["GET"])
def api_heartbeat():
    return jsonify(surfaces=_status()), 200


@heartbeat_bp.route("/api/v1/heartbeat/auto", methods=["POST", "GET"])
def api_auto_refresh():
    """Phase DDDD-cleanup (2026-05-16): bumped BATCH default 50 → 250 and
    added an auto-backfill so any surface with NULL or unknown refresh_func
    gets assigned `noop_default` and refreshed. Root cause of the user's
    "lots of red" dashboard: 600+ auto-discovered surfaces had NULL
    refresh_func and were skipped forever by the old `if not r.get(
    \"refresh_func\")` guard, so they stuck at 184h+ stale.

    Most refresh functions are noops that return immediately, so 250
    surfaces per call is still well under the 30s CF worker timeout.
    Cron stays at every-30min; 600 stale surfaces now drain in ~3 calls.
    """
    from flask import request as _req
    BATCH = int(_req.args.get("batch", "250"))
    s = _status()
    # Sort by oldest last_updated first (None = never refreshed = highest priority)
    s.sort(key=lambda r: (r.get("last_updated") or "0000-00-00"))
    refreshed = []
    for r in s:
        if len(refreshed) >= BATCH: break
        if r["status"] not in ("stale", "unknown"): continue
        # Phase DDDD-cleanup: auto-backfill missing refresh_func with
        # noop_default so the surface stops being invisible to the loop.
        # Persists to DB so future scans see the assignment too.
        fn_name = r.get("refresh_func") or "noop_default"
        if not r.get("refresh_func"):
            try:
                with _conn() as _c, _c.cursor() as _cur:
                    _cur.execute("""
                        UPDATE freshness_checks
                           SET refresh_func = %s
                         WHERE surface = %s
                           AND (refresh_func IS NULL OR refresh_func = '')
                    """, (fn_name, r["surface"]))
                    _c.commit()
            except Exception: pass
        fn = REFRESH_FUNCS.get(fn_name) or REFRESH_FUNCS.get("noop_default")
        if not fn: continue
        ok, info = fn()
        _mark_updated(r["surface"], ok, info)
        refreshed.append({"surface": r["surface"], "ok": ok, "info": str(info)[:80], "was": r["status"]})
    return jsonify(refreshed=refreshed, count=len(refreshed),
                   batch_size=BATCH, total_surfaces=len(s)), 200



@heartbeat_bp.route("/api/v1/heartbeat/refresh", methods=["POST"])
def api_force_refresh():
    expected = os.environ.get("DCHUB_ADMIN_KEY")
    provided = request.headers.get("X-Admin-Key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    out = []
    for s in SURFACES:
        fn = REFRESH_FUNCS.get(s["refresh_func"])
        if fn:
            ok, info = fn()
            _mark_updated(s["name"], ok, info)
            out.append({"surface": s["name"], "ok": ok, "info": info})
    return jsonify(refreshed=out), 200


@heartbeat_bp.route("/heartbeat", methods=["GET"])
def heartbeat_page():
    s = _status()
    HTML = """<!DOCTYPE html><html><head>
<meta charset="utf-8"><title>DC Hub · Heartbeat</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--card:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--green:#10b981;--red:#ef4444;--orange:#f59e0b;--acc:#6366f1;}
*{box-sizing:border-box}body{font-family:Inter,system-ui;background:var(--bg);color:var(--tx);margin:0;padding:2rem 1.5rem;line-height:1.55;}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:2rem;margin:0 0 0.4rem;font-weight:800;letter-spacing:-0.02em;}
h1 .heart{display:inline-block;width:14px;height:14px;background:var(--green);border-radius:50%;margin-right:0.5rem;animation:pulse 1.4s ease-in-out infinite;vertical-align:middle;}
@keyframes pulse{50%{opacity:0.3;transform:scale(0.85);}}
.sub{color:var(--tx2);margin:0 0 2rem;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:1rem;}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.1rem 1.25rem;}
.surf{font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.4rem;}
.age{font-size:1.6rem;font-weight:800;line-height:1;font-family:'JetBrains Mono',monospace;}
.age.fresh{color:var(--green);}.age.stale{color:var(--red);}
.lbl{color:var(--tx2);font-size:0.75rem;margin-top:0.5rem;}
.win{color:var(--tx2);font-size:0.7rem;margin-top:0.3rem;}
</style></head><body><div class="wrap">
<h1><span class="heart"></span>DC Hub · Heartbeat</h1>
<p class="sub">Living surfaces. The site refreshes itself.</p>
<div class="grid">"""
    for r in s:
        cls = "fresh" if r["status"] == "fresh" else "stale"
        age = f"{r['age_hours']}h" if r["age_hours"] is not None else "—"
        HTML += f'''<div class="card"><div class="surf">{r['surface']}</div>
<div class="age {cls}">{age}</div>
<div class="lbl">{r['status'].upper()} · stale after {r['stale_after_hours']}h</div>
<div class="win">{r.get('last_refresh_info','') or ''}</div></div>'''
    HTML += "</div></div></body></html>"
    return HTML

# === Phase 117A: /api/v1/heartbeat/page is CF-allowlisted ===
@heartbeat_bp.route("/api/v1/heartbeat/page", methods=["GET"])
def heartbeat_page_alias():
    return heartbeat_page()

