"""dcgi.py — Data Center Gas Index (DCGI). 2026-05-30.
=======================================================

The gas analog to DCPI (Data Center Power Index). Scores US states on
natural-gas suitability for siting data-center power load — the
behind-the-meter / gas-to-power thesis: grid interconnection queues now run
5-7 years, so on-site / behind-the-meter gas generation is increasingly how
AI capacity actually gets energized THIS decade. No competitor scores gas
the way we score the grid.

Three deliverables, all built on data we already ingest:
  1. DCGI per-state index (gas access + gas cost -> verdict)
  2. Midstream-operator registry — maps FERC pipeline entities (EIA's raw
     `Operator` strings) up to their PARENT midstream company (Energy
     Transfer, Kinder Morgan, Williams/Transco, TC Energy, Enbridge/Texas
     Eastern, Tallgrass, Boardwalk, Berkshire/Northern Natural, Southwest
     Gas, ...) with live pipeline-segment counts.
  3. A shareable pipeline report (mirrors routes/state_of_power.py — JSON
     Dataset + JSON-LD + CC-BY + an HTML page agents and humans can cite).

Data foundations (already live in Neon):
  - gas_pipelines   EIA ArcGIS bulk: operator, pipeline_type
                    ('interstate'|'intrastate'), state, lat/lng (~3.3k rows).
                    NOTE: capacity_mcf / diameter_inches are NULL in this
                    feed, so the access score is built on pipeline DENSITY +
                    OPERATOR DIVERSITY + INTERSTATE SHARE, not throughput.
  - eia_gas_prices  state, price, sector, period (industrial / electric_power
                    series drive the cost score).

Scoring (0..100, higher = better for siting gas-fired DC load):
  Gas Access = 0.45*density + 0.35*operator_diversity + 0.20*interstate_share
  Gas Cost   = inverse of latest industrial/electric gas price ($/Mcf)
  DCGI       = 0.60*access + 0.40*cost
  Verdict    = GAS-ADVANTAGED | ADEQUATE | GAS-CONSTRAINED

Routes (all public — this is an aggregate index / SEO + agent-citation
surface; raw per-pipeline rows stay enterprise-gated on /api/v1/gas-pipelines):
  GET /api/v1/dcgi/operators        midstream registry + live counts
  GET /api/v1/dcgi/scores           all states ranked by DCGI
  GET /api/v1/dcgi/scores/<state>   one state detail
  GET /api/v1/dcgi/methodology      methodology
  GET /api/v1/reports/pipeline      JSON report (Dataset JSON-LD + cite block)
  GET /pipeline-report              shareable HTML report
"""
import json
import math
import time

from flask import Blueprint, jsonify, request, Response

from routes._iso_common import conn

try:
    from dchub_heartbeat import heartbeat as _heartbeat
except ImportError:
    def _heartbeat(*a, **k): pass

dcgi_bp = Blueprint("dcgi", __name__)

# ── Midstream operator registry ──────────────────────────────────────────
# Parent midstream company -> alias substrings that appear in EIA's raw
# `Operator` field for the FERC pipeline entities they own/operate. Matching
# is case-insensitive substring containment. This is the value-add no one
# else publishes: "which megacap actually controls the gas under this market."
_MIDSTREAMS = [
    {"key": "energy_transfer", "name": "Energy Transfer", "type": "Interstate transmission", "hq": "Dallas, TX",
     "aliases": ["Transwestern", "Panhandle Eastern", "Trunkline", "Florida Gas", "Rover Pipeline",
                 "Sea Robin", "Energy Transfer", "Regency", "Midcontinent Express", "Gulf Run"],
     "note": "~125k mi; one of the largest US midstream systems (Gulf, Midcon, FL)."},
    {"key": "kinder_morgan", "name": "Kinder Morgan", "type": "Interstate transmission", "hq": "Houston, TX",
     "aliases": ["Tennessee Gas", "El Paso Natural Gas", "Natural Gas Pipeline Company of America", "NGPL",
                 "Southern Natural Gas", "Colorado Interstate", "Wyoming Interstate", "Kinder Morgan",
                 "EPNG", "Mojave", "Sierrita"],
     "note": "~66k mi; largest US natural-gas transporter."},
    {"key": "williams", "name": "Williams (Transco)", "type": "Interstate transmission", "hq": "Tulsa, OK",
     "aliases": ["Transcontinental Gas", "Transco", "Northwest Pipeline", "Williams"],
     "note": "Transco is the largest-volume US pipeline (Gulf -> Northeast)."},
    {"key": "tc_energy", "name": "TC Energy", "type": "Interstate transmission", "hq": "Calgary, AB",
     "aliases": ["Columbia Gas", "Columbia Gulf", "ANR Pipeline", "Great Lakes Gas", "TC Energy",
                 "TransCanada", "Crossroads", "Portland Natural Gas"],
     "note": "Columbia + ANR; major Appalachia / Midwest takeaway."},
    {"key": "enbridge", "name": "Enbridge (incl. Texas Eastern)", "type": "Interstate transmission", "hq": "Houston / Calgary",
     "aliases": ["Texas Eastern", "Algonquin", "East Tennessee Natural Gas", "Maritimes", "Enbridge",
                 "Spectra", "Saltville", "Big Sandy"],
     "note": "Texas Eastern (TETCO) is a key Gulf -> Northeast corridor."},
    {"key": "tallgrass", "name": "Tallgrass Energy", "type": "Interstate transmission", "hq": "Lakewood, CO",
     "aliases": ["Rockies Express", "Tallgrass", "Trailblazer", "Rockies Exp"],
     "note": "Rockies Express (REX): bidirectional Rockies / Appalachia."},
    {"key": "boardwalk", "name": "Boardwalk Pipelines", "type": "Interstate transmission", "hq": "Houston, TX",
     "aliases": ["Gulf South", "Texas Gas Transmission", "Gulf Crossing", "Boardwalk", "Bistineau"],
     "note": "Gulf South + Texas Gas; Gulf Coast / Southeast."},
    {"key": "berkshire", "name": "Berkshire Hathaway Energy (Northern Natural / Eastern Gas)", "type": "Interstate transmission", "hq": "Des Moines, IA",
     "aliases": ["Northern Natural", "Eastern Gas Transmission", "BHE GT&S", "Dominion Transmission",
                 "East Ohio Gas", "Kern River", "Cove Point"],
     "note": "Northern Natural (largest by mileage) + Eastern Gas (ex-Dominion)."},
    {"key": "dt_midstream", "name": "DT Midstream", "type": "Transmission + gathering", "hq": "Detroit, MI",
     "aliases": ["DT Midstream", "Vector Pipeline", "Millennium Pipeline", "Stonewall Gas"],
     "note": "Appalachia gathering + interstate."},
    {"key": "oneok", "name": "ONEOK", "type": "Gathering + transmission", "hq": "Tulsa, OK",
     "aliases": ["ONEOK", "Roadrunner", "Midwestern Gas Transmission", "Viking Gas", "Guardian Pipeline"],
     "note": "Mid-continent NGL + gas (incl. Magellan assets)."},
    {"key": "southwest_gas", "name": "Southwest Gas", "type": "Local distribution (LDC)", "hq": "Las Vegas, NV",
     "aliases": ["Southwest Gas", "Paiute Pipeline"],
     "note": "AZ / NV / CA distribution; Paiute interstate feeds Nevada."},
    {"key": "spire", "name": "Spire", "type": "LDC + transmission", "hq": "St. Louis, MO",
     "aliases": ["Spire", "Laclede", "Alagasco", "MoGas"],
     "note": "Midwest / Gulf distribution + Spire STL pipeline."},
]


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


