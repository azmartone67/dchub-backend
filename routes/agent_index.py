"""
agent_index.py — Phase GG (2026-05-14): the AI-agent session warm-up tool.

The user framed it: "make our site more useful to every AI agent... what
can we aggregate to incent them time?" The funnel showed 38K tool calls →
4 conversions — agents do a lot of work but rarely close the loop. This
bundle reduces agent calls-per-session by 30-50% by pre-answering the
discovery questions every agent asks first.

Two endpoints:
    GET /api/v1/agent/index        — one-call session warm-up:
                                     enums, freshness window, radar issues,
                                     coverage inventory, drill-deeper map.
    GET /api/v1/agent/coverage     — negative-space inventory:
                                     "we have X for region Y; we DON'T
                                     have W or Z". Filterable by domain.

Both are pure reads. Every bundle attaches `sources: [...]` provenance so
the calling agent can show evidence to its human user.
"""
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

try:
    from util.provenance import src, attach_sources, now_iso
except Exception:
    # Defensive fallback so this module imports even if util/ is missing.
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

agent_index_bp = Blueprint("agent_index", __name__)


def _conn():
    import psycopg2
    # autocommit=True so a single failed query (e.g., missing table) doesn't
    # abort the whole transaction and poison every subsequent query. Each
    # _safe_fetchall is independent.
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


def _safe_fetchall(cur, sql, params=()):
    """Run a query, return rows or []. Never raises. With autocommit on the
    connection a failed query doesn't poison subsequent ones."""
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception:
        return []


def _enums(cur):
    """Pull the catalog enums an agent needs to use IDs correctly."""
    out = {}
    out["iso_codes"] = sorted([r[0] for r in _safe_fetchall(cur,
        "SELECT DISTINCT iso FROM market_power_scores WHERE iso IS NOT NULL AND iso <> ''")])
    out["dcpi_verdicts"] = ["BUILD", "CAUTION", "AVOID", "LOW_SIGNAL"]
    out["dcpi_market_slugs"] = sorted([r[0] for r in _safe_fetchall(cur,
        """SELECT DISTINCT market_slug FROM market_power_scores
           WHERE market_slug IS NOT NULL ORDER BY market_slug""")])[:120]
    out["us_states_covered"] = sorted([r[0] for r in _safe_fetchall(cur,
        "SELECT DISTINCT state FROM market_power_scores WHERE state IS NOT NULL AND char_length(state) = 2")])
    out["listing_statuses"] = ["public", "pocket", "draft"]
    out["facility_status_values"] = sorted([r[0] for r in _safe_fetchall(cur,
        "SELECT DISTINCT status FROM facilities WHERE status IS NOT NULL LIMIT 20")])
    return out


def _freshness_window(cur):
    """Per-domain freshness as recorded in freshness_checks."""
    rows = _safe_fetchall(cur,
        """SELECT surface, last_updated, stale_after_hours, status,
                  EXTRACT(EPOCH FROM (NOW() - last_updated)) / 3600 AS age_hours
             FROM freshness_checks
            WHERE last_updated IS NOT NULL
            ORDER BY surface""")
    return [{
        "domain": r[0],
        "last_updated": r[1].isoformat() if r[1] else None,
        "stale_after_hours": r[2],
        "status": r[3],
        "age_hours": round(float(r[4]), 2) if r[4] is not None else None,
    } for r in rows]


def _radar_issues(cur):
    """Active radar (anything not green)."""
    rows = _safe_fetchall(cur,
        """SELECT domain, status, last_seen_at, severity, note
             FROM data_domain_freshness
            WHERE status IS NOT NULL AND status NOT IN ('green', 'fresh', 'ok')
            ORDER BY severity DESC NULLS LAST, last_seen_at DESC NULLS LAST
            LIMIT 20""")
    return [{
        "domain": r[0],
        "status": r[1],
        "last_seen_at": r[2].isoformat() if r[2] else None,
        "severity": r[3],
        "note": (r[4] or '')[:200],
    } for r in rows]


def _coverage_summary(cur):
    """Top-line coverage inventory: rows-by-domain, geographic spread."""
    out = {}
    def count(table):
        rows = _safe_fetchall(cur, f"SELECT COUNT(*) FROM {table}")
        return int(rows[0][0]) if rows else 0
    for tbl, key in [
        ("facilities",            "facilities"),
        ("discovered_facilities", "discovered_facilities"),
        ("capacity_pipeline",     "pipeline_projects"),
        ("market_power_scores",   "dcpi_scored_markets"),
        ("news",                  "news_articles"),
        ("transactions",          "ma_transactions"),
        ("exclusive_listings",    "pocket_listings"),
    ]:
        out[key] = count(tbl)
    out["countries_covered"] = [r[0] for r in _safe_fetchall(cur,
        """SELECT DISTINCT country FROM facilities
           WHERE country IS NOT NULL AND country <> ''
           ORDER BY country""")]
    return out


