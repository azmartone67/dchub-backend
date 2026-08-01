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

def _internal(path: str, timeout: int = 6) -> tuple[dict, str | None]:
    """GET an internal endpoint over loopback. Returns (json, error) — error is
    None on a 200, else a short string naming WHY the feed gave nothing.

    X-Internal-Key matters: /api/v1/grid/intelligence/* sits behind the metered
    map-session gate (free_tier_gate.METERED_MAP_PREFIXES), which runs at
    before_request — long before the route's own dchub- UA bypass — and only
    privileges keys/loopback-remote_addr. This loopback call was being 402'd by
    our own paywall, the except below ate it, and every grid-intel field on
    /radar silently pinned to its baseline while retrieved_at kept moving
    (diagnosed 2026-07-31: demand blank, LMP stuck at 36.94 since 07-16)."""
    try:
        import requests
        headers = {"User-Agent": "dchub-internal-radar/1.0",
                   "X-Internal-Request": "1"}
        ikey = (os.environ.get("DCHUB_INTERNAL_KEY")
                or os.environ.get("DCHUB_SYNC_KEY") or "").strip()
        if ikey:
            headers["X-Internal-Key"] = ikey
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout,
                         headers=headers)
        if r.status_code != 200:
            return {}, f"HTTP {r.status_code}"
        return (r.json() or {}), None
    except Exception as e:
        return {}, f"{type(e).__name__}: {str(e)[:80]}"

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
        d, _ = _internal("/api/v1/dcpi/scores?limit=400")
        d = d or {}
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


_VA_CLUSTER_CITIES = ("Ashburn", "Sterling", "Manassas")


def _market_rows_db() -> tuple[list, str | None]:
    """City-grain market rows for the templates (city/state/facility_count/
    total_mw/operator_count), read DIRECTLY from the canonical fleet.

    Replaces the old loopback GET /api/v1/markets?…: that endpoint ignores its
    query params (the /api/v1/markets alias calls list_markets() bare) and
    returns rows under "data" with name/total_power_mw fields — while this file
    read .results with city/total_mw. The envelope+field seam meant the call
    NEVER matched, so the NoVA cluster ledger silently rendered the 07-16
    baseline constants forever (printed 176 Ashburn facilities while the
    canonical fleet said 171 / 6,942 MW — verified live 2026-07-31).

    Canonical filters (mirrors metric_truth_check / #1539 + ai_capacity_index):
    COALESCE(is_duplicate,0)=0 is the fleet filter.

    r-status-canon (#2058) removed the second predicate this carried,
    `COALESCE(status,'') <> 'active'` — the canon backfill (Operational <-
    active) makes that literal unmatchable, so the fleet filter now carries the
    exclusion alone. That left the cluster counts at Ashburn 199 / Sterling 119
    / Manassas 76, with MW unchanged at 6,942 / 2,902 / 1,644, and its docstring
    stated the consequence plainly: those counts are fleet ROWS, not energized
    sites.

    This PR takes the next step, because the page does not print them apart. It
    renders the pair in one breath — "NoVA {va_mw} MW / {va_facilities}
    facilities" — so a count over a wider population than the SUM silently
    inflates the MW's denominator. `facility_count` therefore counts METERED
    rows only: the rows that actually carry the power_mw printed beside them.
    Ashburn reads 141 / 6,942 MW instead of 199 / 6,942 MW.

    `tracked_count` keeps the wider population visible — metered + unmetered —
    so the rows this count leaves out stay auditable instead of vanishing, and
    the fleet-row figure #2058 published stays available to any caller that
    wants it. Top-10 by metered count PLUS the three NoVA cluster cities
    normalize() sums for the VA ledger, deduped.

    Keying on power_mw is also immune to the status backfill by construction —
    no status literal is involved at all (verified identical before/after)."""
    try:
        try:
            from main import get_read_db as _gdb
        except Exception:
            from main import get_db as _gdb
        conn = _gdb()
        rows: list = []
        seen: set = set()
        try:
            c = conn.cursor()
            metered = "COALESCE(power_mw,0) > 0"
            base = ("SELECT city, state,"
                    f" COUNT(*) FILTER (WHERE {metered})::int,"
                    " COALESCE(SUM(power_mw),0)::float,"
                    f" COUNT(DISTINCT provider) FILTER (WHERE {metered})::int,"
                    " COUNT(*)::int"
                    " FROM discovered_facilities"
                    # r-status-canon (2026-07-31): the second predicate was
                    # COALESCE(status,'') <> 'active'. It goes away with the canon
                    # backfill (Operational <- active), and the #1539 fleet filter
                    # on the line above already removes 6,110 of those 10,435 rows.
                    " WHERE COALESCE(is_duplicate,0)=0"
                    " AND city IS NOT NULL AND city <> ''"
                    " AND state IS NOT NULL AND state <> ''")
            c.execute(base + " AND city IN %s AND state IN ('VA','Virginia')"
                      " GROUP BY city, state",
                      (_VA_CLUSTER_CITIES,))
            va_rows = c.fetchall()
            c.execute(base + " GROUP BY city, state"
                      f" ORDER BY COUNT(*) FILTER (WHERE {metered}) DESC"
                      " LIMIT 10")
            top_rows = c.fetchall()
        finally:
            try: conn.close()
            except Exception: pass
        for city, state, n, mw, ops, tracked in list(va_rows) + list(top_rows):
            if (city, state) in seen:
                continue
            seen.add((city, state))
            rows.append({"city": city, "state": state, "facility_count": n,
                         "total_mw": round(mw or 0), "operator_count": ops,
                         "tracked_count": tracked})
        return rows, None
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:80]}"