# ── Tier gating (2026-05-30) ────────────────────────────────────────────────
# Mirror DCPI's api_scores() soft-paywall EXACTLY so logged-in paid users
# aren't wrongly gated. The numeric DCGI values (dcgi / gas_access /
# gas_cost + pipeline / operator counts + raw price) are the PAID product;
# non-paid callers (anon / free / identified) get the catalog — state code +
# the GAS-ADVANTAGED/ADEQUATE/GAS-CONSTRAINED verdict as a teaser — with the
# numbers masked SERVER-SIDE so they can't be scraped or unblurred.
#
# resolve_tier() only reads X-API-Key / Bearer JWT, NOT the website's session
# cookie, so a logged-in web user looks anonymous to it (that's why an
# enterprise user could see "Upgrade"). So we ALSO run the cookie-aware
# _detect_caller_tier() that /pockets + DCPI use and take the MORE privileged
# of the two — this never downgrades a paid caller. Fully defensive: any
# failure leaves the resolved plan at its current value (worst case the
# response is gated, never a crash). developer+ (and cookie-authed pro/
# enterprise/founding/internal/admin) get the full numbers.
_DCGI_PAID_PLANS = {"starter", "developer", "pro", "founding", "enterprise",
                    "admin", "internal"}
_DCGI_PLAN_RANK = {"anonymous": 0, "anon": 0, "free": 0, "identified": 1,
                   "starter": 2, "developer": 3, "pro": 4, "founding": 4,
                   "enterprise": 5, "admin": 6, "internal": 6}
# Numeric "gold" fields masked for non-paid. Identity (state) + verdict stay.
_DCGI_MASK_FIELDS = ("dcgi", "gas_access_score", "gas_cost_score",
                     "pipelines", "operators", "interstate",
                     "gas_price", "gas_price_sector", "gas_price_period")


def _dcgi_resolve_plan():
    """Resolve the caller's plan name (lowercased). Mirrors DCPI api_scores().
    Never raises — defaults to 'anonymous' and only ever upgrades from there."""
    plan = "anonymous"
    try:
        from util.tier_gate import resolve_tier
        _t, _ctx = resolve_tier()
        plan = (_ctx.get("plan") or _t.name).lower()
    except Exception:
        pass
    try:
        from map_tier_gating import _detect_caller_tier

        def _dec(_tok):
            try:
                import jwt as _j
                from main import JWT_SECRET
                return _j.decode(_tok, JWT_SECRET, algorithms=["HS256"])
            except Exception:
                return None

        _ct, _ = _detect_caller_tier(decode_jwt_func=_dec)
        _ct = (_ct or "anon").lower()
        if _DCGI_PLAN_RANK.get(_ct, -1) > _DCGI_PLAN_RANK.get(plan, -1):
            plan = _ct
    except Exception:
        pass
    return plan


