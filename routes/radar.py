"""
routes/radar.py — "Grid Transition Radar" daily briefing (backend-served).

A distinct analyst *publication* on dchub.cloud: one live data core rendered as
a new edition each day, in the voice of a different audience (4-day rotation),
tier-gated (full for paid, teaser for anon/agents).

Serving model (chosen 2026-07-16): dynamic, like /research. The frontend
_routes.json forwards /radar* to Railway; this blueprint renders on the fly.
The data core is cached ~15 min, DB-shared across workers/replicas via
brain_meta, stale-while-revalidate (r-radarfast 2026-07-17) — so page views
render warm (<1s) instead of rebuilding the ~10-call core inline (5-6s).
NO static files.

Routes:
  GET /radar                     -> today's edition (tier-gated HTML)
  GET /radar/<slug>              -> a specific edition (capital|siteselect|agents|press)
  GET /radar/<slug>.json         -> machine-readable teaser feed (agents)

Brand note: /radar is intentionally NOT on the dchub brand system (it's a
publication with its own identity) — it is absent from
brain_consistency_radar.check_page_brand_uniformity's PAGES list, so that
detector never scans it. If the rotating sampler (check_page_brand_drift) ever
picks it up, add "/radar" to that sampler's skip set.

Registration (add near the other register_blueprint calls in main.py):
    from routes.radar import register_radar
    register_radar(app)
"""
from __future__ import annotations
import os, json, time, threading, logging, html as _html, datetime as dt
from flask import Blueprint, request, Response, jsonify

from routes import radar_templates as T          # normalize + render + tier gating

try:
    from routes.tier_gate import _resolve_caller_tier
except Exception:                                 # pragma: no cover - defensive
    def _resolve_caller_tier():
        return ("FREE", {})

# webmcp-proto (2026-07-18): per-page WebMCP tools (Chrome origin trial) —
# fail-soft so /radar can never break on the helper.
try:
    from routes._webmcp import webmcp_inject as _webmcp_inject
except Exception:                                 # pragma: no cover - defensive
    def _webmcp_inject(page_html, tools):
        return page_html

log = logging.getLogger("radar")

radar_bp = Blueprint("radar", __name__)
_PAGES_DIR = os.path.join(os.path.dirname(__file__), "radar_pages")

# 4-day rotation. Order == cycle slot (Nº 001..004).
EDITIONS = [
    {"slug": "capital",    "no": 1, "title": "Capital Allocators"},
    {"slug": "siteselect", "no": 2, "title": "Site Selection & Ops"},
    {"slug": "agents",     "no": 3, "title": "Agents & Developers"},
    {"slug": "press",      "no": 4, "title": "Infra Press"},
]
_BY_SLUG = {e["slug"]: e for e in EDITIONS}
PAID = {"DEVELOPER", "PRO", "ENTERPRISE"}         # everyone else -> teaser

# ── the ONE clock (r-radarclock, 2026-07-29) ─────────────────────────────────
# Every date-derived thing on the page — the edition slot, the featured-ISO
# slot, the printed retrieval stamp — must come from a single `now` read per
# request, threaded explicitly. The bug this replaces: the edition number was
# recomputed per request while the printed date came out of the 15-min cached
# core, so for up to 15 min after 00:00 UTC (and INDEFINITELY when the
# background rebuild kept failing silently) the page paired a new edition
# number with the previous day's date — "Nº 004 · 2026-07-29", a pairing
# unreachable from any single consistent clock.
#
# Rotation is anchored on a FIXED EPOCH ORDINAL, not day-of-year: doy resets to
# 1 after 365 (or 366), and 365 % 4 == 1, so the old `doy % 4` jumped phase every
# Jan 1 — 2026-12-31 and 2027-01-01 both landed on `siteselect` and one slug was
# skipped. The epoch is ordinal(2025-12-31), chosen so that
# (ordinal - epoch) == day-of-year for every date in 2026: the whole current
# year's published sequence, today's edition included, is bit-identical to what
# `doy % n` served, and the cycle simply keeps counting across the boundary.
_CYCLE_EPOCH_ORDINAL = dt.date(2025, 12, 31).toordinal()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _cycle_slot(now: dt.datetime, n: int) -> int:
    """Rotation slot in an n-day cycle for `now` (UTC), phase-stable across the
    year boundary. n must be > 0."""
    return (now.date().toordinal() - _CYCLE_EPOCH_ORDINAL) % n

