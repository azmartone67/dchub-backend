"""
mcp_funnel_diag.py — MCP signup funnel leak diagnostic.

Phase ZZZZZ-round36 (2026-05-24). The brain consistency radar flagged:
  - mcp_conversion_rate_below_floor
  - mcp_conversion_stale_critical (945 signals → 0 conversions 24h)
  - paywall_click_leak_critical
  - addressable_demand_unconverted: 2

This endpoint surfaces the funnel state so we can SEE where the leak
is happening (instead of just knowing it exists). Read-only — pulls
from mcp_upgrade_signals + mcp_conversions tables.
"""
import os
import datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

# NOTE: /api/v1/mcp/funnel is already owned by main.py with a richer
# implementation (22,900 tool calls/7d, 18,497 paywall signals, 0
# conversions, top-signal-tools breakdown). Our diagnostic endpoint
# stages-based view lives at /api/v1/mcp/funnel-stages so both surfaces
# coexist for different consumers.
mcp_funnel_bp = Blueprint("mcp_funnel", __name__,
                           url_prefix="/api/v1/mcp")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


# r51-clean (2026-05-26): mask synthetic traffic (our own dchub-selfheal
# CF worker + test harnesses) from funnel reads. mcp_upgrade_signals
# contains 3,248 rows over 7 days all from our monitoring bot — without
# this filter, conversion_rate looks like 0% forever. Mirrors the write-
# side filter in mcp_upgrade_gate._is_synthetic.
_NOT_SYNTHETIC_SQL = """
  AND (mcp_client IS NULL OR (
    LOWER(mcp_client) NOT LIKE 'dchub-%'
    AND LOWER(mcp_client) NOT LIKE 'step2_%'
    AND LOWER(mcp_client) NOT LIKE 'qa-%'
    AND LOWER(mcp_client) NOT LIKE 'probe-%'
    AND LOWER(mcp_client) NOT LIKE 'test-%'
    AND LOWER(mcp_client) NOT LIKE 'monitor-%'
    AND LOWER(mcp_client) NOT LIKE 'r51-%'
    AND LOWER(mcp_client) NOT LIKE 'r52-%'
    AND LOWER(mcp_client) NOT LIKE 'hn-prepost%'
    AND LOWER(mcp_client) NOT LIKE 'paywall-probe%'
    AND LOWER(mcp_client) NOT LIKE 'funnel-test%'
    AND LOWER(mcp_client) NOT LIKE 'e2e-%'
    AND LOWER(mcp_client) NOT LIKE 'recheck%'
    AND LOWER(mcp_client) NOT LIKE 'healthcheck%'
  ))
"""