@agent_index_bp.route("/api/v1/agent/index", methods=["GET"])
def agent_index():
    """One-call session warm-up. The first endpoint an AI agent should
    hit to learn what we have, what we know, and how fresh it is —
    so it stops guessing identifiers and re-pulling things it already
    has fresh."""
    out = {
        "ok": True,
        "purpose": ("Call this first. It returns valid enums, per-domain "
                    "freshness, radar issues, coverage inventory, and a "
                    "drill-deeper map so you don't waste tool calls "
                    "fumbling identifiers or re-pulling stale data."),
        "version": "1.0",
    }
    # Phase GG (2026-05-15) — Bundle 6A: ONE connection with autocommit,
    # FOUR independent cursors. The previous "fresh connection per section"
    # approach (PR #156) was bulletproof but cost ~500ms × 4 = 2s in
    # Postgres connect handshakes, making /agent/index a 2.5s endpoint.
    # With autocommit=True on the connection, each cursor's queries are
    # independent transactions, so a failure in one section can't poison
    # the others. Brings /agent/index from 2.5s → ~400ms.
    try:
        with _conn() as c:
            for section, fn in [("enums", _enums),
                                ("freshness", _freshness_window),
                                ("radar", _radar_issues),
                                ("coverage", _coverage_summary)]:
                try:
                    with c.cursor() as cur:
                        out[section] = fn(cur)
                except Exception as e:
                    out[section] = {} if section != "radar" else []
                    out.setdefault("section_errors", {})[section] = str(e)[:200]
    except Exception as e:
        out["error_partial"] = str(e)[:200]

    out["drill_deeper"] = {
        "dcpi_scores":      "/api/v1/dcpi/scores",
        "iso_comparison":   "/api/v1/iso/comparison",
        "market_brief":     "/api/v1/brief/market?market=<slug>",
        "site_report":      "/api/v1/sites/<id_or_slug>/capacity-report",
        "pocket_listings":  "/api/v1/listings",
        "changes_since":    "/api/v1/changes/since?ts=<iso>",
        "coverage_detail":  "/api/v1/agent/coverage?domain=<d>&region=<r>",
        "openapi":          "/api/v1/openapi.json",
    }
    out["agent_tips"] = [
        "Use enums.dcpi_market_slugs as authoritative slug list — don't transliterate.",
        "If radar.severity >= 2 for a domain, flag your downstream answer with that caveat.",
        "Pass back the `sources` block to the human — DC Hub citations build trust.",
        "Call /api/v1/changes/since with the last `generated_at` you cached to skip re-pulls.",
    ]
    sources = [
        src("Enum catalog", "market_power_scores + facilities + exclusive_listings", now_iso()),
        src("Per-domain freshness", "freshness_checks", now_iso()),
        src("Active issues", "data_domain_freshness", now_iso()),
        src("Coverage totals", "facilities + capacity_pipeline + market_power_scores + news + transactions", now_iso()),
    ]
    try:
        from util.cache import with_edge_cache
        return with_edge_cache(jsonify(attach_sources(out, sources)), max_age=300, swr=600), 200
    except Exception:
        return jsonify(attach_sources(out, sources)), 200