# State lookup by lat/lng (approximate bounding boxes). Inlined VERBATIM from
# eia_gas_bulk_loader.STATE_BOXES / lat_lng_to_state so this module never depends
# on importing that script at runtime — under gunicorn the external import
# silently fails and dropped the rollup into the broken empty-state-column
# fallback, returning 0 states (ok=False). Keeping the mapping local fixes that.
_STATE_BOXES = {
    'AL': (30.2, 35.0, -88.5, -84.9), 'AK': (51.2, 71.4, -179.1, -129.9),
    'AZ': (31.3, 37.0, -114.8, -109.0), 'AR': (33.0, 36.5, -94.6, -89.6),
    'CA': (32.5, 42.0, -124.4, -114.1), 'CO': (37.0, 41.0, -109.1, -102.0),
    'CT': (41.0, 42.1, -73.7, -71.8), 'DE': (38.5, 39.8, -75.8, -75.0),
    'FL': (24.5, 31.0, -87.6, -80.0), 'GA': (30.4, 35.0, -85.6, -80.8),
    'HI': (18.9, 22.2, -160.2, -154.8), 'ID': (42.0, 49.0, -117.2, -111.0),
    'IL': (37.0, 42.5, -91.5, -87.5), 'IN': (37.8, 41.8, -88.1, -84.8),
    'IA': (40.4, 43.5, -96.6, -90.1), 'KS': (37.0, 40.0, -102.1, -94.6),
    'KY': (36.5, 39.1, -89.6, -81.9), 'LA': (29.0, 33.0, -94.0, -89.0),
    'ME': (43.1, 47.5, -71.1, -66.9), 'MD': (38.0, 39.7, -79.5, -75.0),
    'MA': (41.2, 42.9, -73.5, -69.9), 'MI': (41.7, 48.3, -90.4, -82.4),
    'MN': (43.5, 49.4, -97.2, -89.5), 'MS': (30.2, 35.0, -91.7, -88.1),
    'MO': (36.0, 40.6, -95.8, -89.1), 'MT': (44.4, 49.0, -116.0, -104.0),
    'NE': (40.0, 43.0, -104.1, -95.3), 'NV': (35.0, 42.0, -120.0, -114.0),
    'NH': (42.7, 45.3, -72.6, -70.7), 'NJ': (38.9, 41.4, -75.6, -73.9),
    'NM': (31.3, 37.0, -109.1, -103.0), 'NY': (40.5, 45.0, -79.8, -71.9),
    'NC': (33.8, 36.6, -84.3, -75.5), 'ND': (45.9, 49.0, -104.0, -96.6),
    'OH': (38.4, 42.0, -84.8, -80.5), 'OK': (33.6, 37.0, -103.0, -94.4),
    'OR': (42.0, 46.3, -124.6, -116.5), 'PA': (39.7, 42.3, -80.5, -74.7),
    'RI': (41.1, 42.0, -71.9, -71.1), 'SC': (32.0, 35.2, -83.4, -78.5),
    'SD': (42.5, 45.9, -104.1, -96.4), 'TN': (35.0, 36.7, -90.3, -81.6),
    'TX': (25.8, 36.5, -106.6, -93.5), 'UT': (37.0, 42.0, -114.1, -109.0),
    'VT': (42.7, 45.0, -73.4, -71.5), 'VA': (36.5, 39.5, -83.7, -75.2),
    'WA': (45.5, 49.0, -124.8, -116.9), 'WV': (37.2, 40.6, -82.6, -77.7),
    'WI': (42.5, 47.1, -92.9, -86.8), 'WY': (41.0, 45.0, -111.1, -104.1),
}


def _lat_lng_to_state(lat, lng):
    best = None
    best_dist = 999
    for state, (s, n, w, e) in _STATE_BOXES.items():
        if s <= lat <= n and w <= lng <= e:
            # Center distance for tiebreaking
            clat = (s + n) / 2
            clng = (w + e) / 2
            dist = math.sqrt((lat - clat)**2 + (lng - clng)**2)
            if dist < best_dist:
                best_dist = dist
                best = state
    return best or ''


# Gas-price scale for the cost score, $/Mcf. Industrial/electric delivered
# gas typically ranges ~$2.50 (Gulf) to ~$12 (New England constrained).
_PRICE_FLOOR = 2.5
_PRICE_CEIL = 12.0


