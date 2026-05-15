"""
changes_feed.py — Phase GG (2026-05-14): `get_changes_since` diff feed.

The agent persists the `generated_at` timestamp from a previous /agent/index
call, then calls this endpoint with `?since=<that-timestamp>`. We return a
diff across the major domains so the agent can skip re-pulling everything
just to detect what's new.

Endpoints:
    GET /api/v1/changes/since?since=<ISO-8601>&limit=N

If `since` is omitted, defaults to 24h ago. Hard ceiling of 30 days back
(any older and the agent should just do a fresh full pull).

All read-only. Best-effort per domain — if one table errors, we still
return the others.
"""
import os
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request

try:
    from util.provenance import src, attach_sources, now_iso
except Exception:
    def src(claim, source, observed_at=None, url=None):
        return {"claim": claim, "source": source,
                "observed_at": observed_at.isoformat() if hasattr(observed_at, 'isoformat') else observed_at,
                "url": url}
    def attach_sources(p, s, generated_at=None):
        out = dict(p) if isinstance(p, dict) else {"result": p}
        out["sources"] = [x for x in (s or []) if x]
        out["generated_at"] = generated_at or datetime.now(timezone.utc).isoformat()
        return out
    def now_iso():
        return datetime.now(timezone.utc).isoformat()

changes_feed_bp = Blueprint("changes_feed", __name__)


def _conn():
    import psycopg2
    # autocommit so one failed domain query doesn't poison the rest.
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


