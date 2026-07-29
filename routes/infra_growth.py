"""Daily infra growth tracker — one count snapshot per layer per day.

Pure DB COUNT(*)s (no external egress) so it runs fine on Railway; a daily
cron POSTs the snapshot and surfaces FLATLINE warnings — a layer that
should be growing but hasn't changed in N days is an early signal that its
source quietly broke (exactly what happened to gas pipelines: frozen at
918 for weeks while the cron looked green).

Layers are tagged by expected cadence:
  daily    — should gain rows most days (substations, data centers)
  periodic — refreshes in bursts when the gov source republishes (gas,
             FCC fiber, gas compressors/processing)
  static   — annual federal data, no daily/weekly growth expected
The flatline check only fires when days-since-last-change exceeds the
layer's max_stale_days (None = never warn, for static layers).

Endpoints (admin-gated):
  POST /api/v1/admin/infra-growth/snapshot  → record today + return summary
  GET  /api/v1/admin/infra-growth           → summary from stored history
  GET  /api/v1/admin/infra-growth/history?layer=X&days=30 → raw series
"""
import os

import psycopg2
from flask import Blueprint, jsonify, request

infra_growth_bp = Blueprint("infra_growth", __name__)

# (label, source table, category, max_stale_days)  None = never flag stale.
_LAYERS = [
    ("substations",             "substations",              "daily",    10),
    ("data_centers",            "discovered_facilities",    "daily",    14),
    ("gas_pipelines",           "gas_pipelines",            "periodic", 130),
    ("fcc_fiber_hexes",         "fcc_fiber_hex",            "periodic", 230),
    ("metro_fiber_routes",      "fiber_routes",             "periodic", 75),
    ("gas_compressors",         "gas_compressor_stations",  "periodic", 200),
    ("gas_processing",          "gas_processing_plants",    "periodic", 200),
    ("transmission_lines",      "infrastructure_layers",    "static",   None),
    ("power_plants_eia",        "power_plants_eia",          "static",   None),
    ("power_plants_discovered", "discovered_power_plants",   "static",   None),
    # GEM worldwide inventory — gated quarterly refresh (owner re-downloads); "periodic"
    # with generous thresholds so a stale flag = "GEM is overdue for a refresh", not noise.
    ("gem_global_power",        "gem_power",                "periodic", 150),
    ("gem_lng_terminals",       "gem_gas",                  "periodic", 220),
    ("gem_pipelines",           "gem_gas_pipelines",        "periodic", 220),
    ("gem_coal_mines",          "gem_coal_mines",           "periodic", 220),
]
_CAT = {l[0]: l[2] for l in _LAYERS}
_STALE = {l[0]: l[3] for l in _LAYERS}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _admin_ok():
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    got = (request.headers.get("X-Admin-Key") or request.headers.get("X-Internal-Key")
           or request.args.get("admin_key") or "")
    return bool(expected) and got == expected


