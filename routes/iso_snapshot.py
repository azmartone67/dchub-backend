"""
iso_snapshot.py — Phase GG: comprehensive per-ISO snapshot.

The user's ask: "can we make our ISO pull more comprehensive?"

Today the platform tracks 11 ISOs (ERCOT, CAISO, NYISO, MISO, PJM, SPP,
ISO-NE, IESO, AESO, TVA, BPA) via the heartbeat surfaces table — but
the heartbeat only stores `last_updated, status`, not the actual data
points each ISO publishes.

This module is a bundled READ over everything we already have for each
ISO: heartbeat freshness, grid_intelligence (if cached), market_power_
scores filtered to the ISO's footprint, capacity_pipeline rollup, and a
peer-comparison rollup across all 11.

Two endpoints:
    GET /api/v1/iso/<iso_code>/snapshot     — per-ISO full picture
    GET /api/v1/iso/comparison              — head-to-head, all 11

All reads, no writes. The richer per-ISO ingestion (LMPs, fuel mix,
queue capacity) belongs to a separate extractor PR — this exposes
everything we already have through one clean tool-callable endpoint.
"""
import os
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

iso_snapshot_bp = Blueprint("iso_snapshot", __name__)

# The 11 ISOs the platform tracks (matches routes/heartbeat.py).
# 'AESO' removed 2026-05-30: its US-realtime extractor (iso_aeso.py) persisted
# 0 rows, so it surfaced here as a misleading markets_scored=0 row AND
# duplicated the working baseline AESO appended from _INTL_ISOS below. AESO now
# appears exactly ONCE in /iso/comparison — the baseline-model entry.
_KNOWN_ISOS = ['ERCOT', 'CAISO', 'NYISO', 'MISO', 'PJM', 'SPP',
               'ISONE', 'IESO', 'TVA', 'BPA']

# Phase ZZZZZ-round54 (2026-05-29) — international ISOs that ship via
# baseline_model_v1 from sibling iso_*_intl modules. Comparison rollup
# now includes these so the user-facing map can render all 14 ISOs in
# one table. They don't have DCPI markets in market_power_scores yet,
# so we surface their LMP + carbon intensity + renewable_pct + demand
# from their snapshot endpoints instead.
_INTL_ISOS = [
    {"code": "HYDROQUEBEC", "module": "routes.iso_hydroquebec",
     "label": "Hydro-Québec", "region": "Canada (QC)"},
    {"code": "AESO",        "module": "routes.iso_aeso_intl",
     "label": "AESO", "region": "Canada (AB)"},
    {"code": "NORDPOOL",    "module": "routes.iso_nordpool_intl",
     "label": "Nord Pool",   "region": "Nordics + Baltics"},
]


def _intl_snapshot_row(iso_def):
    """Build a comparison-row dict from an international ISO's baseline
    snapshot. Best-effort import + best-effort field extraction so a
    broken intl module never poisons the comparison endpoint.
    Returns None on hard failure (skipped from rollup)."""
    try:
        mod = __import__(iso_def["module"], fromlist=["_baseline_snapshot",
                                                       "GENERATION_MIX",
                                                       "INSTALLED_CAPACITY_MW",
                                                       "RENEWABLE_PCT"])
    except Exception:
        return None
    try:
        snap = mod._baseline_snapshot() or {}
    except Exception:
        snap = {}
    def _mv(key):
        v = snap.get(key)
        if isinstance(v, dict):
            return v.get("value")
        return v
    # Carbon intensity + renewable share are the headline metrics
    # international markets compete on. LMP (spot price) maps to the
    # same column as US LMPs so the frontend can render one table.
    spot_usd = (_mv("spot_price_usd_per_mwh")
                or _mv("avg_lmp_usd_per_mwh")
                or _mv("day_ahead_price_usd_per_mwh"))
    return {
        "iso": iso_def["code"],
        "iso_label": iso_def["label"],
        "region": iso_def["region"],
        "data_method": "baseline_model_v1",
        "markets_scored": 0,
        "build_count": 0,
        "caution_count": 0,
        "avoid_count": 0,
        "avg_excess_power_score": None,
        "avg_constraint_score": None,
        "avg_time_to_power_months": None,
        "pipeline_projects": 0,
        "pipeline_total_mw": None,
        "facility_count": 0,
        "total_facility_mw": None,
        "heartbeat_status": "baseline",
        "heartbeat_age_hours": 0,
        # Intl-specific metrics — frontend uses these when DCPI is missing
        "lmp_usd_per_mwh": _as_float(spot_usd),
        "carbon_intensity_g_kwh": _as_float(_mv("carbon_intensity")),
        "renewable_pct": _as_float(snap.get("renewable_pct")
                                   or _mv("renewable_pct")
                                   or getattr(mod, "RENEWABLE_PCT", None)),
        "demand_mw": _as_float(_mv("demand_mw")),
        "installed_capacity_mw": getattr(mod, "INSTALLED_CAPACITY_MW", None),
    }


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)


