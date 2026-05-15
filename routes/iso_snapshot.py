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
_KNOWN_ISOS = ['ERCOT', 'CAISO', 'NYISO', 'MISO', 'PJM', 'SPP',
               'ISONE', 'IESO', 'AESO', 'TVA', 'BPA']


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
    facility footprint, ranked by avg excess-power score."""
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
    # Rank by avg excess-power (best opportunity first); push None to end.
    out.sort(key=lambda r: (r["avg_excess_power_score"] is None,
                             -(r["avg_excess_power_score"] or 0)))
    return jsonify(
        ok=True,
        generated_at=datetime.now(timezone.utc).isoformat(),
        isos=out,
        ranking_by="avg_excess_power_score",
    ), 200