def _ensure(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS infra_growth_snapshot (
            snapshot_date DATE NOT NULL,
            layer TEXT NOT NULL,
            count BIGINT,
            captured_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (snapshot_date, layer)
        )""")


def _count(cur, tbl, label):
    """COUNT(*) for a layer; transmission_lines is a category of one table."""
    cur.execute("SELECT to_regclass(%s)", (tbl,))
    if not cur.fetchone()[0]:
        return None
    if label == "transmission_lines":
        cur.execute("SELECT COUNT(*) FROM infrastructure_layers WHERE category='transmission'")
    else:
        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
    return cur.fetchone()[0]


def _at_or_before(hist, target):
    """Most recent count at or before a target date. hist = [(date,count)] newest-first."""
    for d, c in hist:
        if d <= target:
            return c, d
    return None, None


def _days_since_change(hist):
    """Days since the count last changed (hist newest-first). None if <2 points."""
    if len(hist) < 2:
        return None
    newest = hist[0][1]
    change_date = hist[0][0]
    for d, c in hist[1:]:
        if c != newest:
            break
        change_date = d
    return (hist[0][0] - change_date).days


def _summary(cur):
    cur.execute("SELECT MAX(snapshot_date) FROM infra_growth_snapshot")
    today = (cur.fetchone() or [None])[0]
    out, flatlines = [], []
    for label, tbl, cat, stale in _LAYERS:
        cur.execute("""SELECT snapshot_date, count FROM infra_growth_snapshot
                        WHERE layer=%s ORDER BY snapshot_date DESC LIMIT 90""", (label,))
        hist = cur.fetchall()
        if not hist:
            continue
        cur_date, cur_count = hist[0]   # SELECT order is (snapshot_date, count)
        d1 = d7 = None
        if today:
            import datetime
            prev_c, _ = _at_or_before(hist[1:], cur_date - datetime.timedelta(days=1))
            wk_c, _ = _at_or_before(hist, cur_date - datetime.timedelta(days=7))
            if prev_c is not None:
                d1 = int(cur_count) - int(prev_c)
            if wk_c is not None:
                d7 = int(cur_count) - int(wk_c)
        dsc = _days_since_change(hist)
        flat = bool(stale is not None and dsc is not None and dsc > stale)
        # Best-available rolling window: current vs the OLDEST snapshot still
        # within 7d. Lets the public feed show a real delta even while the
        # tracker is younger than 7 days (then window_days < 7, labelled so).
        dwin = wdays = None
        for d, cc in reversed(hist):            # reversed(newest-first) = oldest-first
            age = (cur_date - d).days
            if 1 <= age <= 7:
                dwin, wdays = int(cur_count) - int(cc), age
                break
        rec = {"layer": label, "category": cat, "count": int(cur_count),
               "delta_1d": d1, "delta_7d": d7, "delta_window": dwin, "window_days": wdays,
               "days_since_change": dsc, "flatline": flat, "as_of": str(cur_date)}
        out.append(rec)
        if flat:
            flatlines.append(f"{label} (no change in {dsc}d, expected <{stale}d)")
    return out, flatlines


@infra_growth_bp.route("/api/v1/admin/infra-growth/snapshot", methods=["POST"])
def snapshot():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                _ensure(cur)
                recorded = 0
                for label, tbl, cat, stale in _LAYERS:
                    n = _count(cur, tbl, label)
                    if n is None:
                        continue
                    cur.execute("""
                        INSERT INTO infra_growth_snapshot (snapshot_date, layer, count)
                        VALUES (CURRENT_DATE, %s, %s)
                        ON CONFLICT (snapshot_date, layer)
                        DO UPDATE SET count=EXCLUDED.count, captured_at=NOW()""",
                        (label, n))
                    recorded += 1
                c.commit()
                summary, flatlines = _summary(cur)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, recorded=recorded, flatlines=flatlines, layers=summary)


@infra_growth_bp.route("/api/v1/admin/infra-growth", methods=["GET"])
def growth():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                _ensure(cur)
                summary, flatlines = _summary(cur)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, flatlines=flatlines, layers=summary)


@infra_growth_bp.route("/api/v1/admin/infra-growth/history", methods=["GET"])
def history():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    layer = (request.args.get("layer") or "").strip()
    try:
        days = min(int(request.args.get("days", 30)), 365)
    except (TypeError, ValueError):
        days = 30
    dsn = _dsn()
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                _ensure(cur)
                if layer:
                    cur.execute("""SELECT snapshot_date, count FROM infra_growth_snapshot
                                    WHERE layer=%s AND snapshot_date > CURRENT_DATE - %s
                                    ORDER BY snapshot_date""", (layer, days))
                else:
                    cur.execute("""SELECT snapshot_date, layer, count FROM infra_growth_snapshot
                                    WHERE snapshot_date > CURRENT_DATE - %s
                                    ORDER BY snapshot_date, layer""", (days,))
                rows = [list(map(str, r)) for r in cur.fetchall()]
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, layer=layer or "all", days=days, rows=rows)


_FRIENDLY = {
    "data_centers": "Data centers", "substations": "Substations",
    "gas_pipelines": "Gas pipelines", "metro_fiber_routes": "Fiber routes",
    "fcc_fiber_hexes": "Broadband / middle-mile coverage", "gas_compressors": "Gas compressor stations",
    "gas_processing": "Gas processing plants", "transmission_lines": "Transmission lines",
    "power_plants_eia": "Power plants", "power_plants_discovered": "Discovered power plants",
}

# Provenance so the public feed — and anything downstream that messages these
# numbers (media shell, agents) — never implies a third-party open-data layer
# was "discovered" by DC Hub. "curated" = DC Hub crawls/curates the rows;
# "public" = we UNIFY a third-party open dataset (still valuable, but say so).
_PROVENANCE = {
    "data_centers":            ("curated", "DC Hub crawlers"),
    "power_plants_discovered": ("curated", "DC Hub discovery"),
    "substations":             ("public",  "HIFLD"),
    "transmission_lines":      ("public",  "HIFLD"),
    "gas_pipelines":           ("public",  "EIA / HIFLD"),
    "gas_compressors":         ("public",  "HIFLD"),
    "gas_processing":          ("public",  "HIFLD"),
    "fcc_fiber_hexes":         ("public",  "FCC"),
    "metro_fiber_routes":      ("public",  "public + DC Hub"),
    "power_plants_eia":        ("public",  "EIA"),
}


@infra_growth_bp.route("/api/v1/whats-new", methods=["GET"])
def whats_new():
    """PUBLIC: recent additions per category (7d / 1d) for the on-site 'What's New'
    feed. No auth — it's a freshness/marketing signal. Reuses the growth snapshots."""
    import datetime
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    deals = None
    # Platform capability announcements. Stays None until the block below runs,
    # so an unreachable DB publishes `platform: null` + a reason rather than an
    # empty list (an empty list would read as "nothing shipped" — a false claim).
    plat = None
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                _ensure(cur)
                layers, _flat = _summary(cur)
                # Deals: count by DB-insertion time (created_at = when WE added the
                # row), not the text `date` column (that's the deal's announcement
                # date, 2018→today, and is text so date math errors).
                # r-wn-dealcanon (2026-07-17): exclude quarantined rows (the
                # 07-17 deals-integrity pass flagged ~2,823 duplicate/garbage
                # rows via data_flag='quarantine_*'; bare COUNT(*) republished
                # the ~2.9x over-claim here as total=4,304 while /api/v1/stats
                # already reports the deduped ~1,42x). Same predicate as the
                # served /api/deals query. LEFT() not LIKE — a literal % in a
                # psycopg2 query string is a live 500.
                _live = "(data_flag IS NULL OR LEFT(data_flag, 11) <> 'quarantine_')"
                try:
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + _live +
                                " AND created_at::timestamptz >= NOW() - INTERVAL '7 days'")
                    d7 = int(cur.fetchone()[0])
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + _live +
                                " AND created_at::timestamptz >= NOW() - INTERVAL '1 day'")
                    d1 = int(cur.fetchone()[0])
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + _live)
                    dtot = int(cur.fetchone()[0])
                    deals = (d7, d1, dtot)
                except Exception:
                    c.rollback()
                # Verified/deduped data-center count — IDENTICAL query to
                # /api/v1/stats (is_duplicate = 0) so the site never shows two
                # different "verified" numbers. The `total` is the raw tracked
                # count; `verified` is the quality-passed subset. Surfacing both
                # stops any consumer implying all ~21.9K are verified DCs.
                try:
                    cur.execute("SELECT COUNT(*) FROM discovered_facilities WHERE is_duplicate = 0")
                    dc_verified = int(cur.fetchone()[0])
                except Exception:
                    c.rollback()
                    dc_verified = None
                # ── Platform capability announcements (brain-staged, owner-approved)
                # The "New platform capabilities" cards on /whats-new were hardcoded
                # HTML and went stale ("36 grids", "tool #73"). They are data now:
                # routes/capability_announcements.py holds the registry, and every
                # number in a card is resolved HERE, at serve time, on THIS cursor —
                # no nested connection, no HTTP egress in a public request.
                # ★ APPROVAL: a card is served only when its registry entry carries
                # status=STATUS_APPROVED, which happens only by the owner merging the
                # PR that sets it. Brain-staged drafts are STATUS_PENDING and can
                # never reach this payload.
                # FAIL SOFT: this block is its own try/except and touches nothing
                # above it — an announcement failure must never 500 the route or
                # blank the coverage items[] that already work.
                try:
                    from routes.capability_announcements import (
                        capability_announcement_cards)
                    plat = capability_announcement_cards(cur)
                except Exception as _pe:
                    try:
                        c.rollback()
                    except Exception:
                        pass
                    plat = {"ok": False,
                            "reason": f"announcement source unavailable: {str(_pe)[:120]}"}
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500

    def _prov(layer_key):
        p, s = _PROVENANCE.get(layer_key, ("public", "public data"))
        return {"provenance": p, "source_name": s}

    items = []
    if deals is not None:
        # Deals are DC Hub-curated (created_at = when WE logged it) — the
        # strongest "we did the work" number, so it leads the feed.
        items.append({"category": "Data-center deals", "total": deals[2],
                      "added": deals[0], "window_days": 7, "added_1d": deals[1],
                      "cadence": "daily", "as_of": None,
                      "provenance": "curated", "source_name": "DC Hub curated"})
    for l in layers:
        if not l["count"]:        # don't advertise empty layers (transmission lives in HIFLD, not this table → 0)
            continue
        item = {"category": _FRIENDLY.get(l["layer"], l["layer"]), "total": l["count"],
                "added": l.get("delta_window"), "window_days": l.get("window_days"),
                "added_1d": l["delta_1d"], "cadence": l["category"], "as_of": l["as_of"],
                **_prov(l["layer"])}
        # Data centers: expose the verified subset next to the raw tracked total
        # and relabel so the headline can never read as "21.9K verified DCs".
        if l["layer"] == "data_centers":
            item["label"] = "Data centers (tracked)"
            item["verified"] = dc_verified
            item["tracked"] = l["count"]
        items.append(item)
    # Everything counted here was added within the last 7 days (layer windows are ≤7d subsets).
    total_added = sum(i["added"] for i in items if isinstance(i["added"], int) and i["added"] > 0)
    # data_as_of = newest real snapshot date (a DATE, never future). The page
    # should render THIS, not generated_at (whose UTC instant tips into
    # "tomorrow" for US readers late in the day → a future "updated" date).
    _asof_dates = [i["as_of"] for i in items if i.get("as_of")]
    data_as_of = max(_asof_dates) if _asof_dates else None
    # Publish the announcements block. Three distinct states, deliberately:
    #   unavailable  -> platform: null  + platform_unavailable_reason (UNMEASURED)
    #   ok, none approved -> platform: [] + platform_pending count (a true "nothing
    #                        approved yet", which is NOT the same claim as null)
    #   ok, approved -> platform: [cards], each with its own figures[] + verify[]
    _plat_ok = bool(plat and plat.get("ok"))
    platform = plat.get("cards") if _plat_ok else None
    platform_reason = None if _plat_ok else (
        (plat or {}).get("reason") or "announcements not resolved this request")
    resp = jsonify(ok=True,
                   generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   data_as_of=data_as_of,
                   platform=platform,
                   platform_unavailable_reason=platform_reason,
                   platform_as_of=(plat or {}).get("as_of"),
                   platform_withheld=((plat or {}).get("withheld") or []) if _plat_ok else [],
                   platform_pending=(plat or {}).get("staged_count") if _plat_ok else None,
                   total_added=total_added, items=items,
                   facilities_tracked=(layers and next((l["count"] for l in layers if l["layer"] == "data_centers"), None)) or None,
                   facilities_verified=dc_verified,
                   note="Live additions to DC Hub across infrastructure layers (rolling 7-day window). "
                        "'Data centers' total is the raw tracked count; 'verified' is the deduped subset. "
                        "Layers marked provenance='public' unify third-party open data (HIFLD/FCC/EIA); "
                        "'curated' layers are crawled/curated by DC Hub. "
                        "'platform' lists owner-approved capability announcements; every number in a "
                        "card is resolved live at request time and carries the field name and the "
                        "endpoint you can call to verify it. platform=null means the announcement "
                        "source was unavailable (see platform_unavailable_reason), NOT that nothing "
                        "shipped.",
                   source="DC Hub (dchub.cloud), CC-BY-4.0")
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


def register_infra_growth(app):
    try:
        app.register_blueprint(infra_growth_bp)
    except Exception as e:
        print(f"[infra_growth] registration: {e}", flush=True)
