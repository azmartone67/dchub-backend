"""
site_brief.py — Phase FF, Track 3 (part 2): the bundled site-selection brief.

An agent evaluating a market today has to fan out: get_market_intel for
the DCPI read, get_energy_prices for power cost, get_tax_incentives for
the incentive picture, get_grid_data for the ISO context, then maybe
compare_sites against peers. Five-plus calls, five-plus round trips,
five-plus things to stitch together.

This is one call that returns the whole picture for a market:
  GET /api/v1/brief/market?market=<dcpi-slug>
  GET /api/v1/brief/market?state=<XX>          (picks the state's top market)

Everything is a direct read off tables DC Hub already maintains —
market_power_scores (DCPI verdict + grid metrics), eia_retail_rates
(power cost), tax_incentives_neon (incentives), plus same-ISO peers for
instant comparables. No internal HTTP fan-out, one connection, one
response. Best-effort per section: a missing energy or tax row degrades
that section to null rather than failing the brief.

Exposed to agents as the `get_market_brief` MCP tool (free) — a
deliberate top-of-funnel "wow": it shows the depth of DC Hub's data in a
single call, which is exactly the experience that pulls an anonymous
agent toward identifying its key.
"""

import json
import os
import re

from flask import Blueprint, jsonify, request

site_brief_bp = Blueprint("site_brief", __name__)


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)


def _norm_slug(s):
    return re.sub(r"[^a-z0-9-]+", "-", (s or "").strip().lower()).strip("-")


def _as_float(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _json_list(v):
    """top_risks_json / top_opportunities_json may be a JSON string, a
    list, or None — normalise to a list of strings."""
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, dict):
        return [str(x) for x in v.values()]
    return [str(v)]


def _resolve_market(cur, slug, state):
    """Return the market_power_scores row to brief on. Prefer an exact
    slug; else the highest-excess-power market in the given state."""
    cols = ("market_slug, market_name, state, iso, verdict, "
            "excess_power_score, constraint_score, time_to_power_months, "
            "queue_wait_months, queue_capacity_mw, reserve_margin_pct, "
            "gen_additions_12mo_mw, curtailment_pct, stranded_capacity_mw, "
            "emergency_count_30d, top_risks_json, top_opportunities_json, "
            "computed_at")
    if slug:
        cur.execute(
            f"""SELECT {cols} FROM market_power_scores
                 WHERE market_slug = %s
                 ORDER BY computed_at DESC LIMIT 1""", (slug,))
        row = cur.fetchone()
        if row:
            return row
    if state:
        cur.execute(
            f"""SELECT DISTINCT ON (market_slug) {cols}
                  FROM market_power_scores
                 WHERE UPPER(state) = %s
                 ORDER BY market_slug, computed_at DESC""", (state.upper(),))
        rows = cur.fetchall()
        if rows:
            # highest excess_power_score wins (index 5)
            return sorted(rows, key=lambda r: (r[5] or 0), reverse=True)[0]
    return None


def _energy_for_state(cur, state):
    """Latest retail ¢/kWh per sector for a state."""
    if not state:
        return None
    try:
        cur.execute(
            """SELECT DISTINCT ON (LOWER(sector))
                      LOWER(sector), rate_cents_kwh, period
                 FROM eia_retail_rates
                WHERE UPPER(state) = %s
                ORDER BY LOWER(sector), period DESC""", (state.upper(),))
        rows = cur.fetchall()
    except Exception:
        return None
    if not rows:
        return None
    by_sector = {r[0]: (_as_float(r[1]), r[2]) for r in rows}
    period = next((p for (_v, p) in by_sector.values() if p), None)
    return {
        "industrial_cents_kwh": (by_sector.get("industrial") or (None,))[0],
        "commercial_cents_kwh": (by_sector.get("commercial") or (None,))[0],
        "all_sector_cents_kwh": (by_sector.get("all sectors")
                                 or by_sector.get("all") or (None,))[0],
        "period": period,
    }


