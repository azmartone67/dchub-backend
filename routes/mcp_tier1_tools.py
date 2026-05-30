"""
mcp_tier1_tools.py — Tier 1 MCP tool backend endpoints.

Phase ZZZZZ-round33 (2026-05-24). Backends for new MCP tools added to
dchub-mcp-server. Each is gated to Developer tier ($49/mo) or above.

Endpoints:
  POST /api/v1/mcp/tools/rank_markets       — Top-N markets by criteria
  POST /api/v1/mcp/tools/find_alternatives  — Given facility, find similar nearby
  POST /api/v1/mcp/tools/score_facility     — Independent 7-dimension score

These are designed to be CHEAP (sub-200ms) so they don't trip the
worker timeout. Heavy computation happens nightly into materialized
tables; runtime is just SELECT.
"""
import os
import math
from typing import Any
from contextlib import contextmanager

from flask import Blueprint, request, jsonify

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None

mcp_tier1_bp = Blueprint("mcp_tier1_tools", __name__,
                          url_prefix="/api/v1/mcp/tools")


def _dsn():
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("POSTGRES_URL")
            or "")


@contextmanager
def _conn():
    if psycopg2 is None or not _dsn():
        yield None
        return
    c = psycopg2.connect(_dsn(), connect_timeout=8)
    try:
        yield c
    finally:
        try: c.close()
        except Exception: pass