# ★ OPEN PRODUCT QUESTION — the rotation boundary is UTC midnight, not the
# viewer's. The owner is in America/Phoenix (UTC-7), so from ~17:00 local they
# already see tomorrow's edition number and tomorrow's date. Whether the flip
# should follow the viewer's local midnight instead is a PRODUCT decision, NOT
# a bug, and is deliberately NOT decided here: a viewer-local boundary means
# /radar stops being a single cacheable artifact (the served HTML would vary by
# request timezone, and /radar/<slug>.json would no longer have one answer for
# "today"). Left as UTC on purpose.

# ── data core: fetched from THIS backend over loopback, shaped for normalize ──
# r-radarfast (2026-07-17): the old {"date": core} cache was per-process, and
# gunicorn --max-requests recycles workers ~every 35 min — so most requests hit
# a COLD cache and rebuilt the core inline (~10 sequential loopback GETs ≈ 5-6s
# per page view; measured 5.6s at the edge). Same failure class as the deadlink
# sweep pre-r78. Now: 15-min TTL, DB-shared via brain_meta (spans workers AND
# both replicas), stale-while-revalidate (a stale core serves instantly while
# ONE background thread rebuilds). Only the first request ever builds inline.
_CORE_TTL_S = 900
_CORE_DB_KEY = "radar_core_v1"
_CORE_CACHE = {"ts": 0.0, "core": None}
_CORE_REFRESH_LOCK = threading.Lock()
_CORE_REFRESHING = False
# Held for the DURATION of an inline (blocking) rebuild, so the requests that
# arrive together at 00:00 UTC do not each fire the ~10 loopback GETs. Separate
# from _CORE_REFRESH_LOCK, which is only ever held long enough to flip a flag.
# Per-process, like everything else here: across gunicorn workers the brain_meta
# write is what dedupes, so the worst case at the boundary is one inline build
# per worker — the same cost the pre-existing first-ever path already paid.
_CORE_INLINE_LOCK = threading.Lock()
# A core older than this is no longer "~15 min stale while we revalidate" — it
# means the rebuild has been failing for a while, which used to be INVISIBLE
# (every exception swallowed at _refresh_core_async). Past this age the page and
# the JSON feed say so out loud.
_CORE_STALE_WARN_S = _CORE_TTL_S * 4              # 1h
# Last background-refresh failure, surfaced in the staleness envelope.
_CORE_LAST_ERROR: dict = {"at": 0.0, "type": "", "msg": ""}

# Last-known-good per-ISO DCPI (2026-07-16 live scoreboard). Used as the fallback
# for queue/wait/curtail/renewable, which /api/v1/grid/intelligence does NOT carry
# (they live in the DCPI table / grid-headroom). Wiring that per-ISO source live
# (direct DCPI query, mirroring routes/grid_transition_radar.py) is the follow-up —
# until then the chart shows last-known-good while the HEADLINE total and Ashburn
# load/LMP stay live. Never renders None.
_BASELINE_ISOS = {
    "SPP":   {"ren": 33.6, "queue": 187.2, "wait": 24.0, "curtail": 11.2},
    "CAISO": {"ren": 28.5, "queue": 79.5,  "wait": 39.9, "curtail": 9.0},
    "MISO":  {"ren": 6.8,  "queue": 191.1, "wait": 33.7, "curtail": 6.2},
    "ERCOT": {"ren": 13.8, "queue": 434.1, "wait": 32.6, "curtail": 4.3},
    "ISO-NE":{"ren": 10.7, "queue": 14.8,  "wait": 33.3, "curtail": 3.3},
    "NYISO": {"ren": 19.4, "queue": 10.6,  "wait": 31.4, "curtail": 2.0},
    "PJM":   {"ren": 2.8,  "queue": 172.4, "wait": 50.5, "curtail": 1.2},
}
_BASELINE_ASHBURN_LMP = 36.94
_BASELINE_MARKETS = [
    {"city": "Ashburn",  "state": "VA", "facility_count": 176, "operator_count": 50, "total_mw": 6843},
    {"city": "Sterling", "state": "VA", "facility_count": 99,  "operator_count": 17, "total_mw": 2968},
    {"city": "Manassas", "state": "VA", "facility_count": 64,  "operator_count": 22, "total_mw": 1685},
]

