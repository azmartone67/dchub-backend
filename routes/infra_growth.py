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
        cur_count, cur_date = hist[0]
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
        rec = {"layer": label, "category": cat, "count": int(cur_count),
               "delta_1d": d1, "delta_7d": d7, "days_since_change": dsc,
               "flatline": flat, "as_of": str(cur_date)}
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


def register_infra_growth(app):
    try:
        app.register_blueprint(infra_growth_bp)
    except Exception as e:
        print(f"[infra_growth] registration: {e}", flush=True)
