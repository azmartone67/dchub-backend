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

# r-provenance-writer (2026-08-08): all three scored-row writers in this file
# were hand-copies of the recompute statement in routes/dcpi.py, and all three
# had fallen behind it. They now share one definition — see util/dcpi_score_row
# for what each copy had silently dropped.
from util.dcpi_score_row import update_scored_market, upsert_scored_market

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None


dcpi_freshness_bp = Blueprint("dcpi_freshness", __name__)

# ── live feed families (D1, 2026-09-02) ──────────────────────────────────
# The EU DCPI markets (frankfurt/paris/amsterdam/... — iso ENTSOE-<cc> in
# routes/dcpi.py) ride ONE upstream call. During the 2026-09-01 ENTSO-E 503
# outage every EU zone froze for >24h and this endpoint — the one the L23
# audit dim reads to decide whether DCPI is fresh — said nothing, because it
# only buckets market_power_scores.computed_at, and the recompute keeps running
# on frozen inputs. The live grid_data ages for the ENTSOE / EU_* streams are
# rolled up here through the same family rule freshness_public uses, so a dead
# EU feed is one named line on the DCPI freshness surface.
# Target: freshness_public.DOMAIN_SLA_HOURS["iso"] (4h) — upstream A75 lags
# 1-2h and the pull runs every 15 min.
_LIVE_FEED_TARGET_H = 4.0


