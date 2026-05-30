"""
state_of_power.py — "The State of Data Center Power" flagship report.

The citable, recurring data event the industry can't produce: a single
permanent URL that an LLM can both QUERY and CITE. Unlike the time-windowed
/reports/energy/{monthly,quarterly} surfaces (whose slugs roll over and so
can't be a stable footnote), this lives at a fixed, unversioned URL forever:

    GET /state-of-power                        — HTML (canonical, apex)
    GET /reports/state-of-power                — HTML (routed twin)
    GET /api/v1/reports/state-of-power         — JSON (Dataset + cite block)
    GET /reports/state-of-power.md             — paste-ready Markdown
    GET /state-of-power/methodology            — DCPI methodology (verdict model)
    GET /reports/state-of-power/methodology    — methodology (routed twin)

It does NOT duplicate any data layer. It reuses energy_report._gather_energy
(verdict distribution, top BUILD/AVOID, ISO rollup, interconnection queue —
all CC-BY, 5-min Redis cache) and adds:
  • The live US ISO roster (7) + international ISOs (3) — verified static.
  • A real PJM fuel-mix sample: tries a time-capped live read from grid_data,
    falls back to the verified reference snapshot so the surface NEVER renders
    an empty fuel mix (the live REST fuel endpoint is deprecated → []).
  • JSON-LD Dataset + Report + a "cite this" block (stable URL + methodology
    link) so LLMs cite it.
  • A permanent DCPI methodology page documenting the LIVE verdict model
    (Excess Power + Constraint → BUILD/CAUTION/AVOID), which the stale
    /dcpi/methodology static page (CF Pages, DCPI v2 weighted formula) does
    NOT match. This page is the self-consistent footnote for the report.

Same CC-BY-4.0 license + Link header + CORS * pattern as the other reports.
"""

import os
import json
import time
import logging
import datetime as _dt
import html as _html

from flask import Blueprint, Response, jsonify

logger = logging.getLogger(__name__)
state_of_power_bp = Blueprint("state_of_power", __name__)

_CC_LINK_HEADER = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'
STABLE_URL = "https://dchub.cloud/state-of-power"
METHODOLOGY_URL = "https://dchub.cloud/state-of-power/methodology"


# ── ISO roster (verified, 2026-05) ───────────────────────────────────────
# 7 live US ISOs with real-time grid data + 3 international markets.
_US_ISOS = [
    {"code": "ERCOT",  "name": "ERCOT",   "region": "Texas"},
    {"code": "CAISO",  "name": "CAISO",   "region": "California"},
    {"code": "NYISO",  "name": "NYISO",   "region": "New York"},
    {"code": "MISO",   "name": "MISO",    "region": "Midwest / Gulf"},
    {"code": "PJM",    "name": "PJM",     "region": "Mid-Atlantic / Ohio Valley"},
    {"code": "SPP",    "name": "SPP",     "region": "Central Plains"},
    {"code": "ISONE",  "name": "ISO-NE",  "region": "New England"},
]
_INTL_ISOS = [
    {"code": "HYDROQUEBEC", "name": "Hydro-Québec", "region": "Québec, Canada"},
    {"code": "AESO",        "name": "AESO",         "region": "Alberta, Canada"},
    {"code": "NORDPOOL",    "name": "Nord Pool",    "region": "Nordics + Baltics"},
]

# Verified reference fuel-mix snapshot (PJM real-time, 2026-05). Used as a
# guaranteed fallback so the surface never renders an empty fuel mix — the
# live REST fuel endpoint is deprecated (returns []), and the live grid_data
# read is best-effort + time-capped on a single Railway replica.
_PJM_FUEL_REFERENCE = {
    "iso": "PJM",
    "label": "PJM Interconnection",
    "unit": "MWh",
    "source": "PJM real-time generation by fuel (EIA-930 / PJM dataminer)",
    "fuel_mix": [
        {"fuel": "Natural gas", "mwh": 40935},
        {"fuel": "Nuclear",     "mwh": 31171},
        {"fuel": "Coal",        "mwh": 16959},
    ],
}

# Map grid_data fuel_* metric_name suffixes → display labels. The EIA v2
# extractor (parse_eia_v2_fuel_mix, prefix "fuel_") writes rows keyed by the
# EIA-930 fuel code lower-cased: fuel_ng / fuel_nuc / fuel_col / fuel_wnd /
# fuel_wat / fuel_sun / fuel_oth / fuel_oil. Cover both the EIA codes and the
# spelled-out forms so labels always read cleanly.
_FUEL_LABELS = {
    # EIA-930 codes
    "ng": "Natural gas", "nuc": "Nuclear", "col": "Coal", "wnd": "Wind",
    "wat": "Hydro", "sun": "Solar", "oth": "Other", "oil": "Oil",
    "ts": "Storage", "geo": "Geothermal", "bio": "Biomass",
    # spelled-out / alternate forms
    "natural_gas": "Natural gas", "gas": "Natural gas",
    "nuclear": "Nuclear", "coal": "Coal",
    "wind": "Wind", "solar": "Solar", "hydro": "Hydro", "water": "Hydro",
    "petroleum": "Oil", "other": "Other",
    "storage": "Storage", "battery": "Storage", "geothermal": "Geothermal",
    "biomass": "Biomass",
}

_FUEL_CACHE = {"data": None, "at": 0.0}
_FUEL_TTL = 600  # 10 min


