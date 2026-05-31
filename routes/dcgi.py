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


# ── /dcgi dashboard — full visual + structural parity with /dcpi ────────────
# 2026-05-31: the prior /dcgi was a thin ~18KB table reusing _REPORT_CSS. This
# rewrite mirrors routes/dcpi.py's DCPI_INDEX_TEMPLATE so the two indices look
# like siblings: same dark dchub-brand palette + Inter/JetBrains-Mono fonts,
# sticky top-nav, LIVE status strip, hero, stats row, a state LEADERBOARD
# (the gas analog of DCPI's iso-grid — color-coded GAS-ADVANTAGED / ADEQUATE /
# GAS-CONSTRAINED), a Chart.js section, an "Ask the Gas Index" box, a Daily
# Brief subscribe block, a Pro CTA, methodology, footer, JSON-LD Dataset
# (CC BY 4.0), and a prominent ⚡ cross-link to /dcpi.
#
# IMPLEMENTATION NOTE (f-string / brace safety): the template carries hundreds
# of literal { } in its CSS and inline JS. To avoid %-escaping / .format() /
# f-string brace pitfalls (the recurring %s-in-f-string bug class), this is a
# PLAIN string-concatenation build (matching the rest of this module) — NOT an
# f-string and NOT .format(). The only dynamic Python values are the five
# national stat numbers, concatenated via + str(...) +, and the JSON-LD via
# json.dumps(). All CSS/JS braces stay literal and need no escaping.
#
# RESILIENCE: the leaderboard + chart are rendered CLIENT-SIDE from
# /api/v1/dcgi/scores (never /api/v1/dcpi/*). That endpoint soft-paywalls the
# numeric fields for anonymous callers (masks dcgi / gas_access_score / etc. to
# null with locked:true), so the JS guards every numeric read against null and
# degrades each section gracefully — an empty/locked/erroring endpoint shows a
# friendly message, never a JS crash or blank page.