def _gas_state_rollup():
    """Per-state gas access + cost + DCGI. Fail-soft: returns ({}, err) on
    any DB error so the caller can degrade gracefully. Time-capped (8s) to
    protect the 2-replica backend from a slow query starving the worker pool.

    The gas_pipelines.state column is empty for ~all rows, but lat/lng ARE
    populated, so we derive the 2-letter state code in Python from coordinates
    via the module-level _lat_lng_to_state (inlined from eia_gas_bulk_loader,
    NOT imported — the runtime import silently failed under gunicorn and dropped
    us into the broken empty-state-column fallback). The legacy state-column
    grouping is kept only as a defensive guard so this never crashes."""
    states = {}
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute("SET statement_timeout = 8000")
            if _lat_lng_to_state is not None:
                # Pull raw rows (no state filter — the column is empty) and
                # derive each row's state from coordinates in Python.
                cur.execute(
                    """
                    SELECT lat, lng, operator, pipeline_type
                    FROM gas_pipelines
                    """
                )
                # Per-state accumulators; operators dedup via a set.
                ops_sets = {}
                for lat, lng, operator, ptype in cur.fetchall():
                    if lat is None or lng is None:
                        continue
                    try:
                        st = _lat_lng_to_state(float(lat), float(lng))
                    except Exception:
                        st = ""
                    if not st or len(st) != 2:
                        continue
                    st = st.upper()
                    bucket = states.get(st)
                    if bucket is None:
                        bucket = {"state": st, "pipelines": 0,
                                  "operators": 0, "interstate": 0}
                        states[st] = bucket
                        ops_sets[st] = set()
                    bucket["pipelines"] += 1
                    if ptype == "interstate":
                        bucket["interstate"] += 1
                    if operator:
                        ops_sets[st].add(operator)
                for st, bucket in states.items():
                    bucket["operators"] = len(ops_sets.get(st, ()))
            else:
                # Legacy fallback: group by the (mostly empty) state column.
                cur.execute(
                    """
                    SELECT UPPER(state) AS st,
                           COUNT(*) AS n,
                           COUNT(DISTINCT operator) AS ops,
                           SUM(CASE WHEN pipeline_type = 'interstate' THEN 1 ELSE 0 END) AS inter
                    FROM gas_pipelines
                    WHERE state IS NOT NULL AND state <> '' AND LENGTH(state) = 2
                    GROUP BY UPPER(state)
                    """
                )
                for st, n, ops, inter in cur.fetchall():
                    states[st] = {
                        "state": st, "pipelines": int(n or 0),
                        "operators": int(ops or 0), "interstate": int(inter or 0),
                    }
            # Latest industrial / electric-power gas price per state — OPTIONAL.
            # 2026-05-30: this is the cost FACTOR only; a failure here (e.g.
            # eia_gas_prices not present in this DB) must NOT discard the
            # already-built state rollup (it just leaves cost neutral). Own
            # try + rollback so a missing table can't abort the whole function
            # — that bug was zeroing every DCGI score in production.
            try:
                cur.execute(
                    """
                    SELECT DISTINCT ON (UPPER(state)) UPPER(state) AS st, price, sector, period
                    FROM eia_gas_prices
                    WHERE price IS NOT NULL AND price > 0 AND (
                        sector ILIKE '%%indus%%' OR sector ILIKE '%%electric%%'
                        OR sector IN ('PIN', 'PEU'))
                    ORDER BY UPPER(state), period DESC
                    """
                )
                for st, price, sector, period in cur.fetchall():
                    if st in states:
                        states[st]["gas_price"] = round(float(price), 3)
                        states[st]["gas_price_sector"] = sector
                        states[st]["gas_price_period"] = str(period)
            except Exception:
                try: c.rollback()
                except Exception: pass
    except Exception as e:
        return {}, "rollup_error: " + str(e)[:200]

    if not states:
        # Diagnostic (2026-05-30): distinguish empty table vs NULL lat/lng vs
        # derivation-produced-nothing, so we fix the real cause instead of
        # guessing. Surfaced in the /api/v1/dcgi/scores error.
        diag = {"helper_present": _lat_lng_to_state is not None}
        try:
            with conn() as c, c.cursor() as cur:
                cur.execute("SET statement_timeout = 5000")
                cur.execute("SELECT COUNT(*), COUNT(lat), COUNT(lng) FROM gas_pipelines")
                total, nlat, nlng = cur.fetchone()
                cur.execute("SELECT lat, lng FROM gas_pipelines WHERE lat IS NOT NULL LIMIT 1")
                samp = cur.fetchone()
                diag.update({"total_rows": int(total or 0),
                             "rows_with_lat": int(nlat or 0),
                             "rows_with_lng": int(nlng or 0),
                             "sample_coord": [str(samp[0]), str(samp[1])] if samp else None})
        except Exception as e:
            diag["diag_error"] = str(e)[:160]
        return {}, "no states derived | diag=" + json.dumps(diag)

    max_n = max((s["pipelines"] for s in states.values()), default=1) or 1
    for s in states.values():
        s_density = (s["pipelines"] / max_n) * 100.0
        s_ops = _clamp((s["operators"] / 12.0) * 100.0)
        s_inter = (s["interstate"] / s["pipelines"] * 100.0) if s["pipelines"] else 0.0
        access = 0.45 * s_density + 0.35 * s_ops + 0.20 * s_inter

        price = s.get("gas_price")
        if price is not None and price > 0:
            cost = _clamp((_PRICE_CEIL - price) / (_PRICE_CEIL - _PRICE_FLOOR) * 100.0)
        else:
            cost = 50.0  # neutral when no price coverage

        dcgi = 0.60 * access + 0.40 * cost
        if dcgi >= 62 and access >= 50:
            verdict = "GAS-ADVANTAGED"
        elif dcgi >= 42:
            verdict = "ADEQUATE"
        else:
            verdict = "GAS-CONSTRAINED"

        s["gas_access_score"] = round(access, 1)
        s["gas_cost_score"] = round(cost, 1)
        s["dcgi"] = round(dcgi, 1)
        s["verdict"] = verdict
    return states, None


def _operator_rollup():
    """Roll EIA pipeline entities up to parent midstream companies with live
    counts. Returns (registry_list, raw_distinct_operator_count)."""
    raw = []
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute("SET statement_timeout = 8000")
            cur.execute(
                """SELECT operator, COUNT(*) AS n, COUNT(DISTINCT state) AS sts
                   FROM gas_pipelines WHERE operator IS NOT NULL AND operator <> ''
                   GROUP BY operator"""
            )
            raw = cur.fetchall()
    except Exception:
        raw = []

    total_distinct = len(raw)
    out = []
    for m in _MIDSTREAMS:
        segments = 0
        state_set = set()
        matched = []
        al = [a.lower() for a in m["aliases"]]
        for op, cnt, opsts in raw:
            ol = (op or "").lower()
            if any(a in ol for a in al):
                segments += int(cnt or 0)
                matched.append(op)
        out.append({
            "key": m["key"], "name": m["name"], "type": m["type"],
            "hq": m["hq"], "note": m["note"],
            "pipeline_segments": segments,
            "matched_entities": sorted(set(matched))[:12],
            "tracked": segments > 0,
        })
    out.sort(key=lambda x: x["pipeline_segments"], reverse=True)
    return out, total_distinct