def _as_float(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _norm_iso(s):
    return re.sub(r"[^A-Z]+", "", (s or "").upper())


def _heartbeat_for_iso(cur, iso):
    """Pull the iso_<iso> heartbeat surface row if present."""
    surface = f"iso_{iso.lower()}"
    try:
        cur.execute(
            """SELECT last_updated, stale_after_hours, status,
                      last_refresh_attempt, last_refresh_ok, last_refresh_info
                 FROM freshness_checks WHERE surface = %s""",
            (surface,))
        row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    last_updated = row[0]
    age_hours = None
    if last_updated:
        age_hours = (datetime.now(timezone.utc)
                     - (last_updated if last_updated.tzinfo
                        else last_updated.replace(tzinfo=timezone.utc))).total_seconds() / 3600
    return {
        "surface": surface,
        "last_updated": last_updated.isoformat() if last_updated else None,
        "stale_after_hours": row[1],
        "status": row[2],
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "last_refresh_attempt": row[3].isoformat() if row[3] else None,
        "last_refresh_ok": row[4],
        "last_refresh_info": (row[5] or '')[:200],
    }


def _dcpi_for_iso(cur, iso):
    """Roll up market_power_scores to the ISO level."""
    try:
        cur.execute(
            """SELECT verdict, COUNT(*) AS n,
                      AVG(excess_power_score) AS avg_excess,
                      AVG(constraint_score) AS avg_constraint,
                      AVG(time_to_power_months) AS avg_ttp
                 FROM (
                     SELECT DISTINCT ON (market_slug)
                            market_slug, verdict, excess_power_score,
                            constraint_score, time_to_power_months
                       FROM market_power_scores
                      WHERE iso = %s
                      ORDER BY market_slug, computed_at DESC
                 ) latest
                GROUP BY verdict""",
            (iso,))
        rows = cur.fetchall()
    except Exception:
        return None
    by_verdict = {}
    total = 0
    excess_sum = 0
    constraint_sum = 0
    ttp_sum = 0
    excess_n = constraint_n = ttp_n = 0
    for verdict, n, ax, ac, at in rows:
        cnt = int(n or 0)
        by_verdict[verdict or 'UNKNOWN'] = cnt
        total += cnt
        if ax is not None: excess_sum += float(ax) * cnt; excess_n += cnt
        if ac is not None: constraint_sum += float(ac) * cnt; constraint_n += cnt
        if at is not None: ttp_sum += float(at) * cnt; ttp_n += cnt
    return {
        "markets_scored": total,
        "by_verdict": by_verdict,
        "avg_excess_power_score": round(excess_sum / excess_n, 1) if excess_n else None,
        "avg_constraint_score": round(constraint_sum / constraint_n, 1) if constraint_n else None,
        "avg_time_to_power_months": round(ttp_sum / ttp_n, 1) if ttp_n else None,
    }


def _pipeline_for_iso(cur, iso):
    """Construction pipeline rollup for the ISO."""
    try:
        cur.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(capacity_mw), 0) AS total_mw,
                      COUNT(*) FILTER (WHERE
                          LOWER(COALESCE(phase, status, '')) LIKE '%construct%')
                          AS construction_count,
                      COALESCE(SUM(capacity_mw) FILTER (WHERE
                          LOWER(COALESCE(phase, status, '')) LIKE '%construct%'), 0)
                          AS construction_mw
                 FROM capacity_pipeline
                WHERE UPPER(COALESCE(iso, '')) = %s""",
            (iso,))
        row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {
        "project_count": int(row[0] or 0),
        "total_mw": _as_float(row[1]),
        "under_construction_count": int(row[2] or 0),
        "under_construction_mw": _as_float(row[3]),
    }


def _facilities_for_iso(cur, iso):
    """Facility count + total MW in the ISO footprint. ISO mapping is
    loose (we use market_power_scores.iso -> state set, then filter
    facilities by state). Best-effort."""
    try:
        cur.execute(
            "SELECT DISTINCT state FROM market_power_scores WHERE iso = %s",
            (iso,))
        states = [r[0] for r in cur.fetchall() if r[0]]
    except Exception:
        states = []
    if not states:
        return None
    try:
        cur.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(power_mw), 0)
                 FROM facilities
                WHERE UPPER(state) = ANY(%s)
                  AND country IN ('US', 'USA', 'United States', 'Canada', 'CA')""",
            ([s.upper() for s in states],))
        row = cur.fetchone()
    except Exception:
        return None
    return {
        "facility_count": int(row[0] or 0),
        "total_facility_mw": _as_float(row[1]),
        "states": states,
    }