def _internal(path: str, timeout: int = 6) -> dict:
    """GET an internal endpoint over loopback; dchub- UA => full (untiered) data."""
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout,
                         headers={"User-Agent": "dchub-internal-radar/1.0",
                                  "X-Internal-Request": "1"})
        return (r.json() or {}) if r.status_code == 200 else {}
    except Exception:
        return {}

def _norm_iso(iso) -> str:
    k = str(iso).upper().replace("_", "-")
    return "ISO-NE" if k in ("ISONE", "NEISO", "NE-ISO") else k

def _iso_ttp() -> dict:
    """Live per-ISO avg time-to-power (months). time_to_power_months is a
    pro-gated (_locked_fields) column on market_power_scores, so: (1) read it
    directly from the DB (mirrors grid_transition_radar), (2) fall back to the
    dcpi/scores endpoint over LOOPBACK (a 127.0.0.1 self-call bypasses the tier
    gate). Logs both outcomes; {} on total failure so the caller keeps baseline."""
    import logging as _lg
    log = _lg.getLogger("radar")
    # Source 1 — direct DB read
    try:
        try:
            from main import get_read_db as _gdb
        except Exception:
            from main import get_db as _gdb
        conn = _gdb()
        agg = {}; nrows = 0; nttp = 0
        try:
            c = conn.cursor()
            c.execute(
                "SELECT DISTINCT ON (market_slug) iso, time_to_power_months "
                "FROM market_power_scores WHERE published = true "
                "ORDER BY market_slug, computed_at DESC"
            )
            for iso, ttp in c.fetchall():
                nrows += 1
                if iso is None or ttp is None:
                    continue
                nttp += 1
                s = agg.setdefault(_norm_iso(iso), [0.0, 0]); s[0] += float(ttp); s[1] += 1
        finally:
            try: conn.close()
            except Exception: pass
        out = {k: round(v[0] / v[1], 1) for k, v in agg.items() if v[1]}
        log.info("[radar] _iso_ttp DB rows=%s with_ttp=%s -> %s", nrows, nttp, out)
        if out:
            return out
    except Exception as e:
        log.warning("[radar] _iso_ttp DB failed: %s: %s", type(e).__name__, e)
    # Source 2 — loopback dcpi/scores (self-call clears the pro gate)
    try:
        d = _internal("/api/v1/dcpi/scores?limit=400") or {}
        rows = d if isinstance(d, list) else (d.get("scores") or d.get("markets")
                                              or d.get("data") or d.get("results") or [])
        agg = {}
        for r in rows:
            iso = r.get("iso"); ttp = r.get("time_to_power_months")
            if not iso or not isinstance(ttp, (int, float)):
                continue
            s = agg.setdefault(_norm_iso(iso), [0.0, 0]); s[0] += ttp; s[1] += 1
        out = {k: round(v[0] / v[1], 1) for k, v in agg.items() if v[1]}
        log.info("[radar] _iso_ttp endpoint rows=%s -> %s", len(rows), out)
        return out
    except Exception as e:
        log.warning("[radar] _iso_ttp endpoint failed: %s: %s", type(e).__name__, e)
        return {}