def _live_pjm_fuel():
    """Best-effort, time-capped live PJM fuel mix from grid_data.

    Returns a dict shaped like _PJM_FUEL_REFERENCE with origin="live" on
    success, else None. NEVER raises — a single-replica backend can't afford
    a slow/blocked query on a public page (see dchub backend flapping note).
    """
    now = time.monotonic()
    if _FUEL_CACHE["data"] is not None and (now - _FUEL_CACHE["at"]) < _FUEL_TTL:
        return _FUEL_CACHE["data"]
    out = None
    try:
        from main import get_read_db
        conn = get_read_db()
        try:
            cur = conn.cursor()
            # 8s statement timeout so a cold/locked grid_data never stalls
            # the page render. Latest value per fuel_* metric for PJM.
            try:
                cur.execute("SET LOCAL statement_timeout = 8000")
            except Exception:
                pass
            cur.execute(
                """
                SELECT metric_name, metric_value, unit, ts FROM (
                    SELECT DISTINCT ON (metric_name)
                           metric_name, metric_value, unit, timestamp AS ts
                      FROM grid_data
                     WHERE iso = 'PJM' AND metric_name LIKE 'fuel_%%'
                  ORDER BY metric_name, timestamp DESC
                ) latest
                ORDER BY metric_value DESC NULLS LAST
                LIMIT 12
                """
            )
            rows = cur.fetchall() or []
        finally:
            try:
                conn.close()
            except Exception:
                pass

        mix = []
        newest = None
        for row in rows:
            # row may be tuple or RealDictRow depending on cursor factory
            if isinstance(row, dict):
                mn, mv, unit, ts = (row.get("metric_name"), row.get("metric_value"),
                                    row.get("unit"), row.get("ts"))
            else:
                mn, mv, unit, ts = row[0], row[1], row[2], row[3]
            if mv is None:
                continue
            try:
                mwh = float(mv)
            except (TypeError, ValueError):
                continue
            if mwh <= 0:
                continue
            suffix = (mn or "").replace("fuel_", "").strip().lower()
            label = _FUEL_LABELS.get(suffix, suffix.replace("_", " ").title() or "Other")
            mix.append({"fuel": label, "mwh": round(mwh)})
            if ts is not None and (newest is None or ts > newest):
                newest = ts

        # Require at least 3 fuels to consider the live read trustworthy;
        # otherwise fall back to the verified reference snapshot.
        if len(mix) >= 3:
            mix.sort(key=lambda x: -x["mwh"])
            out = {
                "iso": "PJM",
                "label": "PJM Interconnection",
                "unit": "MWh",
                "origin": "live",
                "as_of": (newest.isoformat() if hasattr(newest, "isoformat") else None),
                "source": "PJM real-time generation by fuel (EIA-930 v2, grid_data)",
                "fuel_mix": mix[:8],
            }
    except Exception as e:
        logger.info(f"state_of_power: live PJM fuel read failed ({e}); using reference")
        out = None

    _FUEL_CACHE["data"] = out
    _FUEL_CACHE["at"] = now
    return out


def _fuel_block():
    """Live PJM fuel mix if available, else the verified reference snapshot.
    Always returns a non-empty fuel_mix."""
    live = _live_pjm_fuel()
    if live and live.get("fuel_mix"):
        return live
    ref = dict(_PJM_FUEL_REFERENCE)
    ref["origin"] = "reference"
    ref["fuel_mix"] = [dict(f) for f in _PJM_FUEL_REFERENCE["fuel_mix"]]
    return ref


def _gather():
    """Assemble the State of Power payload. Reuses energy_report._gather_energy
    (5-min cached) for the heavy DCPI/queue/ISO-rollup data — no duplication."""
    try:
        from routes.energy_report import _gather_energy
        e = _gather_energy("monthly")
    except Exception as ex:
        logger.warning(f"state_of_power: _gather_energy failed: {ex}")
        e = {}

    today = _dt.date.today()
    out = {
        "report": "The State of Data Center Power",
        "stable_url": STABLE_URL,
        "methodology_url": METHODOLOGY_URL,
        "as_of_date": e.get("as_of_date") or today.isoformat(),
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        "refresh": "DCPI recomputed daily; this surface reflects the latest snapshot.",
        "summary": {
            "facilities": "21,000+",
            "markets": 233,
            "substations": 126427,
            "countries": "170+",
            "live_us_isos": len(_US_ISOS),
            "international_isos": len(_INTL_ISOS),
            "markets_scored": e.get("markets_scored_total"),
        },
        # — DCPI verdicts (reused) —
        "verdict_distribution": e.get("verdict_distribution") or {},
        "build_markets": e.get("top_build_markets") or [],
        "avoid_markets": e.get("top_avoid_markets") or [],
        "iso_rollup": e.get("iso_rollup") or [],
        # — ISO roster (verified) —
        "live_grids": {
            "us_isos": _US_ISOS,
            "international_isos": _INTL_ISOS,
            "note": ("7 live US ISOs with real-time grid data, plus 3 international "
                     "markets (Hydro-Québec, AESO, Nord Pool)."),
        },
        # — Fuel mix sample (live or verified reference) —
        "fuel_mix_sample": _fuel_block(),
        # — Physical-stack + interconnection coverage (reused) —
        "interconnection_queue": e.get("interconnection_queue") or {},
        "coverage": {
            "physical_stack": [
                "Power (grid headroom, LMP, fuel mix, carbon intensity)",
                "Fiber routes",
                "Substations (126,427)",
                "Gas pipelines",
                "Water risk",
                "Interconnection queues (live ISO scrapes)",
                "Tax incentives",
            ],
            "headline": ("The full physical stack for a site — power + fiber + "
                         "substations + gas pipelines + water risk + interconnection "
                         "queues + tax incentives — in one query."),
        },
        "the_wedge": ("DC Hub is the only data-center-intelligence source an LLM can "
                      "both QUERY (28 MCP tools) and CITE (CC-BY-4.0, stable URLs, "
                      "JSON-LD). Competing sources are paywalled, login-gated, or "
                      "block AI crawlers — none are agent-readable."),
        "license": {
            "id": "CC-BY-4.0",
            "name": "Creative Commons Attribution 4.0 International",
            "url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution_required": True,
            "commercial_use_allowed": True,
        },
    }
    out["citation"] = _citation(out["as_of_date"])
    # Pass through the auto-generated executive narrative if energy_report
    # attached one (it's keyed on the same gathered data).
    if e.get("narrative_summary"):
        out["narrative_summary"] = e["narrative_summary"]
    return out