_METHODOLOGY = {
    "index": "Data Center Gas Index (DCGI)",
    "purpose": ("Score US states on natural-gas suitability for siting data-center "
                "power load, for the behind-the-meter / gas-to-power era."),
    "scores": {
        "gas_access": "0.45*pipeline_density + 0.35*operator_diversity + 0.20*interstate_share",
        "gas_cost": "inverse of latest industrial/electric-power gas price ($/Mcf), floor $2.50 ceil $12",
        "dcgi": "0.60*gas_access + 0.40*gas_cost",
    },
    "verdicts": {
        "GAS-ADVANTAGED": "dcgi >= 62 and gas_access >= 50",
        "ADEQUATE": "dcgi >= 42",
        "GAS-CONSTRAINED": "dcgi < 42",
    },
    "data_sources": [
        "EIA natural-gas interstate/intrastate pipeline geodata (gas_pipelines)",
        "EIA natural-gas prices by sector & state (eia_gas_prices)",
    ],
    "caveats": [
        "Access is density/diversity-based; the EIA pipeline geofeed does not "
        "publish per-segment throughput (capacity_mcf), so this is a relative "
        "infrastructure-presence index, not a deliverability/firm-capacity model.",
        "Cost uses the latest available industrial or electric-power series per state.",
    ],
    "license": "CC BY 4.0 — cite 'DC Hub Data Center Gas Index (DCGI), dchub.cloud'",
}


# ── JSON API ───────────────────────────────────────────────────────────────
def _cache(resp, s_maxage=300, max_age=120):
    resp.headers["Cache-Control"] = "public, max-age=%d, s-maxage=%d" % (max_age, s_maxage)
    return resp


@dcgi_bp.route("/api/v1/dcgi/operators", methods=["GET"])
def dcgi_operators():
    ops, total_distinct = _operator_rollup()
    resp = jsonify({
        "ok": True,
        "index": "DCGI midstream operator registry",
        "note": ("Parent midstream companies mapped to the FERC pipeline "
                 "entities they operate, with live segment counts from EIA "
                 "pipeline geodata. The gas behind the grid."),
        "total_distinct_operators_tracked": total_distinct,
        "operators": ops,
        "license": "CC BY 4.0",
    })
    return _cache(resp), 200


@dcgi_bp.route("/api/v1/dcgi/scores", methods=["GET"])
def dcgi_scores():
    states, err = _gas_state_rollup()
    if err:
        return jsonify({"ok": False, "error": err}), 503
    ranked = sorted(states.values(), key=lambda s: s["dcgi"], reverse=True)
    limit = request.args.get("limit", type=int)
    if limit:
        ranked = ranked[:limit]

    # ── Soft-paywall the numeric scores (2026-05-30) ─────────────────────────
    # Mirrors DCPI api_scores(): non-paid (anon / free / identified) callers
    # get the full state catalog + verdict teaser, but the numeric DCGI fields
    # are masked to null server-side. Paid (developer+ / cookie-authed) get the
    # full numbers. The single-state lookup /api/v1/dcgi/scores/<state> and the
    # /dcgi + /pipeline-report HTML pages are intentionally left as the
    # discovery hook (HTML shows the teaser). Defensive: a resolver failure
    # leaves plan='anonymous' so worst case is gated, never a crash.
    _total_states = len(ranked)
    _gated = False
    _plan = _dcgi_resolve_plan()
    _paid = _plan in _DCGI_PAID_PLANS
    if not _paid:
        _masked = []
        for _s in ranked:
            _s = dict(_s)
            for _k in _DCGI_MASK_FIELDS:
                if _k in _s:
                    _s[_k] = None
            _s["locked"] = True
            _masked.append(_s)
        ranked = _masked
        _gated = True

    payload = {
        "ok": True,
        "index": "Data Center Gas Index (DCGI)",
        "count": len(ranked),
        "verdict_legend": ["GAS-ADVANTAGED", "ADEQUATE", "GAS-CONSTRAINED"],
        "states": ranked,
        "methodology": "/api/v1/dcgi/methodology",
        "license": "CC BY 4.0",
    }
    if _gated:
        payload["_gated"] = True
        payload["_total_available"] = _total_states
        payload["_locked_fields"] = list(_DCGI_MASK_FIELDS)
        payload["_required_tier"] = "pro"
        payload["_upgrade_cta"] = (
            "State list + GAS-ADVANTAGED/ADEQUATE/GAS-CONSTRAINED verdicts are "
            "free. The numeric DCGI scores (gas-access, gas-cost, composite "
            "DCGI) plus pipeline / operator counts are Pro — unlock all "
            f"{_total_states} states with scores at https://dchub.cloud/pricing."
        )
        payload["_signup_url"] = "https://dchub.cloud/pricing"
    resp = jsonify(payload)
    return _cache(resp), 200