def _build_core(now: dt.datetime | None = None) -> dict:
    """Assemble the `core` dict radar_templates.normalize() expects.
    EXPENSIVE (~10 sequential loopback GETs + a DB read) — only call via
    _pull_core(), which caches/SWRs it. No cache reads or writes in here.

    MAPPING SEAM — verify these field names against the live JSON of
    /api/v1/grid/intelligence/<ISO> on first run (the .get() fallbacks make it
    fail soft, not crash). Everything else in the pipeline is already verified.

    `now` is the request's single clock read (see _utc_now). Passing it makes the
    core's retrieved_at stamp and the edition number provably the same instant,
    instead of two separate now() calls that can straddle midnight.
    """
    today = now or _utc_now()

    # Per-ISO: baseline for queue/wait/curtail (not on grid/intelligence); live
    # renewable share where the endpoint returns it. Always a complete row.
    grids = []
    for iso in T.US_ISOS:
        b = _BASELINE_ISOS.get(iso, {})
        d = _internal(f"/api/v1/grid/intelligence/{iso}") or {}
        ren = d.get("renewable_share_pct")
        grids.append({
            "iso": iso,
            "renewable_share_pct": ren if ren is not None else b.get("ren"),
            "interconnection_queue": {"queued_gw": b.get("queue")},
            "dcpi_detail": {
                "avg_queue_wait_months": b.get("wait"),
                "avg_curtailment_pct":   b.get("curtail"),
                "build_markets":         0,
            },
        })
    # LIVE per-ISO time-to-power from the DCPI table (the thesis metric); baseline
    # curtail/queue stay (separate sources). Fail-soft: missing ISO keeps baseline.
    _ttp = _iso_ttp()
    for g in grids:
        live = _ttp.get(g["iso"])
        if live is not None:
            g["dcpi_detail"]["avg_queue_wait_months"] = live

    # Headline "US interconnection queue" = canonical all-US total from the
    # interconnection-queue snapshot (~1,737 GW), LIVE. Fallback = baseline sum.
    _snap = _internal("/api/v1/interconnection-queue/snapshot")
    us_q = (_snap.get("totals") or {}).get("queued_load_gw") \
        or round(sum((g["interconnection_queue"]["queued_gw"] or 0) for g in grids), 1)
    # Ashburn — LIVE load + LMP (baseline LMP fallback so a KPI is never blank).
    ash = _internal("/api/v1/grid/intelligence/PJM-DOM") or {}
    markets = _internal("/api/v1/markets?region=us&sort=facilities&limit=10") or {}
    mkt_rows = markets.get("results") or markets.get("markets") or []

    core = {
        "retrieved_at": today.isoformat(timespec="seconds"),
        "depth": "full",
        "citation": {"cite_as": "DC Hub, dchub.cloud", "license": "CC-BY-4.0"},
        "scoreboard": {"us_interconnection_queue_gw": us_q, "grids": grids},
        "ashburn": {"demand_mw": ash.get("demand_mw"),
                    "lmp_rt_usd_mwh": ash.get("lmp_rt_usd_mwh") or _BASELINE_ASHBURN_LMP,
                    "lmp_congestion_usd_mwh": ash.get("lmp_congestion_usd_mwh")},
        "markets": {"results": mkt_rows or _BASELINE_MARKETS},
    }
    return core


def _load_core_db() -> tuple[float, dict | None]:
    """(ts, core) from brain_meta, or (0, None). Never raises."""
    try:
        from routes.brain_v2_store import get_meta
        row = get_meta(_CORE_DB_KEY)
        if row and row.get("value"):
            p = json.loads(row["value"])
            if isinstance(p.get("core"), dict):
                return float(p.get("ts") or 0), p["core"]
    except Exception:
        pass
    return 0.0, None


def _save_core_db(ts: float, core: dict) -> None:
    try:
        from routes.brain_v2_store import set_meta
        set_meta(_CORE_DB_KEY, json.dumps({"ts": ts, "core": core}))
    except Exception:
        pass


def _refresh_core_sync(now: dt.datetime | None = None) -> dict:
    core = _build_core(now)
    wall = time.time()
    _CORE_CACHE.update(ts=wall, core=core)
    _save_core_db(wall, core)
    return core


def _refresh_core_async() -> None:
    """Rebuild the core in ONE background thread (single-flight)."""
    global _CORE_REFRESHING
    with _CORE_REFRESH_LOCK:
        if _CORE_REFRESHING:
            return
        _CORE_REFRESHING = True

    def _work():
        global _CORE_REFRESHING
        try:
            _refresh_core_sync()
        except Exception as e:
            # NOT swallowed any more. A rebuild that fails forever used to pin a
            # stale core (and, before the date check below, yesterday's date) on
            # the page with zero signal anywhere. Record it and say so.
            _CORE_LAST_ERROR.update(at=time.time(), type=type(e).__name__,
                                    msg=str(e)[:200])
            log.warning("[radar] background core refresh FAILED: %s: %s — "
                        "serving the stale core until a rebuild succeeds",
                        type(e).__name__, e)
        finally:
            _CORE_REFRESHING = False

    threading.Thread(target=_work, name="radar-core-refresh", daemon=True).start()