# ═════════════════════════════════════════════════════════════════════
# rank_markets — Top-N markets by criteria
# ═════════════════════════════════════════════════════════════════════
@mcp_tier1_bp.route("/rank_markets", methods=["POST", "GET"])
def rank_markets():
    """Rank data center markets by criteria. Returns top N markets sorted
    by score.

    Inputs:
      criteria:    cheapest_power | most_capacity | most_operators | fastest_growing | best_overall
      region:      global | us | canada | eu | apac | americas    (default: us)
      limit:       int                                              (default: 10, max: 50)
      min_capacity_mw: float                                        (default: 0)
    """
    args = request.get_json(silent=True) or request.args.to_dict()
    criteria = (args.get("criteria") or "best_overall").lower()
    region   = (args.get("region")   or "us").lower()
    try: limit = max(1, min(50, int(args.get("limit", 10))))
    except (TypeError, ValueError): limit = 10
    try: min_cap = float(args.get("min_capacity_mw", 0) or 0)
    except (TypeError, ValueError): min_cap = 0

    valid_criteria = {"cheapest_power", "most_capacity", "most_operators",
                       "fastest_growing", "best_overall"}
    if criteria not in valid_criteria:
        return jsonify({
            "error": "invalid criteria",
            "valid_options": sorted(valid_criteria),
        }), 400

    # Map region to country filter
    region_countries = {
        "us":       ["US", "USA", "United States"],
        "canada":   ["CA", "Canada"],
        "eu":       ["UK", "Germany", "France", "Netherlands", "Ireland", "Spain", "Italy", "Sweden", "Finland", "Norway", "Denmark"],
        "apac":     ["Japan", "Australia", "Singapore", "Korea", "South Korea", "China", "Hong Kong", "India"],
        "americas": ["US", "USA", "Brazil", "Mexico", "Canada", "Chile", "Argentina"],
        "global":   [],
    }
    countries = region_countries.get(region, region_countries["us"])

    with _conn() as c:
        if c is None:
            return jsonify({"error": "database unavailable"}), 503

        # Country filter SQL
        if countries:
            country_filter = "AND country = ANY(%s)"
            country_param  = countries
        else:
            country_filter = ""
            country_param  = None

        # Build base aggregate query
        query = f"""
            SELECT
                LOWER(REPLACE(city,' ','-')) || '-' || LOWER(state)        AS slug,
                city, state, country,
                COUNT(*)                                                    AS facility_count,
                COALESCE(SUM(power_mw), 0)::numeric(10,1)                  AS total_mw,
                COUNT(DISTINCT provider)                                    AS operator_count,
                COALESCE(AVG(power_mw), 0)::numeric(10,1)                  AS avg_mw,
                COALESCE(MAX(power_mw), 0)::numeric(10,1)                  AS max_mw
              FROM discovered_facilities
             WHERE city IS NOT NULL AND city != ''
               AND state IS NOT NULL AND state != ''
               AND status = 'active'
               {country_filter}
          GROUP BY city, state, country
            HAVING COUNT(*) >= 2 AND COALESCE(SUM(power_mw), 0) >= %s
        """
        params: list[Any] = []
        if country_param:
            params.append(country_param)
        params.append(min_cap)

        # Order by criteria
        # r36-fix: ORDER BY ARITHMETIC must use raw column refs, not aliases.
        # PostgreSQL allows ORDER BY alias only for plain references —
        # the moment you wrap the alias in an expression (e.g. total_mw * 0.4)
        # it must be the original COALESCE(SUM(power_mw),0) form.
        _SUM_MW = "COALESCE(SUM(power_mw), 0)"
        _CNT_OPS = "COUNT(DISTINCT provider)"
        _CNT_FAC = "COUNT(*)"
        order_clause = {
            "cheapest_power":   "ORDER BY total_mw DESC NULLS LAST",
            "most_capacity":    "ORDER BY total_mw DESC NULLS LAST",
            "most_operators":   "ORDER BY operator_count DESC, total_mw DESC",
            "fastest_growing":  "ORDER BY facility_count DESC",
            "best_overall":     f"ORDER BY ({_SUM_MW} * 0.4 + {_CNT_OPS} * 50 + {_CNT_FAC} * 20) DESC",
        }[criteria]
        query += " " + order_clause + " LIMIT %s"
        params.append(limit)

        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        except Exception as e:
            return jsonify({"error": f"query_failed: {type(e).__name__}: {str(e)[:200]}"}), 500

    results = []
    for i, r in enumerate(rows):
        # Compose value + score
        if criteria == "cheapest_power":
            # Larger markets → cheaper power (heuristic until we have direct LMP)
            value_str = f"~${42 - min(20, float(r['total_mw'])/200):.2f}/MWh"
        elif criteria == "most_capacity":
            value_str = f"{r['total_mw']:.0f} MW"
        elif criteria == "most_operators":
            value_str = f"{r['operator_count']} operators"
        elif criteria == "fastest_growing":
            value_str = f"{r['facility_count']} facilities"
        else:
            value_str = f"{r['facility_count']} fac / {r['total_mw']:.0f} MW / {r['operator_count']} ops"

        # Score 0-100 normalized to rank
        score = round(100 - (i * 100 / max(1, len(rows))), 1)

        results.append({
            "rank":             i + 1,
            "market":           r["slug"],
            "city":             r["city"],
            "state":            r["state"],
            "country":          r["country"],
            "score":            score,
            "value":            value_str,
            "facility_count":   r["facility_count"],
            "total_mw":         float(r["total_mw"]),
            "operator_count":   r["operator_count"],
            "url":              f"https://dchub.cloud/markets/{r['slug']}",
        })

    return jsonify({
        "criteria":       criteria,
        "region":         region,
        "results":        results,
        "result_count":   len(results),
        "methodology":    {
            "cheapest_power":  "Proxy: largest markets typically have lowest LMP. Direct LMP integration coming Q3.",
            "most_capacity":   "Sum of power_mw across all active facilities in market.",
            "most_operators":  "Distinct operator count, tiebreaker by total MW.",
            "fastest_growing": "Proxy: facility count. Pipeline-weighted growth coming Q3.",
            "best_overall":    "Composite: 0.4×total_mw + 50×operators + 20×facilities.",
        }[criteria],
        "data_source":    "DC Hub facility database, status='active'",
        "tier":           "developer",  # required tier for full results
    }), 200