def _content_rev(core: dict) -> str:
    """Short stable hash of the SUBSTANTIVE numbers — the anti-re-stamp-drift
    primitive. retrieved_at moves every rebuild; content_rev moves only when a
    figure actually changed, so 'is the radar evolving?' becomes checkable."""
    import hashlib
    sb = core.get("scoreboard") or {}
    basis = {
        "q": sb.get("us_interconnection_queue_gw"),
        "g": [(g.get("iso"),
               (g.get("interconnection_queue") or {}).get("queued_gw"),
               (g.get("dcpi_detail") or {}).get("avg_queue_wait_months"))
              for g in (sb.get("grids") or [])],
        "a": core.get("ashburn"),
        "m": [(m.get("city"), m.get("facility_count"), m.get("total_mw"))
              for m in ((core.get("markets") or {}).get("results") or [])],
    }
    return hashlib.sha1(json.dumps(basis, sort_keys=True,
                                   default=str).encode()).hexdigest()[:12]


def _build_core(now: dt.datetime | None = None) -> dict:
    """Assemble the `core` dict radar_templates.normalize() expects.
    Only call via _pull_core(), which caches/SWRs it. No cache reads/writes here.

    Every fetch lands in core["feeds"] as live=True/False + why — a rebuild can
    no longer 'succeed' into an all-baseline core with zero signal anywhere
    (the re-stamp drift this page shipped between 07-16 and 07-31). The old
    7×  GET /api/v1/grid/intelligence/<ISO> loop is gone: it existed to read
    renewable_share_pct, a field that endpoint has never returned — renewable
    share is a calibrated reference (see _prov_strip), stated as such.

    `now` is the request's single clock read (see _utc_now)."""
    today = now or _utc_now()
    feeds: dict = {}

    def _mark(name: str, live: bool, error: str | None = None, **detail):
        feeds[name] = {"live": bool(live)}
        if error:
            feeds[name]["error"] = error
        feeds[name].update({k: v for k, v in detail.items() if v is not None})

    # 1) Interconnection-queue snapshot — headline US total AND per-ISO depth
    #    (by_iso was always in this response; it was fetched and thrown away
    #    while the page printed 07-16 baseline depths).
    _snap, _snap_err = _internal("/api/v1/interconnection-queue/snapshot")
    by_iso_q: dict = {}
    for r in (_snap.get("by_iso") or []):
        try:
            v = r.get("queued_load_total_gw")
            if r.get("iso") and v is not None:
                by_iso_q[_norm_iso(r["iso"])] = float(v)
        except Exception:
            continue
    us_q_live = (_snap.get("totals") or {}).get("queued_load_gw")
    _mark("queue_snapshot", bool(us_q_live or by_iso_q), _snap_err,
          as_of=_snap.get("as_of"), isos=len(by_iso_q) or None)

    # 2) Per-ISO time-to-power from the DCPI table (the thesis metric).
    _ttp = _iso_ttp()
    _mark("iso_time_to_power", bool(_ttp), None if _ttp else "no rows",
          isos=len(_ttp) or None)

    grids = []
    for iso in T.US_ISOS:
        b = _BASELINE_ISOS.get(iso, {})
        q_live = by_iso_q.get(iso)
        grids.append({
            "iso": iso,
            # Calibrated reference — no live per-ISO renewable feed is wired
            # (grid/intelligence never carried renewable_share_pct).
            "renewable_share_pct": b.get("ren"),
            "interconnection_queue": {
                "queued_gw": q_live if q_live is not None else b.get("queue")},
            "dcpi_detail": {
                "avg_queue_wait_months": _ttp.get(iso, b.get("wait")),
                "avg_curtailment_pct":   b.get("curtail"),
                "build_markets":         0,
            },
        })
    us_q = us_q_live or round(sum(
        (g["interconnection_queue"]["queued_gw"] or 0) for g in grids), 1)

    # 3) Ashburn — LIVE PJM-DOM load + LMP (needs the X-Internal-Key in
    #    _internal to clear the metered map gate; see that docstring).
    #    timeout 12s, not the default 6: the PJM-DOM branch of grid/intelligence
    #    bypasses _grid_intel_cached and hits gridstatus/PJM upstreams inline
    #    every call — first post-deploy rebuild measured a 6s ReadTimeout
    #    (feeds ledger, 2026-07-31 08:59Z). This runs in the background/SWR
    #    rebuild path, so the extra wait costs no page view anything.
    ash, ash_err = _internal("/api/v1/grid/intelligence/PJM-DOM", timeout=12)
    ash_live = (ash.get("demand_mw") is not None
                or ash.get("lmp_rt_usd_mwh") is not None)
    _mark("ashburn_telemetry", ash_live,
          ash_err or (None if ash_live else
                      str(ash.get("error") or ash.get("source_errors")
                          or ash.get("note") or "no live fields")[:120]),
          period=ash.get("demand_period"))

    # 4) NoVA cluster + top markets — canonical fleet DB read.
    mkt_rows, mkt_err = _market_rows_db()
    _mark("markets_fleet_db", bool(mkt_rows), mkt_err,
          rows=len(mkt_rows) or None)

    core = {
        "retrieved_at": today.isoformat(timespec="seconds"),
        "depth": "full",
        "citation": {"cite_as": "DC Hub, dchub.cloud", "license": "CC-BY-4.0"},
        "scoreboard": {"us_interconnection_queue_gw": us_q, "grids": grids},
        "ashburn": {"demand_mw": ash.get("demand_mw"),
                    "lmp_rt_usd_mwh": ash.get("lmp_rt_usd_mwh") or _BASELINE_ASHBURN_LMP,
                    "lmp_live": ash.get("lmp_rt_usd_mwh") is not None,
                    "lmp_congestion_usd_mwh": ash.get("lmp_congestion_usd_mwh")},
        "markets": {"results": mkt_rows or _BASELINE_MARKETS},
        "feeds": feeds,
        "live_feed_count": sum(1 for f in feeds.values() if f.get("live")),
    }
    core["content_rev"] = _content_rev(core)
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