def _core_date(core) -> str:
    """UTC date the core's figures are stamped with ('' if absent)."""
    return str((core or {}).get("retrieved_at") or "")[:10]


def _staleness(ts: float, core: dict, now: dt.datetime) -> dict | None:
    """None when the cached core may be published without comment.

    Two publishable-but-not-silent conditions:
      date_crossed    — the core is stamped with a DIFFERENT UTC date than the
                        edition number we are about to print beside it. This is
                        the split-brain pairing itself; never emit it mutely.
      refresh_failing — same date, but the copy is hours old, i.e. the
                        background rebuild is not landing.
    """
    today = now.date().isoformat()
    cd = _core_date(core)
    age = max(0.0, time.time() - (ts or 0.0))
    reason = None
    if cd and cd != today:
        reason = "date_crossed"
    elif age > _CORE_STALE_WARN_S:
        reason = "refresh_failing"
    if not reason:
        return None
    out = {"reason": reason, "core_date": cd, "edition_date": today,
           "age_s": int(age)}
    if _CORE_LAST_ERROR.get("type"):
        out["last_refresh_error"] = _CORE_LAST_ERROR["type"]
        out["last_refresh_error_msg"] = _CORE_LAST_ERROR["msg"]
    return out


def _pull_core(now: dt.datetime | None = None) -> tuple[dict, dict | None]:
    """Serve the data core from cache; never build inline except first-ever or on
    a UTC date crossing. Returns (core, staleness) — staleness is None when the
    core is coherent with `now` and recent, else the _staleness envelope.

    Order: fresh in-process → fresh brain_meta (another worker/replica built it)
    → STALE copy served now + single-flight background rebuild → inline build
    when no copy exists anywhere, or when the only copy is stamped with a
    different UTC date than the edition we are about to number.

    ★ The date check is the fix for the two-clock defect: freshness is no longer
    TTL alone. A core whose retrieved_at DATE differs from `now`'s UTC date is
    stale BY DEFINITION, because the page prints that date next to an edition
    number derived from `now` — so a persistently-failing refresh can no longer
    pin yesterday on today's edition. On a date crossing we rebuild INLINE
    (at most once per replica per UTC day) instead of serving the stale copy,
    and if even that fails we serve it with staleness surfaced rather than
    silently lying.
    """
    now = now or _utc_now()
    today = now.date().isoformat()
    wall = time.time()

    if (_CORE_CACHE["core"] and (wall - _CORE_CACHE["ts"]) < _CORE_TTL_S
            and _core_date(_CORE_CACHE["core"]) == today):
        return _CORE_CACHE["core"], None

    ts, core = _load_core_db()
    if core and (wall - ts) < _CORE_TTL_S and _core_date(core) == today:
        _CORE_CACHE.update(ts=ts, core=core)
        return core, None

    if core:
        stale, stale_ts = core, ts
    else:
        stale, stale_ts = _CORE_CACHE["core"], _CORE_CACHE["ts"]

    if not stale:
        return _refresh_core_sync(now), None

    if _core_date(stale) != today:
        with _CORE_INLINE_LOCK:
            # Another request may have rebuilt it while we queued on the lock.
            if (_CORE_CACHE["core"] and _core_date(_CORE_CACHE["core"]) == today
                    and (time.time() - _CORE_CACHE["ts"]) < _CORE_TTL_S):
                return _CORE_CACHE["core"], None
            try:
                return _refresh_core_sync(now), None
            except Exception as e:
                _CORE_LAST_ERROR.update(at=time.time(), type=type(e).__name__,
                                        msg=str(e)[:200])
                log.warning("[radar] inline core rebuild FAILED on UTC date cross "
                            "(core_date=%s edition_date=%s): %s: %s — publishing "
                            "the stale core WITH a staleness notice",
                            _core_date(stale), today, type(e).__name__, e)
                return stale, _staleness(stale_ts, stale, now)

    _refresh_core_async()
    return stale, _staleness(stale_ts, stale, now)