@mcp_funnel_bp.route("/funnel-stages", methods=["GET"])
def funnel_diag():
    out = {
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "stages": {},
        "leaks": [],
    }
    if not (_pg and _dsn()):
        out["error"] = "no_db"
        return jsonify(out), 200

    # Stage 1: total MCP tool calls 24h (top of funnel)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(call_count),0)::int FROM mcp_tool_usage "
                "WHERE date >= CURRENT_DATE - INTERVAL '1 day'")
            out["stages"]["1_tool_calls_24h"] = cur.fetchone()[0]
    except Exception as e:
        out["stages"]["1_tool_calls_24h"] = {"_error": type(e).__name__}

    # Stage 2: paywall signals (limit_hit, tier_required)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT signal_type, COUNT(*)::int "
                "FROM mcp_upgrade_signals "
                "WHERE created_at > NOW() - INTERVAL '1 day' "
                "GROUP BY signal_type ORDER BY 2 DESC")
            out["stages"]["2_paywall_signals_24h"] = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        out["stages"]["2_paywall_signals_24h"] = {"_error": type(e).__name__}

    # Stage 3: signals with email (identified users)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*)::int FROM mcp_upgrade_signals "
                "WHERE created_at > NOW() - INTERVAL '7 days' "
                "AND user_email IS NOT NULL AND user_email != ''")
            out["stages"]["3_identified_signals_7d"] = cur.fetchone()[0]
    except Exception as e:
        out["stages"]["3_identified_signals_7d"] = {"_error": type(e).__name__}

    # Stage 4: outreach sent
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*)::int FROM mcp_upgrade_signals "
                "WHERE outreach_sent = TRUE AND outreach_sent_at > NOW() - INTERVAL '7 days'")
            out["stages"]["4_outreach_sent_7d"] = cur.fetchone()[0]
    except Exception as e:
        out["stages"]["4_outreach_sent_7d"] = {"_error": type(e).__name__}

    # Stage 5: actual conversions
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT plan_to, COUNT(*)::int, COALESCE(SUM(mrr_cents),0)::int "
                "FROM mcp_conversions "
                "WHERE created_at > NOW() - INTERVAL '30 days' "
                "GROUP BY plan_to")
            out["stages"]["5_conversions_30d"] = [
                {"plan": r[0], "count": r[1], "mrr_cents": r[2]}
                for r in cur.fetchall()
            ]
    except Exception as e:
        out["stages"]["5_conversions_30d"] = {"_error": type(e).__name__}

    # Top tools triggering paywall hits (sales lead intel)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT tool_requested, COUNT(*)::int "
                "FROM mcp_upgrade_signals "
                "WHERE created_at > NOW() - INTERVAL '7 days' "
                "AND tool_requested IS NOT NULL "
                "GROUP BY tool_requested ORDER BY 2 DESC LIMIT 10")
            out["top_tools_blocked_7d"] = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        out["top_tools_blocked_7d"] = {"_error": type(e).__name__}

    # Leak detection
    s = out["stages"]
    sig_24h = s.get("2_paywall_signals_24h", {})
    if isinstance(sig_24h, dict):
        total_24h = sum(v for v in sig_24h.values() if isinstance(v, int))
        if total_24h > 100 and not s.get("5_conversions_30d"):
            out["leaks"].append({
                "name": "no_conversions_despite_signals",
                "severity": "critical",
                "detail": f"{total_24h} paywall signals 24h, 0 conversions 30d",
                "likely_cause": "Stripe price IDs not wired, /pricing CTA broken, or upgrade_url 401-locking",
            })
    ident = s.get("3_identified_signals_7d")
    outr = s.get("4_outreach_sent_7d")
    if isinstance(ident, int) and isinstance(outr, int):
        if ident > 0 and outr == 0:
            out["leaks"].append({
                "name": "identified_users_no_outreach",
                "severity": "high",
                "detail": f"{ident} users left email, 0 received outreach in 7d",
                "likely_cause": "Outreach cron disabled or SENDGRID/RESEND env vars unset",
            })

    return jsonify(out), 200


