"""
market_intel_preview.py — free-tier preview of get_market_intel.

Phase ZZZZZ-round44 (2026-05-25). Brain shows get_market_intel is the
top blocked tool with 4,500 paywall hits / 30d (11% of all signals).
Fully blocking it loses ~50 conversions/year vs letting free tier see
ONE market × ONE metric. This endpoint provides a paywall-friendly
preview that returns just enough data to validate the tool's value.

Endpoint: GET /api/v1/market-intel-preview?market=<slug>
Returns: top-line metric + upgrade CTA pointing at /pricing/upgrade
"""
import os, datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify, request
try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

from util.status_taxonomy import (  # noqa: E402
    operational_sql as _status_operational_sql,
    basis as _status_basis,
)

market_intel_preview_bp = Blueprint("market_intel_preview", __name__)

# Same depth basis as routes/ai_capacity_index.py — see the long note there.
# r-status-canon (#2058) moved the shell exclusion onto the #1539 fleet filter;
# this counts distinct FACILITIES rather than rows on that axis (is_duplicate=0
# still admits exact-name collisions), reads lifecycle through the taxonomy that
# owns both spellings of the backfilled cohort, and does not treat '' as an
# operator. Narration stays out of the SQL string — the backfill scanner reads
# string constants and a quoted dead predicate re-arms its block.
_FLEET = "COALESCE(is_duplicate, 0) = 0"
_FACILITIES = "COUNT(DISTINCT LOWER(TRIM(name)))"
_OPERATORS = "COUNT(DISTINCT NULLIF(TRIM(provider),''))"
_OPERATIONAL = _status_operational_sql()

# Normalised state, blank and NULL collapsed to the same thing. Two spellings so
# the fragment stays a plain greppable constant on both sides of the CROSS JOIN
# rather than a string built at call time.
_NSTATE = "NULLIF(UPPER(TRIM(COALESCE(state,''))),'')"
_NSTATE_S = "NULLIF(UPPER(TRIM(COALESCE(s.state,''))),'')"

def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()