# Shared dchub-brand dashboard CSS (mirrors DCPI_INDEX_TEMPLATE's <style>),
# recolored for GAS: emerald accent (gas-advantaged green) + the same
# amber/red verdict ramp, plus the indigo/violet gradient kept for the
# power-flywheel cross-link so it visually rhymes with /dcpi.
_DCGI_DASH_CSS = """
:root{
  --bg:#0a0a12; --bg2:#0f1119; --bg3:#181a25; --card:#11121a; --card-hi:#1a1c28;
  --bd:#1f2030; --bd-hi:#2a2c3e; --tx:#fff; --tx2:#9ca3af; --tx3:#6b7280;
  --acc:#10b981; --acc-light:#34d399; --acc-vivid:#34d399;
  --green:#10b981; --orange:#f59e0b; --red:#ef4444;
  --gradient:linear-gradient(135deg,#10b981 0%,#0ea5e9 100%);
  --power:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
}
*{box-sizing:border-box}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;background:var(--bg);color:var(--tx);margin:0;padding:0;line-height:1.55;-webkit-font-smoothing:antialiased}
code,pre,.mono{font-family:'JetBrains Mono',monospace}
.top-nav{border-bottom:1px solid var(--bd);background:rgba(10,10,18,0.85);backdrop-filter:blur(8px);position:sticky;top:0;z-index:100}
.top-nav-inner{max-width:1280px;margin:0 auto;padding:1rem 1.5rem;display:flex;align-items:center;justify-content:space-between;gap:1.5rem}
.logo{font-weight:800;font-size:1.05rem;color:var(--tx);text-decoration:none;letter-spacing:-0.01em}
.logo span{color:var(--acc)}
.nav-links{display:flex;gap:1.5rem;flex-wrap:wrap}
.nav-links a{color:var(--tx2);text-decoration:none;font-size:0.92rem;font-weight:500;position:relative}
.nav-links a:hover{color:var(--tx)}
.nav-links a.active{color:var(--tx)}
.nav-links a sup{color:var(--green);font-size:0.55rem;font-weight:800;letter-spacing:0.04em;margin-left:0.2rem;vertical-align:super}
.status-strip{background:var(--bg2);border-bottom:1px solid var(--bd);padding:0.55rem 1.5rem;text-align:center;font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--tx2);letter-spacing:0.04em;text-transform:uppercase}
.pulse{display:inline-block;width:8px;height:8px;background:var(--green);border-radius:50%;margin-right:0.5rem;animation:pulse 1.6s ease-in-out infinite;vertical-align:middle}
@keyframes pulse{50%{opacity:0.3;transform:scale(0.85)}}
.wrap{max-width:1280px;margin:0 auto;padding:3rem 1.5rem}
.hero{margin-bottom:2rem}
.hero h1{font-size:clamp(2.4rem,5vw,3.6rem);margin:0 0 1rem;font-weight:800;letter-spacing:-0.025em;line-height:1.05}
.hero h1 .accent{background:var(--gradient);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero .lede{color:var(--tx2);font-size:1.1rem;max-width:720px;margin:0 0 1.5rem}
.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin:2rem 0 3rem;padding:1.5rem;background:var(--card);border:1px solid var(--bd);border-radius:12px}
.stat .num{font-family:'JetBrains Mono',monospace;font-size:2rem;font-weight:700;color:var(--tx);letter-spacing:-0.02em}
.stat .label{color:var(--tx2);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.06em;margin-top:0.3rem}
.section-h{display:flex;align-items:center;gap:0.6rem;margin:3rem 0 1rem;font-size:0.78rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:var(--tx2)}
.section-h .pip{width:4px;height:12px;background:var(--acc);border-radius:2px}
h2{font-size:1.6rem;font-weight:700;margin:0 0 1rem;letter-spacing:-0.015em}
.toggle{display:inline-flex;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;margin:0 0 1.5rem}
.toggle button{background:transparent;color:var(--tx2);border:0;padding:0.7rem 1.25rem;cursor:pointer;font-weight:600;font-size:0.85rem;font-family:inherit;transition:all 0.15s}
.toggle button.active{background:var(--gradient);color:white}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:1.4rem 1.5rem;transition:all 0.18s ease;position:relative;overflow:hidden}
.card:hover{transform:translateY(-3px);border-color:var(--bd-hi);background:var(--card-hi);box-shadow:0 12px 32px rgba(16,185,129,0.10)}
.card .market-name{font-size:1.1rem;font-weight:600;margin:0 0 0.25rem;letter-spacing:-0.01em}
.card .iso{color:var(--tx2);font-family:'JetBrains Mono',monospace;font-size:0.75rem;margin-bottom:1rem}
.score{font-family:'JetBrains Mono',monospace;font-size:2.6rem;font-weight:800;line-height:1;letter-spacing:-0.04em}
.score.green{color:var(--green)}
.score.orange{color:var(--orange)}
.score.red{color:var(--red)}
.label{color:var(--tx2);font-size:0.7rem;text-transform:uppercase;letter-spacing:0.08em;margin-top:0.4rem;font-weight:600}
.verdict{display:inline-block;padding:0.22rem 0.7rem;border-radius:5px;font-size:0.7rem;font-weight:800;letter-spacing:0.06em;margin-top:0.9rem}
.verdict.adv{background:rgba(16,185,129,0.18);color:var(--green)}
.verdict.adq{background:rgba(245,158,11,0.18);color:var(--orange)}
.verdict.con{background:rgba(239,68,68,0.18);color:var(--red)}
.subline{font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--tx2);margin-top:0.55rem}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-top:0.5rem}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--bd);font-size:14px}
th{color:var(--tx2);font-weight:600;text-transform:uppercase;letter-spacing:.06em;font-size:11px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.bar{height:7px;border-radius:4px;background:#23283a;overflow:hidden;min-width:70px;display:inline-block;vertical-align:middle}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#10b981,#0ea5e9)}
.note{color:var(--tx2);font-size:13px;margin-top:8px}
.cta-banner{background:var(--gradient);padding:2rem 2.25rem;border-radius:14px;margin:3rem 0 2rem;position:relative;overflow:hidden}
.cta-banner::after{content:'';position:absolute;right:-40px;bottom:-40px;width:200px;height:200px;background:radial-gradient(circle,rgba(255,255,255,0.15),transparent 70%);pointer-events:none}
.cta-banner h2{margin:0 0 0.4rem;font-size:1.4rem;color:white}
.cta-banner p{margin:0 0 1.1rem;color:rgba(255,255,255,0.88);font-size:0.95rem;max-width:540px}
.cta-banner a.btn{display:inline-block;background:white;color:#0b8f63;padding:0.7rem 1.3rem;border-radius:7px;text-decoration:none;font-weight:700;font-size:0.92rem;transition:transform 0.1s}
.cta-banner a.btn:hover{transform:translateY(-1px)}
.power-cross{display:block;text-decoration:none;background:var(--power);border-radius:14px;padding:1.6rem 2rem;margin:2rem 0 1rem;position:relative;overflow:hidden;transition:transform 0.12s}
.power-cross:hover{transform:translateY(-2px)}
.power-cross .pk{font-family:'JetBrains Mono',monospace;font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:rgba(255,255,255,.82);margin-bottom:6px}
.power-cross .pt{font-size:1.25rem;font-weight:800;color:#fff;letter-spacing:-0.01em}
.power-cross .pp{color:rgba(255,255,255,.9);font-size:0.95rem;margin:0.35rem 0 0;max-width:640px}
footer{border-top:1px solid var(--bd);margin-top:3rem;padding:2rem 0 1rem;color:var(--tx3);font-size:0.84rem}
footer a{color:var(--tx2)}
footer a:hover{color:var(--acc-light)}
@media (max-width:600px){.nav-links{display:none}}
"""