def _citation(as_of: str) -> dict:
    year = (as_of or "")[:4] or str(_dt.date.today().year)
    apa = (f"DC Hub. ({year}). The State of Data Center Power. "
           f"{STABLE_URL} (accessed {as_of}). Methodology: {METHODOLOGY_URL}. "
           f"Licensed CC-BY-4.0.")
    bibtex = (
        "@misc{dchub_state_of_power,\n"
        "  author       = {{DC Hub}},\n"
        "  title        = {The State of Data Center Power},\n"
        f"  year         = {{{year}}},\n"
        f"  howpublished = {{\\url{{{STABLE_URL}}}}},\n"
        f"  note         = {{Accessed {as_of}. Methodology: {METHODOLOGY_URL}. "
        "Licensed CC-BY-4.0.}}\n"
        "}"
    )
    return {
        "stable_url": STABLE_URL,
        "methodology_url": METHODOLOGY_URL,
        "apa": apa,
        "bibtex": bibtex,
        "license": "CC-BY-4.0",
    }


# ─────────────────────────────────────────────────────────────────────────
# JSON
# ─────────────────────────────────────────────────────────────────────────
def _json_headers():
    return {"Cache-Control": "public, max-age=900, s-maxage=3600",
            "Link": _CC_LINK_HEADER,
            "X-License": "CC-BY-4.0",
            "Access-Control-Allow-Origin": "*"}


@state_of_power_bp.route("/api/v1/reports/state-of-power",
                         methods=["GET"], strict_slashes=False)
def state_of_power_json():
    return jsonify(_gather()), 200, _json_headers()


# ─────────────────────────────────────────────────────────────────────────
# Markdown
# ─────────────────────────────────────────────────────────────────────────
@state_of_power_bp.route("/reports/state-of-power.md",
                         methods=["GET"], strict_slashes=False)
def state_of_power_md():
    return Response(_render_md(_gather()),
                    mimetype="text/markdown; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=900",
                             "Link": _CC_LINK_HEADER,
                             "Access-Control-Allow-Origin": "*"})


def _render_md(d: dict) -> str:
    v = d.get("verdict_distribution") or {}
    fuel = d.get("fuel_mix_sample") or {}
    L = []
    L.append("# The State of Data Center Power")
    L.append(f"_DC Hub · daily-refreshed · CC-BY-4.0 · as of {d.get('as_of_date','')}_")
    L.append("")
    L.append(f"The recurring, machine-readable state of where AI data centers can "
             f"actually get power. {d['summary'].get('markets_scored') or '—'} markets "
             f"scored by DCPI across {len(_US_ISOS)} live US ISOs + "
             f"{len(_INTL_ISOS)} international grids.")
    L.append("")
    L.append(f"- Stable URL: {STABLE_URL}")
    L.append(f"- JSON: https://dchub.cloud/api/v1/reports/state-of-power")
    L.append(f"- Methodology: {METHODOLOGY_URL}")
    L.append("")

    narr = (d.get("narrative_summary") or {}).get("text")
    if narr:
        L.append("## Executive summary")
        L.append(narr.strip())
        L.append("")

    L.append("## DCPI verdicts")
    L.append(f"- BUILD: {v.get('BUILD', 0)} · CAUTION: {v.get('CAUTION', 0)} · "
             f"AVOID: {v.get('AVOID', 0)}")
    L.append("")

    if d.get("build_markets"):
        L.append("## Where to BUILD (top markets by composite)")
        for m in d["build_markets"][:8]:
            comp = m.get("composite")
            ex = m.get("excess_power")
            L.append(f"- **{m.get('market','?')}** ({m.get('iso','—')}) — "
                     f"composite {comp if comp is not None else '—'}, "
                     f"excess power {ex if ex is not None else '—'} · "
                     f"{m.get('page','')}")
        L.append("")

    if d.get("avoid_markets"):
        L.append("## Where to AVOID (most grid-constrained)")
        for m in d["avoid_markets"][:8]:
            c = m.get("constraint")
            L.append(f"- **{m.get('market','?')}** ({m.get('iso','—')}) — "
                     f"constraint {c if c is not None else '—'} · {m.get('page','')}")
        L.append("")

    L.append("## Live grids")
    L.append(f"**{len(_US_ISOS)} live US ISOs:** "
             + ", ".join(f"{i['name']} ({i['region']})" for i in _US_ISOS) + ".")
    L.append("")
    L.append(f"**{len(_INTL_ISOS)} international:** "
             + ", ".join(f"{i['name']} ({i['region']})" for i in _INTL_ISOS) + ".")
    L.append("")

    if fuel.get("fuel_mix"):
        origin = "live" if fuel.get("origin") == "live" else "reference snapshot"
        L.append(f"## Real-time fuel mix — {fuel.get('label', fuel.get('iso'))} ({origin})")
        for f in fuel["fuel_mix"]:
            L.append(f"- {f.get('fuel','?')}: {f.get('mwh', 0):,} {fuel.get('unit','MWh')}")
        L.append(f"_Source: {fuel.get('source','')}_")
        L.append("")

    q = d.get("interconnection_queue") or {}
    by_iso = q.get("by_iso") or []
    if by_iso:
        L.append("## Interconnection-queue coverage (data-center load)")
        L.append("| ISO | Queued total (GW) | DC load (GW) | DC share |")
        L.append("|-----|------------------:|-------------:|---------:|")
        for r in by_iso[:8]:
            tot = r.get("queued_load_total_gw")
            dc = r.get("queued_load_data_center_gw")
            share = r.get("queued_load_dc_share_pct")
            L.append(f"| {r.get('iso','—')} | {tot if tot is not None else '—'} | "
                     f"{dc if dc is not None else '—'} | "
                     f"{(str(share) + '%') if share is not None else '—'} |")
        L.append("")

    L.append("## Physical stack in one query")
    for item in d["coverage"]["physical_stack"]:
        L.append(f"- {item}")
    L.append("")

    L.append("## Cite this")
    L.append("```")
    L.append((d.get("citation") or {}).get("apa", ""))
    L.append("```")
    L.append("")
    L.append("---")
    L.append(f"_Generated {(d.get('generated_at') or '')[:19].replace('T',' ')} UTC · "
             f"CC-BY-4.0 · {STABLE_URL}_")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────
