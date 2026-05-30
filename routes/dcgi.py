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


# Gas-price scale for the cost score, $/Mcf. Industrial/electric delivered
# gas typically ranges ~$2.50 (Gulf) to ~$12 (New England constrained).
_PRICE_FLOOR = 2.5
_PRICE_CEIL = 12.0


def _gas_state_rollup():
    """Per-state gas access + cost + DCGI. Fail-soft: returns ({}, err) on
    any DB error so the caller can degrade gracefully. Time-capped (8s) to
    protect the 2-replica backend from a slow query starving the worker pool."""
    states = {}
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute("SET statement_timeout = 8000")
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
            # Latest industrial / electric-power gas price per state.
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
    except Exception as e:
        return {}, "rollup_error: " + str(e)[:200]

    if not states:
        return {}, "no gas_pipelines rows"

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
    resp = jsonify({
        "ok": True,
        "index": "Data Center Gas Index (DCGI)",
        "count": len(ranked),
        "verdict_legend": ["GAS-ADVANTAGED", "ADEQUATE", "GAS-CONSTRAINED"],
        "states": ranked,
        "methodology": "/api/v1/dcgi/methodology",
        "license": "CC BY 4.0",
    })
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


def register_dcgi(app):
    app.register_blueprint(dcgi_bp)