@dcgi_bp.route("/api/v1/dcgi/scores/<state>", methods=["GET"])
def dcgi_score_state(state):
    st = (state or "").upper()[:2]
    states, err = _gas_state_rollup()
    if err:
        return jsonify({"ok": False, "error": err}), 503
    if st not in states:
        return jsonify({"ok": False, "error": "no DCGI data for state", "state": st,
                        "see": "/api/v1/dcgi/scores"}), 404
    resp = jsonify({"ok": True, "index": "DCGI", "state": st,
                    "score": states[st], "methodology": "/api/v1/dcgi/methodology",
                    "license": "CC BY 4.0"})
    return _cache(resp), 200


@dcgi_bp.route("/api/v1/dcgi/methodology", methods=["GET"])
def dcgi_methodology():
    return _cache(jsonify({"ok": True, "methodology": _METHODOLOGY})), 200


# ── Shareable pipeline report ───────────────────────────────────────────────
def _report_payload():
    states, err = _gas_state_rollup()
    ops, total_distinct = _operator_rollup()
    states = states or {}
    ranked = sorted(states.values(), key=lambda s: s["dcgi"], reverse=True)
    advantaged = [s for s in ranked if s["verdict"] == "GAS-ADVANTAGED"]
    total_pipelines = sum(s["pipelines"] for s in states.values())
    tracked_ops = [o for o in ops if o["tracked"]]
    return {
        "title": "The Gas Behind the Grid — DC Hub Pipeline Report",
        "subtitle": "Where natural gas can power AI data centers when the grid can't",
        "generated_for": "behind-the-meter / gas-to-power siting",
        "national": {
            "pipeline_segments": total_pipelines,
            "states_scored": len(states),
            "distinct_operators": total_distinct,
            "midstreams_tracked": len(tracked_ops),
            "gas_advantaged_states": len(advantaged),
        },
        "top_gas_advantaged": ranked[:12],
        "midstream_operators": ops,
        "methodology": _METHODOLOGY,
        "error": err,
        "license": "CC BY 4.0 — cite 'DC Hub Data Center Gas Index (DCGI), dchub.cloud'",
        "endpoints": {
            "scores": "/api/v1/dcgi/scores",
            "operators": "/api/v1/dcgi/operators",
            "methodology": "/api/v1/dcgi/methodology",
        },
    }


@dcgi_bp.route("/api/v1/reports/pipeline", methods=["GET"])
def pipeline_report_json():
    payload = _report_payload()
    jsonld = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "DC Hub Data Center Gas Index (DCGI) & Pipeline Report",
        "description": ("Per-state natural-gas suitability index for data-center "
                        "power siting, plus a midstream-operator registry."),
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "url": "https://dchub.cloud/pipeline-report",
        "keywords": ["data center", "natural gas", "pipeline", "gas-to-power",
                     "behind-the-meter", "DCGI", "midstream"],
    }
    resp = jsonify({"ok": True, "report": payload, "json_ld": jsonld,
                    "cite": "DC Hub Data Center Gas Index (DCGI), dchub.cloud, CC BY 4.0"})
    return _cache(resp), 200


_REPORT_CSS = """
:root{--bg:#0f1119;--panel:#171a26;--ink:#e8ecf5;--mut:#9aa3b8;--acc:#41d1a7;--warn:#f0b429;--bad:#ef6461;--line:#262b3a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:40px 22px 80px}
.kick{color:var(--acc);font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:12px}
h1{font-size:40px;line-height:1.1;margin:8px 0 6px;font-weight:800}
.sub{color:var(--mut);font-size:19px;margin:0 0 26px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:24px 0 34px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.stat b{display:block;font-size:30px;font-weight:800}
.stat span{color:var(--mut);font-size:13px}
h2{font-size:24px;margin:34px 0 12px;font-weight:800}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--line);font-size:14px}
th{color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.06em;font-size:11px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.v{font-weight:700;font-size:12px;padding:3px 9px;border-radius:999px;white-space:nowrap}
.v.adv{background:rgba(65,209,167,.16);color:var(--acc)}
.v.adq{background:rgba(240,180,41,.16);color:var(--warn)}
.v.con{background:rgba(239,100,97,.16);color:var(--bad)}
.note{color:var(--mut);font-size:13px;margin-top:8px}
.foot{margin-top:46px;padding-top:20px;border-top:1px solid var(--line);color:var(--mut);font-size:13px}
a{color:var(--acc)}
.bar{height:7px;border-radius:4px;background:#23283a;overflow:hidden;min-width:70px;display:inline-block;vertical-align:middle}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#41d1a7,#3aa0ff)}
"""


def _vclass(v):
    return {"GAS-ADVANTAGED": "adv", "ADEQUATE": "adq"}.get(v, "con")