def _safe(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception:
        return []


def _parse_since(raw):
    """Parse a since= param robustly. Returns a tz-aware datetime."""
    now = datetime.now(timezone.utc)
    if not raw:
        return now - timedelta(hours=24), "default-24h"
    try:
        # Allow plain "24h" / "7d" shorthand
        s = raw.strip().lower()
        if s.endswith("h") and s[:-1].isdigit():
            return now - timedelta(hours=int(s[:-1])), f"shorthand-{s}"
        if s.endswith("d") and s[:-1].isdigit():
            return now - timedelta(days=int(s[:-1])), f"shorthand-{s}"
        # ISO-8601 with or without 'Z'
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Clamp: no further back than 30 days
        floor = now - timedelta(days=30)
        if dt < floor:
            return floor, "clamped-30d"
        return dt, "iso"
    except Exception:
        return now - timedelta(hours=24), "parse-error-fallback-24h"


@changes_feed_bp.route("/api/v1/changes/since", methods=["GET"])
def changes_since():
    """Cross-domain diff feed.

    Query params:
        since   ISO-8601 timestamp OR shorthand "24h" / "7d". Defaults to 24h.
        limit   max rows per domain (default 25, max 200)
    """
    raw_since = request.args.get("since") or request.args.get("ts") or ""
    since, since_mode = _parse_since(raw_since)
    try:
        limit = int(request.args.get("limit") or 25)
    except Exception:
        limit = 25
    limit = max(1, min(limit, 200))

    payload = {"ok": True, "since": since.isoformat(),
               "since_mode": since_mode, "limit_per_domain": limit}
    diff = {}
    counts = {}
    sources = []

    try:
        with _conn() as c, c.cursor() as cur:
            # Pipeline: new projects (use first_seen as the new-row signal)
            try:
                rows = _safe(cur, """
                    SELECT operator, market, capacity_mw, phase, status,
                           completion_date, first_seen
                      FROM capacity_pipeline
                     WHERE first_seen IS NOT NULL AND first_seen > %s
                     ORDER BY first_seen DESC LIMIT %s""", (since, limit))
                pipeline_new = [{
                    "operator": r[0], "market": r[1],
                    "capacity_mw": float(r[2]) if r[2] is not None else None,
                    "phase": r[3], "status": r[4],
                    "completion_date": r[5].isoformat() if r[5] and hasattr(r[5], 'isoformat') else r[5],
                    "first_seen": r[6].isoformat() if r[6] else None,
                } for r in rows]
                diff["pipeline_new"] = pipeline_new
                counts["pipeline_new"] = len(pipeline_new)
                if pipeline_new:
                    sources.append(src(f"{len(pipeline_new)} new pipeline projects",
                                       "capacity_pipeline", pipeline_new[0]["first_seen"]))
            except Exception:
                diff["pipeline_new"] = []

            # News: new articles
            try:
                rows = _safe(cur, """
                    SELECT title, url, published_date, source
                      FROM news
                     WHERE published_date IS NOT NULL AND published_date > %s
                     ORDER BY published_date DESC LIMIT %s""", (since, limit))
                news_new = [{
                    "title": r[0], "url": r[1],
                    "published_date": r[2].isoformat() if r[2] and hasattr(r[2], 'isoformat') else r[2],
                    "source": r[3],
                } for r in rows]
                diff["news_new"] = news_new
                counts["news_new"] = len(news_new)
                if news_new:
                    sources.append(src(f"{len(news_new)} new articles",
                                       "news", news_new[0]["published_date"]))
            except Exception:
                diff["news_new"] = []

            # DCPI: re-scored markets (flip detection — verdict changed)
            try:
                rows = _safe(cur, """
                    SELECT market_slug, verdict, excess_power_score,
                           constraint_score, time_to_power_months, computed_at
                      FROM market_power_scores
                     WHERE computed_at IS NOT NULL AND computed_at > %s
                     ORDER BY computed_at DESC LIMIT %s""", (since, limit))
                dcpi_changes = [{
                    "market_slug": r[0], "verdict": r[1],
                    "excess_power_score": float(r[2]) if r[2] is not None else None,
                    "constraint_score": float(r[3]) if r[3] is not None else None,
                    "time_to_power_months": float(r[4]) if r[4] is not None else None,
                    "computed_at": r[5].isoformat() if r[5] else None,
                } for r in rows]
                diff["dcpi_rescores"] = dcpi_changes
                counts["dcpi_rescores"] = len(dcpi_changes)
                if dcpi_changes:
                    sources.append(src(f"{len(dcpi_changes)} DCPI re-scores",
                                       "market_power_scores", dcpi_changes[0]["computed_at"]))
            except Exception:
                diff["dcpi_rescores"] = []

            # Transactions: new deals
            try:
                rows = _safe(cur, """
                    SELECT target, acquirer, value_usd, announced_date,
                           market, deal_type
                      FROM transactions
                     WHERE announced_date IS NOT NULL AND announced_date > %s
                     ORDER BY announced_date DESC LIMIT %s""", (since, limit))
                tx_new = [{
                    "target": r[0], "acquirer": r[1],
                    "value_usd": float(r[2]) if r[2] is not None else None,
                    "announced_date": r[3].isoformat() if r[3] and hasattr(r[3], 'isoformat') else r[3],
                    "market": r[4], "deal_type": r[5],
                } for r in rows]
                diff["transactions_new"] = tx_new
                counts["transactions_new"] = len(tx_new)
                if tx_new:
                    sources.append(src(f"{len(tx_new)} new transactions",
                                       "transactions", tx_new[0]["announced_date"]))
            except Exception:
                diff["transactions_new"] = []

            # Pocket listings: newly created
            try:
                rows = _safe(cur, """
                    SELECT slug, title, market, state, capacity_mw, status,
                           tier_required, created_at
                      FROM exclusive_listings
                     WHERE created_at IS NOT NULL AND created_at > %s
                       AND status IN ('public', 'pocket')
                     ORDER BY created_at DESC LIMIT %s""", (since, limit))
                li_new = [{
                    "slug": r[0], "title": r[1], "market": r[2],
                    "state": r[3],
                    "capacity_mw": float(r[4]) if r[4] is not None else None,
                    "status": r[5], "tier_required": r[6],
                    "created_at": r[7].isoformat() if r[7] else None,
                } for r in rows]
                diff["pocket_listings_new"] = li_new
                counts["pocket_listings_new"] = len(li_new)
                if li_new:
                    sources.append(src(f"{len(li_new)} new pocket listings",
                                       "exclusive_listings", li_new[0]["created_at"]))
            except Exception:
                diff["pocket_listings_new"] = []

            # Facilities: newly discovered
            try:
                rows = _safe(cur, """
                    SELECT id, name, provider, city, state, power_mw, first_seen
                      FROM facilities
                     WHERE first_seen IS NOT NULL AND first_seen > %s
                     ORDER BY first_seen DESC LIMIT %s""", (since, limit))
                fac_new = [{
                    "id": r[0], "name": r[1], "provider": r[2],
                    "city": r[3], "state": r[4],
                    "power_mw": float(r[5]) if r[5] is not None else None,
                    "first_seen": r[6].isoformat() if r[6] else None,
                } for r in rows]
                diff["facilities_new"] = fac_new
                counts["facilities_new"] = len(fac_new)
                if fac_new:
                    sources.append(src(f"{len(fac_new)} newly discovered facilities",
                                       "facilities", fac_new[0]["first_seen"]))
            except Exception:
                diff["facilities_new"] = []
    except Exception as e:
        payload["error_partial"] = str(e)[:200]

    payload["counts"] = counts
    payload["total_changes"] = sum(counts.values())
    payload["diff"] = diff
    payload["agent_tip"] = (
        "Cache this response's `generated_at`. On your next session, "
        "pass it back as `?since=<that-value>` to get only what's new. "
        "If total_changes is 0, your previous full pull is still fresh.")
    payload["drill_deeper"] = {
        "pipeline_full":      "/api/v1/pipeline",
        "news_full":          "/api/v1/news",
        "dcpi_full":          "/api/v1/dcpi/scores",
        "transactions_full":  "/api/v1/transactions",
        "listings_full":      "/api/v1/listings",
    }
    return jsonify(attach_sources(payload, sources)), 200