# HTML (flagship)
# ─────────────────────────────────────────────────────────────────────────
def _html_headers():
    return {"Cache-Control": "public, max-age=900, s-maxage=3600",
            "Link": _CC_LINK_HEADER,
            "X-License": "CC-BY-4.0"}


@state_of_power_bp.route("/state-of-power", methods=["GET"], strict_slashes=False)
@state_of_power_bp.route("/reports/state-of-power", methods=["GET"], strict_slashes=False)
def state_of_power_html():
    return Response(_render_html(_gather()), mimetype="text/html",
                    headers=_html_headers())


def _jsonld(d: dict) -> str:
    v = d.get("verdict_distribution") or {}
    return json.dumps({
        "@context": "https://schema.org",
        "@type": ["Dataset", "Report"],
        "name": "The State of Data Center Power",
        "alternateName": "DC Hub State of Data Center Power",
        "description": (
            "The recurring, machine-readable state of where AI data centers can "
            "get power: live BUILD/AVOID markets scored by the Data Center Power "
            "Index (DCPI), 7 live US ISOs + 3 international grids, real-time fuel "
            "mix, and interconnection-queue depth. Refreshed daily, CC-BY-4.0."),
        "url": STABLE_URL,
        "sameAs": "https://dchub.cloud/api/v1/reports/state-of-power",
        "datePublished": d.get("generated_at", ""),
        "dateModified": d.get("generated_at", ""),
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "keywords": ["data center", "power availability", "DCPI", "interconnection queue",
                     "ISO grid", "BUILD verdict", "AI data center", "fuel mix",
                     "grid headroom", "site selection"],
        "spatialCoverage": "Global · 233 markets · 7 live US ISOs + Hydro-Québec, AESO, Nord Pool",
        "temporalCoverage": f"Daily refresh; snapshot as of {d.get('as_of_date','')}",
        "measurementTechnique": "DCPI: Excess Power score + Constraint score → BUILD/CAUTION/AVOID verdict, recomputed daily.",
        "variableMeasured": [
            {"@type": "PropertyValue", "name": "DCPI Verdict",
             "description": "BUILD | CAUTION | AVOID"},
            {"@type": "PropertyValue", "name": "Excess Power Score", "minValue": 0, "maxValue": 100},
            {"@type": "PropertyValue", "name": "Constraint Score", "minValue": 0, "maxValue": 100},
            {"@type": "PropertyValue", "name": "DCPI Composite Score", "minValue": 0, "maxValue": 100},
            {"@type": "PropertyValue", "name": "Interconnection-queue data-center share (%)"},
        ],
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": "https://dchub.cloud/api/v1/reports/state-of-power"},
            {"@type": "DataDownload", "encodingFormat": "text/markdown",
             "contentUrl": "https://dchub.cloud/reports/state-of-power.md"},
            {"@type": "DataDownload", "encodingFormat": "text/html", "contentUrl": STABLE_URL},
        ],
        "citation": (d.get("citation") or {}).get("apa", ""),
        "subjectOf": {"@type": "CreativeWork", "name": "DCPI Methodology", "url": METHODOLOGY_URL},
    }, ensure_ascii=False)


