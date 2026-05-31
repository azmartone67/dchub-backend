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
    "/api/v1/dcpi/recompute-stale", methods=["POST"]
)
def recompute_stale_markets():
    """r55 (2026-05-25): batch-rescore every market that's stale >7d.

    User observation: 89 markets stuck at 351 hours stale because the
    canonical MARKETS list in routes/dcpi.py shrank (from ~286 → ~197),
    but market_power_scores still has the older rows. Cron's chunk-by-
    offset iteration never touches them. This endpoint walks the DB
    directly + rescores each, surfacing any silent gather_metrics
    failures along the way.

    Params:
      ?max=N         cap how many markets to attempt (default 50)
      ?dry_run=1     just list candidates, don't rescore

    Admin-keyed.
    """
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    try:
        max_n = max(1, min(int(request.args.get("max", 50)), 200))
    except Exception:
        max_n = 50
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")

    # Find stale market slugs straight from the DB
    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, state, iso,
                           latitude, longitude, computed_at
                      FROM market_power_scores
                     ORDER BY market_slug, computed_at DESC
                )
                SELECT market_slug, market_name, state, iso,
                       latitude, longitude, computed_at,
                       EXTRACT(EPOCH FROM (NOW() - computed_at))/3600 AS hours_stale
                  FROM latest
                 WHERE computed_at < NOW() - INTERVAL '7 days'
                 ORDER BY computed_at ASC
                 LIMIT %s
            """, (max_n,))
            stale_rows = cur.fetchall()
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    if dry_run:
        return jsonify({
            "ok":         True,
            "dry_run":    True,
            "count":      len(stale_rows),
            "candidates": [
                {"slug": r["market_slug"], "name": r["market_name"],
                 "iso": r["iso"], "hours_stale": round(float(r["hours_stale"] or 0), 1)}
                for r in stale_rows
            ],
        }), 200

    # Real rescoring path — uses the scoring helpers directly so we
    # don't depend on the slug being in the canonical MARKETS list
    try:
        from routes.dcpi import (
            gather_metrics_for_market,
            compute_constraint_score, compute_excess_power_score,
            estimate_time_to_power, derive_verdict, derive_top_signals,
            _conn as _dcpi_conn,
        )
    except Exception as e:
        return jsonify({"ok": False,
                         "error": f"dcpi_module_unavailable: {e}"}), 500

    import json as _json
    rescored = 0
    failed = []
    for row in stale_rows:
        slug = row["market_slug"]
        # Reconstruct the market tuple from the DB row (this is the
        # whole point — the canonical MARKETS list no longer has them
        # but the DB row preserved name/state/iso/lat/lon at last score)
        m = (slug, row["market_name"], row["state"], row["iso"],
             row["latitude"], row["longitude"])
        try:
            metrics = gather_metrics_for_market(m)
            c_score = compute_constraint_score(metrics)
            e_score = compute_excess_power_score(metrics)
            ttp     = estimate_time_to_power(metrics)
            verdict = derive_verdict(c_score, e_score)
            risks, opps = derive_top_signals(m, metrics, c_score, e_score)
            _vals = (
                row["market_name"], row["state"], row["iso"],
                row["latitude"], row["longitude"],
                c_score, e_score, ttp,
                metrics.get("queue_capacity_mw"), metrics.get("queue_wait_months"),
                metrics.get("reserve_margin_pct"),
                metrics.get("gen_additions_12mo_mw"),
                metrics.get("curtailment_pct"),
                metrics.get("stranded_capacity_mw"),
                metrics.get("emergency_count_30d") or 0,
                _json.dumps(risks), _json.dumps(opps), verdict,
            )
            with _dcpi_conn() as wc, wc.cursor() as wcur:
                wcur.execute("""
                    UPDATE market_power_scores SET
                        market_name=%s, state=%s, iso=%s, latitude=%s, longitude=%s,
                        constraint_score=%s, excess_power_score=%s,
                        time_to_power_months=%s,
                        queue_capacity_mw=%s, queue_wait_months=%s,
                        reserve_margin_pct=%s,
                        gen_additions_12mo_mw=%s, curtailment_pct=%s,
                        stranded_capacity_mw=%s, emergency_count_30d=%s,
                        top_risks_json=%s, top_opportunities_json=%s,
                        verdict=%s, computed_at=NOW()
                    WHERE market_slug=%s
                """, _vals + (slug,))
                wc.commit()
            rescored += 1
        except Exception as e:
            failed.append({"slug": slug, "error": f"{type(e).__name__}: {str(e)[:160]}"})

    return jsonify({
        "ok":            True,
        "attempted":     len(stale_rows),
        "rescored":      rescored,
        "failed_count":  len(failed),
        "failed_sample": failed[:5],
        "hint":          ("Run again to clear more (capped at "
                           f"{max_n}/call). Add ?dry_run=1 to preview without writing."),
    }), 200


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
                    VALUES (%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s, %s,%s,%s, %s, NOW() ON CONFLICT DO NOTHING)
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


# ── r58 (2026-05-25) ───────────────────────────────────────────────
# recompute-missing: walks MARKETS, finds slugs ABSENT from
# market_power_scores, scores + INSERTs them. Closes the gap left by
# r57's intl expansion — the 16 new markets are in MARKETS but
# 0 are in the DB, so the daily cron's UPDATE-only path never
# touches them.
#
# Also auto-fires when called WITHOUT an admin key — only when the
# request comes from a trusted internal source (X-Internal-Cron
# header set by GH Actions). This lets us put it on a cron without
# rotating admin keys, while still keeping random users off it.

@dcpi_freshness_bp.route(
    "/api/v1/dcpi/recompute-missing", methods=["POST"]
)
def recompute_missing_markets():
    """r58 (2026-05-25): score every MARKETS entry that's MISSING from
    market_power_scores. Idempotent — re-running is a no-op once
    everything is filled in.

    Params:
      ?max=N       cap how many to do per call (default 30; intl set is 16)
      ?dry_run=1   list candidates without writing

    Auth: admin key OR X-Internal-Cron header matching DCHUB_CRON_SECRET.
    """
    # Auth: admin OR internal-cron
    is_admin = _admin_authorized()
    cron_secret_env = os.environ.get("DCHUB_CRON_SECRET", "")
    cron_secret_hdr = request.headers.get("X-Internal-Cron", "")
    is_cron = bool(cron_secret_env) and cron_secret_hdr == cron_secret_env
    if not (is_admin or is_cron):
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    try:
        max_n = max(1, min(int(request.args.get("max", 30)), 100))
    except Exception:
        max_n = 30
    dry_run = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")

    # Get the canonical MARKETS list + the set of slugs already in DB
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

    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("SELECT DISTINCT market_slug FROM market_power_scores")
            present = {row[0] for row in cur.fetchall()}
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    # Identify missing markets (handle both tuple + dict shapes the
    # MARKETS loader can emit — see _load_markets_dynamic).
    def _market_slug(m):
        if isinstance(m, tuple) and m:
            return m[0]
        if isinstance(m, dict):
            return m.get("slug")
        return None

    missing = []
    for m in MARKETS:
        slug = _market_slug(m)
        if slug and slug not in present:
            missing.append(m)
        if len(missing) >= max_n:
            break

    if dry_run:
        return jsonify({
            "ok":         True,
            "dry_run":    True,
            "count":      len(missing),
            "candidates": [_market_slug(m) for m in missing],
        }), 200

    import json as _json
    inserted = 0
    failed = []
    for m in missing:
        # Normalize to tuple shape gather_metrics expects
        if isinstance(m, dict):
            mt = (m.get("slug"), m.get("name"), m.get("state"),
                  m.get("iso"), m.get("latitude"), m.get("longitude"))
        else:
            mt = m
        slug = mt[0]
        try:
            metrics = gather_metrics_for_market(mt)
            c_score = compute_constraint_score(metrics)
            e_score = compute_excess_power_score(metrics)
            ttp     = estimate_time_to_power(metrics)
            verdict = derive_verdict(c_score, e_score)
            risks, opps = derive_top_signals(mt, metrics, c_score, e_score)

            with _dcpi_conn() as wc, wc.cursor() as wcur:
                # r58 (2026-05-25): set published=true so the inserted
                # row appears on /dcpi immediately (every public-facing
                # query has WHERE published=true; rows default to NULL
                # in the schema and would otherwise stay invisible).
                wcur.execute("""
                    INSERT INTO market_power_scores (
                        market_slug, market_name, state, iso, latitude, longitude,
                        constraint_score, excess_power_score, time_to_power_months,
                        queue_capacity_mw, queue_wait_months, reserve_margin_pct,
                        gen_additions_12mo_mw, curtailment_pct, stranded_capacity_mw,
                        emergency_count_30d,
                        top_risks_json, top_opportunities_json, verdict,
                        published, computed_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s,
                             %s,%s,%s, %s, %s,%s,%s, TRUE, NOW() ON CONFLICT DO NOTHING)
                """, (
                    slug, mt[1], mt[2], mt[3], mt[4], mt[5],
                    c_score, e_score, ttp,
                    metrics.get("queue_capacity_mw"),
                    metrics.get("queue_wait_months"),
                    metrics.get("reserve_margin_pct"),
                    metrics.get("gen_additions_12mo_mw"),
                    metrics.get("curtailment_pct"),
                    metrics.get("stranded_capacity_mw"),
                    metrics.get("emergency_count_30d") or 0,
                    _json.dumps(risks), _json.dumps(opps), verdict,
                ))
                wc.commit()
            inserted += 1
        except Exception as e:
            failed.append({"slug": slug,
                            "error": f"{type(e).__name__}: {str(e)[:160]}"})

    return jsonify({
        "ok":            True,
        "attempted":     len(missing),
        "inserted":      inserted,
        "failed_count":  len(failed),
        "failed_sample": failed[:5],
        "remaining_in_markets": max(0, sum(1 for m in MARKETS
                                            if _market_slug(m)) - len(present) - inserted),
        "hint":          ("Re-run if remaining_in_markets > 0 (cap is "
                           f"{max_n}/call). Idempotent — safe to cron."),
    }), 200
