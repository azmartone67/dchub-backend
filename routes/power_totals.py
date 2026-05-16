"""Phase NNN (2026-05-17) — DCPI Total Power + Being Built.

Two big numbers everyone in the industry wants to see but nobody
publishes live:

  OPERATING POWER       — SUM(power_mw) across all known data center
                           facilities (USA + intl). The actual installed
                           capacity running today.
  BEING BUILT (PIPELINE)— SUM(capacity_mw) across capacity_pipeline rows
                           with status IN ('construction','planned',
                           'permitting','Under Construction','Planned').

Plus per-state and per-ISO breakdowns. Live data, every page load.
DCHawk publishes nothing live. dcByte gates behind login. DC Hub:
public, real-time, schema.org marked-up so AI agents fact-cite us.

Endpoints:
  GET /api/v1/power/totals        — JSON: operating + pipeline aggregates
  GET /dcpi/totals                — HTML page with the two big numbers

Cached 5 min — the underlying tables don't change minute-to-minute and
the aggregation queries are mildly expensive.
"""

from __future__ import annotations

import os
import time
import datetime
from flask import Blueprint, jsonify, Response
import psycopg2
import psycopg2.extras


power_totals_bp = Blueprint("power_totals", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


_CACHE = {"data": None, "ts": 0.0}
_TTL_S = 300.0  # 5 min


def _compute_totals() -> dict:
    """Aggregate operating + pipeline + per-state + per-ISO."""
    out = {
        "operating_mw":         0.0,
        "operating_count":      0,
        "pipeline_mw":          0.0,
        "pipeline_count":       0,
        "total_mw":             0.0,
        "by_state":             [],
        "by_iso":               [],
        "top_pipeline_markets": [],
        "computed_at":          datetime.datetime.utcnow().isoformat() + "Z",
    }
    c = _conn()
    if c is None: return out
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ── Operating power from discovered_facilities ──
            try:
                cur.execute("""
                    SELECT
                      COALESCE(SUM(power_mw), 0) AS op_mw,
                      COUNT(*) FILTER (WHERE power_mw > 0) AS op_count
                      FROM discovered_facilities
                     WHERE power_mw IS NOT NULL
                       AND (status IS NULL
                            OR LOWER(status) IN ('operational','operating','live','active','running','in-service'))
                """)
                r = cur.fetchone() or {}
                out["operating_mw"]    = round(float(r.get("op_mw") or 0), 1)
                out["operating_count"] = int(r.get("op_count") or 0)
            except Exception as e:
                print(f"[power_totals] operating query: {e}")

            # ── Pipeline (being built) ──
            try:
                cur.execute("""
                    SELECT COALESCE(SUM(power_mw), 0) AS pp_mw,
                           COUNT(*) AS pp_count
                      FROM discovered_facilities
                     WHERE power_mw IS NOT NULL
                       AND LOWER(status) IN ('construction','planned','permitting',
                                              'under construction','proposed','development')
                """)
                r = cur.fetchone() or {}
                out["pipeline_mw"]    = round(float(r.get("pp_mw") or 0), 1)
                out["pipeline_count"] = int(r.get("pp_count") or 0)
            except Exception as e:
                print(f"[power_totals] pipeline query: {e}")

            # Fallback: capacity_pipeline table if discovered_facilities pipeline is empty
            if out["pipeline_mw"] == 0:
                try:
                    cur.execute("""
                        SELECT COALESCE(SUM(power_mw), 0) AS pp_mw,
                               COUNT(*) AS pp_count
                          FROM capacity_pipeline
                         WHERE power_mw IS NOT NULL
                    """)
                    r = cur.fetchone() or {}
                    out["pipeline_mw"]    = round(float(r.get("pp_mw") or 0), 1)
                    out["pipeline_count"] = int(r.get("pp_count") or 0)
                except Exception:
                    pass

            out["total_mw"] = round(out["operating_mw"] + out["pipeline_mw"], 1)

            # ── By-state breakdown ──
            try:
                cur.execute("""
                    SELECT state,
                           COALESCE(SUM(power_mw) FILTER (WHERE LOWER(status) IN
                                          ('operational','operating','live','active','running','in-service'))
                                    , 0) AS op_mw,
                           COALESCE(SUM(power_mw) FILTER (WHERE LOWER(status) IN
                                          ('construction','planned','permitting','under construction','proposed','development'))
                                    , 0) AS pp_mw,
                           COUNT(*) AS facility_count
                      FROM discovered_facilities
                     WHERE state IS NOT NULL
                       AND LENGTH(state) = 2
                       AND state ~ '^[A-Z]{2}$'
                       AND (country = 'US' OR country = 'USA' OR country IS NULL)
                     GROUP BY state
                    HAVING SUM(power_mw) > 0
                     ORDER BY SUM(power_mw) DESC NULLS LAST
                     LIMIT 25
                """)
                out["by_state"] = [
                    {"state":         r["state"],
                     "operating_mw":  round(float(r["op_mw"] or 0), 0),
                     "pipeline_mw":   round(float(r["pp_mw"] or 0), 0),
                     "total_mw":      round(float((r["op_mw"] or 0) + (r["pp_mw"] or 0)), 0),
                     "facility_count": int(r["facility_count"] or 0)}
                    for r in cur.fetchall()
                ]
            except Exception as e:
                print(f"[power_totals] by_state query: {e}")

            # ── Top pipeline markets (where the build-out is happening) ──
            try:
                cur.execute("""
                    SELECT city, state,
                           SUM(power_mw) AS pp_mw,
                           COUNT(*)      AS n
                      FROM discovered_facilities
                     WHERE power_mw > 0
                       AND city IS NOT NULL
                       AND LOWER(status) IN ('construction','planned','permitting',
                                              'under construction','proposed','development')
                     GROUP BY city, state
                     ORDER BY SUM(power_mw) DESC NULLS LAST
                     LIMIT 15
                """)
                out["top_pipeline_markets"] = [
                    {"city": r["city"], "state": r["state"],
                     "pipeline_mw": round(float(r["pp_mw"] or 0), 0),
                     "project_count": int(r["n"] or 0)}
                    for r in cur.fetchall()
                ]
            except Exception as e:
                print(f"[power_totals] pipeline markets: {e}")
    finally:
        try: c.close()
        except Exception: pass
    return out


def _cached_totals() -> dict:
    now = time.time()
    if _CACHE["data"] and (now - _CACHE["ts"]) < _TTL_S:
        data = dict(_CACHE["data"])
        data["_cached"] = True
        data["_cache_age_seconds"] = round(now - _CACHE["ts"], 1)
        return data
    data = _compute_totals()
    _CACHE["data"] = data
    _CACHE["ts"]   = now
    return data


@power_totals_bp.route("/api/v1/power/totals", methods=["GET"])
def api_totals():
    """Public — JSON aggregates. 5-min cache."""
    data = _cached_totals()
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


def _fmt_mw(mw: float) -> str:
    if mw >= 1_000_000:  return f"{mw/1_000_000:.1f} TW"
    if mw >= 10_000:     return f"{mw/1_000:.0f} GW"
    if mw >= 1_000:      return f"{mw/1_000:.1f} GW"
    return f"{mw:,.0f} MW"


@power_totals_bp.route("/dcpi/totals", methods=["GET"], strict_slashes=False)
@power_totals_bp.route("/power-totals", methods=["GET"], strict_slashes=False)
def html_totals():
    """The two-big-numbers page. Public, indexable, schema.org marked up."""
    d = _cached_totals()

    try:
        from routes.surface_brain import auto_log
        auto_log("dcpi", "view", target="/dcpi/totals")
    except Exception: pass

    op_mw      = d.get("operating_mw") or 0
    pp_mw      = d.get("pipeline_mw")  or 0
    total_mw   = d.get("total_mw") or 0
    pp_pct     = round(100.0 * pp_mw / max(1, total_mw), 0) if total_mw else 0

    by_state   = d.get("by_state") or []
    top_pp     = d.get("top_pipeline_markets") or []
    computed   = d.get("computed_at", "")

    state_rows = "\n".join(
        f'<tr><td><a href="/dcpi/{s["state"].lower()}">{s["state"]}</a></td>'
        f'<td>{_fmt_mw(s["operating_mw"])}</td>'
        f'<td style="color:#1e40af;font-weight:600">{_fmt_mw(s["pipeline_mw"])}</td>'
        f'<td>{_fmt_mw(s["total_mw"])}</td>'
        f'<td>{s["facility_count"]:,}</td></tr>'
        for s in by_state[:25]
    )
    pp_market_rows = "\n".join(
        f'<tr><td>{m["city"]}, {m["state"] or "-"}</td>'
        f'<td style="color:#1e40af;font-weight:600">{_fmt_mw(m["pipeline_mw"])}</td>'
        f'<td>{m["project_count"]:,}</td></tr>'
        for m in top_pp[:15]
    )

    schema_org = {
        "@context": "https://schema.org",
        "@type":    "Dataset",
        "name":     "DC Hub Total Power: Operating + Being Built",
        "description": (
            f"Live aggregate of {_fmt_mw(op_mw)} operating data center capacity + "
            f"{_fmt_mw(pp_mw)} in active pipeline across {len(by_state)} US states. "
            f"Updated continuously."
        ),
        "url":      "https://dchub.cloud/dcpi/totals",
        "creator":  {"@type": "Organization", "name": "DC Hub"},
        "license":  "https://dchub.cloud/terms",
    }
    import json as _json

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Total Power + Pipeline — {_fmt_mw(total_mw)} tracked | DC Hub DCPI</title>
<meta name="description" content="Live total US data center power: {_fmt_mw(op_mw)} operating + {_fmt_mw(pp_mw)} being built across {len(by_state)} states. Updated continuously. Free + indexable.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/dcpi/totals">
<meta property="og:title" content="DC Hub Total Power: {_fmt_mw(total_mw)} tracked">
<meta property="og:description" content="{_fmt_mw(op_mw)} operating + {_fmt_mw(pp_mw)} being built — live, free, indexable.">
<meta property="og:url" content="https://dchub.cloud/dcpi/totals">
<script type="application/ld+json">{_json.dumps(schema_org)}</script>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        max-width:1100px;margin:0 auto;padding:1.5rem 1rem;color:#1f2937;line-height:1.55;
        background:#fafbfc}}
  h1{{margin:0 0 .25rem;font-size:2rem}}
  h1+p{{color:#6b7280;margin:0 0 1.5rem}}
  .hero{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:1rem 0 2rem}}
  .hero .card{{background:white;padding:2rem 1.5rem;border-radius:12px;
                box-shadow:0 1px 3px rgba(0,0,0,.06)}}
  .hero .card.operating{{border-top:6px solid #16a34a}}
  .hero .card.pipeline{{border-top:6px solid #1e40af}}
  .hero .label{{font-size:.85rem;text-transform:uppercase;letter-spacing:.08em;color:#6b7280;font-weight:600}}
  .hero .number{{font-size:3.5rem;font-weight:700;color:#111827;line-height:1}}
  .hero .number small{{font-size:1.2rem;color:#9ca3af;font-weight:400}}
  .hero .sub{{color:#6b7280;font-size:.95rem;margin-top:.5rem}}
  .hero .ratio{{margin-top:1rem;font-size:.85rem;color:#1e40af;font-weight:600}}
  h2{{margin:2rem 0 .75rem;font-size:1.3rem}}
  table{{width:100%;border-collapse:collapse;font-size:.92rem;background:white;border-radius:8px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
  th,td{{text-align:left;padding:.5rem .8rem;border-bottom:1px solid #f3f4f6}}
  th{{background:#f9fafb;font-weight:600;color:#374151;font-size:.82rem;text-transform:uppercase;letter-spacing:.04em}}
  td a{{color:#1e40af;text-decoration:none;font-weight:600}}
  td a:hover{{text-decoration:underline}}
  .footnote{{color:#9ca3af;font-size:.85rem;margin-top:3rem;text-align:center}}
  @media (max-width: 720px) {{ .hero{{grid-template-columns:1fr}} .hero .number{{font-size:2.5rem}} }}
</style>
</head>
<body>
<h1>Total US Data Center Power · Operating + Being Built</h1>
<p>Live aggregate from {len(by_state)} US states. Updated every 5 minutes. <a href="/dcpi">DCPI scoring</a> · <a href="/api/v1/power/totals">JSON</a></p>

<div class="hero">
  <div class="card operating">
    <div class="label">Operating now</div>
    <div class="number">{_fmt_mw(op_mw)}</div>
    <div class="sub">{d.get("operating_count", 0):,} facilities tracked</div>
  </div>
  <div class="card pipeline">
    <div class="label">Being built (pipeline)</div>
    <div class="number">{_fmt_mw(pp_mw)}</div>
    <div class="sub">{d.get("pipeline_count", 0):,} projects in construction, permitting, or planned</div>
    <div class="ratio">+{pp_pct}% of operating — the build-out is real</div>
  </div>
</div>

<h2>By state (top 25)</h2>
<table>
  <thead><tr><th>State</th><th>Operating</th><th>Being Built</th><th>Total</th><th>Facilities</th></tr></thead>
  <tbody>{state_rows or '<tr><td colspan="5" style="color:#9ca3af;padding:2rem;text-align:center">No state data yet.</td></tr>'}</tbody>
</table>

<h2>Top pipeline markets (where the build-out is happening)</h2>
<table>
  <thead><tr><th>Market</th><th>Pipeline MW</th><th>Projects</th></tr></thead>
  <tbody>{pp_market_rows or '<tr><td colspan="3" style="color:#9ca3af;padding:2rem;text-align:center">No pipeline data yet.</td></tr>'}</tbody>
</table>

<p class="footnote">
  Computed at {computed[:19]}Z · cached 5 min · <a href="/api/v1/power/totals">JSON</a> ·
  Part of <a href="/dcpi">DCPI</a> · <a href="/llms.txt">/llms.txt</a> for AI agents
</p>
</body>
</html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300"})