def live_feeds_from_ages(rows, target_hours=_LIVE_FEED_TARGET_H):
    """PURE. rows = [(iso, age_hours|None)] for the ENTSOE / EU_* streams ->
    {"families": {...}, "stale": [family, ...]}. Empty rows -> no families,
    and `stale` is [] with `measured` False — absence is not "fresh"."""
    from routes.freshness_public import summarize_feed_families
    per_stream = [{"stream": str(iso), "age_hours": (float(age) if age is not None else None)}
                  for iso, age in (rows or [])]
    fams = summarize_feed_families(per_stream, float(target_hours))
    return {
        "families": fams,
        "stale": sorted(k for k, v in fams.items() if not v.get("live_feed_ok")),
        "measured": bool(fams),
        "target_hours": float(target_hours),
        "basis": ("MAX(grid_data.timestamp) per ENTSOE / EU_* stream, rolled up per "
                  "producer family (routes/freshness_public.summarize_feed_families). "
                  "Since 2026-09-02 that timestamp is the upstream observation time, "
                  "so age here is data age, not insert age."),
    }


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
            # D1 (2026-09-02): the live feed families behind the EU markets.
            cur.execute("""
                SELECT iso,
                       EXTRACT(EPOCH FROM (NOW() - MAX(timestamp)))/3600.0 AS age_hours
                  FROM grid_data
                 WHERE iso = %s OR iso LIKE %s
                 GROUP BY iso
            """, ("ENTSOE", "EU\\_%"))
            live_feeds = live_feeds_from_ages(
                [(r["iso"], r["age_hours"]) for r in cur.fetchall()])
        return jsonify({
            "ok":           True,
            "stats":        stats,
            "oldest_15":    oldest,
            "live_feeds":   live_feeds,
            "stale_live_feeds": live_feeds["stale"],
            "checked_at":   datetime.datetime.utcnow().isoformat() + "Z",
            "purpose":      ("Per-market DCPI freshness. Cron is supposed to "
                              "refresh all 300+ markets every 6h. Markets in "
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

    rescored = 0
    failed = []
    from util.iso_taxonomy import resolve_iso as _resolve_iso

    for row in stale_rows:
        slug = row["market_slug"]
        # r-iso-taxonomy (2026-07-28): resolve the label instead of echoing
        # the stored one. This path reconstructs the market from the DB row,
        # so it used to write row["iso"] straight back — meaning a market
        # that fell out of the canonical MARKETS list kept its wrong ISO
        # forever, immune to the corrected taxonomy. Exactly the markets
        # least likely to be noticed. `default=` keeps intl labels
        # (ENTSOE-*, AESO…) untouched, since resolve_iso only knows US.
        _iso = _resolve_iso(slug, row["state"], default=row["iso"])
        # Reconstruct the market tuple from the DB row (this is the
        # whole point — the canonical MARKETS list no longer has them
        # but the DB row preserved name/state/iso/lat/lon at last score)
        m = (slug, row["market_name"], row["state"], _iso,
             row["latitude"], row["longitude"])
        try:
            metrics = gather_metrics_for_market(m)
            c_score = compute_constraint_score(metrics)
            e_score = compute_excess_power_score(metrics)
            ttp     = estimate_time_to_power(metrics)
            verdict = derive_verdict(c_score, e_score)
            risks, opps = derive_top_signals(m, metrics, c_score, e_score)
            with _dcpi_conn() as wc, wc.cursor() as wcur:
                # r-provenance-writer (2026-08-08): shared with the recompute
                # in routes/dcpi.py. This is the path where a STALE stamp was
                # most reachable: re-scoring a stale market under today's
                # method while leaving method_version at whatever the last
                # daily sweep wrote. DCPI_METHOD_VERSION moved twice on
                # 2026-08-08 (2.1.0 r-universe-dedup, 2.2.0 r-radius-dedup),
                # both score-moving, so the window for a row to advertise a
                # version that did not produce its numbers was real.
                #
                # UPDATE-only, as before: `m` is reconstructed from the DB row
                # this loop just selected, so a 0 rowcount means the row went
                # away mid-run, not that a market needs creating.
                update_scored_market(wcur, m, metrics, c_score, e_score, ttp,
                                     verdict, risks, opps)
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

    try:
        with _dcpi_conn() as c, c.cursor() as cur:
            # r-provenance-writer (2026-08-08): was a hand-copy of the recompute
            # statement in routes/dcpi.py that had fallen three columns behind
            # it — no data_basis_json, no signal_tier, no method_version — so
            # re-scoring a market here left its provenance pinned to whatever
            # the LAST daily sweep stamped, i.e. a row advertising a method
            # version that did not produce its numbers. It also bound
            # `latitude=%s` with no COALESCE, which is exactly the regression
            # this file's own r-market-resolve-guard sentinel (below) exists to
            # catch, and never wrote iso_type at all. One shared writer, so all
            # three stay fixed without three people remembering.
            #
            # publish=False keeps the pre-existing behaviour that a market with
            # no row does not become publicly visible from a single admin
            # force-recompute; recompute-missing is the path that publishes.
            # It does NOT weaken the stamp — the triple is written either way.
            upsert_scored_market(cur, market_tup, metrics, c_score, e_score,
                                 ttp, verdict, risks, opps, publish=False)
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
                # r-provenance-writer (2026-08-08): ★ THIS is the writer that
                # published the 8 unattributed markets. Measured 09:24 UTC:
                # laurel, lenoir, luckey, maiden, modesto, monroe, salem and
                # west-chester carried constraint_score, excess_power_score and
                # verdict with method_version, signal_tier and data_basis_json
                # all NULL, inserted 08:51 UTC — this cron fires at 08:37 and
                # they are exactly the 8 rows dcpi_method 2.1.1 describes as
                # "new to the table" under r-universe-dedup.
                #
                # It was never a scoring bug: this endpoint runs the SAME
                # gather_metrics_for_market as the daily recompute, so all
                # three values were already sitting in `metrics`. The
                # hand-written INSERT above simply never listed the columns.
                # It is now the shared writer, which also gives these rows the
                # iso_type this copy never wrote.
                #
                # publish=True keeps r58's fix: `published` DEFAULTs to false
                # (the old comment here said NULL — either way the public
                # queries filter WHERE published=true), so a fresh row without
                # it stays invisible on /dcpi, which is the gap this whole
                # endpoint exists to close.
                upsert_scored_market(wcur, mt, metrics, c_score, e_score, ttp,
                                     verdict, risks, opps, publish=True)
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


# ---------------------------------------------------------------------------
# r-market-resolve-guard (2026-07-06): "stay fixed" sentinel for the two
# facility→market fixes (commits 0c297e05 + ee400bc0):
#   1. market_power_scores.latitude/longitude must stay populated (a future
#      writer without COALESCE, or _load_markets_dynamic dropping the median
#      centroid, would silently NULL them again → 198/317 regression).
#   2. A facility must resolve to its OWN metro, not collapse to an arbitrary
#      same-state row — the canonical symptom was Dallas→Midland-Odessa.
# GET returns status; when it detects a breach it files a brain_finding so the
# autopilot/consistency stream surfaces it. Public GET (read-only, no secrets).
# ---------------------------------------------------------------------------
@dcpi_freshness_bp.route("/api/v1/dcpi/resolution-guard", methods=["GET"])
def dcpi_resolution_guard():
    NULL_PCT_WARN = 5.0   # was 62% (198/317) before the fix; healthy is ~0.3%
    # (city, state, lat, lng, expected_slug, must_not_slug)
    CANARIES = [
        ("Dallas",  "TX", 32.78, -96.80, "dallas",  "midland-tx"),
        ("Houston", "TX", 29.76, -95.37, "houston", "midland-tx"),
        ("Austin",  "TX", 30.27, -97.74, "austin",  "midland-tx"),
    ]
    result = {"ok": True, "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
              "checks": {}, "breaches": []}

    # Check 1 — coord coverage.
    c = _conn()
    if c:
        try:
            with c, c.cursor() as cur:
                cur.execute("""
                    WITH latest AS (
                        SELECT DISTINCT ON (market_slug) market_slug, latitude, longitude
                          FROM market_power_scores ORDER BY market_slug, computed_at DESC)
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE latitude IS NULL OR longitude IS NULL)
                      FROM latest
                """)
                total, nulls = cur.fetchone()
            pct = round(100.0 * nulls / total, 2) if total else 0.0
            cov = {"total": total, "null_coords": nulls, "null_pct": pct,
                   "status": "warn" if pct > NULL_PCT_WARN else "ok"}
            result["checks"]["coord_coverage"] = cov
            if pct > NULL_PCT_WARN:
                result["breaches"].append(
                    f"market_power_scores coord coverage regressed: {nulls}/{total} "
                    f"({pct}%) NULL (>{NULL_PCT_WARN}% threshold)")
        except Exception as e:
            result["checks"]["coord_coverage"] = {"error": str(e)[:160]}

    # Check 2 — facility→market resolution canaries (Dallas must ≠ Midland).
    try:
        from routes.facility_profile_page import _market_dcpi
        canary_out = []
        for city, st, lat, lng, expect, forbid in CANARIES:
            row = _market_dcpi(city, st, lat, lng) or {}
            got = (row.get("market_slug") or "").lower()
            ok = (got == expect)
            collapsed = (got == forbid)
            canary_out.append({"city": city, "expected": expect, "got": got, "ok": ok})
            if collapsed or not ok:
                result["breaches"].append(
                    f"resolution regressed: {city},{st} → '{got or 'none'}' "
                    f"(expected '{expect}'"
                    + (f", COLLAPSED to forbidden '{forbid}'" if collapsed else "") + ")")
        result["checks"]["resolution_canaries"] = canary_out
    except Exception as e:
        result["checks"]["resolution_canaries"] = {"error": str(e)[:160]}

    if result["breaches"]:
        result["ok"] = False
        # File to brain_findings so the autopilot/consistency stream surfaces it.
        try:
            from routes.brain_findings_writer import upsert_brain_finding
            c2 = _conn()
            if c2:
                with c2, c2.cursor() as cur:
                    upsert_brain_finding(
                        cur,
                        issue="Facility→market resolution/coord regression",
                        url="/api/v1/dcpi/resolution-guard",
                        detail=" | ".join(result["breaches"])[:900],
                        detector="dcpi_resolution_guard",
                        status="open",
                    )
                    c2.commit()
        except Exception as e:
            result["finding_error"] = str(e)[:160]
    return jsonify(result), 200