# ── tier + rendering ─────────────────────────────────────────────────────────
def _tier() -> str:
    try:
        name, _ = _resolve_caller_tier()
    except Exception:
        name = "FREE"
    return "full" if str(name).upper() in PAID else "tease"

_NAV = (
    '<div style="position:sticky;top:0;z-index:200;display:flex;gap:16px;align-items:center;'
    'background:#0C0F12;border-bottom:1px solid #242A30;padding:11px clamp(16px,4vw,40px);'
    'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;letter-spacing:.03em">'
    '<a href="/" style="color:#93A8FF;text-decoration:none;font-weight:700">DC&nbsp;Hub</a>'
    '<span style="color:#3A434C">/</span>'
    '<a href="/radar" style="color:#ECEFF2;text-decoration:none">Grid&nbsp;Radar</a>'
    '<a href="/research" style="color:#7E868D;text-decoration:none;margin-left:auto">Research</a>'
    '<a href="/whats-new" style="color:#7E868D;text-decoration:none">What\'s&nbsp;New</a>'
    '<a href="/playground" style="color:#7E868D;text-decoration:none">Playground</a>'
    '</div>'
)

# Honest provenance strip. The page says "LIVE", but not every metric refreshes
# per request: the queue total + Ashburn telemetry + per-ISO time-to-power do;
# per-ISO curtailment is a calibrated reference (dcpi.py iso_defaults — there is
# no live per-ISO curtailment feed yet) and queue depth moves on the ISO ingest.
# Say so plainly rather than let "LIVE" imply more than it should.
_PROV = (
    '<div style="max-width:1060px;margin:0 auto;padding:20px clamp(16px,4vw,40px) 44px;'
    'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;line-height:1.75;'
    'color:#6B747D;border-top:1px solid #242A30">'
    '<b style="color:#93A8FF">PROVENANCE</b><br>'
    '<b style="color:#22B7A6">Live (refreshed within ~15 min):</b> U.S. interconnection-queue total &middot; '
    'Ashburn zone load &amp; real-time LMP &middot; per-ISO time-to-power (DCPI).<br>'
    '<b style="color:#E0982E">Calibrated reference:</b> per-ISO curtailment (no live per-ISO '
    'curtailment feed yet) &middot; per-ISO queue depth (moves on the ISO queue ingest, not per request).<br>'
    'Data: DC Hub (dchub.cloud) &middot; CC-BY-4.0 &middot; cite as "DC Hub, dchub.cloud".'
    '</div>'
)


def _stale_banner(stale: dict | None) -> str:
    """Make a stuck data core VISIBLE on the page. Empty string when healthy.

    The failure mode this exists for: the background rebuild raises every time,
    the exception is caught, and the page keeps serving an old core forever while
    still printing "LIVE". Silence is the defect — so when the core cannot be
    refreshed we say which date the figures carry and which date the edition is."""
    if not stale:
        return ""
    hours = stale.get("age_s", 0) / 3600.0
    age = f"{hours:.1f} h" if hours >= 1 else f"{int(stale.get('age_s', 0) / 60)} min"
    if stale.get("reason") == "date_crossed":
        lead = ("These figures are stamped <b>%s</b> UTC, but this is the <b>%s</b> "
                "edition &mdash; the data core could not be rebuilt across the UTC "
                "date boundary, so the retrieval date below is NOT today."
                % (stale.get("core_date") or "unknown", stale.get("edition_date") or ""))
    else:
        lead = ("These figures were retrieved <b>%s</b> ago and the background "
                "refresh is not landing, so \"live\" currently means that old."
                % age)
    err = ""
    if stale.get("last_refresh_error"):
        err = (' Last rebuild error: <code>%s</code>.'
               % _html.escape(str(stale["last_refresh_error"])))
    return (
        '<div style="max-width:1060px;margin:0 auto;padding:12px clamp(16px,4vw,40px);'
        'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;'
        'line-height:1.7;color:#E0982E;background:#1A1509;border-top:1px solid #3D3111;'
        'border-bottom:1px solid #3D3111">'
        '<b>STALE DATA CORE</b> &mdash; ' + lead + err +
        ' Age ' + age + '.</div>'
    )