def _render_html(d: dict) -> str:
    v = d.get("verdict_distribution") or {}
    s = d.get("summary") or {}
    fuel = d.get("fuel_mix_sample") or {}
    cite = d.get("citation") or {}

    build = d.get("build_markets") or []
    avoid = d.get("avoid_markets") or []

    build_rows = "\n".join(
        f'<tr><td><a href="{_html.escape(m.get("page",""))}"><strong>'
        f'{_html.escape(str(m.get("market","?")))}</strong></a></td>'
        f'<td>{_html.escape(str(m.get("iso","—")))}</td>'
        f'<td style="text-align:right">{m.get("composite") if m.get("composite") is not None else "—"}</td>'
        f'<td style="text-align:right">{m.get("excess_power") if m.get("excess_power") is not None else "—"}</td>'
        f'<td style="text-align:right">{m.get("constraint") if m.get("constraint") is not None else "—"}</td></tr>'
        for m in build[:10]
    ) or '<tr><td colspan="5" style="color:#71717a;text-align:center"><em>No BUILD markets in the current snapshot.</em></td></tr>'

    avoid_rows = "\n".join(
        f'<tr><td><a href="{_html.escape(m.get("page",""))}"><strong>'
        f'{_html.escape(str(m.get("market","?")))}</strong></a></td>'
        f'<td>{_html.escape(str(m.get("iso","—")))}</td>'
        f'<td style="text-align:right;color:#ef4444">{m.get("constraint") if m.get("constraint") is not None else "—"}</td></tr>'
        for m in avoid[:10]
    ) or '<tr><td colspan="3" style="color:#71717a;text-align:center"><em>No AVOID markets in the current snapshot.</em></td></tr>'

    us_iso_chips = " ".join(
        f'<span class="chip">{_html.escape(i["name"])}<small>{_html.escape(i["region"])}</small></span>'
        for i in _US_ISOS)
    intl_iso_chips = " ".join(
        f'<span class="chip chip-intl">{_html.escape(i["name"])}<small>{_html.escape(i["region"])}</small></span>'
        for i in _INTL_ISOS)

    fuel_origin = "live" if fuel.get("origin") == "live" else "reference"
    fuel_total = sum((f.get("mwh") or 0) for f in (fuel.get("fuel_mix") or []))
    fuel_rows = "\n".join(
        f'<tr><td><strong>{_html.escape(str(f.get("fuel","?")))}</strong></td>'
        f'<td style="text-align:right">{(f.get("mwh") or 0):,} {_html.escape(fuel.get("unit","MWh"))}</td>'
        f'<td style="text-align:right;color:#94a3b8">'
        f'{(100*(f.get("mwh") or 0)/fuel_total):.0f}%</td></tr>'
        for f in (fuel.get("fuel_mix") or [])
    ) if fuel_total else ''

    q = d.get("interconnection_queue") or {}
    by_iso = q.get("by_iso") or []
    queue_rows = "\n".join(
        f'<tr><td><strong>{_html.escape(str(r.get("iso","—")))}</strong>'
        + (f'<br><small style="color:#64748b">'
           + _html.escape(str((r.get("source_name") or ""))) + '</small>' if r.get("source_name") else '')
        + f'</td>'
        f'<td style="text-align:right">{r.get("queued_load_total_gw") if r.get("queued_load_total_gw") is not None else "—"}</td>'
        f'<td style="text-align:right">{r.get("queued_load_data_center_gw") if r.get("queued_load_data_center_gw") is not None else "—"}</td>'
        f'<td style="text-align:right;color:#a855f7">'
        f'{(str(r.get("queued_load_dc_share_pct")) + "%") if r.get("queued_load_dc_share_pct") is not None else "—"}</td></tr>'
        for r in by_iso[:10]
    ) or '<tr><td colspan="4" style="color:#71717a;text-align:center"><em>Queue snapshot loading.</em></td></tr>'

    stack_items = "".join(f'<li>{_html.escape(x)}</li>'
                          for x in d["coverage"]["physical_stack"])

    narr = (d.get("narrative_summary") or {}).get("text", "")
    narr_paras = [p.strip() for p in (narr or "").split("\n\n") if p.strip()]
    narr_html = "\n".join(f"<p>{_html.escape(p)}</p>" for p in narr_paras)

    markets_scored = s.get("markets_scored")
    markets_scored_str = f"{markets_scored}" if markets_scored is not None else "—"

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The State of Data Center Power — DC Hub</title>
<meta name="description" content="The recurring, machine-readable state of where AI data centers can get power. {markets_scored_str} markets scored by DCPI, live BUILD/AVOID verdicts, 7 US ISOs + 3 international grids, real-time fuel mix, interconnection-queue depth. Daily refresh, CC-BY-4.0.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="{STABLE_URL}">
<meta property="og:title" content="The State of Data Center Power">
<meta property="og:description" content="{v.get('BUILD',0)} BUILD · {v.get('AVOID',0)} AVOID markets · 7 live US ISOs + 3 international · real-time fuel mix · daily refresh · CC-BY-4.0">
<meta property="og:type" content="website">
<meta property="og:url" content="{STABLE_URL}">
<script type="application/ld+json">{_jsonld(d)}</script>
<style>
  :root {{ --bg:#070b16; --card:rgba(255,255,255,.04); --ink:#e5e7eb; --mut:#94a3b8; --acc:#6366f1; --grn:#10b981; --red:#ef4444; --pur:#a855f7 }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif; line-height:1.55 }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:64px 28px 110px }}
  .eyebrow {{ font-family:ui-monospace,Menlo,monospace; font-size:11px; text-transform:uppercase; letter-spacing:.14em; color:var(--acc); margin-bottom:14px }}
  h1 {{ font-size:48px; line-height:1.05; margin:0 0 18px; letter-spacing:-.025em }}
  h2 {{ font-size:24px; margin:56px 0 14px; letter-spacing:-.01em }}
  .lede {{ font-size:19px; color:#cbd5e1; margin:0 0 36px; max-width:72ch }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:32px 0 8px }}
  .stat {{ background:var(--card); padding:18px 16px; border-radius:10px; border-left:3px solid var(--acc) }}
  .stat-num {{ font-size:30px; font-weight:800; display:block; letter-spacing:-.02em }}
  .stat-lbl {{ font-family:ui-monospace,Menlo,monospace; font-size:10.5px; text-transform:uppercase; color:var(--mut); margin-top:6px; letter-spacing:.06em }}
  .verdicts {{ display:flex; gap:10px; flex-wrap:wrap; margin:8px 0 0 }}
  .vpill {{ padding:8px 16px; border-radius:999px; font-weight:700; font-size:14px; background:var(--card) }}
  .vbuild {{ color:var(--grn); border:1px solid rgba(16,185,129,.4) }}
  .vcaution {{ color:#f59e0b; border:1px solid rgba(245,158,11,.4) }}
  .vavoid {{ color:var(--red); border:1px solid rgba(239,68,68,.4) }}
  table {{ width:100%; border-collapse:collapse; margin:14px 0 28px }}
  th, td {{ padding:11px 13px; text-align:left; font-size:14px; border-bottom:1px solid rgba(255,255,255,.06); vertical-align:top }}
  th {{ background:rgba(255,255,255,.03); font-family:ui-monospace,Menlo,monospace; font-size:10.5px; text-transform:uppercase; letter-spacing:.08em; color:var(--mut) }}
  a {{ color:#93c5fd }}
  .chip {{ display:inline-flex; flex-direction:column; background:var(--card); border:1px solid rgba(99,102,241,.3); border-radius:8px; padding:8px 14px; margin:0 8px 8px 0; font-weight:700; font-size:14px }}
  .chip small {{ font-weight:400; color:var(--mut); font-size:11px; margin-top:2px }}
  .chip-intl {{ border-color:rgba(168,85,247,.35) }}
  .narr {{ background:rgba(99,102,241,.06); border-left:3px solid var(--acc); padding:22px 26px; border-radius:8px; margin:24px 0 36px }}
  .narr p {{ margin:0 0 12px; font-size:16px; line-height:1.65 }} .narr p:last-child {{ margin:0 }}
  .badge {{ display:inline-block; padding:3px 9px; border-radius:5px; font-size:10.5px; font-weight:700; letter-spacing:.5px; text-transform:uppercase; vertical-align:middle; margin-left:8px }}
  .badge-live {{ background:var(--grn); color:#062012 }}
  .badge-ref {{ background:rgba(148,163,184,.2); color:var(--mut) }}
  .cite {{ background:rgba(99,102,241,.08); border-left:3px solid var(--acc); padding:20px 24px; border-radius:8px; margin:24px 0; }}
  .cite code {{ display:block; background:rgba(255,255,255,.05); padding:12px 14px; border-radius:6px; font-size:13px; color:#c7d2fe; font-family:ui-monospace,Menlo,monospace; white-space:pre-wrap; word-break:break-word; margin-top:10px }}
  .license-foot {{ background:rgba(16,185,129,.06); border-left:3px solid var(--grn); padding:20px 24px; border-radius:8px; margin-top:56px; font-size:14px; color:#cbd5e1 }}
  ul.stack {{ columns:2; gap:28px; padding-left:20px; margin:8px 0 0 }} ul.stack li {{ margin:6px 0; font-size:15px }}
  .wedge {{ font-size:17px; color:#cbd5e1; background:var(--card); border-radius:10px; padding:22px 26px; margin:8px 0 0; border-left:3px solid var(--pur) }}
  @media (max-width:720px) {{ h1 {{ font-size:34px }} .stats {{ grid-template-columns:1fr 1fr }} ul.stack {{ columns:1 }} .wrap {{ padding:40px 20px 80px }} }}
</style></head><body>
<div class="wrap">
  <div class="eyebrow">DC Hub · The State of Data Center Power · CC-BY-4.0 · {d.get('as_of_date','')}</div>
  <h1>The State of Data Center Power</h1>
  <p class="lede">The recurring, machine-readable answer to the only question that matters for an AI build: <strong>where can you actually get power, and where can't you?</strong> {markets_scored_str} markets scored daily by the Data Center Power Index across {len(_US_ISOS)} live US ISOs and {len(_INTL_ISOS)} international grids. Free, citable, and the only data-center-intelligence source an LLM can both query and cite.</p>

  <div class="stats">
    <div class="stat"><span class="stat-num">{s.get('facilities','—')}</span><span class="stat-lbl">Facilities tracked</span></div>
    <div class="stat"><span class="stat-num">{s.get('markets','—')}</span><span class="stat-lbl">Markets</span></div>
    <div class="stat"><span class="stat-num">{s.get('substations',0):,}</span><span class="stat-lbl">Substations</span></div>
    <div class="stat"><span class="stat-num">{len(_US_ISOS)}+{len(_INTL_ISOS)}</span><span class="stat-lbl">Live ISOs (US + intl)</span></div>
  </div>
  <div class="verdicts">
    <span class="vpill vbuild">{v.get('BUILD',0)} BUILD</span>
    <span class="vpill vcaution">{v.get('CAUTION',0)} CAUTION</span>
    <span class="vpill vavoid">{v.get('AVOID',0)} AVOID</span>
    <span class="vpill" style="color:var(--mut)">{markets_scored_str} markets scored</span>
  </div>

  {('<div class="narr">' + narr_html + '</div>') if narr_html else ''}

  <h2>Where to BUILD</h2>
  <p style="color:var(--mut);margin:0 0 6px">Highest composite DCPI scores — most buildable headroom, lowest grid constraint. <a href="{METHODOLOGY_URL}">How this is scored →</a></p>
  <table>
    <thead><tr><th>Market</th><th>ISO</th><th style="text-align:right">Composite</th><th style="text-align:right">Excess Power</th><th style="text-align:right">Constraint</th></tr></thead>
    <tbody>{build_rows}</tbody>
  </table>

  <h2>Where to AVOID</h2>
  <p style="color:var(--mut);margin:0 0 6px">Most grid-constrained markets — long interconnection waits, congested transmission.</p>
  <table>
    <thead><tr><th>Market</th><th>ISO</th><th style="text-align:right">Constraint score</th></tr></thead>
    <tbody>{avoid_rows}</tbody>
  </table>

  <h2>Live grids</h2>
  <p style="color:var(--mut);margin:0 0 12px"><strong>{len(_US_ISOS)} live US ISOs</strong> with real-time grid data:</p>
  <div>{us_iso_chips}</div>
  <p style="color:var(--mut);margin:18px 0 12px"><strong>{len(_INTL_ISOS)} international markets:</strong></p>
  <div>{intl_iso_chips}</div>

  <h2>Real-time fuel mix — {_html.escape(str(fuel.get('label', fuel.get('iso','PJM'))))}
    <span class="badge {'badge-live' if fuel_origin=='live' else 'badge-ref'}">{fuel_origin}</span></h2>
  <p style="color:var(--mut);margin:0 0 6px">A live sample of what's actually generating on the grid that hosts the largest data-center interconnection queue. {('Live from grid_data.' if fuel_origin=='live' else 'Verified reference snapshot — query the live value via the get_fuel_mix MCP tool.')}</p>
  <table>
    <thead><tr><th>Fuel</th><th style="text-align:right">Output</th><th style="text-align:right">Share</th></tr></thead>
    <tbody>{fuel_rows or '<tr><td colspan="3" style="color:#71717a;text-align:center"><em>Fuel mix unavailable.</em></td></tr>'}</tbody>
  </table>
  <p style="color:#64748b;font-size:12px;margin-top:-16px">Source: {_html.escape(str(fuel.get('source','')))}</p>

  <h2>Interconnection-queue depth</h2>
  <p style="color:var(--mut);margin:0 0 6px">Live data-center load in each ISO's interconnection queue — the leading indicator of where power demand is piling up.</p>
  <table>
    <thead><tr><th>ISO</th><th style="text-align:right">Total queued (GW)</th><th style="text-align:right">DC load (GW)</th><th style="text-align:right">DC share</th></tr></thead>
    <tbody>{queue_rows}</tbody>
  </table>

  <h2>The full physical stack — in one query</h2>
  <p style="color:var(--mut);margin:0 0 6px">{_html.escape(d['coverage']['headline'])}</p>
  <ul class="stack">{stack_items}</ul>

  <h2>Why this exists</h2>
  <div class="wedge">{_html.escape(d['the_wedge'])}</div>

  <h2>Cite this</h2>
  <div class="cite">
    Permanent URL: <a href="{STABLE_URL}">{STABLE_URL}</a> ·
    Methodology: <a href="{METHODOLOGY_URL}">{METHODOLOGY_URL}</a>
    <code>{_html.escape(cite.get('apa',''))}</code>
  </div>

  <div class="license-foot">
    <span style="display:inline-block;padding:3px 9px;background:var(--grn);color:#062012;font-weight:700;border-radius:4px;font-size:11px;letter-spacing:.5px;margin-right:10px">CC-BY-4.0</span>
    Licensed under <a rel="license" href="https://creativecommons.org/licenses/by/4.0/">Creative Commons Attribution 4.0 International</a>.
    Quote, chart, and republish freely with attribution to DC Hub. Refreshed daily.
  </div>

  <p style="text-align:center;color:#64748b;font-size:13px;margin-top:56px">
    DC Hub · <a href="/">dchub.cloud</a> ·
    <a href="/reports/state-of-power.md">Markdown</a> ·
    <a href="/api/v1/reports/state-of-power">JSON</a> ·
    <a href="{METHODOLOGY_URL}">Methodology</a> ·
    <a href="/built-for-ai">Built for AI</a> ·
    <a href="/llms.txt">llms.txt</a>
  </p>
</div>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────
# DCPI METHODOLOGY (permanent, matches the LIVE verdict model)
# ─────────────────────────────────────────────────────────────────────────
@state_of_power_bp.route("/state-of-power/methodology",
                         methods=["GET"], strict_slashes=False)
@state_of_power_bp.route("/reports/state-of-power/methodology",
                         methods=["GET"], strict_slashes=False)
def state_of_power_methodology():
    return Response(_render_methodology(), mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=86400, s-maxage=86400",
                             "Link": _CC_LINK_HEADER, "X-License": "CC-BY-4.0"})


def _methodology_jsonld() -> str:
    return json.dumps({
        "@context": "https://schema.org",
        "@type": ["TechArticle", "Dataset"],
        "name": "DCPI Methodology — Data Center Power Index",
        "description": ("The exact model behind the Data Center Power Index: an "
                        "Excess Power score and a Constraint score, combined into a "
                        "BUILD/CAUTION/AVOID verdict and a 0–100 composite, "
                        "recomputed daily."),
        "url": METHODOLOGY_URL,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "isPartOf": {"@type": "Dataset", "name": "The State of Data Center Power", "url": STABLE_URL},
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": "https://dchub.cloud/api/v1/dcpi/leaderboard"},
        ],
    }, ensure_ascii=False)


def _render_methodology() -> str:
    today = _dt.date.today().isoformat()
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DCPI Methodology — Data Center Power Index — DC Hub</title>
<meta name="description" content="The exact model behind the Data Center Power Index: Excess Power score + Constraint score → BUILD/CAUTION/AVOID verdict, recomputed daily. Sources, thresholds, and the composite formula. The footnote URL for The State of Data Center Power.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="{METHODOLOGY_URL}">
<meta property="og:title" content="DCPI Methodology — Data Center Power Index">
<meta property="og:description" content="Excess Power + Constraint → BUILD/CAUTION/AVOID, recomputed daily. The methodology behind The State of Data Center Power.">
<meta property="og:type" content="article">
<meta property="og:url" content="{METHODOLOGY_URL}">
<script type="application/ld+json">{_methodology_jsonld()}</script>
<style>
  body{{background:#0a0a12;color:#e6e6f0;font-family:'IBM Plex Serif',Georgia,serif;margin:0;line-height:1.7}}
  .wrap{{max-width:780px;margin:0 auto;padding:64px 24px 110px}}
  h1{{font-size:2.6rem;font-weight:800;letter-spacing:-.02em;margin:0 0 8px}}
  h2{{font-size:1.5rem;font-weight:800;margin:48px 0 12px;border-bottom:1px solid rgba(255,255,255,.1);padding-bottom:8px}}
  h3{{font-size:1.12rem;font-weight:700;margin:24px 0 8px}}
  .sub{{color:#8a8aa0;font-size:1.05rem;margin:0 0 28px}}
  code{{background:rgba(255,255,255,.06);padding:2px 6px;border-radius:4px;font-size:.92em;font-family:'JetBrains Mono',ui-monospace,monospace}}
  pre{{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:18px;overflow-x:auto;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:.9em;line-height:1.5}}
  table{{width:100%;border-collapse:collapse;margin:16px 0;font-family:-apple-system,system-ui,sans-serif;font-size:.95rem}}
  th,td{{padding:12px 16px;text-align:left;border-bottom:1px solid rgba(255,255,255,.08)}}
  th{{color:#8a8aa0;font-weight:600;text-transform:uppercase;font-size:.78rem;letter-spacing:.08em}}
  a{{color:#a8a8f0}}
  .nav{{font-size:.9rem;color:#8a8aa0;margin-bottom:28px}}
  .nav a{{color:#a8a8f0;text-decoration:none}}
  .cite{{background:rgba(99,102,241,.08);border-left:3px solid #6366f1;padding:16px 20px;margin:24px 0;border-radius:4px;font-style:italic}}
  .v{{display:inline-block;background:rgba(99,102,241,.18);color:#a8a8f0;padding:2px 10px;border-radius:999px;font-size:.78rem;font-weight:700;font-family:-apple-system,system-ui,sans-serif;text-transform:uppercase;letter-spacing:.08em}}
  .build{{color:#10b981;font-weight:700}} .caution{{color:#f59e0b;font-weight:700}} .avoid{{color:#ef4444;font-weight:700}}
</style></head><body>
<div class="wrap">
  <div class="nav"><a href="/">DC Hub</a> / <a href="/state-of-power">State of Power</a> / DCPI Methodology</div>
  <h1>DCPI Methodology</h1>
  <p class="sub">The <strong>Data Center Power Index</strong> scores data-center markets on two daily-refreshing numbers — <strong>Excess Power</strong> (buildable headroom) and <strong>Constraint</strong> (friction to new load) — and combines them into a <strong>BUILD / CAUTION / AVOID</strong> verdict plus a single 0–100 composite rank. This is the canonical, machine-readable reference behind <a href="/state-of-power">The State of Data Center Power</a> and every <code>/dcpi/&lt;market&gt;</code> page.</p>
  <p><span class="v">Verdict model · v3</span> · recomputed daily · last reviewed {today}</p>

  <h2>The two scores</h2>
  <h3>Excess Power Score (0–100)</h3>
  <p>Higher means more buildable headroom — capacity that can absorb new data-center load. It blends ISO grid headroom, approved-but-unbuilt generation (queue velocity), signed-contract subscription ratio, local renewable surplus, and distance from saturated tier-1 concentrators (Northern Virginia, Phoenix, Silicon Valley).</p>
  <h3>Constraint Score (0–100)</h3>
  <p>Higher means more friction. It blends interconnection-queue wait time, transmission-congestion (LMP spread), substation distance to load, water stress, and regulatory friction (moratoria, special-use permitting).</p>

  <h2>Verdict</h2>
  <p>The verdict is derived directly from the two scores. These thresholds are the live values in production (<code>derive_verdict</code>):</p>
  <pre>if excess &gt;= 65 and constraint &lt;= 50:   verdict = BUILD
elif excess &gt;= 50 and constraint &lt;= 70: verdict = CAUTION
else:                                  verdict = AVOID</pre>
  <table>
    <thead><tr><th>Excess Power</th><th>Constraint</th><th>Verdict</th></tr></thead>
    <tbody>
      <tr><td>&ge; 65</td><td>&le; 50</td><td class="build">BUILD</td></tr>
      <tr><td>&ge; 50</td><td>&le; 70</td><td class="caution">CAUTION</td></tr>
      <tr><td colspan="2">anything else</td><td class="avoid">AVOID</td></tr>
    </tbody>
  </table>

  <h2>Composite score (0–100)</h2>
  <p>A single sortable rank derived from the two scores plus time-to-power (TTP), with a verdict-aware quality multiplier so a data-sparse market can't outrank a real BUILD market. This is the live formula (<code>derive_composite_score</code>):</p>
  <pre>raw = (excess * 0.60)
    + ((100 - constraint) * 0.30)
    + ((1 - min(ttp_months, 60) / 60) * 100 * 0.10)

composite = clamp(0, 100, raw * verdict_multiplier)</pre>
  <table>
    <thead><tr><th>Verdict</th><th>Multiplier</th><th>Rationale</th></tr></thead>
    <tbody>
      <tr><td class="build">BUILD</td><td>1.00</td><td>Trusted, actionable</td></tr>
      <tr><td class="caution">CAUTION</td><td>0.85</td><td>Trusted but bordered</td></tr>
      <tr><td class="avoid">AVOID</td><td>0.60</td><td>Known grid issues</td></tr>
      <tr><td>LOW_SIGNAL</td><td>0.35</td><td>Data integrity unknown — heavy penalty</td></tr>
    </tbody>
  </table>
  <p><strong>Time to Power (TTP)</strong> is the estimated months from application to energization, capped at 60 in the composite. It is published per market alongside the scores.</p>

  <h2>Data sources</h2>
  <table>
    <thead><tr><th>Signal</th><th>Source</th><th>Cadence</th></tr></thead>
    <tbody>
      <tr><td>Grid headroom · fuel mix · carbon intensity · demand</td><td>7 live US ISOs (ERCOT, CAISO, NYISO, MISO, PJM, SPP, ISO-NE) via EIA-930 v2 + ISO feeds; intl: Hydro-Québec, AESO, Nord Pool</td><td>15&nbsp;min – hourly</td></tr>
      <tr><td>Interconnection-queue depth + data-center share</td><td>Per-ISO interconnection queues (ERCOT MIS, PJM, CAISO, MISO, SPP, NYISO, ISO-NE)</td><td>daily</td></tr>
      <tr><td>Transmission congestion (LMP spread)</td><td>ISO LMP node data, rolling window</td><td>15&nbsp;min</td></tr>
      <tr><td>Substations</td><td>HIFLD substations (126,427)</td><td>quarterly</td></tr>
      <tr><td>Water stress</td><td>EPA + USGS</td><td>monthly</td></tr>
      <tr><td>Regulatory friction</td><td>County/state filings (curated)</td><td>quarterly</td></tr>
    </tbody>
  </table>

  <h2>Refresh cadence</h2>
  <p>The index <strong>recomputes daily</strong>. Underlying ISO source pulls run every 15&nbsp;minutes to hourly. Stale data points are flagged in the JSON.</p>

  <h2>Get the data</h2>
  <p>Ranked leaderboard (all markets, every score + verdict): <a href="/api/v1/dcpi/leaderboard">/api/v1/dcpi/leaderboard</a>. Per-market detail: <code>/dcpi/&lt;market-slug&gt;</code>. The rolled-up daily report: <a href="/api/v1/reports/state-of-power">/api/v1/reports/state-of-power</a>. No API key required. All CC-BY-4.0.</p>

  <h2>Cite this</h2>
  <div class="cite">DC Hub. The State of Data Center Power — DCPI Methodology. {METHODOLOGY_URL} (accessed {today}). Licensed CC-BY-4.0.</div>

  <p style="color:#64748b;font-size:.85rem;margin-top:40px"><strong>Note on versions.</strong> An earlier methodology page described a weighted four-component DCPI v2 model. The live index uses the verdict model documented here (Excess Power + Constraint &rarr; BUILD/CAUTION/AVOID); this page is the source of truth that matches production.</p>
</div>
</body></html>"""