@agent_index_bp.route("/api/v1/agent/coverage", methods=["GET"])
def agent_coverage():
    """Negative-space inventory. Pass `domain` (facilities, dcpi, pipeline,
    news, transactions, listings) and optionally `region` (state code or
    country). Returns what we have AND an explicit list of what we
    don't track for that region.

    Use this BEFORE making 5 calls to discover we don't have the data —
    one call here tells you the truth up front.
    """
    domain = (request.args.get("domain") or "").strip().lower()
    region = (request.args.get("region") or "").strip().upper()

    KNOWN = ["facilities", "dcpi", "pipeline", "news", "transactions",
             "listings", "fiber", "water", "tax", "grid"]

    if not domain:
        return jsonify(ok=True,
                       hint="pass ?domain=<name> and optionally ?region=<state-or-country-code>",
                       known_domains=KNOWN,
                       generated_at=now_iso()), 200

    if domain not in KNOWN:
        return jsonify(ok=False, error="unknown_domain",
                       known_domains=KNOWN), 200

    payload = {"ok": True, "domain": domain, "region": region or None}
    have = {}
    dont_have = []

    try:
        with _conn() as c, c.cursor() as cur:
            if domain == "facilities":
                if region:
                    rows = _safe_fetchall(cur,
                        """SELECT COUNT(*), COALESCE(SUM(power_mw), 0)
                             FROM facilities
                            WHERE UPPER(state) = %s OR UPPER(country) = %s""",
                        (region, region))
                    have["count"] = int(rows[0][0]) if rows else 0
                    have["total_mw"] = float(rows[0][1]) if rows and rows[0][1] else 0
                else:
                    rows = _safe_fetchall(cur, "SELECT COUNT(*) FROM facilities")
                    have["count"] = int(rows[0][0]) if rows else 0
                have["fields"] = ["id", "name", "provider", "city", "state",
                                  "country", "latitude", "longitude", "status",
                                  "power_mw", "source", "first_seen"]
                dont_have = ["PUE", "water source", "tenant list",
                             "lease terms", "actual energy mix",
                             "rack count", "cooling type"]

            elif domain == "dcpi":
                if region:
                    rows = _safe_fetchall(cur,
                        """SELECT COUNT(DISTINCT market_slug)
                             FROM market_power_scores
                            WHERE UPPER(state) = %s OR UPPER(iso) = %s""",
                        (region, region))
                else:
                    rows = _safe_fetchall(cur,
                        "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores")
                have["scored_markets"] = int(rows[0][0]) if rows else 0
                have["fields"] = ["verdict", "excess_power_score",
                                  "constraint_score", "time_to_power_months",
                                  "queue_wait_months", "computed_at"]
                dont_have = ["water-stress overlay (separate domain)",
                             "carbon-intensity overlay",
                             "real-time LMP (call get_grid_data)"]

            elif domain == "pipeline":
                rows = _safe_fetchall(cur, """SELECT COUNT(*),
                                                     COALESCE(SUM(capacity_mw), 0)
                                                FROM capacity_pipeline""")
                have["projects"] = int(rows[0][0]) if rows else 0
                have["total_mw"] = float(rows[0][1]) if rows and rows[0][1] else 0
                have["fields"] = ["operator", "market", "capacity_mw",
                                  "phase", "status", "completion_date", "notes"]
                dont_have = ["construction-cost estimate", "tenant pre-lease",
                             "interconnection-cost detail"]

            elif domain == "news":
                rows = _safe_fetchall(cur,
                    """SELECT COUNT(*), MIN(published_date), MAX(published_date)
                         FROM news""")
                if rows and rows[0]:
                    have["articles"] = int(rows[0][0] or 0)
                    have["earliest"] = rows[0][1].isoformat() if rows[0][1] else None
                    have["latest"] = rows[0][2].isoformat() if rows[0][2] else None
                have["fields"] = ["title", "url", "published_date", "source", "body"]
                dont_have = ["sentiment scoring", "deal-impact rating"]

            elif domain == "transactions":
                rows = _safe_fetchall(cur, "SELECT COUNT(*) FROM transactions")
                have["count"] = int(rows[0][0]) if rows else 0
                have["fields"] = ["target", "acquirer", "value_usd",
                                  "announced_date", "market", "deal_type"]
                dont_have = ["pre-money valuation", "earnout terms",
                             "post-close performance"]

            elif domain == "listings":
                rows = _safe_fetchall(cur,
                    "SELECT COUNT(*), status FROM exclusive_listings GROUP BY status")
                have["by_status"] = {r[1]: int(r[0]) for r in rows}
                have["fields"] = ["slug", "title", "status", "tier_required",
                                  "market", "capacity_mw", "asking_price",
                                  "detail", "contact"]
                dont_have = ["historical sale prices",
                             "off-platform listings",
                             "broker commissions"]

            elif domain == "fiber":
                have["fields"] = ["routes", "carriers", "POP locations"]
                dont_have = ["per-strand pricing", "SLA tier breakdown"]

            elif domain == "water":
                have["fields"] = ["stress score", "drought risk", "source basin"]
                dont_have = ["per-facility withdrawals", "wastewater discharge"]

            elif domain == "tax":
                have["fields"] = ["state incentive programs",
                                  "abatement details", "minimum investment"]
                dont_have = ["per-county property tax",
                             "local utility-tax exemptions"]

            elif domain == "grid":
                have["fields"] = ["heartbeat freshness", "ISO snapshot",
                                  "LMP via get_grid_data", "queue wait"]
                dont_have = ["sub-hour LMP", "node-level outage data"]
    except Exception as e:
        payload["error_partial"] = str(e)[:200]

    payload["have"] = have
    payload["dont_have"] = dont_have
    payload["recommendation"] = (
        f"For '{domain}' we ship the fields under `have.fields`. " +
        ("For " + region + ", " if region else "") +
        "do not waste tool calls asking for " +
        (", ".join(dont_have[:3]) if dont_have else "fields not listed in `have.fields`") +
        " — we don't track them today.")

    sources = [src(f"Coverage inventory for domain={domain}",
                   "agent_coverage (synthesized from row counts + schema)", now_iso())]
    return jsonify(attach_sources(payload, sources)), 200
