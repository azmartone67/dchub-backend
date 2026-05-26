"""
dcpi_freshness_watchdog.py — Phase r54 (2026-05-25).

User report: DCPI per-market staleness. Grand Forks shows updated
2026-05-11 (2 weeks old). Cron runs 4x daily covering 4 chunks
of 100 markets each — but individual markets can silently fail in
gather_metrics_for_market or trip a per-market exception, leaving
their computed_at frozen.

This module adds:

  1. GET  /api/v1/dcpi/freshness
       Per-market staleness breakdown:
       - fresh_24h: markets recomputed in last 24h
       - stale_3d:  not recomputed in 3+ days (alarming)
       - stale_7d:  not recomputed in 7+ days (critical)
       - oldest 10 markets by computed_at

  2. POST /api/v1/dcpi/recompute/<market_slug>?force=1
       Forces a re-score of a specific market, bypassing the chunk
       offset machinery. Returns the fresh score + duration.

  3. New L23 audit dim: dcpi_freshness
       Reads /api/v1/dcpi/freshness, flags weak when any market is
       >3d stale. Brain catches the silent-per-market failure that
       cron metrics miss.
"""
from __future__ import annotations

import datetime
import os

from flask import Blueprint, jsonify, request

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None


dcpi_freshness_bp = Blueprint("dcpi_freshness", __name__)


def _conn():
    if not psycopg2:
        return None
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected


@dcpi_freshness_bp.route("/api/v1/dcpi/freshness", methods=["GET"])
def dcpi_freshness():
    """Per-market staleness breakdown."""
    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, state, iso,
                           computed_at, verdict,
                           NOW() - computed_at AS age
                      FROM market_power_scores
                     ORDER BY market_slug, computed_at DESC
                )
                SELECT
                    COUNT(*)                                       AS total,
                    COUNT(*) FILTER (WHERE age <= INTERVAL '24 hours')   AS fresh_24h,
                    COUNT(*) FILTER (WHERE age >  INTERVAL '24 hours'
                                       AND age <= INTERVAL '3 days')     AS stale_1_3d,
                    COUNT(*) FILTER (WHERE age >  INTERVAL '3 days'
                                       AND age <= INTERVAL '7 days')     AS stale_3_7d,
                    COUNT(*) FILTER (WHERE age >  INTERVAL '7 days')     AS stale_7d
                  FROM latest
            """)
            stats = dict(cur.fetchone())
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, state, iso,
                           computed_at, verdict
                      FROM market_power_scores
                     ORDER BY market_slug, computed_at DESC
                )
                SELECT market_slug, market_name, state, iso, verdict,
                       computed_at,
                       EXTRACT(EPOCH FROM (NOW() - computed_at))/3600 AS hours_stale
                  FROM latest
                 ORDER BY computed_at ASC NULLS FIRST
                 LIMIT 15
            """)
            oldest = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("computed_at"):
                    d["computed_at"] = d["computed_at"].isoformat()
                if d.get("hours_stale") is not None:
                    d["hours_stale"] = round(float(d["hours_stale"]), 1)
                oldest.append(d)
        return jsonify({
            "ok":           True,
            "stats":        stats,
            "oldest_15":    oldest,
            "checked_at":   datetime.datetime.utcnow().isoformat() + "Z",
            "purpose":      ("Per-market DCPI freshness. Cron is supposed to "
                              "refresh all 286 markets every 6h. Markets in "
                              "oldest_15 are silently failing during recompute."),
        }), 200
    except Exception as e:
        return jsonify({"ok": False,
                         "error": f"{type(e).__name__}: {str(e)[:160]}"}), 200


