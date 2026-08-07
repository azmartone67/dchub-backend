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

★ A COUNT(*) DELTA CANNOT SEE A FULL-RELOAD LAYER. Several loaders here
replace their table wholesale (truncate/upsert every row) rather than
appending, so the row count is flat while every row is rewritten. Measured
2026-08-07: gas_pipelines rewrote 30,000 rows on 08-03, gas_compressors all
1,768 on 08-02, gem_power all 182,428 on 08-01 — and every one of them
reported delta_7d = 0, indistinguishable from an abandoned table. That is
why each layer also carries a freshness column (_FRESH_COL): last ingest
timestamp is the only signal that separates "refreshed in place" from
"dead". A layer with no such column is UNMEASURABLE for freshness and says
so — freshness is never inferred from a total.

Endpoints (admin-gated):
  POST /api/v1/admin/infra-growth/snapshot  → record today + return summary
  GET  /api/v1/admin/infra-growth           → summary from stored history
  GET  /api/v1/admin/infra-growth/history?layer=X&days=30 → raw series
"""
import os

import psycopg2
from flask import Blueprint, jsonify, request

from util.deals import DEALS_OK

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
    # ★ Both repointed 2026-08-07 — each had been counting an abandoned twin.
    # transmission_lines counted `infrastructure_layers WHERE category='transmission'`,
    # which returns 0: that table's categories are infrastructure/fiber/
    # power_generation/substation and it has NO 'transmission' category at all.
    # The real table holds 95,560 rows (eia-arcgis-runner 94,619 + hifld 934),
    # last refreshed 2026-08-03 — the layer was rendering as absent/zero while
    # its loader was one of the healthiest on the board.
    ("transmission_lines",      "transmission_lines",       "periodic", 120),
    # power_plants_eia counted the `power_plants_eia` table: 13,446 rows, NO
    # timestamp column of any kind, and reporting_period/source_survey empty on
    # every row. The live twin `power_plants` is 100% source='eia-860'
    # (14,480 rows, refreshed 2026-07-31), so the label stays accurate.
    ("power_plants_eia",        "power_plants",             "periodic", 120),
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

# label -> the REAL ingestion-timestamp column on its source table. Every entry
# was read out of information_schema on 2026-08-07, not guessed: the names are
# inconsistent across tables (created_at / loaded_at / ingested_at / first_seen)
# and two of them are stored as TEXT, so they need an explicit cast.
# A label ABSENT from this dict has no usable timestamp column and is reported
# as freshness_measurable=False — see power_plants_discovered below.
_FRESH_COL = {
    "substations":             "created_at",
    "data_centers":            "first_seen",      # discovered_at is TEXT; first_seen is tz-aware, 0 nulls
    "gas_pipelines":           "created_at",
    "fcc_fiber_hexes":         "loaded_at",
    "metro_fiber_routes":      "created_at",
    "gas_compressors":         "loaded_at",
    "gas_processing":          "loaded_at",
    "transmission_lines":      "created_at",
    "power_plants_eia":        "created_at",
    "power_plants_discovered": "discovered_at",   # TEXT — cast below
    "gem_global_power":        "ingested_at",
    "gem_lng_terminals":       "ingested_at",
    "gem_pipelines":           "ingested_at",
    "gem_coal_mines":          "ingested_at",
}
# Columns stored as TEXT rather than a timestamp type; need ::timestamptz.
_FRESH_TEXT = {"power_plants_discovered"}

# label -> date its source table was repointed. Snapshots recorded before this
# counted a DIFFERENT table, so a delta spanning the boundary would invent a
# one-time spike (transmission_lines would have read +95,560 overnight, and
# power_plants_eia +1,034). History before the cutover is ignored, not deleted.
_HISTORY_FROM = {
    "transmission_lines": "2026-08-07",
    "power_plants_eia":   "2026-08-07",
}

# Expected republish cadence per layer, so a board can judge "quiet" against
# INTENT instead of one global threshold. Federal/NGO datasets genuinely
# republish a few times a year — silence from them is not a broken loader.
_EXPECTED_CADENCE = {
    "substations":             "daily",
    "data_centers":            "daily",
    "gas_pipelines":           "quarterly",     # HIFLD/EIA republish
    "fcc_fiber_hexes":         "semiannual",    # FCC BDC release cycle
    "metro_fiber_routes":      "quarterly",
    "gas_compressors":         "quarterly",
    "gas_processing":          "quarterly",
    "transmission_lines":      "quarterly",
    "power_plants_eia":        "monthly",       # EIA-860M
    "power_plants_discovered": "adhoc",
    "gem_global_power":        "quarterly",     # owner re-downloads GEM
    "gem_lng_terminals":       "quarterly",
    "gem_pipelines":           "quarterly",
    "gem_coal_mines":          "quarterly",
}


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
    """COUNT(*) for a layer.

    ★ There used to be a hardcoded `if label == "transmission_lines"` branch
    here that ignored `tbl` and counted `infrastructure_layers WHERE
    category='transmission'` (0 rows — that category does not exist). It is
    gone: while it stood, repointing the layer in _LAYERS was a silent no-op,
    because the table named in the tuple was never the table queried.
    """
    cur.execute("SELECT to_regclass(%s)", (tbl,))
    if not cur.fetchone()[0]:
        return None
    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
    return cur.fetchone()[0]


def _freshness(cur, tbl, label):
    """(last_ingest_iso, age_days) for a layer, or (None, None) if unmeasurable.

    Isolated in its own try/except on purpose: a freshness read that blows up
    (bad cast on a TEXT column, column dropped upstream) must degrade to
    "unmeasurable" for that one layer, never take down the whole snapshot.
    """
    col = _FRESH_COL.get(label)
    if not col:
        return None, None
    cast = "::timestamptz" if label in _FRESH_TEXT else ""
    try:
        cur.execute(
            f"SELECT MAX({col}{cast})::timestamptz, "
            f"       (NOW()::date - MAX({col}{cast})::date) FROM {tbl}")
        row = cur.fetchone()
        if not row or row[0] is None:
            return None, None
        return row[0].isoformat(), int(row[1])
    except Exception:
        cur.connection.rollback()
        return None, None


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
        # _HISTORY_FROM drops snapshots taken while this layer pointed at a
        # different table — counting across that boundary invents a spike.
        cur.execute("""SELECT snapshot_date, count FROM infra_growth_snapshot
                        WHERE layer=%s AND snapshot_date >= COALESCE(%s::date, '-infinity'::date)
                        ORDER BY snapshot_date DESC LIMIT 90""",
                    (label, _HISTORY_FROM.get(label)))
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
        last_ingest, ingest_age = _freshness(cur, tbl, label)
        # ★ A flat COUNT(*) is NOT evidence of a dead layer — full-reload
        # loaders rewrite every row and leave the count unchanged. If the table
        # was re-ingested inside its own staleness window, it is alive, so the
        # flatline warning is withheld and the freshness read is why.
        flat = bool(stale is not None and dsc is not None and dsc > stale)
        if flat and ingest_age is not None and ingest_age <= stale:
            flat = False
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
               "days_since_change": dsc, "flatline": flat, "as_of": str(cur_date),
               # Freshness: the only signal that separates a full-reload layer
               # from an abandoned one. False = no timestamp column exists on
               # the source table, so freshness is UNKNOWN — never assumed.
               "last_ingest_at": last_ingest,
               "ingest_age_days": ingest_age,
               "freshness_measurable": last_ingest is not None,
               "freshness_column": _FRESH_COL.get(label),
               "expected_cadence": _EXPECTED_CADENCE.get(label)}
        out.append(rec)
        if flat:
            flatlines.append(f"{label} (no change in {dsc}d, expected <{stale}d, "
                             f"last ingest {last_ingest or 'UNMEASURABLE'})")
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
    # Measured 2026-08-07 on the repointed table: eia-arcgis-runner 94,619 +
    # hifld 934 + news_extraction 7. It was labelled HIFLD-only while pointing
    # at a table that returned 0 rows.
    "transmission_lines":      ("public",  "EIA / HIFLD"),
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
                # served /api/deals query — literally the same string now.
                # This was a function-LOCAL copy (`_live = "..."`), the exact
                # shape that made the 07-27 capacity_pipeline audit's "every
                # read is guarded" claim false for fourteen reads: nothing
                # could import it, so nothing could check anything against it.
                # Spelled DEALS_OK at each site rather than re-aliased, so the
                # census in tests/test_deals_guard.py can see it.
                try:
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + DEALS_OK +
                                " AND created_at::timestamptz >= NOW() - INTERVAL '7 days'")
                    d7 = int(cur.fetchone()[0])
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + DEALS_OK +
                                " AND created_at::timestamptz >= NOW() - INTERVAL '1 day'")
                    d1 = int(cur.fetchone()[0])
                    cur.execute("SELECT COUNT(*) FROM deals WHERE " + DEALS_OK)
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
                # HTML and went stale ("36 grids", "tool #73"). They are data now.
                #
                # ★ ONE STORE (2026-08-01). This used to read a SECOND registry
                # (routes/capability_announcements.ANNOUNCEMENTS) while the brain
                # staged its cards into data/platform_updates.json. Two stores
                # shipped the same day and the brain wrote to the one this route
                # does not read, so four owner-approved cards were live at
                # /api/v1/platform-updates and invisible on the page they were
                # written for. Worse, the two card SHAPES differ: the page's
                # renderer reads `metric`/`link_href`/`code`, which the other
                # registry never emitted, so even the five cards that did render
                # lost their metric tile and their CTA link.
                # data/platform_updates.json is now the ONLY source — the store the
                # brain already writes to, and the shape the page already renders.
                # ★ APPROVAL is unchanged and still the whole point: a card is
                # served only when its entry carries the literal status
                # "published", which happens only by the owner merging the PR that
                # sets it. There is no write endpoint.
                # ★ Numbers are bound HERE, at serve time, from in-process canon —
                # no nested connection and no HTTP egress in a public request. The
                # page binds the SAME token from the SAME source, so the JSON an
                # agent reads and the figure a human sees cannot disagree.
                # FAIL SOFT: its own try/except, touching nothing above it — an
                # announcement failure must never 500 the route or blank the
                # coverage items[] that already work.
                try:
                    from routes.platform_updates import (
                        canon_values, published_updates, resolve_card_metrics)
                    plat = resolve_card_metrics(published_updates(),
                                                canon_values())
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
        if not l["count"]:        # don't advertise empty layers
            continue
        item = {"category": _FRIENDLY.get(l["layer"], l["layer"]), "total": l["count"],
                "added": l.get("delta_window"), "window_days": l.get("window_days"),
                "added_1d": l["delta_1d"], "cadence": l["category"], "as_of": l["as_of"],
                # ★ Freshness travels with the total. Without it a layer that
                # refreshes in place is unreadable: 55,064 fiber routes looks
                # the same whether it reloaded today or was abandoned in March.
                # `added` legitimately stays 0 for a full-reload layer — the
                # honest signal there is last_ingest_at, not the delta.
                "last_ingest_at": l.get("last_ingest_at"),
                "ingest_age_days": l.get("ingest_age_days"),
                "freshness_measurable": l.get("freshness_measurable", False),
                "expected_cadence": l.get("expected_cadence"),
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
    # `platform_pending` keeps its published meaning — entries the brain staged
    # that the owner has NOT approved. In the one-store model that is exactly
    # the set the approval gate withheld, so it is counted from the gate's own
    # reason rather than tracked separately (two counters would drift).
    _plat_pending = None
    if _plat_ok:
        _plat_pending = sum(
            1 for w in (plat.get("withheld") or [])
            if "not approved" in str((w or {}).get("reason") or ""))
    resp = jsonify(ok=True,
                   generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   data_as_of=data_as_of,
                   platform=platform,
                   platform_unavailable_reason=platform_reason,
                   platform_as_of=(datetime.datetime.now(datetime.timezone.utc)
                                   .isoformat() if _plat_ok else None),
                   platform_withheld=((plat or {}).get("withheld") or []) if _plat_ok else [],
                   platform_pending=_plat_pending,
                   total_added=total_added, items=items,
                   facilities_tracked=(layers and next((l["count"] for l in layers if l["layer"] == "data_centers"), None)) or None,
                   facilities_verified=dc_verified,
                   note="Live additions to DC Hub across infrastructure layers (rolling 7-day window). "
                        "'Data centers' total is the raw tracked count; 'verified' is the deduped subset. "
                        "Layers marked provenance='public' unify third-party open data (HIFLD/FCC/EIA); "
                        "'curated' layers are crawled/curated by DC Hub. "
                        "'platform' lists owner-approved capability announcements. A card never "
                        "stores a figure: it carries metric.token plus the basis and the "
                        "source_url you can call to verify it, and metric.value is bound live at "
                        "request time from that same canonical source. A token with no live "
                        "keyless source stays value=null with metric.unmeasured_reason — never 0, "
                        "never a frozen literal. platform=null means the announcement source was "
                        "unavailable (see platform_unavailable_reason), NOT that nothing shipped.",
                   source="DC Hub (dchub.cloud), CC-BY-4.0")
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


def register_infra_growth(app):
    try:
        app.register_blueprint(infra_growth_bp)
    except Exception as e:
        print(f"[infra_growth] registration: {e}", flush=True)