# ═════════════════════════════════════════════════════════════════════
# find_alternatives — given facility, find similar nearby
# ═════════════════════════════════════════════════════════════════════
@mcp_tier1_bp.route("/find_alternatives", methods=["POST", "GET"])
def find_alternatives():
    """Given a target facility, find similar nearby alternatives.

    Inputs:
      facility_id:        REQUIRED
      radius_km:          default 50, max 500
      match_on:           all | capacity | tier | operator_class    (default: all)
      exclude_operator:   bool — exclude same-operator results       (default: false)
      limit:              default 5, max 20
    """
    args = request.get_json(silent=True) or request.args.to_dict()
    facility_id = (args.get("facility_id") or "").strip()
    try: radius_km = max(1, min(500, float(args.get("radius_km", 50))))
    except (TypeError, ValueError): radius_km = 50
    match_on  = (args.get("match_on") or "all").lower()
    exclude_operator = str(args.get("exclude_operator", "false")).lower() in ("true", "1", "yes")
    try: limit = max(1, min(20, int(args.get("limit", 5))))
    except (TypeError, ValueError): limit = 5

    if not facility_id:
        return jsonify({"error": "facility_id is required"}), 400

    with _conn() as c:
        if c is None:
            return jsonify({"error": "database unavailable"}), 503
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Get the target facility (cast id since discovered_facilities.id is SERIAL int)
                cur.execute("""
                    SELECT id, name, provider, city, state, country,
                           latitude, longitude, power_mw, status
                      FROM discovered_facilities
                     WHERE CAST(id AS TEXT) = %s
                     LIMIT 1
                """, (str(facility_id),))
                target = cur.fetchone()

                if not target:
                    return jsonify({
                        "error": "facility not found",
                        "facility_id": facility_id,
                    }), 404

                # tier column doesn't exist in discovered_facilities — default to 0
                target.setdefault('tier', 0)

                # Find candidates — same market first, then expand
                same_op_filter = "AND provider != %s" if exclude_operator else ""
                params: list[Any] = [target["city"], target["state"], target["id"]]
                if exclude_operator:
                    params.append(target["provider"])

                cur.execute(f"""
                    SELECT id, name, provider, city, state, country,
                           latitude, longitude, power_mw, status
                      FROM discovered_facilities
                     WHERE city = %s AND state = %s
                       AND id != %s
                       AND COALESCE(is_duplicate, 0) = 0
                       {same_op_filter}
                  ORDER BY power_mw DESC NULLS LAST
                     LIMIT 50
                """, params)
                candidates = cur.fetchall()
                for cand in candidates:
                    cand.setdefault('tier', 0)
        except Exception as e:
            return jsonify({"error": f"query_failed: {type(e).__name__}: {str(e)[:200]}"}), 500

    # Score each candidate
    target_mw   = float(target.get("power_mw") or 0)
    target_tier = int(target.get("tier") or 0)
    target_lat  = float(target.get("latitude") or 0)
    target_lon  = float(target.get("longitude") or 0)

    scored = []
    for cand in candidates:
        cand_mw   = float(cand.get("power_mw") or 0)
        cand_tier = int(cand.get("tier") or 0)
        cand_lat  = float(cand.get("latitude") or 0)
        cand_lon  = float(cand.get("longitude") or 0)

        # Distance (approximate, in km, fine for ~100km radius)
        if target_lat and cand_lat:
            dlat = (cand_lat - target_lat) * 111
            dlon = (cand_lon - target_lon) * 111 * math.cos(math.radians(target_lat))
            distance_km = math.sqrt(dlat*dlat + dlon*dlon)
        else:
            distance_km = None

        if distance_km is not None and distance_km > radius_km:
            continue

        # Score components (each 0-1)
        cap_match = 1 - min(1, abs(target_mw - cand_mw) / max(target_mw, 50)) if target_mw else 0.5
        tier_match = 1 if cand_tier == target_tier and cand_tier > 0 else (0.5 if cand_tier > 0 else 0.3)
        proximity = 1 - min(1, (distance_km or radius_km) / radius_km) if distance_km is not None else 0.5

        if match_on == "capacity":
            similarity = cap_match
        elif match_on == "tier":
            similarity = tier_match
        elif match_on == "operator_class":
            similarity = 1 if (cand.get("provider") == target.get("provider")) else 0.3
        else:  # "all"
            similarity = (cap_match * 0.45 + tier_match * 0.25 + proximity * 0.30)

        match_reasons = []
        if cap_match > 0.7:
            match_reasons.append(f"similar capacity ({cand_mw:.0f} vs {target_mw:.0f} MW)")
        if cand_tier == target_tier and cand_tier > 0:
            match_reasons.append(f"same tier ({cand_tier})")
        if distance_km is not None and distance_km < 20:
            match_reasons.append(f"close ({distance_km:.1f}km)")
        if cand.get("provider") == target.get("provider"):
            match_reasons.append("same operator")

        diffs = []
        mw_diff = cand_mw - target_mw
        if abs(mw_diff) > 20:
            diffs.append(f"{'larger' if mw_diff > 0 else 'smaller'} ({mw_diff:+.0f} MW)")
        if cand.get("provider") != target.get("provider"):
            diffs.append(f"different operator ({cand.get('provider') or 'unknown'})")

        scored.append({
            "facility_id":     cand["id"],
            "name":            cand["name"],
            "provider":        cand.get("provider"),
            "distance_km":     round(distance_km, 1) if distance_km is not None else None,
            "similarity_score": round(similarity, 3),
            "power_mw":        cand_mw,
            "tier":            cand_tier,
            "match_reasons":   match_reasons,
            "key_differences": diffs,
            "url":             f"https://dchub.cloud/facility/{cand['id']}",
        })

    scored.sort(key=lambda x: x["similarity_score"], reverse=True)
    scored = scored[:limit]

    return jsonify({
        "target_facility": {
            "facility_id": target["id"],
            "name":        target["name"],
            "provider":    target.get("provider"),
            "city":        target.get("city"),
            "state":       target.get("state"),
            "power_mw":    target_mw,
            "tier":        target_tier,
            "url":         f"https://dchub.cloud/facility/{target['id']}",
        },
        "alternatives":    scored,
        "result_count":    len(scored),
        "radius_km":       radius_km,
        "match_on":        match_on,
        "search_method":   "weighted_similarity: capacity (0.45) + tier (0.25) + proximity (0.30)",
        "tier":            "free",  # 3 results free; full 20 require Developer
    }), 200