_PREVDAY_DB_KEY = "radar_core_prevday_v1"


def _save_core_db(ts: float, core: dict) -> None:
    try:
        from routes.brain_v2_store import set_meta
        # Day-roll: the first save of a NEW UTC date parks the outgoing day's
        # core under _PREVDAY_DB_KEY, so the page can print honest since-
        # yesterday deltas instead of relabeling the same numbers each morning.
        try:
            _, stored = _load_core_db()
            if stored and _core_date(stored) and \
                    _core_date(stored) != _core_date(core):
                set_meta(_PREVDAY_DB_KEY, json.dumps(
                    {"date": _core_date(stored), "core": stored}))
        except Exception:
            pass
        set_meta(_CORE_DB_KEY, json.dumps({"ts": ts, "core": core}))
    except Exception:
        pass


def _load_prevday_core() -> dict | None:
    """Yesterday's parked core (or None). Never raises."""
    try:
        from routes.brain_v2_store import get_meta
        row = get_meta(_PREVDAY_DB_KEY)
        if row and row.get("value"):
            p = json.loads(row["value"])
            if isinstance(p.get("core"), dict):
                return p["core"]
    except Exception:
        pass
    return None


def _refresh_core_sync(now: dt.datetime | None = None) -> dict:
    core = _build_core(now)
    wall = time.time()
    _CORE_CACHE.update(ts=wall, core=core)
    _save_core_db(wall, core)
    # LOUD per-feed outcome. The 07-16→07-31 regression survived because every
    # feed failure was a silent {} — a "successful" rebuild of baselines.
    dead = [f"{k} ({v.get('error', 'no data')})"
            for k, v in (core.get("feeds") or {}).items() if not v.get("live")]
    if dead:
        log.warning("[radar] core rebuilt with %d/%d feeds DOWN: %s",
                    len(dead), len(core.get("feeds") or {}), "; ".join(dead))
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

    Publishable-but-not-silent conditions:
      date_crossed    — the core is stamped with a DIFFERENT UTC date than the
                        edition number we are about to print beside it. This is
                        the split-brain pairing itself; never emit it mutely.
      refresh_failing — same date, but the copy is hours old, i.e. the
                        background rebuild is not landing.
      feeds_down      — the rebuild itself "succeeded", but EVERY live feed
                        failed, so the figures are reference constants wearing a
                        fresh stamp. This is the re-stamp drift that pinned the
                        page 07-16→07-31; a fresh timestamp on all-fallback data
                        must never publish silently. (Cores from before the feeds
                        ledger existed skip this check.)
    """
    today = now.date().isoformat()
    cd = _core_date(core)
    age = max(0.0, time.time() - (ts or 0.0))
    reason = None
    if cd and cd != today:
        reason = "date_crossed"
    elif age > _CORE_STALE_WARN_S:
        reason = "refresh_failing"
    elif (core or {}).get("feeds") and not (core or {}).get("live_feed_count"):
        reason = "feeds_down"
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

    # Fresh paths still run _staleness: TTL/date freshness cannot veto the
    # feeds_down check — an all-fallback core is stale no matter how young.
    if (_CORE_CACHE["core"] and (wall - _CORE_CACHE["ts"]) < _CORE_TTL_S
            and _core_date(_CORE_CACHE["core"]) == today):
        return _CORE_CACHE["core"], _staleness(_CORE_CACHE["ts"],
                                               _CORE_CACHE["core"], now)

    ts, core = _load_core_db()
    if core and (wall - ts) < _CORE_TTL_S and _core_date(core) == today:
        _CORE_CACHE.update(ts=ts, core=core)
        return core, _staleness(ts, core, now)

    if core:
        stale, stale_ts = core, ts
    else:
        stale, stale_ts = _CORE_CACHE["core"], _CORE_CACHE["ts"]

    if not stale:
        built = _refresh_core_sync(now)
        return built, _staleness(time.time(), built, now)

    if _core_date(stale) != today:
        with _CORE_INLINE_LOCK:
            # Another request may have rebuilt it while we queued on the lock.
            if (_CORE_CACHE["core"] and _core_date(_CORE_CACHE["core"]) == today
                    and (time.time() - _CORE_CACHE["ts"]) < _CORE_TTL_S):
                return _CORE_CACHE["core"], _staleness(_CORE_CACHE["ts"],
                                                       _CORE_CACHE["core"], now)
            try:
                built = _refresh_core_sync(now)
                return built, _staleness(time.time(), built, now)
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

# Honest provenance strip — built PER RENDER from the core's feeds ledger, so
# the page reports which sources were actually live on THIS build instead of a
# hardcoded claim that outlives the wiring (the 07-16 static strip kept saying
# "Ashburn LMP live" for 15 days of baseline). Calibrated-reference labels stay
# static because that's what they are: per-ISO curtailment and renewable share
# have no live per-ISO feed wired.
_FEED_LABELS = {
    "queue_snapshot":    "U.S. interconnection queue (total + per-ISO depth)",
    "iso_time_to_power": "per-ISO time-to-power (DCPI)",
    "ashburn_telemetry": "Ashburn zone load &amp; real-time LMP",
    "markets_fleet_db":  "NoVA cluster ledger (canonical fleet DB)",
}


def _prov_strip(core: dict) -> str:
    feeds = (core or {}).get("feeds") or {}
    live, down = [], []
    for key, label in _FEED_LABELS.items():
        f = feeds.get(key)
        if f is None:
            continue
        if f.get("live"):
            extra = f.get("as_of") or f.get("period") or ""
            live.append(label + (f" <span style='color:#3A434C'>({_html.escape(str(extra))})</span>" if extra else ""))
        else:
            down.append(label)
    live_line = (
        '<b style="color:#22B7A6">Live this build:</b> ' + " &middot; ".join(live) + "<br>"
        if live else "")
    down_line = (
        '<b style="color:#E0982E">Offline this build (showing last reference value):</b> '
        + " &middot; ".join(down) + "<br>" if down else "")
    rev = _html.escape(str((core or {}).get("content_rev") or ""))
    rev_line = (f'Content revision <code>{rev}</code> &mdash; changes only when '
                'a figure changes, not when the stamp does.<br>' if rev else "")
    return (
        '<div style="max-width:1060px;margin:0 auto;padding:20px clamp(16px,4vw,40px) 44px;'
        'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;line-height:1.75;'
        'color:#6B747D;border-top:1px solid #242A30">'
        '<b style="color:#93A8FF">PROVENANCE</b><br>'
        + live_line + down_line +
        '<b style="color:#E0982E">Calibrated reference:</b> per-ISO curtailment '
        '&amp; renewable share (no live per-ISO feed yet).<br>'
        + rev_line +
        'Data: DC Hub (dchub.cloud) &middot; CC-BY-4.0 &middot; cite as "DC Hub, dchub.cloud".'
        '</div>'
    )


def _yesterday_deltas(core: dict, prev: dict | None) -> list[dict]:
    """Real day-over-day movement, computed from yesterday's parked core.
    Only figures that are live-wired both days qualify — never a delta between
    a live number and a baseline constant. Empty list = render nothing."""
    if not prev or not isinstance(prev, dict):
        return []
    out: list[dict] = []

    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _live(c, feed):
        return bool(((c.get("feeds") or {}).get(feed) or {}).get("live"))

    sb, psb = core.get("scoreboard") or {}, prev.get("scoreboard") or {}
    if _live(core, "queue_snapshot") and _live(prev, "queue_snapshot"):
        a, b = _n(psb.get("us_interconnection_queue_gw")), _n(sb.get("us_interconnection_queue_gw"))
        if a is not None and b is not None and round(b - a, 1) != 0:
            out.append({"label": "U.S. interconnection queue",
                        "from": a, "to": b, "delta": round(b - a, 1), "unit": "GW"})
        # biggest per-ISO queue mover
        pq = {g.get("iso"): _n((g.get("interconnection_queue") or {}).get("queued_gw"))
              for g in (psb.get("grids") or [])}
        best = None
        for g in (sb.get("grids") or []):
            iso, v = g.get("iso"), _n((g.get("interconnection_queue") or {}).get("queued_gw"))
            o = pq.get(iso)
            if v is None or o is None or round(v - o, 1) == 0:
                continue
            if best is None or abs(v - o) > abs(best["delta"]):
                best = {"label": f"{iso} queue depth", "from": o, "to": v,
                        "delta": round(v - o, 1), "unit": "GW"}
        if best:
            out.append(best)
    if _live(core, "iso_time_to_power") and _live(prev, "iso_time_to_power"):
        cur = {g.get("iso"): _n((g.get("dcpi_detail") or {}).get("avg_queue_wait_months"))
               for g in (sb.get("grids") or [])}
        old = {g.get("iso"): _n((g.get("dcpi_detail") or {}).get("avg_queue_wait_months"))
               for g in (psb.get("grids") or [])}
        a, b = old.get("PJM"), cur.get("PJM")
        if a is not None and b is not None and round(b - a, 1) != 0:
            out.append({"label": "Ashburn time-to-power", "from": a, "to": b,
                        "delta": round(b - a, 1), "unit": "months"})
    if _live(core, "ashburn_telemetry") and _live(prev, "ashburn_telemetry"):
        a = _n((prev.get("ashburn") or {}).get("demand_mw"))
        b = _n((core.get("ashburn") or {}).get("demand_mw"))
        if a is not None and b is not None and round(b - a) != 0:
            out.append({"label": "Ashburn zone load", "from": a, "to": b,
                        "delta": round(b - a), "unit": "MW"})
    return out


def _delta_strip(core: dict) -> str:
    """SINCE-YESTERDAY strip: the visible daily evolution of the radar, from
    real deltas only. Empty string when there is nothing honest to show."""
    try:
        rows = _yesterday_deltas(core, _load_prevday_core())
    except Exception:
        rows = []
    if not rows:
        return ""
    cells = []
    for r in rows[:4]:
        arrow = "▲" if r["delta"] > 0 else "▼"
        color = "#E0982E" if r["delta"] > 0 else "#22B7A6"
        sign = "+" if r["delta"] > 0 else ""
        cells.append(
            '<span style="margin-right:22px;white-space:nowrap">'
            f'<span style="color:{color}">{arrow} {sign}{r["delta"]:g} {r["unit"]}</span> '
            f'<span style="color:#7E868D">{_html.escape(r["label"])}</span> '
            f'<span style="color:#3A434C">({r["from"]:g} &rarr; {r["to"]:g})</span></span>')
    return (
        '<div style="max-width:1060px;margin:0 auto;padding:14px clamp(16px,4vw,40px);'
        'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;'
        'line-height:2;color:#ECEFF2;border-top:1px solid #242A30">'
        '<b style="color:#93A8FF">SINCE YESTERDAY</b> &nbsp;' + "".join(cells) +
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
    elif stale.get("reason") == "feeds_down":
        lead = ("The last rebuild reached <b>none</b> of its live sources &mdash; "
                "every figure below is a calibrated reference value, not live "
                "data, despite the fresh retrieval stamp.")
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
    # frame the publication with a slim, navigable DC Hub bar (own identity
    # below), real since-yesterday movement, and a per-build provenance strip.
    # A core that cannot be refreshed (or that reached zero live feeds) gets an
    # explicit banner rather than a silent fresh-looking stamp.
    return (_NAV + body + _stale_banner(stale) + _delta_strip(core)
            + _prov_strip(core))

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
        # Machine-checkable freshness: which sources were live on this build,
        # and a content hash that moves only when a figure moves. An agent (or
        # our own freshness monitor) can tell "evolving" from "re-stamped".
        "data_health": {
            "live_feeds": core.get("live_feed_count"),
            "total_feeds": len(core.get("feeds") or {}) or None,
            "feeds": core.get("feeds"),
            "content_rev": core.get("content_rev"),
        },
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