# Client-side hydration for /dcgi. Kept as a separate plain-string constant
# (no f-string / .format()) so its many literal { } braces need no escaping.
# Every numeric read is null-guarded because /api/v1/dcgi/scores masks the
# numeric fields (dcgi / gas_access_score / gas_cost_score / pipelines /
# operators / gas_price) to null with locked:true for anonymous callers — the
# leaderboard then shows the verdict-only teaser, and the chart hides itself
# rather than crashing. Fetches NEVER target /api/v1/dcpi/*.
_DCGI_DASH_JS = r"""
(function(){
  var VCLASS = function(v){
    if (v === 'GAS-ADVANTAGED') return 'adv';
    if (v === 'ADEQUATE') return 'adq';
    return 'con';
  };
  var STATE = { rows: [], mode: 'dcgi', locked: false };

  function num(v){ return (v === null || v === undefined) ? null : v; }
  function scoreClass(v){
    if (v === null) return '';
    if (v >= 62) return 'green';
    if (v >= 42) return 'orange';
    return 'red';
  }
  function fmt(v){ return (v === null || v === undefined) ? '—' : v; }

  function renderGrid(){
    var grid = document.getElementById('dcgi-grid');
    if (!grid) return;
    var rows = STATE.rows.slice();
    var mode = STATE.mode;
    rows.sort(function(a,b){
      var ka = mode === 'access' ? num(a.gas_access_score) : num(a.dcgi);
      var kb = mode === 'access' ? num(b.gas_access_score) : num(b.dcgi);
      // null (locked) values sort last but keep stable verdict order otherwise
      ka = (ka === null) ? -1 : ka; kb = (kb === null) ? -1 : kb;
      return kb - ka;
    });
    if (!rows.length){
      grid.innerHTML = '<div style="grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;">DCGI scoring is being recomputed — check back shortly.</div>';
      return;
    }
    grid.innerHTML = rows.map(function(s){
      var primary = mode === 'access' ? num(s.gas_access_score) : num(s.dcgi);
      var primaryLabel = mode === 'access' ? 'Gas Access' : 'DCGI';
      var vc = VCLASS(s.verdict || '');
      var pipes = num(s.pipelines), ops = num(s.operators), price = num(s.gas_price);
      var sub;
      if (STATE.locked || primary === null){
        sub = '<div class="subline">🔒 Score is Pro · <a href="/pricing" style="color:var(--acc-light);text-decoration:none">unlock</a></div>';
      } else {
        var bits = [];
        if (pipes !== null) bits.push(pipes + ' pipes');
        if (ops !== null) bits.push(ops + ' ops');
        if (price !== null) bits.push('$' + Number(price).toFixed(2) + '/Mcf');
        sub = '<div class="subline">' + (bits.join(' · ') || '&nbsp;') + '</div>';
      }
      var scoreHtml = (primary === null)
        ? '<div class="score" style="color:var(--tx3)">🔒</div>'
        : '<div class="score ' + scoreClass(primary) + '">' + primary + '</div>';
      return '<div class="card">'
        + '<div class="market-name">' + (s.state || '?') + '</div>'
        + '<div class="iso">United States · gas</div>'
        + scoreHtml
        + '<div class="label">' + primaryLabel + '</div>'
        + '<div class="verdict ' + vc + '">' + (s.verdict || '—') + '</div>'
        + sub
        + '</div>';
    }).join('');
  }

  function renderChart(){
    var canvas = document.getElementById('dcgi-dist-chart');
    var section = document.getElementById('dcgi-chart-section');
    if (!canvas || typeof Chart === 'undefined'){ if (section) section.style.display='none'; return; }
    // Need real numbers — if locked/masked, hide the chart (degrade gracefully).
    var scored = STATE.rows.filter(function(s){ return num(s.dcgi) !== null; });
    if (!scored.length){ if (section) section.style.display='none'; return; }
    scored.sort(function(a,b){ return b.dcgi - a.dcgi; });
    var top = scored.slice(0, 25);
    var labels = top.map(function(s){ return s.state; });
    var data = top.map(function(s){ return s.dcgi; });
    var bg = top.map(function(s){
      var v = s.verdict || '';
      if (v === 'GAS-ADVANTAGED') return '#10b981';
      if (v === 'ADEQUATE') return '#f59e0b';
      return '#ef4444';
    });
    try {
      new Chart(canvas, {
        type: 'bar',
        data: { labels: labels, datasets: [{ label: 'DCGI', data: data, backgroundColor: bg, borderRadius: 4 }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#9ca3af', maxRotation: 0, autoSkip: true }, grid: { display: false } },
            y: { ticks: { color: '#9ca3af' }, grid: { color: '#1f2030' }, suggestedMin: 0, suggestedMax: 100 }
          }
        }
      });
    } catch(e){ if (section) section.style.display='none'; }
  }

  // Hydrate leaderboard + chart from /api/v1/dcgi/scores (NEVER /api/v1/dcpi).
  fetch('/api/v1/dcgi/scores').then(function(r){ return r.json(); }).then(function(d){
    if (!d || d.ok === false){ renderGrid(); renderChart(); return; }
    STATE.rows = (d && d.states) || [];
    STATE.locked = !!d._gated;
    renderGrid();
    renderChart();
  }).catch(function(){
    var grid = document.getElementById('dcgi-grid');
    if (grid) grid.innerHTML = '<div style="grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;">DCGI leaderboard temporarily offline.</div>';
    var section = document.getElementById('dcgi-chart-section');
    if (section) section.style.display = 'none';
  });

  // Toggle: DCGI composite vs Gas Access.
  var toggleBtns = document.querySelectorAll('.toggle button');
  toggleBtns.forEach(function(b){
    b.addEventListener('click', function(){
      toggleBtns.forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      STATE.mode = b.getAttribute('data-mode') || 'dcgi';
      renderGrid();
    });
  });

  // Daily Brief subscribe — shared /api/v1/digest/subscribe endpoint.
  var f = document.getElementById('dcgi-sub-form');
  if (f){
    f.addEventListener('submit', function(e){
      e.preventDefault();
      var em = (document.getElementById('dcgi-sub-email').value || '').trim();
      var msg = document.getElementById('dcgi-sub-msg');
      var btn = document.getElementById('dcgi-sub-go');
      btn.disabled = true; msg.textContent = 'Subscribing...';
      fetch('/api/v1/digest/subscribe', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({email: em})
      }).then(function(r){ return r.json(); }).then(function(d){
        if (d && d.ok){
          msg.innerHTML = '<span style="color:#10b981">✓ You\'re in. First brief lands tomorrow at 14:00 UTC.</span>';
          document.getElementById('dcgi-sub-email').value = '';
        } else {
          msg.innerHTML = '<span style="color:#ef4444">' + ((d && d.error) || 'error') + '</span>';
        }
      }).catch(function(err){
        msg.innerHTML = '<span style="color:#ef4444">Error: ' + err + '</span>';
      }).finally(function(){ btn.disabled = false; });
    });
  }

  // Ask the Gas Index — reuses /api/v1/dcpi/ask (the shared DC-power Q&A
  // tool-loop; gas-to-power is a DC power topic). No /api/v1/dcgi/ask exists,
  // so we frame the prompt for gas but delegate to the working endpoint.
  (function(){
    var go = document.getElementById('gask-go');
    var q = document.getElementById('gask-q');
    var out = document.getElementById('gask-out');
    if (!go || !q || !out) return;
    function showError(m){ out.innerHTML = '<span style="color:#ef4444;">' + m + '</span>'; }
    function send(){
      var question = (q.value || '').trim();
      if (!question){ q.focus(); return; }
      out.innerHTML = '<em style="color:#9ca3af;">Thinking…</em>';
      go.disabled = true; go.style.opacity = '0.6';
      fetch('/api/v1/dcpi/ask?q=' + encodeURIComponent(question), {
        method: 'GET', headers: { 'Accept': 'application/json' }, credentials: 'same-origin'
      }).then(function(resp){
        if (!resp.ok){ return resp.text().then(function(t){ showError('HTTP ' + resp.status + ': ' + t.slice(0,200)); }); }
        return resp.json().then(function(data){
          if (data && data.error){ showError(data.error); return; }
          var answer = (data && data.answer || 'No answer.')
            .replace(/\n/g, '<br>')
            .replace(/\[([^\]]+)\]/g, '<strong style="color:#6ee7b7">[$1]</strong>');
          out.innerHTML = answer;
        });
      }).catch(function(e){
        showError('Error: ' + (e && e.message ? e.message : e));
      }).finally(function(){ go.disabled = false; go.style.opacity = '1'; });
    }
    go.addEventListener('click', send);
    q.addEventListener('keydown', function(e){
      if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
    });
  })();
})();
"""