@market_intel_preview_bp.route("/api/v1/market-intel-preview", methods=["GET"])
def preview():
    market = (request.args.get("market") or "ashburn").strip().lower()
    out = {
        "market": market,
        "preview": True,
        "note": "Free preview shows ONE metric. Full intel (supply/demand, pricing, vacancy, pipeline) requires Developer plan.",
        "upgrade_url": f"https://api.dchub.cloud/pricing/upgrade?tool=get_market_intel&surface=market-intel-preview&ref={market}",
        "upgrade_tier": "developer",
        "upgrade_price": "$49/mo",
    }
    if not (_pg and _dsn()):
        out["data"] = None
        return jsonify(out), 200
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # r-status-canon (2026-07-31): the zero-MW shells were excluded by a
            # status literal, which the canon backfill (Operational <- active)
            # erases. Swapped for the #1539 fleet filter, which survives it. The
            # ('Ashburn','VA') group moves 184 fac / 7,481 MW -> 199 / 6,942 on
            # the read replica; the MW *drops* because the fleet filter also
            # drops duplicate rows that were double-counting real capacity.
            #
            # THE ARBITRARY-GROUP BUG, which #2058 measured and scoped to its
            # own PR: #2057 was that PR. `GROUP BY city, state LIMIT 1` had no
            # ORDER BY, and 'ashburn' normalizes to FOUR groups —
            # ('Ashburn','VA'), ('ASHBURN','VA'), ('Ashburn','') and
            # ('Ashburn',NULL) — so the route served an ARBITRARY one. Verified
            # against PRODUCTION 2026-07-31, not inferred: it returned
            # ('Ashburn','') = 3 facilities / 0.0 MW, i.e. the $49/mo upsell
            # preview advertised the flagship market as empty.
            #
            # r-market-slug-groups (2026-07-31), follow-on to #2057. Ordering
            # made the pick DETERMINISTIC but still picked one raw group and
            # discarded the rest, and the slug does not identify a group: those
            # four are ONE market wearing four spellings. Measured on the read
            # replica, 629 of 2,781 city slugs split this way, and picking the
            # largest still published 0.0 MW for 26 markets that HAVE capacity
            # in a sibling group — the same "flagship market looks empty"
            # failure, relocated off Ashburn onto Sao Paulo (0 vs 150 MW),
            # Muskogee (0 vs 70) and Markham (0 vs 34).
            #
            # So group on the NORMALISED key. Case folds outright. Blank/NULL
            # state folds into the market's real state only when the slug has
            # exactly ONE — 'ashburn' has only VA, so its four groups collapse;
            # 'london' spans several, so they stay apart and a genuine homonym
            # is never merged into one fictional market. On the replica: 21 of
            # the 26 zero-MW markets cured, 0 caused, no market loses a
            # facility. Ashburn 179 -> 181 facilities, 54 -> 55 operators,
            # 6,304 MW unchanged; Dublin 1,055 -> 1,755 MW; Singapore 709 ->
            # 959 MW. City/state are reported as the most common spelling in
            # the group, not the normalised key, so the response still reads
            # 'Ashburn' / 'VA'.
            #
            # NOT fixed here, measured and left for its own PR: country is not
            # in the key, so 'london' still blends GB, US, CA and UG into one
            # 286-facility "market", and the 5 uncured markets are ones whose
            # state is spelled out in one group and abbreviated in another
            # ('Oklahoma' vs 'OK'). Both are the same bug class on a wider
            # blast radius than folding spellings.
            #
            # Narration stays out of the SQL string — the backfill scanner
            # reads string constants, and a quoted dead predicate re-arms it.
            cur.execute(f"""
                WITH scoped AS (
                    SELECT city, state, name, provider, power_mw, status
                      FROM discovered_facilities
                     WHERE LOWER(REPLACE(city, ' ', '-')) = %s
                       AND {_FLEET}
                ), real_states AS (
                    SELECT COUNT(DISTINCT {_NSTATE}) AS n_states,
                           MAX({_NSTATE})            AS only_state
                      FROM scoped
                ), keyed AS (
                    SELECT s.*,
                           LOWER(TRIM(s.city)) AS n_city,
                           CASE WHEN r.n_states = 1 THEN r.only_state
                                ELSE {_NSTATE_S} END AS n_state
                      FROM scoped s CROSS JOIN real_states r
                )
                SELECT MODE() WITHIN GROUP (ORDER BY city)            AS city,
                       COALESCE(MODE() WITHIN GROUP (
                           ORDER BY NULLIF(TRIM(state),'')), '')      AS state,
                       {_FACILITIES} FILTER (WHERE {_OPERATIONAL})
                           ::int                                          AS facility_count,
                       {_FACILITIES} FILTER (WHERE {_OPERATIONAL}
                           AND COALESCE(power_mw,0) > 0)::int             AS metered_facility_count,
                       {_FACILITIES}::int                                 AS tracked_count,
                       -- OPERATIONAL only. Pipeline MW stays out of the free
                       -- preview entirely: pipeline_mw_under_construction is a
                       -- _locked_field, so it is paywalled by product decision,
                       -- and it must not leak in through a total_mw that
                       -- quietly summed announced capacity into "installed".
                       COALESCE(SUM(power_mw) FILTER (WHERE {_OPERATIONAL}), 0)
                           ::numeric(10,1)::float                         AS total_mw,
                       {_OPERATORS} FILTER (WHERE {_OPERATIONAL})
                           ::int                                          AS operator_count
                FROM keyed
                GROUP BY n_city, n_state
                ORDER BY {_FACILITIES} FILTER (WHERE {_OPERATIONAL}) DESC,
                         COALESCE(SUM(power_mw) FILTER (WHERE {_OPERATIONAL}),0) DESC,
                         n_state ASC
                LIMIT 1
            """, (market,))
            row = cur.fetchone()
        if not row:
            out["data"] = {"error": "market_not_found",
                            "suggestion": "Try: ashburn, santa-clara, dallas, chicago, atlanta"}
        else:
            out["data"] = {
                "city": row["city"],
                "state": row["state"],
                "facility_count": row["facility_count"],
                # Published, not filtered: power_mw is populated on roughly a
                # third of rows, so facility_count is a presence signal and this
                # says how much of it is metered.
                "metered_facility_count": row["metered_facility_count"],
                "tracked_count": row["tracked_count"],
                "total_mw": float(row["total_mw"]),
                "operator_count": row["operator_count"],
                "_depth_basis": {
                    "facility_count": ("distinct OPERATIONAL facilities in the "
                                        "fleet (COUNT DISTINCT name over the "
                                        "#1539 fleet filter) — no status literal"),
                    "status_basis": _status_basis(scope="market_intel_preview"),
                    "market_grouping": ("case-folded city; blank/NULL state folded "
                                        "into the market's real state only when the "
                                        "slug has exactly one, so homonym cities are "
                                        "never merged. Country is NOT in the key."),
                },
                "_locked_fields": ["supply_demand_score", "vacancy_rate", "avg_price_per_kw",
                                    "pipeline_mw_under_construction", "12mo_growth_rate",
                                    "regulatory_risk_score", "competitor_breakdown"],
                "_locked_count": 7,
            }
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    out["computed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return jsonify(out), 200