@dcgi_bp.route("/pipeline-report", methods=["GET"])
@dcgi_bp.route("/reports/pipeline", methods=["GET"])
def pipeline_report_html():
    p = _report_payload()
    nat = p["national"]
    rows = []
    for s in p["top_gas_advantaged"]:
        price = s.get("gas_price")
        price_txt = ("$%.2f" % price) if price else "—"
        rows.append(
            "<tr><td><b>{st}</b></td>"
            "<td class=n>{dcgi}</td>"
            "<td class=n><span class=bar><i style=\"width:{accw}%\"></i></span> {acc}</td>"
            "<td class=n>{cost}</td>"
            "<td class=n>{pipes}</td>"
            "<td class=n>{ops}</td>"
            "<td class=n>{price}</td>"
            "<td><span class=\"v {vc}\">{v}</span></td></tr>".format(
                st=s["state"], dcgi=s["dcgi"], acc=s["gas_access_score"],
                accw=int(s["gas_access_score"]), cost=s["gas_cost_score"],
                pipes=s["pipelines"], ops=s["operators"], price=price_txt,
                vc=_vclass(s["verdict"]), v=s["verdict"]))
    state_rows = "".join(rows) or "<tr><td colspan=8 class=note>Scoring warming up…</td></tr>"

    op_rows = []
    for o in p["midstream_operators"]:
        if not o["tracked"]:
            continue
        op_rows.append(
            "<tr><td><b>{name}</b><div class=note>{note}</div></td>"
            "<td>{type}</td><td>{hq}</td><td class=n>{seg}</td></tr>".format(
                name=o["name"], note=o["note"], type=o["type"], hq=o["hq"],
                seg=o["pipeline_segments"]))
    op_rows_html = "".join(op_rows) or "<tr><td colspan=4 class=note>Registry warming up…</td></tr>"

    jsonld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "DC Hub Data Center Gas Index (DCGI) & Pipeline Report",
        "description": "Per-state natural-gas suitability index for data-center power siting.",
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "url": "https://dchub.cloud/pipeline-report",
    }

    html = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
        "<title>The Gas Behind the Grid — DC Hub Pipeline Report</title>"
        "<meta name=description content=\"Data Center Gas Index (DCGI): which US states "
        "can power AI data centers with natural gas when the grid can't. Plus a live "
        "midstream-operator registry. CC BY 4.0.\">"
        "<link rel=canonical href=\"https://dchub.cloud/pipeline-report\">"
        "<meta property=\"og:title\" content=\"The Gas Behind the Grid — DC Hub Pipeline Report\">"
        "<meta property=\"og:description\" content=\"DCGI: gas-to-power siting index for data centers.\">"
        "<style>" + _REPORT_CSS + "</style>"
        "<script type=\"application/ld+json\">" + json.dumps(jsonld) + "</script>"
        "</head><body><div class=wrap>"
        "<div class=kick>DC Hub · Data Center Gas Index</div>"
        "<h1>The Gas Behind the Grid</h1>"
        "<p class=sub>Where natural gas can power AI data centers when the grid can&rsquo;t. "
        "Grid interconnect queues run 5&ndash;7 years &mdash; so behind-the-meter gas is how "
        "capacity gets energized now.</p>"
        "<div class=stats>"
        "<div class=stat><b>" + str(nat["pipeline_segments"]) + "</b><span>Pipeline segments tracked</span></div>"
        "<div class=stat><b>" + str(nat["states_scored"]) + "</b><span>States scored</span></div>"
        "<div class=stat><b>" + str(nat["distinct_operators"]) + "</b><span>Distinct operators</span></div>"
        "<div class=stat><b>" + str(nat["midstreams_tracked"]) + "</b><span>Megacap midstreams</span></div>"
        "<div class=stat><b>" + str(nat["gas_advantaged_states"]) + "</b><span>Gas-advantaged states</span></div>"
        "</div>"
        "<h2>Top gas-advantaged states</h2>"
        "<table><thead><tr><th>State</th><th class=n>DCGI</th><th class=n>Gas access</th>"
        "<th class=n>Cost</th><th class=n>Pipelines</th><th class=n>Operators</th>"
        "<th class=n>$/Mcf</th><th>Verdict</th></tr></thead><tbody>"
        + state_rows +
        "</tbody></table>"
        "<p class=note>DCGI = 0.60 &times; gas access + 0.40 &times; gas cost. "
        "Access blends pipeline density, operator diversity and interstate share. "
        "<a href=\"/api/v1/dcgi/methodology\">Full methodology &rarr;</a></p>"
        "<h2>Midstream operators &mdash; the gas behind the markets</h2>"
        "<table><thead><tr><th>Parent midstream</th><th>Type</th><th>HQ</th>"
        "<th class=n>Segments</th></tr></thead><tbody>"
        + op_rows_html +
        "</tbody></table>"
        "<div class=foot>"
        "Data: EIA natural-gas pipeline geodata + EIA gas prices. "
        "Machine-readable: <a href=\"/api/v1/reports/pipeline\">/api/v1/reports/pipeline</a> · "
        "<a href=\"/api/v1/dcgi/scores\">/api/v1/dcgi/scores</a> · "
        "<a href=\"/api/v1/dcgi/operators\">/api/v1/dcgi/operators</a><br>"
        "License: CC BY 4.0 &mdash; cite &ldquo;DC Hub Data Center Gas Index (DCGI), dchub.cloud&rdquo;."
        "</div></div></body></html>")

    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=120, s-maxage=300"
    resp.headers["Link"] = "<https://creativecommons.org/licenses/by/4.0/>; rel=\"license\""
    return resp