@mcp_funnel_bp.route("/signal-attribution", methods=["GET"])
def signal_attribution():
    """Phase r51-attr (2026-05-26). The anon-ua-breakdown showed all 3,299
    paywall hits have (null IP, empty UA). Before optimizing further, we
    need to know WHERE those rows are written from. This exposes raw
    distributions: signal_type, has_ip%, has_ua%, has_session%, has_email%,
    tool concentration, and the latest 10 raw rows (with hash-redacted IPs)
    so we can see the actual shape of what's being captured.
    """
    if _pg is None:
        return jsonify({"error": "psycopg2 not available"}), 500
    import hashlib
    out = {"at": datetime.datetime.utcnow().isoformat() + "Z"}
    try:
        with _conn() as c, c.cursor() as cur:
            # Signal-type distribution
            cur.execute(
                """SELECT COALESCE(signal_type, '(null)'), COUNT(*)
                     FROM mcp_upgrade_signals
                    WHERE created_at > NOW() - INTERVAL '7 days'""" + _NOT_SYNTHETIC_SQL + """
                 GROUP BY 1 ORDER BY 2 DESC"""
            )
            out["signal_types_7d"] = {r[0]: int(r[1]) for r in cur.fetchall()}
            # Capture quality
            cur.execute(
                """SELECT
                     COUNT(*) AS total,
                     COUNT(*) FILTER (WHERE ip_address IS NOT NULL AND ip_address <> '') AS has_ip,
                     COUNT(*) FILTER (WHERE user_agent IS NOT NULL AND user_agent <> '') AS has_ua,
                     COUNT(*) FILTER (WHERE session_id IS NOT NULL AND session_id <> '') AS has_session,
                     COUNT(*) FILTER (WHERE user_email IS NOT NULL AND user_email <> '') AS has_email,
                     COUNT(*) FILTER (WHERE mcp_client IS NOT NULL AND mcp_client NOT IN ('','mcp','unknown')) AS has_client
                   FROM mcp_upgrade_signals
                  WHERE created_at > NOW() - INTERVAL '7 days'""" + _NOT_SYNTHETIC_SQL
            )
            r = cur.fetchone()
            total = max(int(r[0] or 0), 1)
            out["capture_quality_7d"] = {
                "total":       int(r[0] or 0),
                "has_ip_pct":      round(100.0 * (r[1] or 0) / total, 1),
                "has_ua_pct":      round(100.0 * (r[2] or 0) / total, 1),
                "has_session_pct": round(100.0 * (r[3] or 0) / total, 1),
                "has_email_pct":   round(100.0 * (r[4] or 0) / total, 1),
                "has_client_pct":  round(100.0 * (r[5] or 0) / total, 1),
            }
            # 10 latest raw rows (UA/IP-hash redacted)
            cur.execute(
                """SELECT created_at, signal_type, tool_requested, tier_current,
                          session_id, user_agent, ip_address, mcp_client, message_shown
                     FROM mcp_upgrade_signals
                    WHERE created_at > NOW() - INTERVAL '7 days'""" + _NOT_SYNTHETIC_SQL + """
                 ORDER BY created_at DESC LIMIT 10"""
            )
            latest = []
            for created, st, tool, tier, sid, ua, ip, mc, msg in cur.fetchall():
                ip_clean = (str(ip or '').split(',')[0].strip())
                ip_hash = hashlib.sha256(ip_clean.encode('utf-8')).hexdigest()[:12] if ip_clean else None
                latest.append({
                    "at":             created.isoformat() if created else None,
                    "signal_type":    st,
                    "tool":           tool,
                    "tier":           tier,
                    "session_id":     (sid or '')[:16] + "..." if sid else None,
                    "ip_hash":        ip_hash,
                    "user_agent":     (ua or '')[:80],
                    "mcp_client":     mc,
                    "msg_first_80c":  (msg or '')[:80],
                })
            out["latest_10"] = latest
            # Top distinct session_ids by hit count — same session repeatedly?
            cur.execute(
                """SELECT COALESCE(NULLIF(session_id, ''), '(null)'), COUNT(*)
                     FROM mcp_upgrade_signals
                    WHERE created_at > NOW() - INTERVAL '7 days'""" + _NOT_SYNTHETIC_SQL + """
                 GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""
            )
            out["top_sessions_7d"] = [
                {"session_prefix": (s[:16] + "...") if s and s != '(null)' else s, "hits": int(n)}
                for s, n in cur.fetchall()
            ]
        return jsonify(out), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@mcp_funnel_bp.route("/backfill-empty-city", methods=["GET", "POST"])
def backfill_empty_city():
    """Phase r-citybackfill (2026-05-27). ~5,382 of 19,542 mapable rows in
    discovered_facilities have city='' or NULL — 27% of the dataset.
    Search misses them entirely ('reno' returns 2 instead of 48). Many are
    real named facilities (Meta Gallatin, Facebook Altoona, etc.) that
    were ingested with lat/lon but no reverse-geocoded address.

    Backfills city + state + market by finding the nearest non-empty-city
    facility within ~25km via Euclidean distance on lat/lon. Idempotent.

    Dry-run by default. Pass ?execute=1 to commit. Internal-key required.
    Pass ?limit=N (default 100) to bound how many rows are updated per
    call (the LATERAL nearest-neighbor join is O(N×M) — running 5K rows
    at once could timeout the Railway request).
    """
    from flask import request as _req
    import os as _os
    _sent = _req.headers.get("X-Internal-Key", "") or ""
    _allowed = {"dchub-internal-sync-2026"}
    for _name in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY"):
        _v = _os.environ.get(_name)
        if _v:
            _allowed.add(_v)
    if not _sent or _sent not in _allowed:
        return jsonify({"error": "forbidden"}), 403
    if _pg is None:
        return jsonify({"error": "psycopg2 not available"}), 500

    execute = (_req.args.get("execute") or "").lower() in ("1", "true", "yes")
    try:
        limit = max(1, min(int(_req.args.get("limit") or 100), 1000))
    except (TypeError, ValueError):
        limit = 100

    try:
        with _conn() as c, c.cursor() as cur:
            # First — how many candidates are there?
            cur.execute("""
                SELECT COUNT(*) FROM discovered_facilities
                 WHERE (city IS NULL OR city = '')
                   AND latitude IS NOT NULL AND longitude IS NOT NULL
            """)
            total_empty = int((cur.fetchone() or [0])[0])

            # Dry-run preview: top 3 (empty row, nearest match) pairs
            cur.execute("""
                SELECT e.id, e.name, e.latitude, e.longitude,
                       n.city, n.state, n.market, n.name AS donor_name,
                       SQRT(POWER(n.latitude - e.latitude, 2)
                          + POWER(n.longitude - e.longitude, 2)) AS dist_deg
                  FROM discovered_facilities e
                  CROSS JOIN LATERAL (
                    SELECT city, state, market, name, latitude, longitude
                      FROM discovered_facilities
                     WHERE (city IS NOT NULL AND city <> '')
                       AND latitude IS NOT NULL AND longitude IS NOT NULL
                       AND NOT (latitude = 0 AND longitude = 0)
                       AND ABS(latitude - e.latitude) < 0.5
                       AND ABS(longitude - e.longitude) < 0.5
                     ORDER BY POWER(latitude - e.latitude, 2)
                            + POWER(longitude - e.longitude, 2) ASC
                     LIMIT 1
                  ) n
                 WHERE (e.city IS NULL OR e.city = '')
                   AND e.latitude IS NOT NULL AND e.longitude IS NOT NULL
                   AND NOT (e.latitude = 0 AND e.longitude = 0)
                 LIMIT 3
            """)
            preview_rows = []
            for r in cur.fetchall():
                preview_rows.append({
                    "id":          r[0],
                    "empty_name":  r[1],
                    "lat":         float(r[2]) if r[2] is not None else None,
                    "lon":         float(r[3]) if r[3] is not None else None,
                    "donor_city":  r[4],
                    "donor_state": r[5],
                    "donor_market": r[6],
                    "donor_name":  r[7],
                    "dist_deg":    round(float(r[8] or 0), 4),
                })

            result = {
                "total_empty_city": total_empty,
                "dry_run":          not execute,
                "rows_updated":     0,
                "limit":            limit,
                "sample_matches":   preview_rows,
            }

            if execute and total_empty > 0:
                # Pick `limit` rows, find their nearest donor, update in one shot.
                # The CTE materializes the picks so the UPDATE doesn't re-run the
                # LATERAL for each row.
                cur.execute("""
                    WITH picks AS (
                      SELECT e.id AS empty_id, n.city, n.state, n.market
                        FROM discovered_facilities e
                        CROSS JOIN LATERAL (
                          SELECT city, state, market
                            FROM discovered_facilities
                           WHERE (city IS NOT NULL AND city <> '')
                             AND latitude IS NOT NULL AND longitude IS NOT NULL
                             AND ABS(latitude - e.latitude) < 0.5
                             AND ABS(longitude - e.longitude) < 0.5
                           ORDER BY POWER(latitude - e.latitude, 2)
                                  + POWER(longitude - e.longitude, 2) ASC
                           LIMIT 1
                        ) n
                       WHERE (e.city IS NULL OR e.city = '')
                         AND e.latitude IS NOT NULL AND e.longitude IS NOT NULL
                       LIMIT %s
                    )
                    UPDATE discovered_facilities df
                       SET city   = COALESCE(NULLIF(df.city, ''), picks.city),
                           state  = COALESCE(NULLIF(df.state, ''), picks.state),
                           market = COALESCE(df.market, picks.market)
                      FROM picks
                     WHERE df.id = picks.empty_id
                """, (limit,))
                result["rows_updated"] = cur.rowcount
                c.commit()

        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@mcp_funnel_bp.route("/backfill-market-by-bbox", methods=["POST"])
def backfill_market_by_bbox():
    """Phase r-marketbackfill (2026-05-27). The post-city-backfill state
    left McCarran-tagged facilities in the Reno corridor invisible to a
    'reno' search because the matchesFacility frontend logic checks each
    field as substring. McCarran is administratively distinct but
    functionally Reno metro. Same issue affects every multi-city US
    metro (Ashburn corridor, Dallas-Plano-Frisco, etc.).

    This endpoint takes a JSON body { bbox: [W,S,E,N], market: "Name" }
    and stamps that market on every row inside the bbox whose market is
    null / empty / 'Unknown'.

    Dry-run by default. ?execute=1 commits. Internal-key required.
    """
    from flask import request as _req
    import os as _os
    _sent = _req.headers.get("X-Internal-Key", "") or ""
    _allowed = {"dchub-internal-sync-2026"}
    for _name in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY"):
        _v = _os.environ.get(_name)
        if _v:
            _allowed.add(_v)
    if not _sent or _sent not in _allowed:
        return jsonify({"error": "forbidden"}), 403
    if _pg is None:
        return jsonify({"error": "psycopg2 not available"}), 500

    body = _req.get_json(silent=True) or {}
    bbox = body.get("bbox") or []
    market = (body.get("market") or "").strip()
    if (not isinstance(bbox, list) or len(bbox) != 4 or
        not market or len(market) > 80):
        return jsonify({"error": "expected {bbox:[W,S,E,N], market:'Name'}"}), 400
    try:
        w, s, e, n = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return jsonify({"error": "bbox must be 4 floats"}), 400
    if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= 90 and -90 <= n <= 90 and w < e and s < n):
        return jsonify({"error": "invalid bbox"}), 400

    execute = (_req.args.get("execute") or "").lower() in ("1", "true", "yes")
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM discovered_facilities
                 WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                   AND longitude BETWEEN %s AND %s
                   AND latitude BETWEEN %s AND %s
                   AND (market IS NULL OR market = '' OR LOWER(market) = 'unknown')
            """, (w, e, s, n))
            candidates = int((cur.fetchone() or [0])[0])
            result = {
                "bbox":          [w, s, e, n],
                "market":        market,
                "candidates":    candidates,
                "dry_run":       not execute,
                "rows_updated":  0,
            }
            if execute and candidates > 0:
                cur.execute("""
                    UPDATE discovered_facilities
                       SET market = %s
                     WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                       AND longitude BETWEEN %s AND %s
                       AND latitude BETWEEN %s AND %s
                       AND (market IS NULL OR market = '' OR LOWER(market) = 'unknown')
                """, (market, w, e, s, n))
                result["rows_updated"] = cur.rowcount
                c.commit()
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@mcp_funnel_bp.route("/backfill-market-from-city", methods=["POST"])
def backfill_market_from_city():
    """Phase r-marketfromcity (2026-05-27). After the bbox-based market
    backfill covered ~150 major DC metros, ~5,820 rows still had no
    market. They're long-tail cities outside any bbox. This endpoint
    copies city → market for all rows where market is null/empty/Unknown
    AND city is non-empty. Every facility gets SOME market label so
    search has at least one searchable token.

    Idempotent. Internal-key required. Dry-run by default, ?execute=1
    commits.
    """
    from flask import request as _req
    import os as _os
    _sent = _req.headers.get("X-Internal-Key", "") or ""
    _allowed = {"dchub-internal-sync-2026"}
    for _name in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY"):
        _v = _os.environ.get(_name)
        if _v:
            _allowed.add(_v)
    if not _sent or _sent not in _allowed:
        return jsonify({"error": "forbidden"}), 403
    if _pg is None:
        return jsonify({"error": "psycopg2 not available"}), 500
    execute = (_req.args.get("execute") or "").lower() in ("1", "true", "yes")
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM discovered_facilities
                 WHERE (market IS NULL OR market = '' OR LOWER(market) = 'unknown')
                   AND city IS NOT NULL AND city <> ''
            """)
            candidates = int((cur.fetchone() or [0])[0])
            result = {"candidates": candidates, "dry_run": not execute, "rows_updated": 0}
            if execute and candidates > 0:
                cur.execute("""
                    UPDATE discovered_facilities
                       SET market = city
                     WHERE (market IS NULL OR market = '' OR LOWER(market) = 'unknown')
                       AND city IS NOT NULL AND city <> ''
                """)
                result["rows_updated"] = cur.rowcount
                c.commit()
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@mcp_funnel_bp.route("/users/inspect", methods=["GET"])
def users_inspect():
    """Phase r-userinsp (2026-05-27). Who is using DC Hub, what are they
    consuming, and where's the upgrade opportunity?

    Returns three buckets:
      1. tier_counts — total users at each tier
      2. paying_customers — every Stripe-active user with their tool mix
      3. high_activity_anon — anonymous sessions with >5 calls (warm leads)
      4. upgrade_candidates — free-key users approaching daily limits
         (top conversion opportunities for Starter pitch)

    Emails masked (first 3 chars + @domain) so this is safe to share.
    Internal-key required.
    """
    from flask import request as _req
    import os as _os
    _sent = _req.headers.get("X-Internal-Key", "") or ""
    _allowed = {"dchub-internal-sync-2026"}
    for _name in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY"):
        _v = _os.environ.get(_name)
        if _v:
            _allowed.add(_v)
    if not _sent or _sent not in _allowed:
        return jsonify({"error": "forbidden"}), 403
    if _pg is None:
        return jsonify({"error": "psycopg2 not available"}), 500

    def _mask_email(e):
        if not e or "@" not in str(e):
            return None
        e = str(e)
        local, _, domain = e.partition("@")
        return (local[:3] + "***@" + domain) if local else e

    out = {"at": datetime.datetime.utcnow().isoformat() + "Z"}
    try:
        with _conn() as c, c.cursor() as cur:
            # ── 1. Tier counts (everyone with a dev key) ──
            cur.execute("""
                SELECT COALESCE(tier, 'free') AS tier, COUNT(*) AS n
                  FROM mcp_dev_keys
                 WHERE status = 'active'
                 GROUP BY 1 ORDER BY 2 DESC
            """)
            out["tier_counts"] = {r[0]: int(r[1]) for r in cur.fetchall()}

            # ── 2. Paying customers (Stripe-active) with tool mix ──
            # mcp_conversions has plan/mrr. Join to mcp_call_log for usage.
            try:
                cur.execute("""
                    SELECT email, plan, mrr_cents, created_at,
                           EXTRACT(DAY FROM NOW() - created_at)::int AS days_old
                      FROM mcp_conversions
                     WHERE mrr_cents > 0
                       OR plan ILIKE '%research%' OR plan ILIKE '%founding%'
                     ORDER BY mrr_cents DESC NULLS LAST
                     LIMIT 25
                """)
                paying = []
                for email, plan, mrr_cents, created_at, days_old in cur.fetchall():
                    paying.append({
                        "email":      _mask_email(email),
                        "plan":       plan,
                        "mrr_usd":    round((mrr_cents or 0) / 100.0, 2),
                        "days_active": int(days_old or 0),
                        "created_at": created_at.isoformat() if created_at else None,
                    })
                out["paying_customers"] = paying
                out["paying_customer_count"] = len(paying)
            except Exception as e:
                out["paying_customers_error"] = str(e)[:160]

            # ── 3. Free-key users with high tool-call volume (upgrade candidates) ──
            # mcp_dev_keys has the keys; mcp_call_log has the activity.
            try:
                cur.execute("""
                    WITH usage AS (
                      SELECT api_key, COUNT(*) AS calls,
                             COUNT(DISTINCT tool) AS distinct_tools,
                             MAX(timestamp) AS last_seen,
                             array_agg(DISTINCT tool ORDER BY tool) AS tools
                        FROM mcp_call_log
                       WHERE api_key IS NOT NULL AND api_key <> ''
                         AND timestamp > NOW() - INTERVAL '30 days'
                       GROUP BY api_key
                    )
                    SELECT k.email, k.tier, k.created_at,
                           u.calls, u.distinct_tools, u.last_seen, u.tools
                      FROM mcp_dev_keys k
                      JOIN usage u ON u.api_key = k.api_key
                     WHERE k.status = 'active'
                       AND COALESCE(k.tier, 'free') = 'free'
                     ORDER BY u.calls DESC
                     LIMIT 25
                """)
                upgrades = []
                for email, tier, created_at, calls, dt, last_seen, tools in cur.fetchall():
                    upgrades.append({
                        "email":          _mask_email(email),
                        "tier":           tier or "free",
                        "calls_30d":      int(calls or 0),
                        "distinct_tools": int(dt or 0),
                        "last_seen":      last_seen.isoformat() if last_seen else None,
                        "top_tools":      list(tools or [])[:5],
                        "upgrade_pitch":  (
                            f"{int(calls or 0)} calls/30d ≈ {int(calls or 0) // 30}/day. "
                            + ("Hitting limits — pitch $9 Starter (10K/day)." if (calls or 0) >= 500 else
                               "Engaged user — pitch $49 Developer once they hit caps.")
                        ),
                    })
                out["upgrade_candidates"] = upgrades
            except Exception as e:
                out["upgrade_candidates_error"] = str(e)[:160]

            # ── 4. Anonymous sessions with activity (warm leads, pre-email) ──
            try:
                cur.execute("""
                    SELECT platform, session_id, COUNT(*) AS calls,
                           array_agg(DISTINCT tool ORDER BY tool) AS tools,
                           MAX(timestamp) AS last_seen
                      FROM mcp_call_log
                     WHERE (api_key IS NULL OR api_key = '')
                       AND timestamp > NOW() - INTERVAL '7 days'
                       AND platform NOT LIKE 'dchub-%'
                       AND platform NOT LIKE 'loop%'
                       AND platform NOT LIKE 'r51%'
                       AND platform NOT LIKE 'r52%'
                       AND platform NOT LIKE 'leak%'
                       AND platform NOT LIKE '%test%'
                       AND platform NOT LIKE '%probe%'
                       AND platform NOT LIKE 'gate-audit%'
                       AND platform NOT LIKE 'trial-%'
                     GROUP BY platform, session_id
                    HAVING COUNT(*) > 3
                     ORDER BY calls DESC
                     LIMIT 20
                """)
                anons = []
                for platform, sid, calls, tools, last_seen in cur.fetchall():
                    anons.append({
                        "platform":   platform,
                        "session":    (sid or "")[:16] + "..." if sid else None,
                        "calls_7d":   int(calls or 0),
                        "top_tools":  list(tools or [])[:5],
                        "last_seen":  last_seen.isoformat() if last_seen else None,
                    })
                out["high_activity_anon"] = anons
            except Exception as e:
                out["high_activity_anon_error"] = str(e)[:160]

            # ── 5. Tool-mix across all users (what's actually consumed) ──
            try:
                cur.execute("""
                    SELECT tool, COUNT(*) AS calls,
                           COUNT(DISTINCT api_key) AS unique_users
                      FROM mcp_call_log
                     WHERE timestamp > NOW() - INTERVAL '7 days'
                       AND tool IS NOT NULL AND tool <> ''
                     GROUP BY 1 ORDER BY 2 DESC LIMIT 15
                """)
                out["top_tools_7d"] = [
                    {"tool": r[0], "calls": int(r[1]), "unique_users": int(r[2] or 0)}
                    for r in cur.fetchall()
                ]
            except Exception as e:
                out["top_tools_7d_error"] = str(e)[:160]

        return jsonify(out), 200
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@mcp_funnel_bp.route("/anon-ua-breakdown", methods=["GET"])
def anon_ua_breakdown():
    """Phase r51-cluster (2026-05-26). 99.7% of paywall hits classify as
    mcp_client='mcp' (no clientInfo on init). To decide whether to keep
    optimizing UI prompts or pivot to programmatic-consumer outreach,
    we need to know if those hits are 3 IPs hammering or 300 diffuse.

    Returns the top 50 (ip_hash, user_agent) pairs from the unclassified
    bucket over the last 7 days, with counts. ip_hash is sha256-prefix
    so we don't leak full IPs in the response.
    """
    if _pg is None:
        return jsonify({"error": "psycopg2 not available"}), 500
    import hashlib
    try:
        rows = []
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT ip_address, user_agent, mcp_client,
                       COUNT(*) AS hits,
                       MIN(created_at) AS first_seen,
                       MAX(created_at) AS last_seen
                  FROM mcp_upgrade_signals
                 WHERE created_at > NOW() - INTERVAL '7 days'
                   AND (mcp_client IS NULL OR mcp_client IN ('mcp', 'unknown', ''))
                 GROUP BY 1, 2, 3
                 ORDER BY hits DESC
                 LIMIT 50
                """
            )
            for ip, ua, mc, hits, first_seen, last_seen in cur.fetchall():
                ip_clean = (str(ip or '').split(',')[0].strip())
                ip_hash = hashlib.sha256(ip_clean.encode('utf-8')).hexdigest()[:12] if ip_clean else None
                rows.append({
                    "ip_hash":    ip_hash,
                    "user_agent": (ua or '')[:200],
                    "mcp_client": mc,
                    "hits":       int(hits),
                    "first_seen": first_seen.isoformat() if first_seen else None,
                    "last_seen":  last_seen.isoformat() if last_seen else None,
                })
        total = sum(r["hits"] for r in rows)
        top5_pct = (sum(r["hits"] for r in rows[:5]) * 100.0 / total) if total else 0.0
        return jsonify({
            "at":                     datetime.datetime.utcnow().isoformat() + "Z",
            "rows":                   rows,
            "total_hits_in_sample":   total,
            "distinct_ip_ua_pairs":   len(rows),
            "top5_concentration_pct": round(top5_pct, 1),
            "interpretation": (
                "Top-5 >=80% = handful of programmatic consumers (email-outreach play). "
                "Top-5 <30% = diffuse traffic (UI/discovery play). "
                "30-80% = mixed."
            ),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