# r-radartease (2026-07-18): one cut-off line "from today's full brief" per
# edition — rotates daily with the edition cycle. Deliberately ends mid-thought.
_PULL_QUOTES = {
    "capital":    "The cost of a stranded gigawatt is now visible in three ISOs' forward positioning — and the market is mispricing the one where",
    "siteselect": "Two markets clear every screen we run — power, fiber, water, tax — and neither is on a coast. The first is sitting on",
    "agents":     "One tool call now returns the whole buildability frame, but the highest-signal field is the one most agents never read:",
    "press":      "The story isn't the queue's size — it's the 14-month spread between the grid everyone names and the one where operators are quietly",
}


def _edition_tokens(slug: str, data: dict, now: dt.datetime | None = None) -> dict:
    """Edition-context + daily-rotation tokens for the templates (r-radartease).

    - edition_*/tomorrow_*: fixes the teaser's hardcoded 'Edition Nº 002' (it
      showed 002 every day regardless of the actual rotation slot) and powers
      the tomorrow-hook + edition rail.
    - featured_*: ONE full ISO row unlocked free, rotating daily through all 7
      (fixed-epoch ordinal — see _cycle_slot; this was `doy % 7`, which repeated
      a row across every Jan 1), so the free page genuinely changes every day and
      gives a taste of the paid scoreboard depth.
    - pull_quote: the day's cut-off line from the full brief.

    `now` is the request's single clock read — the SAME one the edition slug and
    the data core were resolved from.
    """
    now = now or _utc_now()
    ed = _BY_SLUG[slug]
    idx = ed["no"] - 1
    nxt = EDITIONS[(idx + 1) % len(EDITIONS)]
    isos = data.get("isos") or []
    feat = isos[_cycle_slot(now, len(isos))] if isos else {}
    return {
        "edition_slug":   slug,
        "edition_no":     f"{ed['no']:03d}",
        "edition_title":  ed["title"],
        "tomorrow_slug":  nxt["slug"],
        "tomorrow_no":    f"{nxt['no']:03d}",
        "tomorrow_title": nxt["title"],
        # The UTC date this edition NUMBER belongs to. Same clock as edition_no,
        # so {{edition_date}} can never contradict {{edition_no}} the way
        # {{retrieved_date}} (a property of the cached core) could.
        "edition_date":   now.date().isoformat(),
        "featured_iso":     feat.get("iso"),
        "featured_wait":    feat.get("wait_mo"),
        "featured_queue":   feat.get("queue_gw"),
        "featured_ren":     feat.get("ren"),
        "featured_curtail": feat.get("curtail"),
        "pull_quote":     _PULL_QUOTES.get(slug, _PULL_QUOTES["capital"]),
    }


def _render_edition(slug: str, tier: str, now: dt.datetime | None = None) -> str:
    # TIER GATE: free/anon see the public teaser (thesis + free headline metrics +
    # locked deep sections + upgrade/agent CTA — daily-fresh via live numbers, and
    # crawlable for SEO/GEO reach); paid (DEVELOPER/PRO/ENTERPRISE) see the full
    # edition. The tease is the acquisition hook; the decision-grade depth converts.
    now = now or _utc_now()
    template = "teaser" if tier == "tease" else slug
    core, stale = _pull_core(now)
    data = T.normalize(core)
    data.update(_edition_tokens(slug, data, now))
    with open(os.path.join(_PAGES_DIR, f"{template}.html"), encoding="utf-8") as f:
        body = T.render(f.read(), data, tier)
    # frame the publication with a slim, navigable DC Hub bar (own identity below)
    # and close with an honest live-vs-calibrated provenance strip. A core that
    # cannot be refreshed gets an explicit banner rather than a silent old date.
    return _NAV + body + _stale_banner(stale) + _PROV

def _today_slug(now: dt.datetime | None = None) -> str:
    """Today's edition slug, from the request's single clock read."""
    now = now or _utc_now()
    return EDITIONS[_cycle_slot(now, len(EDITIONS))]["slug"]