@dcgi_bp.route("/dcgi", methods=["GET"])
def dcgi_html():
    """Branded /dcgi landing page — the full per-state DCGI table plus the
    midstream operator registry. Mirrors pipeline_report_html() and reuses the
    same _REPORT_CSS + _report_payload(). The /pipeline-report page leads with
    the 'gas behind the grid' narrative; this page is the index itself."""
    p = _report_payload()
    nat = p["national"]

    # Full per-state ranking (not just the top gas-advantaged slice). Re-derive
    # the complete ranked list so the index page shows every scored state.
    states, _err = _gas_state_rollup()
    states = states or {}
    ranked = sorted(states.values(), key=lambda s: s["dcgi"], reverse=True)

    rows = []
    for s in ranked:
        price = s.get("gas_price")
        price_txt = ("$%.2f" % price) if price else "—"
        rows.append(
            "<tr><td><b>{st}</b></td>"
            "<td class=n>{dcgi}</td>"
            "<td class=n><span class=bar><i style=\"width:{accw}%\"></i></span> {acc}</td>"
            "<td class=n>{cost}</td>"
            "<td class=n>{pipes}</td>"
            "<td class=n>{ops}</td>"
            "<td class=n>{price}</td>"
            "<td><span class=\"v {vc}\">{v}</span></td></tr>".format(
                st=s["state"], dcgi=s["dcgi"], acc=s["gas_access_score"],
                accw=int(s["gas_access_score"]), cost=s["gas_cost_score"],
                pipes=s["pipelines"], ops=s["operators"], price=price_txt,
                vc=_vclass(s["verdict"]), v=s["verdict"]))
    state_rows = "".join(rows) or "<tr><td colspan=8 class=note>Scoring warming up…</td></tr>"

    op_rows = []
    for o in p["midstream_operators"]:
        if not o["tracked"]:
            continue
        op_rows.append(
            "<tr><td><b>{name}</b><div class=note>{note}</div></td>"
            "<td>{type}</td><td>{hq}</td><td class=n>{seg}</td></tr>".format(
                name=o["name"], note=o["note"], type=o["type"], hq=o["hq"],
                seg=o["pipeline_segments"]))
    op_rows_html = "".join(op_rows) or "<tr><td colspan=4 class=note>Registry warming up…</td></tr>"

    jsonld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "DC Hub Data Center Gas Index (DCGI)",
        "description": "Per-state natural-gas suitability index for data-center power siting.",
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "url": "https://dchub.cloud/dcgi",
    }

    html = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
        "<title>DCGI — Data Center Gas Index</title>"
        "<meta name=description content=\"Data Center Gas Index (DCGI): every US state "
        "scored on natural-gas suitability for siting AI data-center power load, plus a "
        "live midstream-operator registry. CC BY 4.0.\">"
        "<link rel=canonical href=\"https://dchub.cloud/dcgi\">"
        "<meta property=\"og:title\" content=\"DCGI — Data Center Gas Index\">"
        "<meta property=\"og:description\" content=\"DCGI: per-state gas-to-power siting index for data centers.\">"
        "<style>" + _REPORT_CSS + "</style>"
        "<script type=\"application/ld+json\">" + json.dumps(jsonld) + "</script>"
        "</head><body><div class=wrap>"
        "<div class=kick>DC Hub · Data Center Gas Index</div>"
        "<h1>DCGI &mdash; Data Center Gas Index</h1>"
        "<p class=sub>The gas analog to DCPI: every US state scored on natural-gas "
        "suitability for siting data-center power load. Grid interconnect queues run "
        "5&ndash;7 years &mdash; so behind-the-meter gas is how AI capacity gets "
        "energized now.</p>"
        "<div class=stats>"
        "<div class=stat><b>" + str(nat["pipeline_segments"]) + "</b><span>Pipeline segments tracked</span></div>"
        "<div class=stat><b>" + str(nat["states_scored"]) + "</b><span>States scored</span></div>"
        "<div class=stat><b>" + str(nat["distinct_operators"]) + "</b><span>Distinct operators</span></div>"
        "<div class=stat><b>" + str(nat["midstreams_tracked"]) + "</b><span>Megacap midstreams</span></div>"
        "<div class=stat><b>" + str(nat["gas_advantaged_states"]) + "</b><span>Gas-advantaged states</span></div>"
        "</div>"
        "<h2>Per-state DCGI ranking</h2>"
        "<table><thead><tr><th>State</th><th class=n>DCGI</th><th class=n>Gas access</th>"
        "<th class=n>Cost</th><th class=n>Pipelines</th><th class=n>Operators</th>"
        "<th class=n>$/Mcf</th><th>Verdict</th></tr></thead><tbody>"
        + state_rows +
        "</tbody></table>"
        "<p class=note>DCGI = 0.60 &times; gas access + 0.40 &times; gas cost. "
        "Access blends pipeline density, operator diversity and interstate share. "
        "<a href=\"/api/v1/dcgi/methodology\">Full methodology &rarr;</a></p>"
        "<h2>Midstream operator registry &mdash; the gas behind the markets</h2>"
        "<table><thead><tr><th>Parent midstream</th><th>Type</th><th>HQ</th>"
        "<th class=n>Segments</th></tr></thead><tbody>"
        + op_rows_html +
        "</tbody></table>"
        "<div class=foot>"
        "Data: EIA natural-gas pipeline geodata + EIA gas prices. "
        "Machine-readable: <a href=\"/api/v1/dcgi/scores\">/api/v1/dcgi/scores</a> · "
        "<a href=\"/api/v1/dcgi/operators\">/api/v1/dcgi/operators</a> · "
        "narrative report: <a href=\"/pipeline-report\">/pipeline-report</a><br>"
        "License: CC BY 4.0 &mdash; cite &ldquo;DC Hub Data Center Gas Index (DCGI), dchub.cloud&rdquo;."
        "</div></div></body></html>")

    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=120, s-maxage=300"
    resp.headers["Link"] = "<https://creativecommons.org/licenses/by/4.0/>; rel=\"license\""
    return resp


def register_dcgi(app):
    app.register_blueprint(dcgi_bp)