# ═════════════════════════════════════════════════════════════════════
# score_facility — independent 7-dimension score
# ═════════════════════════════════════════════════════════════════════
@mcp_tier1_bp.route("/score_facility", methods=["POST", "GET"])
def score_facility():
    """Calculate independent facility score (0-100) across 7 dimensions.

    Inputs:
      facility_id:  REQUIRED
      weighting:    balanced | power_priority | risk_priority | expansion_priority  (default: balanced)
    """
    args = request.get_json(silent=True) or request.args.to_dict()
    facility_id = (args.get("facility_id") or "").strip()
    weighting   = (args.get("weighting") or "balanced").lower()

    if not facility_id:
        return jsonify({"error": "facility_id is required"}), 400

    with _conn() as c:
        if c is None:
            return jsonify({"error": "database unavailable"}), 503
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # discovered_facilities lacks tier/sqft/certifications/connectivity
                # — graceful absence (defaults to 0/None)
                cur.execute("""
                    SELECT id, name, provider, city, state, country,
                           latitude, longitude, power_mw, status,
                           source, source_url, confidence_score
                      FROM discovered_facilities
                     WHERE CAST(id AS TEXT) = %s LIMIT 1
                """, (str(facility_id),))
                f = cur.fetchone()

                if not f:
                    return jsonify({"error": "facility not found", "facility_id": facility_id}), 404

                # Default missing columns so the rest of the scoring works
                f.setdefault('tier', 0)
                f.setdefault('sqft', 0)
                f.setdefault('certifications', None)
                f.setdefault('connectivity', None)

                # Get market context (count of facilities + avg MW in market)
                cur.execute("""
                    SELECT COUNT(*) AS n, AVG(power_mw) AS avg_mw,
                           COUNT(DISTINCT provider) AS operators
                      FROM discovered_facilities
                     WHERE city = %s AND state = %s
                       AND COALESCE(is_duplicate, 0) = 0
                       AND id != %s
                """, (f["city"], f["state"], f["id"]))
                market = cur.fetchone() or {"n": 0, "avg_mw": 0, "operators": 0}
        except Exception as e:
            return jsonify({"error": f"query_failed: {type(e).__name__}: {str(e)[:200]}"}), 500

    # Build 7-dimension scoring
    power_mw   = float(f.get("power_mw") or 0)
    tier       = int(f.get("tier") or 0)
    market_n   = int(market.get("n") or 0)
    avg_market = float(market.get("avg_mw") or 0)

    # Power score: higher MW = better, normalized against market average
    power_score = min(100, int(50 + (power_mw / max(50, avg_market)) * 30)) if power_mw else 40
    power_detail = f"{power_mw} MW capacity ({'above' if power_mw > avg_market else 'below'} market avg of {avg_market:.0f} MW)"

    # Fiber score: connectivity field present + market depth = better
    fiber_score = 65 + (15 if f.get("connectivity") else 0) + (10 if market_n > 5 else 0)
    fiber_detail = f"{'Connectivity declared' if f.get('connectivity') else 'Connectivity TBD'}; {market_n} other facilities in market (peering depth)"

    # Water score: placeholder until water_risk table joined
    water_score = 78
    water_detail = "Baseline estimate; integrate get_water_risk for precise score"

    # Climate risk: rough proxy by state
    state_risk = {
        "FL": 55, "LA": 50, "TX": 70, "CA": 65, "NV": 88, "AZ": 82,
        "VA": 85, "GA": 82, "OH": 90, "PA": 88, "IL": 88, "WA": 80,
        "QC": 92,  # Quebec (cold climate, low natural disaster risk)
    }
    climate_score = state_risk.get((f.get("state") or "").upper(), 78)
    climate_detail = f"State {f.get('state')} baseline risk score"

    # Tax: state-level baseline (placeholder)
    tax_state = {
        "VA": 92, "TX": 88, "OH": 90, "GA": 87, "AZ": 85, "WY": 95,
        "NY": 62, "CA": 55, "QC": 80,
    }
    tax_score = tax_state.get((f.get("state") or "").upper(), 70)
    tax_detail = f"State {f.get('state')} data center tax climate"

    # Talent: market depth proxy
    talent_score = min(95, 50 + market_n * 3)
    talent_detail = f"{market_n} other facilities in market = talent pool indicator"

    # Expansion: provider type heuristic
    expansion_score = 75
    if (f.get("provider") or "").lower() in ("equinix", "digital realty", "qts", "cyrusone", "coresite"):
        expansion_score = 90  # major operators have land banks
    expansion_detail = f"Operator {f.get('provider')} expansion baseline"

    dimensions = {
        "power":           {"score": power_score, "detail": power_detail},
        "fiber":           {"score": fiber_score, "detail": fiber_detail},
        "water":           {"score": water_score, "detail": water_detail},
        "climate_risk":    {"score": climate_score, "detail": climate_detail},
        "tax_environment": {"score": tax_score, "detail": tax_detail},
        "talent_pool":     {"score": talent_score, "detail": talent_detail},
        "expansion":       {"score": expansion_score, "detail": expansion_detail},
    }

    # Apply weighting
    weights = {
        "balanced":            {"power": 1, "fiber": 1, "water": 1, "climate_risk": 1, "tax_environment": 1, "talent_pool": 1, "expansion": 1},
        "power_priority":      {"power": 3, "fiber": 1, "water": 1, "climate_risk": 1, "tax_environment": 1, "talent_pool": 1, "expansion": 1},
        "risk_priority":       {"power": 1, "fiber": 1, "water": 2, "climate_risk": 3, "tax_environment": 1, "talent_pool": 1, "expansion": 1},
        "expansion_priority":  {"power": 2, "fiber": 1, "water": 1, "climate_risk": 1, "tax_environment": 1, "talent_pool": 1, "expansion": 3},
    }.get(weighting, {"power": 1, "fiber": 1, "water": 1, "climate_risk": 1, "tax_environment": 1, "talent_pool": 1, "expansion": 1})

    total_w = sum(weights.values())
    composite = sum(dimensions[k]["score"] * weights[k] for k in dimensions) / total_w
    composite = round(composite, 1)

    tier_class = "tier_1_premier" if composite >= 90 else \
                 "tier_1_solid" if composite >= 80 else \
                 "tier_2_capable" if composite >= 70 else \
                 "tier_3_marginal" if composite >= 60 else "tier_4_avoid"

    return jsonify({
        "facility_id":         f["id"],
        "name":                f["name"],
        "operator":            f.get("provider"),
        "city":                f.get("city"),
        "state":               f.get("state"),
        "composite_score":     composite,
        "tier_classification": tier_class,
        "dimensions":          dimensions,
        "weighting_used":      weighting,
        "weights":             weights,
        "peer_comparison": {
            "market_size":          market_n + 1,
            "rank_estimate":        max(1, int((100 - composite) / 100 * (market_n + 1))),
            "percentile_estimate":  int(composite),
        },
        "methodology":         "7 dimensions weighted by user preference. Phase 1 uses state-level baselines for water/climate/tax; Phase 2 integrates real-time water-risk + tax-incentive APIs.",
        "tier":                "developer",
        "url":                 f"https://dchub.cloud/facility/{f['id']}",
    }), 200


# AUTO-REPAIR: duplicate route '/health' also in main.py:3758 — review and remove one
@mcp_tier1_bp.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "blueprint": "mcp_tier1_tools",
        "version": "round-33-v1",
        "endpoints": [
            "POST /api/v1/mcp/tools/rank_markets",
            "POST /api/v1/mcp/tools/find_alternatives",
            "POST /api/v1/mcp/tools/score_facility",
        ],
    }), 200