def _tax_for_state(cur, state):
    """Data-center tax incentive snapshot for a state."""
    if not state:
        return None
    try:
        cur.execute(
            """SELECT state_name, sales_tax_exempt, property_tax_abatement,
                      data_center_specific, qualifying_investment,
                      incentive_details
                 FROM tax_incentives_neon
                WHERE state_abbr = %s LIMIT 1""", (state.upper(),))
        row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    summary = row[5]
    if summary and len(summary) > 240:
        summary = summary[:240] + "…"
    return {
        "state_name": row[0],
        "sales_tax_exempt": row[1],
        "property_tax_abatement": row[2],
        "data_center_specific": row[3],
        "qualifying_investment": row[4],
        "summary": summary,
    }


def _comparables(cur, iso, exclude_slug):
    """Same-ISO peer markets — instant comparables, top by excess power."""
    if not iso:
        return []
    try:
        cur.execute(
            """SELECT DISTINCT ON (market_slug)
                      market_slug, market_name, verdict,
                      excess_power_score, constraint_score,
                      time_to_power_months
                 FROM market_power_scores
                WHERE iso = %s AND market_slug <> %s
                ORDER BY market_slug, computed_at DESC""",
            (iso, exclude_slug or ""))
        rows = cur.fetchall()
    except Exception:
        return []
    rows.sort(key=lambda r: (r[3] or 0), reverse=True)
    return [
        {"slug": r[0], "name": r[1], "verdict": r[2],
         "excess_power_score": _as_float(r[3]),
         "constraint_score": _as_float(r[4]),
         "time_to_power_months": _as_float(r[5])}
        for r in rows[:4]
    ]


@site_brief_bp.route("/api/v1/brief/market", methods=["GET"])
def market_brief():
    """One-call site-selection brief for a DCPI market.

    ?market=<slug>  exact market, or
    ?state=<XX>     the state's top market by excess-power score
    """
    slug = _norm_slug(request.args.get("market") or request.args.get("slug") or "")
    state = (request.args.get("state") or "").strip()
    if not slug and not state:
        return jsonify(ok=False,
                       error="pass ?market=<dcpi-slug> or ?state=<XX>"), 400

    try:
        with _conn() as c, c.cursor() as cur:
            row = _resolve_market(cur, slug, state)
            if not row:
                # Surface a few valid slugs so the caller can retry.
                try:
                    cur.execute(
                        "SELECT DISTINCT market_slug FROM market_power_scores "
                        "ORDER BY market_slug LIMIT 12")
                    sample = [r[0] for r in cur.fetchall()]
                except Exception:
                    sample = []
                return jsonify(ok=False, error="market not found",
                               sample_markets=sample), 404

            (m_slug, m_name, m_state, m_iso, verdict, excess, constraint,
             ttp, queue_wait, queue_cap, reserve, gen_add, curtail,
             stranded, emergency, risks_json, opps_json, computed_at) = row

            energy = _energy_for_state(cur, m_state)
            tax = _tax_for_state(cur, m_state)
            comps = _comparables(cur, m_iso, m_slug)

        brief = {
            "ok": True,
            "market": {
                "slug": m_slug, "name": m_name, "state": m_state,
                "iso": m_iso, "verdict": verdict,
                "excess_power_score": _as_float(excess),
                "constraint_score": _as_float(constraint),
                "time_to_power_months": _as_float(ttp),
                "computed_at": computed_at.isoformat() if computed_at else None,
            },
            "grid": {
                "queue_wait_months": _as_float(queue_wait),
                "queue_capacity_mw": _as_float(queue_cap),
                "reserve_margin_pct": _as_float(reserve),
                "gen_additions_12mo_mw": _as_float(gen_add),
                "curtailment_pct": _as_float(curtail),
                "stranded_capacity_mw": _as_float(stranded),
                "emergency_count_30d": emergency,
            },
            "energy": energy,
            "tax_incentives": tax,
            "top_opportunities": _json_list(opps_json),
            "top_risks": _json_list(risks_json),
            "comparables": comps,
            "drill_deeper": {
                "grid_intelligence": "get_grid_intelligence",
                "fiber": "get_fiber_intel",
                "water_risk": "get_water_risk",
                "facilities": "search_facilities",
                "movement_alerts": "subscribe_market_alerts",
            },
            "note": ("One-call brief — DCPI verdict + grid context, power "
                     "cost, tax incentives, and same-ISO comparables. Use "
                     "the drill_deeper tools for depth on any section."),
        }
        return jsonify(brief), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 200