@iso_snapshot_bp.route("/api/v1/iso/<iso_code>/snapshot", methods=["GET"])
def iso_snapshot(iso_code):
    """Full per-ISO snapshot: heartbeat freshness, DCPI rollup,
    pipeline, facilities. Single connection, best-effort per section."""
    iso = _norm_iso(iso_code)
    if iso not in _KNOWN_ISOS:
        return jsonify(ok=False, error="unknown_iso",
                       known=_KNOWN_ISOS), 404
    try:
        with _conn() as c, c.cursor() as cur:
            heartbeat = _heartbeat_for_iso(cur, iso)
            dcpi = _dcpi_for_iso(cur, iso)
            pipeline = _pipeline_for_iso(cur, iso)
            facilities = _facilities_for_iso(cur, iso)
        return jsonify(
            ok=True,
            iso=iso,
            heartbeat=heartbeat,
            dcpi=dcpi,
            pipeline=pipeline,
            facilities=facilities,
            drill_deeper={
                "live_grid": f"/api/v1/grid/{iso}",
                "dcpi_markets_in_iso": f"/api/v1/dcpi/iso/{iso}",
                "comparison_with_other_isos": "/api/v1/iso/comparison",
            },
            generated_at=datetime.now(timezone.utc).isoformat(),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 200


@iso_snapshot_bp.route("/api/v1/iso/comparison", methods=["GET"])
def iso_comparison():
    """Head-to-head: every tracked ISO with its DCPI rollup + pipeline +
    facility footprint, ranked by avg excess-power score.

    Phase ZZZZZ-round54: now includes 3 international ISOs (HYDROQUEBEC,
    AESO-intl, NORDPOOL) from sibling baseline-model modules. These show
    up with `data_method: 'baseline_model_v1'` and carry intl-specific
    metrics (lmp_usd_per_mwh, carbon_intensity_g_kwh, renewable_pct).
    """
    out = []
    try:
        with _conn() as c, c.cursor() as cur:
            for iso in _KNOWN_ISOS:
                dcpi = _dcpi_for_iso(cur, iso) or {}
                pipeline = _pipeline_for_iso(cur, iso) or {}
                facilities = _facilities_for_iso(cur, iso) or {}
                heartbeat = _heartbeat_for_iso(cur, iso) or {}
                out.append({
                    "iso": iso,
                    "data_method": "realtime",
                    "markets_scored": dcpi.get("markets_scored", 0),
                    "build_count": (dcpi.get("by_verdict") or {}).get("BUILD", 0),
                    "caution_count": (dcpi.get("by_verdict") or {}).get("CAUTION", 0),
                    "avoid_count": (dcpi.get("by_verdict") or {}).get("AVOID", 0),
                    "avg_excess_power_score": dcpi.get("avg_excess_power_score"),
                    "avg_constraint_score": dcpi.get("avg_constraint_score"),
                    "avg_time_to_power_months": dcpi.get("avg_time_to_power_months"),
                    "pipeline_projects": pipeline.get("project_count", 0),
                    "pipeline_total_mw": pipeline.get("total_mw"),
                    "facility_count": facilities.get("facility_count", 0),
                    "total_facility_mw": facilities.get("total_facility_mw"),
                    "heartbeat_status": heartbeat.get("status"),
                    "heartbeat_age_hours": heartbeat.get("age_hours"),
                })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 200

    # Append intl ISOs after US/CA real-time set. Best-effort — a
    # broken module is skipped (does NOT 500 the endpoint).
    for iso_def in _INTL_ISOS:
        row = _intl_snapshot_row(iso_def)
        if row:
            out.append(row)

    # Rank by avg excess-power (best opportunity first); push None to end.
    out.sort(key=lambda r: (r["avg_excess_power_score"] is None,
                             -(r["avg_excess_power_score"] or 0)))
    return jsonify(
        ok=True,
        generated_at=datetime.now(timezone.utc).isoformat(),
        isos=out,
        ranking_by="avg_excess_power_score",
        iso_count=len(out),
        coverage={
            "realtime_us_ca": _KNOWN_ISOS,
            "baseline_intl": [d["code"] for d in _INTL_ISOS],
        },
    ), 200