@dcgi_bp.route("/dcgi", methods=["GET"])
def dcgi_html():
    """Branded /dcgi dashboard — full visual + structural parity with /dcpi.

    Server renders the shell (nav, hero, stats, section frames, JSON-LD,
    cross-links) with the five national stat numbers injected from
    _report_payload(); the state LEADERBOARD, the Chart.js distribution
    chart, and the midstream-operator registry are hydrated client-side from
    /api/v1/dcgi/scores + /api/v1/dcgi/operators so they stay fresh and
    degrade gracefully if an endpoint is empty/locked/down. Mirrors
    routes/dcpi.py DCPI_INDEX_TEMPLATE (iso-grid, chart section, ask box,
    subscribe, pro-cta) but every data fetch targets /api/v1/dcgi/*."""
    p = _report_payload()
    nat = p["national"]

    # Static fallback leaderboard + operator rows so the page is never empty
    # even with JS disabled / before hydration (progressive enhancement).
    states, _err = _gas_state_rollup()
    states = states or {}
    ranked = sorted(states.values(), key=lambda s: s["dcgi"], reverse=True)

    noscript_rows = []
    for s in ranked:
        price = s.get("gas_price")
        price_txt = ("$%.2f" % price) if price else "—"
        noscript_rows.append(
            "<tr><td><b>{st}</b></td>"
            "<td class=n>{dcgi}</td>"
            "<td class=n>{acc}</td>"
            "<td class=n>{cost}</td>"
            "<td class=n>{pipes}</td>"
            "<td class=n>{ops}</td>"
            "<td class=n>{price}</td>"
            "<td><span class=\"verdict {vc}\">{v}</span></td></tr>".format(
                st=s["state"], dcgi=s["dcgi"], acc=s["gas_access_score"],
                cost=s["gas_cost_score"], pipes=s["pipelines"],
                ops=s["operators"], price=price_txt,
                vc=_vclass(s["verdict"]), v=s["verdict"]))
    noscript_table = ("<table><thead><tr><th>State</th><th class=n>DCGI</th>"
                      "<th class=n>Gas access</th><th class=n>Cost</th>"
                      "<th class=n>Pipelines</th><th class=n>Operators</th>"
                      "<th class=n>$/Mcf</th><th>Verdict</th></tr></thead><tbody>"
                      + ("".join(noscript_rows)
                         or "<tr><td colspan=8 class=note>Scoring warming up…</td></tr>")
                      + "</tbody></table>")

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
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "Data Center Gas Index (DCGI)",
        "alternateName": "DCGI",
        "description": ("Per-state natural-gas suitability index for siting AI "
                        "data-center power load — the behind-the-meter / gas-to-power "
                        "thesis. Blends pipeline density, midstream-operator diversity, "
                        "interstate share and delivered gas price into a 0–100 DCGI with "
                        "a GAS-ADVANTAGED / ADEQUATE / GAS-CONSTRAINED verdict per state."),
        "url": "https://dchub.cloud/dcgi",
        "sameAs": "https://dchub.cloud/dcgi",
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "keywords": ("data center, natural gas, gas index, pipeline, gas-to-power, "
                     "behind-the-meter, DCGI, midstream, Energy Transfer, Kinder Morgan, "
                     "Williams, Transco"),
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "isAccessibleForFree": True,
        "spatialCoverage": {"@type": "Place", "name": "United States"},
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": "https://dchub.cloud/api/v1/dcgi/scores",
             "name": "All state DCGI scores (current)"},
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": "https://dchub.cloud/api/v1/dcgi/operators",
             "name": "Midstream operator registry"},
        ],
        "citation": "DC Hub Data Center Gas Index (DCGI). https://dchub.cloud/dcgi",
    }

    html = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>DCGI · Data Center Gas Index | DC Hub</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"description\" content=\"DCGI (Data Center Gas Index) scores every "
        "US state on natural-gas suitability for siting AI data-center power load — the "
        "behind-the-meter / gas-to-power play when the grid queue is 5–7 years. Live "
        "midstream-operator registry. The gas analog to DCPI. CC BY 4.0.\">"
        "<meta property=\"og:title\" content=\"DCGI — The Data Center Gas Index | DC Hub\">"
        "<meta property=\"og:description\" content=\"Every US state scored on gas-to-power "
        "suitability for data centers. The gas behind the grid — when interconnection "
        "queues run 5–7 years.\">"
        "<meta property=\"og:image\" content=\"https://dchub.cloud/dcpi/og.svg\">"
        "<meta property=\"og:url\" content=\"https://dchub.cloud/dcgi\">"
        "<meta name=\"twitter:card\" content=\"summary_large_image\">"
        "<meta name=\"robots\" content=\"index,follow,max-snippet:-1,max-image-preview:large\">"
        "<link rel=\"canonical\" href=\"https://dchub.cloud/dcgi\">"
        "<script type=\"application/ld+json\">" + json.dumps(jsonld) + "</script>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&display=swap\" rel=\"stylesheet\">"
        "<style>" + _DCGI_DASH_CSS + "</style>"
        "</head><body>"
        # ── top nav (mirrors /dcpi) ──
        "<nav class=\"top-nav\"><div class=\"top-nav-inner\">"
        "<a class=\"logo\" href=\"/\">DC <span>Hub</span></a>"
        "<div class=\"nav-links\">"
        "<a href=\"/\">Home</a>"
        "<a href=\"/markets\">Markets</a>"
        "<a href=\"/dcpi\">DCPI</a>"
        "<a href=\"/dcgi\" class=\"active\">DCGI<sup>NEW</sup></a>"
        "<a href=\"/land-power\">Land &amp; Power</a>"
        "<a href=\"/ai\">AI Platform</a>"
        "<a href=\"/news\">News</a>"
        "<a href=\"/pricing\">Pricing</a>"
        "</div></div></nav>"
        # ── live status strip ──
        "<div class=\"status-strip\"><span class=\"pulse\"></span>"
        "LIVE · " + str(nat["states_scored"]) + " STATES SCORED · "
        + str(nat["pipeline_segments"]) + " PIPELINE SEGMENTS · FREE FOR PRESS CITATION"
        "</div>"
        "<div class=\"wrap\">"
        # ── hero ──
        "<section class=\"hero\">"
        "<h1>The <span class=\"accent\">Data Center Gas Index</span></h1>"
        "<p class=\"lede\">The gas analog to DCPI. Every US state scored on natural-gas "
        "suitability for siting AI data-center power load. Grid interconnect queues run "
        "5&ndash;7 years &mdash; so <strong>behind-the-meter gas</strong> is increasingly "
        "how AI capacity actually gets energized this decade. No one else scores the gas "
        "behind the grid.</p>"
        "</section>"
        # ── power-flywheel cross-link (⚡ → /dcpi) ──
        "<a class=\"power-cross\" href=\"/dcpi\">"
        "<div class=\"pk\">⚡ The other half of the story</div>"
        "<div class=\"pt\">See the power story &rarr; DC Hub Power Index (DCPI)</div>"
        "<p class=\"pp\">Gas is how capacity gets energized when the grid can&rsquo;t. DCPI "
        "scores the grid itself &mdash; where the interconnection queue is dead and where "
        "stranded excess power is hiding in plain sight across U.S. markets.</p>"
        "</a>"
        # ── stats row ──
        "<div class=\"stats-row\">"
        "<div class=\"stat\"><div class=\"num\">" + str(nat["states_scored"]) + "</div><div class=\"label\">States Scored</div></div>"
        "<div class=\"stat\"><div class=\"num\">" + str(nat["pipeline_segments"]) + "</div><div class=\"label\">Pipeline Segments</div></div>"
        "<div class=\"stat\"><div class=\"num\">" + str(nat["distinct_operators"]) + "</div><div class=\"label\">Distinct Operators</div></div>"
        "<div class=\"stat\"><div class=\"num\">" + str(nat["midstreams_tracked"]) + "</div><div class=\"label\">Megacap Midstreams</div></div>"
        "<div class=\"stat\"><div class=\"num\">" + str(nat["gas_advantaged_states"]) + "</div><div class=\"label\">Gas-Advantaged States</div></div>"
        "</div>"
        # ── leaderboard (gas analog of DCPI iso-grid) ──
        "<div class=\"section-h\"><span class=\"pip\"></span>🔥 State Leaderboard</div>"
        "<div class=\"toggle\" role=\"tablist\" aria-label=\"Switch score axis\">"
        "<button class=\"active\" data-mode=\"dcgi\">DCGI · Composite</button>"
        "<button data-mode=\"access\">Gas Access</button>"
        "</div>"
        "<div class=\"grid\" id=\"dcgi-grid\">"
        "<div style=\"grid-column:1/-1;color:var(--tx2);font-size:0.85rem;padding:14px;text-align:center;border:1px dashed rgba(255,255,255,0.06);border-radius:10px;\">Loading state leaderboard…</div>"
        "</div>"
        # ── Chart.js distribution chart (mirrors DCPI chart section) ──
        "<script src=\"https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js\"></script>"
        "<div id=\"dcgi-chart-section\" style=\"margin:3rem 0;background:#11121a;border:1px solid #1f2030;border-radius:14px;padding:1.5rem;\">"
        "<div style=\"display:flex;align-items:center;gap:0.6rem;margin-bottom:1rem;\">"
        "<span style=\"width:4px;height:12px;background:#10b981;border-radius:2px;\"></span>"
        "<span style=\"font-size:0.78rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#9ca3af;\">📊 DCGI by state · distribution</span>"
        "</div>"
        "<div style=\"position:relative;height:300px;\"><canvas id=\"dcgi-dist-chart\"></canvas></div>"
        "</div>"
        # ── methodology ──
        "<div class=\"section-h\"><span class=\"pip\"></span>📋 Methodology</div>"
        "<p style=\"color:var(--tx2);font-size:0.92rem;max-width:760px;\">"
        "<strong style=\"color:var(--acc-light);\">DCGI</strong> = 0.60 &times; Gas Access + "
        "0.40 &times; Gas Cost. <strong>Gas Access</strong> blends pipeline density, "
        "midstream-operator diversity and interstate share (the EIA geofeed publishes no "
        "per-segment throughput, so this is a relative infrastructure-presence index). "
        "<strong>Gas Cost</strong> inverts the latest industrial / electric-power delivered "
        "gas price ($/Mcf). Verdicts: <strong style=\"color:var(--green)\">GAS-ADVANTAGED</strong> "
        "(dcgi&nbsp;&ge;&nbsp;62 &amp; access&nbsp;&ge;&nbsp;50), "
        "<strong style=\"color:var(--orange)\">ADEQUATE</strong> (dcgi&nbsp;&ge;&nbsp;42), "
        "<strong style=\"color:var(--red)\">GAS-CONSTRAINED</strong> (below). "
        "<a href=\"/api/v1/dcgi/methodology\" style=\"color:var(--acc-light)\">Full methodology &rarr;</a></p>"
        # ── midstream operator registry ──
        "<div class=\"section-h\"><span class=\"pip\"></span>🛢️ Midstream Operator Registry</div>"
        "<p style=\"color:var(--tx2);font-size:0.95rem;max-width:780px;margin-bottom:14px;\">"
        "Parent midstream companies mapped to the FERC pipeline entities they operate, "
        "with live segment counts. The value-add no one else publishes: which megacap "
        "actually controls the gas under this market.</p>"
        "<table><thead><tr><th>Parent midstream</th><th>Type</th><th>HQ</th>"
        "<th class=n>Segments</th></tr></thead><tbody>"
        + op_rows_html +
        "</tbody></table>"
        # ── Pro CTA (mirrors DCPI pro-cta-block) ──
        "<div id=\"dcgi-pro-cta\">"
        "<div class=\"section-h\"><span class=\"pip\"></span>🔓 Pro Access</div>"
        "<div class=\"cta-banner\">"
        "<h2>Unlock every state&rsquo;s gas scores. Map the operators. Export reports.</h2>"
        "<p>The state list + GAS-ADVANTAGED / ADEQUATE / GAS-CONSTRAINED verdicts are free. "
        "Pro unlocks the numeric DCGI scores (gas-access, gas-cost, composite) plus pipeline "
        "&amp; operator counts for all scored states, and the raw per-pipeline rows. $199/mo.</p>"
        "<a class=\"btn\" href=\"/pricing\">Upgrade to Pro &rarr;</a>"
        "</div></div>"
        "<script>"
        "(function(){ try{ fetch('/api/v1/me/tier',{credentials:'include'})"
        ".then(function(r){return r.json();}).then(function(d){"
        "var t=((d&&(d.tier||d.plan))||'').toLowerCase();"
        "if(['pro','enterprise','founding','developer','starter','admin'].indexOf(t)>=0){"
        "var b=document.getElementById('dcgi-pro-cta'); if(b)b.style.display='none';}"
        "}).catch(function(){}); }catch(e){} })();"
        "</script>"
        # ── Daily Brief subscribe (mirrors DCPI dcpi-subscribe; shared endpoint) ──
        "<div id=\"dcgi-subscribe\" style=\"margin:3rem 0;background:linear-gradient(135deg,rgba(16,185,129,0.10),rgba(14,165,233,0.06));border:1px solid #2a2c3e;border-radius:14px;padding:1.5rem;\">"
        "<div style=\"display:flex;align-items:center;gap:0.6rem;margin-bottom:0.6rem;\">"
        "<span style=\"width:4px;height:12px;background:#10b981;border-radius:2px;\"></span>"
        "<span style=\"font-size:0.78rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#9ca3af;\">📬 Daily DC Hub Brief</span>"
        "</div>"
        "<h3 style=\"margin:0 0 0.4rem;font-size:1.2rem;font-weight:700;\">Gas + power, every morning.</h3>"
        "<p style=\"margin:0 0 1rem;color:#9ca3af;font-size:0.92rem;\">Top gas-advantaged states, biggest power movers, news count — emailed Mon&ndash;Fri at 14:00 UTC. Free.</p>"
        "<form id=\"dcgi-sub-form\" style=\"display:flex;gap:0.5rem;flex-wrap:wrap;\">"
        "<input type=\"email\" id=\"dcgi-sub-email\" placeholder=\"you@company.com\" required "
        "style=\"flex:1;min-width:220px;background:#0a0a12;border:1px solid #1f2030;color:white;padding:0.7rem 1rem;border-radius:6px;font-size:0.92rem;outline:none;\">"
        "<button type=\"submit\" id=\"dcgi-sub-go\" "
        "style=\"background:linear-gradient(135deg,#10b981,#0ea5e9);color:white;border:0;padding:0.7rem 1.3rem;border-radius:6px;font-weight:700;font-size:0.9rem;cursor:pointer;\">Subscribe &rarr;</button>"
        "</form>"
        "<div id=\"dcgi-sub-msg\" style=\"margin-top:0.6rem;font-size:0.85rem;color:#9ca3af;\"></div>"
        "</div>"
        # ── Cite block ──
        "<div style=\"background:#11121a;border:1px solid #1f2030;border-radius:12px;padding:20px;margin:32px auto;max-width:760px;\">"
        "<div style=\"font-size:12px;color:#9eb5d8;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px\">Cite this index</div>"
        "<code style=\"display:block;background:rgba(255,255,255,.03);padding:12px;border-radius:6px;color:#e8eef8;font-size:13px;margin-bottom:8px\">DC Hub. (2026). Data Center Gas Index (DCGI). https://dchub.cloud/dcgi</code>"
        "<a href=\"/api/v1/dcgi/methodology\" style=\"color:#34d399;font-size:14px;text-decoration:none\">View methodology &rarr;</a>"
        "</div>"
        # ── footer ──
        "<footer>"
        "<p>This is the free preview. Numeric scores + raw per-pipeline rows via "
        "<a href=\"/api/v1/dcgi/scores\">API</a>. Narrative report: "
        "<a href=\"/pipeline-report\">The Gas Behind the Grid &rarr;</a></p>"
        "<p>Data: EIA natural-gas pipeline geodata + EIA gas prices. "
        "Machine-readable: <a href=\"/api/v1/dcgi/scores\">/api/v1/dcgi/scores</a> · "
        "<a href=\"/api/v1/dcgi/operators\">/api/v1/dcgi/operators</a> · "
        "<a href=\"/api/v1/dcgi/methodology\">/api/v1/dcgi/methodology</a><br>"
        "License: CC BY 4.0 &mdash; cite &ldquo;DC Hub Data Center Gas Index (DCGI), dchub.cloud&rdquo;. "
        "© 2026 DC Hub · <a href=\"/dcpi\">DCPI</a> · <a href=\"/pricing\">Pricing</a></p>"
        "</footer>"
        "</div>"  # /.wrap
        # ── noscript fallback table (progressive enhancement) ──
        "<noscript><div style=\"max-width:1280px;margin:0 auto;padding:0 1.5rem 3rem;\">"
        "<div class=\"section-h\"><span class=\"pip\"></span>Per-state DCGI ranking</div>"
        + noscript_table +
        "</div></noscript>"
        # ── Ask the Gas Index (mirrors DCPI #ask-the-index; reuses /api/v1/dcpi/ask) ──
        "<div id=\"ask-the-gas-index\" style=\"position:fixed;bottom:1.5rem;right:1.5rem;width:400px;max-width:calc(100vw - 3rem);background:#11121a;border:1px solid #2a2c3e;border-radius:14px;padding:1.1rem;color:white;box-shadow:0 16px 48px rgba(0,0,0,0.5);z-index:1000;\">"
        "<div style=\"display:flex;align-items:center;gap:0.5rem;margin-bottom:0.6rem;\">"
        "<span style=\"display:inline-block;width:8px;height:8px;background:#10b981;border-radius:50%;animation:pulse 1.4s ease-in-out infinite;\"></span>"
        "<strong style=\"font-size:0.78rem;letter-spacing:0.06em;text-transform:uppercase;color:#9ca3af;\">Ask the Gas Index</strong>"
        "</div>"
        "<div id=\"gask-out\" style=\"font-size:0.88rem;line-height:1.55;min-height:80px;color:#ddd;margin-bottom:0.6rem;max-height:340px;overflow-y:auto;padding:0.4rem 0;\">"
        "Ask about gas-to-power siting for data centers — try: <em style=\"color:#6ee7b7\">which states have the best pipeline access for behind-the-meter gas?</em>"
        "</div>"
        "<textarea id=\"gask-q\" placeholder=\"e.g. where can I site gas-fired DC load near cheap gas?\" style=\"width:100%;background:#0a0a12;border:1px solid #1f2030;color:white;padding:0.6rem 0.8rem;border-radius:6px;font-family:inherit;font-size:0.88rem;min-height:54px;resize:none;outline:none;\"></textarea>"
        "<button id=\"gask-go\" style=\"width:100%;margin-top:0.5rem;background:linear-gradient(135deg,#10b981,#0ea5e9);color:white;border:0;padding:0.6rem;border-radius:6px;font-weight:700;font-size:0.88rem;cursor:pointer;\">Ask DCGI &rarr;</button>"
        "</div>"
        # ── client hydration + interactions ──
        "<script>" + _DCGI_DASH_JS + "</script>"
        "</body></html>")

    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=120, s-maxage=300"
    resp.headers["Link"] = "<https://creativecommons.org/licenses/by/4.0/>; rel=\"license\""
    return resp


def register_dcgi(app):
    app.register_blueprint(dcgi_bp)