@dcpi_freshness_bp.route(
    "/api/v1/dcpi/recompute/<market_slug>", methods=["POST"]
)
def force_recompute_market(market_slug):
    """Force a re-score of a specific market. Admin-keyed."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    try:
        from routes.dcpi import (
            MARKETS, gather_metrics_for_market,
            compute_constraint_score, compute_excess_power_score,
            estimate_time_to_power, derive_verdict, derive_top_signals,
            _conn as _dcpi_conn,
        )
    except Exception as e:
        return jsonify({"ok": False,
                         "error": f"dcpi_module_unavailable: {e}"}), 500

    # Find the market tuple in the MARKETS list
    market_tup = next((m for m in MARKETS if m[0] == market_slug), None)
    if not market_tup:
        return jsonify({
            "ok":            False,
            "error":         "market_not_in_MARKETS_list",
            "market_slug":   market_slug,
            "hint":          "Add to routes.dcpi.MARKETS if this is a new market.",
        }), 404

    slug, name, state, iso, lat, lon = market_tup
    t0 = datetime.datetime.utcnow()
    try:
        metrics = gather_metrics_for_market(market_tup)
        c_score = compute_constraint_score(metrics)
        e_score = compute_excess_power_score(metrics)
        ttp     = estimate_time_to_power(metrics)
        verdict = derive_verdict(c_score, e_score)
        risks, opps = derive_top_signals(market_tup, metrics, c_score, e_score)
    except Exception as e:
        return jsonify({
            "ok":     False,
            "stage":  "gather_or_score",
            "error":  f"{type(e).__name__}: {str(e)[:200]}",
            "fix":    ("This is the silent failure category — gather_metrics "
                        "raised for this market. Inspect the trace + fix the "
                        "metric collector."),
        }), 200

    import json as _json
    try:
        with _dcpi_conn() as c, c.cursor() as cur:
            _vals = (
                name, state, iso, lat, lon,
                c_score, e_score, ttp,
                metrics.get("queue_capacity_mw"), metrics.get("queue_wait_months"),
                metrics.get("reserve_margin_pct"),
                metrics.get("gen_additions_12mo_mw"), metrics.get("curtailment_pct"),
                metrics.get("stranded_capacity_mw"),
                metrics.get("emergency_count_30d") or 0,
                _json.dumps(risks), _json.dumps(opps), verdict,
            )
            cur.execute("""
                UPDATE market_power_scores SET
                    market_name=%s, state=%s, iso=%s, latitude=%s, longitude=%s,
                    constraint_score=%s, excess_power_score=%s, time_to_power_months=%s,
                    queue_capacity_mw=%s, queue_wait_months=%s, reserve_margin_pct=%s,
                    gen_additions_12mo_mw=%s, curtailment_pct=%s, stranded_capacity_mw=%s,
                    emergency_count_30d=%s,
                    top_risks_json=%s, top_opportunities_json=%s, verdict=%s,
                    computed_at=NOW()
                WHERE market_slug=%s
            """, _vals + (slug,))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO market_power_scores (
                        market_name, state, iso, latitude, longitude,
                        constraint_score, excess_power_score, time_to_power_months,
                        queue_capacity_mw, queue_wait_months, reserve_margin_pct,
                        gen_additions_12mo_mw, curtailment_pct, stranded_capacity_mw,
                        emergency_count_30d,
                        top_risks_json, top_opportunities_json, verdict,
                        market_slug, computed_at
                    )
                    VALUES (%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s, %s,%s,%s, %s, NOW())
                """, _vals + (slug,))
            c.commit()
    except Exception as e:
        return jsonify({
            "ok":     False,
            "stage":  "db_write",
            "error":  f"{type(e).__name__}: {str(e)[:200]}",
        }), 200

    elapsed = (datetime.datetime.utcnow() - t0).total_seconds()
    return jsonify({
        "ok":               True,
        "market_slug":      slug,
        "market_name":      name,
        "verdict":          verdict,
        "constraint_score": c_score,
        "excess_power_score": e_score,
        "time_to_power_months": ttp,
        "elapsed_seconds":  round(elapsed, 2),
        "computed_at":      datetime.datetime.utcnow().isoformat() + "Z",
    }), 200