def _teaser_json(slug: str, now: dt.datetime | None = None) -> dict:
    now = now or _utc_now()
    core, stale = _pull_core(now)
    data = T.normalize(core)
    ed = _BY_SLUG[slug]
    isos = {r["iso"]: r for r in data["isos"]}
    return {
        "edition": slug, "cycle_no": ed["no"], "title": ed["title"],
        "retrieved_at": data["retrieved_at"],
        # edition_date = the UTC date the edition NUMBER is for; retrieved_at =
        # when the figures were pulled. They used to be conflated on the page.
        "edition_date": now.date().isoformat(),
        "stale": bool(stale),
        "staleness": stale,
        "citation": core.get("citation", {"cite_as": "DC Hub, dchub.cloud", "license": "CC-BY-4.0"}),
        "teaser_metrics": [
            {"label": "US interconnection queue", "value": data["us_queue_gw"], "unit": "GW"},
            {"label": "Ashburn time-to-power", "value": isos.get("PJM", {}).get("wait_mo"), "unit": "months"},
        ],
        "locked": {"positioning_book": True, "buildability_quadrant": True,
                   "full_ledger": True, "provenance_envelope": True},
        "unlock": {"why": "Full edition (all ISO rows, deep radar) is paid depth.",
                   "human_cta": "Upgrade or connect DC Hub to read the full brief.",
                   "free_path": "claim_free_key", "agent_next_tool": "unlock_more_data",
                   "playground": "https://dchub.cloud/playground"},
    }

# ── WebMCP page tools (webmcp-proto, 2026-07-18) ─────────────────────────────
# Registered by routes/_webmcp.webmcp_inject at the route seam (Chrome origin
# trial; feature-detected no-op elsewhere; whole block absent unless env
# WEBMCP_ORIGIN_TRIAL_TOKEN is set). Thin wraps of the SAME public endpoints
# this page renders: its own /radar/<slug>.json teaser feed plus two keyless
# APIs already in webmcp_master_shell.BOUND_API_PATHS.
def _webmcp_tools(slug: str) -> list[dict]:
    return [
        {
            "name": "get-grid-radar-brief",
            "description": ("Machine-readable brief of this Grid Transition "
                            "Radar edition: headline US interconnection-queue "
                            "GW, Ashburn time-to-power, citation. Use for a "
                            "cited summary of today's grid-transition picture."),
            "schema": {"type": "object", "properties": {
                "edition": {"type": "string",
                            "enum": [e["slug"] for e in EDITIONS],
                            "description": "Audience edition (default: this page's)"}}},
            "js_body": ("var ed=(input&&input.edition)||%s;"
                        "return api('/radar/'+encodeURIComponent(ed)+'.json');"
                        % json.dumps(slug)),
        },
        {
            "name": "get-live-grid-scoreboard",
            "description": ("Live ranked scoreboard of the US ISO grids behind "
                            "this Radar: demand, fuel mix, renewable share — "
                            "right now. No parameters."),
            "schema": {"type": "object", "properties": {}},
            "js_body": "return api('/api/v1/iso/comparison');",
        },
        {
            "name": "get-dcpi-trending-markets",
            "description": ("DC Hub Power Index (DCPI) trending movers — which "
                            "data-center markets are gaining or losing "
                            "buildability right now. No parameters."),
            "schema": {"type": "object", "properties": {}},
            "js_body": "return api('/api/v1/dcpi/trending');",
        },
    ]


# ── routes ───────────────────────────────────────────────────────────────────
@radar_bp.route("/radar")
def radar_today():
    # ONE clock read per request, threaded through slug + render + core. Two
    # now() calls here is exactly how the edition number and the printed date
    # came from different clocks.
    now = _utc_now()
    slug = _today_slug(now)
    return Response(_webmcp_inject(_render_edition(slug, _tier(), now),
                                   _webmcp_tools(slug)),
                    mimetype="text/html")

@radar_bp.route("/radar/<slug>")
def radar_edition(slug: str):
    now = _utc_now()
    if slug.endswith(".json"):
        base = slug[:-5]
        if base not in _BY_SLUG:
            return jsonify({"error": "unknown edition", "editions": list(_BY_SLUG)}), 404
        return jsonify(_teaser_json(base, now))
    if slug not in _BY_SLUG:
        return jsonify({"error": "unknown edition", "editions": list(_BY_SLUG)}), 404
    return Response(_webmcp_inject(_render_edition(slug, _tier(), now),
                                   _webmcp_tools(slug)),
                    mimetype="text/html")


def register_radar(app):
    app.register_blueprint(radar_bp)
