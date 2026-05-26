"""
Phase r42k (2026-05-26) — Monthly + Quarterly Energy Report.

A power-and-grid focused complement to the existing monthly_trend
(M&A + facilities) and comprehensive_report (broad market snapshot)
endpoints. This one leverages DCPI exclusively: verdict distribution,
ISO health, power-availability shifts, interconnection-queue depth,
and the BUILD/AVOID watch list.

Endpoints:
  GET /api/v1/reports/energy/monthly        — JSON with narrative + license
  GET /api/v1/reports/energy/quarterly      — JSON with narrative + license
  GET /api/v1/reports/energy/monthly/narrative   — narrative-only shortcut
  GET /api/v1/reports/energy/quarterly/narrative — narrative-only shortcut
  GET /reports/energy/monthly               — HTML
  GET /reports/energy/quarterly             — HTML
  GET /reports/energy/monthly.md            — paste-ready Markdown
  GET /reports/energy/quarterly.md          — paste-ready Markdown

Same CC-BY-4.0 license + Link header + visible footer pattern as the
existing reports. Same auto-narrative + brain-drift detector wiring.
"""

import os
import json
import logging
import datetime as _dt
import html as _html
from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
energy_report_bp = Blueprint("energy_report", __name__)

_CC_LINK_HEADER = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'


def _license_block(window: str) -> dict:
    """Same shape as the other reports' license blocks."""
    return {
        "id":   "CC-BY-4.0",
        "name": "Creative Commons Attribution 4.0 International",
        "url":  "https://creativecommons.org/licenses/by/4.0/",
        "citation": (f"DC Hub. (2026). {window.title()} Data Center Energy "
                     f"Report. https://dchub.cloud/reports/energy/{window}. "
                     f"Licensed CC-BY-4.0."),
        "attribution_required": True,
        "commercial_use_allowed": True,
    }


def _gather_energy(window: str) -> dict:
    """Pull live DCPI + interconnection data into a single report dict.

    window: 'monthly' | 'quarterly' (same data, different framing —
    quarterly emphasizes structural shifts, monthly emphasizes the
    current snapshot).
    """
    out = {
        "window": window,
        "as_of_date": _dt.date.today().isoformat(),
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        "data_sources": [
            "market_power_scores (DCPI, 100+ markets, daily recompute)",
            "interconnection_queue (live ISO scrapes)",
            "grid status (10 ISOs: 7 US + HQ + AESO + Nord Pool)",
        ],
    }

    import requests
    BASE = "http://localhost:8080"

    # ── 1. DCPI verdict distribution + leaderboard ─────────────────
    try:
        r = requests.get(f"{BASE}/api/v1/dcpi/leaderboard",
                          params={"limit": 500}, timeout=8)
        leaderboard = (r.json() or {}).get("leaderboard") or []
    except Exception as e:
        leaderboard = []
        out["_leaderboard_err"] = str(e)[:120]

    verdicts = {"BUILD": 0, "CAUTION": 0, "AVOID": 0, "LOW_SIGNAL": 0}
    for row in leaderboard:
        v = (row.get("verdict") or "LOW_SIGNAL").upper()
        verdicts[v] = verdicts.get(v, 0) + 1
    out["markets_scored_total"] = len(leaderboard)
    out["verdict_distribution"] = verdicts

    # Top 10 BUILD + AVOID by composite_score
    build_rows = [r for r in leaderboard
                  if (r.get("verdict") or "").upper() == "BUILD"]
    build_rows.sort(key=lambda r: -(r.get("composite_score") or 0))
    out["top_build_markets"] = [{
        "market":     r.get("market_name"),
        "slug":       r.get("market_slug"),
        "iso":        r.get("iso"),
        "composite":  r.get("composite_score"),
        "excess_power": r.get("excess_power_score"),
        "constraint": r.get("constraint_score"),
        "ttp_months": r.get("time_to_power_months"),
        "page": f"https://dchub.cloud/dcpi/{r.get('market_slug')}",
    } for r in build_rows[:10]]

    avoid_rows = [r for r in leaderboard
                  if (r.get("verdict") or "").upper() == "AVOID"]
    avoid_rows.sort(key=lambda r: -(r.get("constraint_score") or 0))
    out["top_avoid_markets"] = [{
        "market":     r.get("market_name"),
        "slug":       r.get("market_slug"),
        "iso":        r.get("iso"),
        "composite":  r.get("composite_score"),
        "constraint": r.get("constraint_score"),
        "page": f"https://dchub.cloud/dcpi/{r.get('market_slug')}",
    } for r in avoid_rows[:10]]

    # ── 2. ISO-level rollup ────────────────────────────────────────
    iso_rollup = {}
    for row in leaderboard:
        iso = (row.get("iso") or "UNKNOWN").upper()
        if iso not in iso_rollup:
            iso_rollup[iso] = {
                "iso": iso, "count": 0, "build_count": 0,
                "avoid_count": 0, "avg_excess": 0, "avg_constraint": 0,
                "avg_ttp": 0,
            }
        b = iso_rollup[iso]
        b["count"] += 1
        v = (row.get("verdict") or "").upper()
        if v == "BUILD": b["build_count"] += 1
        if v == "AVOID": b["avoid_count"] += 1
        b["avg_excess"] += row.get("excess_power_score") or 0
        b["avg_constraint"] += row.get("constraint_score") or 0
        b["avg_ttp"] += row.get("time_to_power_months") or 0
    for iso, b in iso_rollup.items():
        if b["count"]:
            b["avg_excess"] = round(b["avg_excess"] / b["count"], 1)
            b["avg_constraint"] = round(b["avg_constraint"] / b["count"], 1)
            b["avg_ttp"] = round(b["avg_ttp"] / b["count"], 1)
            b["build_pct"] = round(100 * b["build_count"] / b["count"], 1)
    out["iso_rollup"] = sorted(iso_rollup.values(),
                                key=lambda x: -x["build_pct"])[:15]

    # ── 3. Interconnection queue snapshot (US ISOs) ─────────────────
    try:
        r = requests.get(f"{BASE}/api/v1/interconnection-queue/snapshot",
                          timeout=5)
        if r.status_code == 200:
            ic = r.json() or {}
            out["interconnection_queue"] = {
                "total_mw_queued": ic.get("total_mw_queued"),
                "data_center_share_pct": ic.get("data_center_share_pct"),
                "by_iso": ic.get("by_iso") or [],
                "snapshot_date": ic.get("as_of") or ic.get("snapshot_date"),
            }
    except Exception as e:
        out["interconnection_queue"] = {"_err": str(e)[:120]}

    # ── 4. Grid mix snapshot across major ISOs ────────────────────
    grid_mix = []
    for iso in ("PJM", "ERCOT", "CAISO", "MISO", "SPP"):
        try:
            r = requests.get(f"{BASE}/api/v1/grid/status",
                              params={"iso": iso}, timeout=4)
            if r.status_code == 200:
                g = r.json() or {}
                grid_mix.append({
                    "iso": iso,
                    "renewable_pct": g.get("renewable_pct"),
                    "carbon_g_per_kwh": g.get("carbon_g_per_kwh"),
                    "current_demand_mw": g.get("current_demand_mw")
                                          or g.get("demand_mw"),
                })
        except Exception:
            pass
    out["grid_mix_now"] = grid_mix

    # ── 5. vs proprietary research framing ────────────────────────
    out["vs_proprietary_research"] = {
        "headline": ("Live equivalent of CBRE / JLL H2 energy-and-power "
                      "chapters — refreshed daily, machine-readable, CC-BY-4.0."),
        "we_cover": [
            "Per-market DCPI verdicts (BUILD/CAUTION/AVOID, 100+ markets, daily)",
            "Interconnection-queue depth + data-center share by ISO",
            "Reserve margin + queue-wait + curtailment per market",
            "Real-time fuel mix + carbon intensity across 10 ISOs",
            "Excess-power score + time-to-power (TTP) per market",
        ],
        "they_cover_we_dont_yet": [
            "Long-term capacity outlook (3-5 year forecast)",
            "PPA pricing benchmarks per region",
            "Behind-the-meter project disclosures (private deals)",
        ],
        "edge_vs_them": {
            "freshness":    "Daily DCPI recompute vs annual H2 publish cadence",
            "license":      "CC-BY-4.0 vs proprietary © + NDA",
            "access":       "Free public JSON + MCP + Markdown vs $5-25K PDF",
            "distribution": "AI-agent native (27 MCP tools) vs human PDF only",
        },
        "honest_caveat": (
            "We are a power-data layer, not a multi-year capacity forecast. "
            "If you need a 5-year PPA outlook with bilateral-deal context, "
            "buy the CBRE/JLL report. If you need to know which 10 markets "
            "are BUILD-verdict TODAY and why, this is faster + free."
        ),
    }

    return out


def _attach_narrative_safe(d: dict, kind: str = "energy_monthly") -> dict:
    """Reuse the report_narrative module by routing through a custom
    prompt builder we install at import time."""
    try:
        from routes.report_narrative import attach_narrative
        return attach_narrative(d, kind=kind)
    except Exception as e:
        logger.warning(f"_attach_narrative_safe failed: {e}")
        return d


# ── ROUTES — JSON ────────────────────────────────────────────────────
@energy_report_bp.route("/api/v1/reports/energy/monthly",
                        methods=["GET"], strict_slashes=False)
def energy_monthly_json():
    d = _gather_energy("monthly")
    d["license"] = _license_block("monthly")
    d = _attach_narrative_safe(d, kind="energy_monthly")
    return jsonify(d), 200, {"Cache-Control": "public, max-age=900",
                              "Link": _CC_LINK_HEADER,
                              "X-License": "CC-BY-4.0",
                              "Access-Control-Allow-Origin": "*"}


@energy_report_bp.route("/api/v1/reports/energy/quarterly",
                        methods=["GET"], strict_slashes=False)
def energy_quarterly_json():
    d = _gather_energy("quarterly")
    d["license"] = _license_block("quarterly")
    d = _attach_narrative_safe(d, kind="energy_quarterly")
    return jsonify(d), 200, {"Cache-Control": "public, max-age=900",
                              "Link": _CC_LINK_HEADER,
                              "X-License": "CC-BY-4.0",
                              "Access-Control-Allow-Origin": "*"}


@energy_report_bp.route("/api/v1/reports/energy/monthly/narrative",
                        methods=["GET"], strict_slashes=False)
def energy_monthly_narrative_only():
    d = _gather_energy("monthly")
    d["license"] = _license_block("monthly")
    d = _attach_narrative_safe(d, kind="energy_monthly")
    narr = d.get("narrative_summary") or {}
    out = {
        "window":       "monthly",
        "as_of_date":   d.get("as_of_date"),
        "narrative":    narr.get("text"),
        "model":        narr.get("model"),
        "generated_at": narr.get("generated_at"),
        "permalink":    "https://dchub.cloud/reports/energy/monthly",
        "full_report":  "https://dchub.cloud/api/v1/reports/energy/monthly",
        "license":      d.get("license"),
    }
    return jsonify(out), 200, {"Cache-Control": "public, max-age=900",
                                "Link": _CC_LINK_HEADER,
                                "X-License": "CC-BY-4.0",
                                "Access-Control-Allow-Origin": "*"}


@energy_report_bp.route("/api/v1/reports/energy/quarterly/narrative",
                        methods=["GET"], strict_slashes=False)
def energy_quarterly_narrative_only():
    d = _gather_energy("quarterly")
    d["license"] = _license_block("quarterly")
    d = _attach_narrative_safe(d, kind="energy_quarterly")
    narr = d.get("narrative_summary") or {}
    out = {
        "window":       "quarterly",
        "as_of_date":   d.get("as_of_date"),
        "narrative":    narr.get("text"),
        "model":        narr.get("model"),
        "generated_at": narr.get("generated_at"),
        "permalink":    "https://dchub.cloud/reports/energy/quarterly",
        "full_report":  "https://dchub.cloud/api/v1/reports/energy/quarterly",
        "license":      d.get("license"),
    }
    return jsonify(out), 200, {"Cache-Control": "public, max-age=900",
                                "Link": _CC_LINK_HEADER,
                                "X-License": "CC-BY-4.0",
                                "Access-Control-Allow-Origin": "*"}


# ── ROUTES — HTML + Markdown ─────────────────────────────────────────
@energy_report_bp.route("/reports/energy/monthly",
                        methods=["GET"], strict_slashes=False)
def energy_monthly_html():
    d = _attach_narrative_safe(_gather_energy("monthly"), kind="energy_monthly")
    return Response(_render_html(d, "monthly"),
                    mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=900, s-maxage=3600",
                             "Link": _CC_LINK_HEADER,
                             "X-License": "CC-BY-4.0"})


@energy_report_bp.route("/reports/energy/quarterly",
                        methods=["GET"], strict_slashes=False)
def energy_quarterly_html():
    d = _attach_narrative_safe(_gather_energy("quarterly"), kind="energy_quarterly")
    return Response(_render_html(d, "quarterly"),
                    mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=900, s-maxage=3600",
                             "Link": _CC_LINK_HEADER,
                             "X-License": "CC-BY-4.0"})


@energy_report_bp.route("/reports/energy/monthly.md",
                        methods=["GET"], strict_slashes=False)
def energy_monthly_md():
    d = _attach_narrative_safe(_gather_energy("monthly"), kind="energy_monthly")
    return Response(_render_md(d, "monthly"),
                    mimetype="text/markdown; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=900",
                             "Link": _CC_LINK_HEADER,
                             "Access-Control-Allow-Origin": "*"})


@energy_report_bp.route("/reports/energy/quarterly.md",
                        methods=["GET"], strict_slashes=False)
def energy_quarterly_md():
    d = _attach_narrative_safe(_gather_energy("quarterly"), kind="energy_quarterly")
    return Response(_render_md(d, "quarterly"),
                    mimetype="text/markdown; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=900",
                             "Link": _CC_LINK_HEADER,
                             "Access-Control-Allow-Origin": "*"})


# ── Markdown renderer ────────────────────────────────────────────────
def _render_md(d: dict, window: str) -> str:
    label = f"{_dt.date.today().strftime('%B %Y')}" if window == "monthly" else (
        f"Q{(_dt.date.today().month - 1)//3 + 1} {_dt.date.today().year}"
    )
    narr = d.get("narrative_summary") or {}
    verdicts = d.get("verdict_distribution") or {}
    total = d.get("markets_scored_total") or 0
    build = d.get("top_build_markets") or []
    avoid = d.get("top_avoid_markets") or []
    isos = d.get("iso_rollup") or []
    grid = d.get("grid_mix_now") or []
    queue = d.get("interconnection_queue") or {}

    lines = []
    lines.append(f"# DC Hub — {label} Data Center Energy Report")
    lines.append(f"_Live DCPI snapshot, refreshed daily._ "
                 f"[Full report](https://dchub.cloud/reports/energy/{window}) · "
                 f"[JSON](https://dchub.cloud/api/v1/reports/energy/{window}) · "
                 f"CC-BY-4.0")
    lines.append("")

    if narr.get("text"):
        lines.append("## Executive summary")
        lines.append(f"_auto-generated · {narr.get('model','claude')} · "
                     f"{(narr.get('generated_at') or '')[:10]}_")
        lines.append("")
        lines.append(narr["text"])
        lines.append("")

    lines.append("## DCPI verdict distribution")
    lines.append(f"- **{total} markets scored**")
    for v in ("BUILD", "CAUTION", "AVOID", "LOW_SIGNAL"):
        c = verdicts.get(v, 0)
        if total:
            lines.append(f"- **{v}**: {c} ({100*c/total:.0f}%)")
        else:
            lines.append(f"- **{v}**: {c}")
    lines.append("")

    if build:
        lines.append("## Top BUILD markets")
        for m in build[:10]:
            comp = m.get("composite")
            comp_str = f"{comp:.0f}/100" if comp is not None else "—"
            ttp = m.get("ttp_months")
            ttp_str = f", TTP {ttp:.0f}mo" if ttp else ""
            lines.append(f"- **{m.get('market','?')}** ({m.get('iso','—')}) · "
                         f"composite {comp_str}{ttp_str} · "
                         f"[detail]({m.get('page','')})")
        lines.append("")

    if avoid:
        lines.append("## Top AVOID markets")
        for m in avoid[:10]:
            cons = m.get("constraint") or 0
            lines.append(f"- **{m.get('market','?')}** ({m.get('iso','—')}) · "
                         f"constraint {cons:.0f}/100 · "
                         f"[detail]({m.get('page','')})")
        lines.append("")

    if isos:
        lines.append("## ISO health rollup")
        lines.append("| ISO | Markets | BUILD % | Avg Excess | Avg Constraint | Avg TTP (mo) |")
        lines.append("|-----|---------|---------|------------|----------------|--------------|")
        for b in isos[:10]:
            lines.append(f"| {b.get('iso','—')} | {b.get('count',0)} | "
                         f"{b.get('build_pct',0):.0f}% | {b.get('avg_excess',0):.0f} | "
                         f"{b.get('avg_constraint',0):.0f} | {b.get('avg_ttp',0):.0f} |")
        lines.append("")

    if queue and queue.get("total_mw_queued"):
        lines.append("## Interconnection queue snapshot")
        lines.append(f"- **Total MW queued**: {queue.get('total_mw_queued'):,} MW")
        share = queue.get("data_center_share_pct")
        if share is not None:
            lines.append(f"- **Data-center share**: {share:.1f}%")
        if queue.get("by_iso"):
            for q in (queue.get("by_iso") or [])[:5]:
                lines.append(f"  - {q.get('iso','—')}: {q.get('mw_queued', 0):,} MW")
        lines.append("")

    if grid:
        lines.append("## Live grid mix (snapshot)")
        for g in grid:
            rp = g.get("renewable_pct")
            ci = g.get("carbon_g_per_kwh")
            d_mw = g.get("current_demand_mw")
            parts = []
            if rp is not None: parts.append(f"{rp:.0f}% renewable")
            if ci is not None: parts.append(f"{ci:.0f} g CO2/kWh")
            if d_mw is not None: parts.append(f"{d_mw:,.0f} MW demand")
            lines.append(f"- **{g.get('iso','—')}**: {' · '.join(parts) or '—'}")
        lines.append("")

    lines.append("## Attribution")
    lines.append((d.get("license") or {}).get("citation", ""))
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated {(d.get('generated_at') or '')[:19].replace('T',' ')} UTC · "
                 f"[/api/v1/reports/energy/{window}](https://dchub.cloud/api/v1/reports/energy/{window}) · "
                 f"[/llms.txt](https://dchub.cloud/llms.txt)_")
    return "\n".join(lines)


# ── HTML renderer ────────────────────────────────────────────────────
def _render_html(d: dict, window: str) -> str:
    label = f"{_dt.date.today().strftime('%B %Y')}" if window == "monthly" else (
        f"Q{(_dt.date.today().month - 1)//3 + 1} {_dt.date.today().year}"
    )
    narr = d.get("narrative_summary") or {}
    narr_text = (narr.get("text") or "").strip()
    paragraphs = [p.strip() for p in narr_text.split("\n\n") if p.strip()]
    narr_html = "\n".join(f"<p>{_html.escape(p)}</p>" for p in paragraphs)

    verdicts = d.get("verdict_distribution") or {}
    total = d.get("markets_scored_total") or 0
    build = d.get("top_build_markets") or []
    avoid = d.get("top_avoid_markets") or []
    isos = d.get("iso_rollup") or []

    build_rows = "\n".join(
        f'<tr><td><a href="{_html.escape(m.get("page",""))}">'
        f'<strong>{_html.escape(str(m.get("market","?")))}</strong></a></td>'
        f'<td>{_html.escape(str(m.get("iso","—")))}</td>'
        f'<td style="text-align:right">{m.get("composite") or "—"}</td>'
        f'<td style="text-align:right">{m.get("ttp_months") or "—"}</td></tr>'
        for m in build[:10]
    ) or '<tr><td colspan="4" style="color:#71717a;text-align:center"><em>No BUILD markets currently scored.</em></td></tr>'

    avoid_rows = "\n".join(
        f'<tr><td><a href="{_html.escape(m.get("page",""))}">'
        f'<strong>{_html.escape(str(m.get("market","?")))}</strong></a></td>'
        f'<td>{_html.escape(str(m.get("iso","—")))}</td>'
        f'<td style="text-align:right;color:#ef4444">{m.get("constraint") or "—"}</td></tr>'
        for m in avoid[:10]
    ) or '<tr><td colspan="3" style="color:#71717a;text-align:center"><em>No AVOID markets currently scored.</em></td></tr>'

    iso_rows = "\n".join(
        f'<tr><td><strong>{_html.escape(str(b.get("iso","—")))}</strong></td>'
        f'<td style="text-align:right">{b.get("count",0)}</td>'
        f'<td style="text-align:right;color:#10b981">{b.get("build_pct",0):.0f}%</td>'
        f'<td style="text-align:right">{b.get("avg_excess",0):.0f}</td>'
        f'<td style="text-align:right">{b.get("avg_constraint",0):.0f}</td>'
        f'<td style="text-align:right">{b.get("avg_ttp",0):.0f}</td></tr>'
        for b in isos[:12]
    ) or '<tr><td colspan="6" style="color:#71717a;text-align:center"><em>ISO rollup unavailable.</em></td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub — {label} Data Center Energy Report</title>
<meta name="description" content="Live data-center energy + power-availability report. {total} markets scored by DCPI, BUILD/AVOID watch lists, ISO health rollup, interconnection-queue depth. CC-BY-4.0.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/reports/energy/{window}">
<meta property="og:title" content="DC Hub — {label} Energy Report">
<meta property="og:description" content="{total} markets scored · {verdicts.get('BUILD',0)} BUILD · {verdicts.get('AVOID',0)} AVOID · daily refresh · CC-BY-4.0">
<style>
  body {{ margin:0; background:#0a0e1a; color:#e5e7eb; font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif; line-height:1.55 }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:60px 28px 100px }}
  .eyebrow {{ font-family:ui-monospace,Menlo,monospace; font-size:11px; text-transform:uppercase; letter-spacing:.12em; color:#6366f1; margin-bottom:14px }}
  h1 {{ font-size:42px; line-height:1.1; margin:0 0 16px; letter-spacing:-.02em }}
  h2 {{ font-size:22px; margin:48px 0 12px; letter-spacing:-.01em }}
  .lede {{ font-size:18px; color:#cbd5e1; margin-bottom:32px; max-width:70ch }}
  .narr {{ background:rgba(99,102,241,.06); border-left:3px solid #6366f1; padding:24px 28px; border-radius:6px; margin:24px 0 40px }}
  .narr p {{ margin:0 0 14px; font-size:16px; line-height:1.65 }}
  .narr p:last-child {{ margin-bottom:0 }}
  .narr-meta {{ font-family:ui-monospace,Menlo,monospace; font-size:11px; text-transform:uppercase; letter-spacing:.12em; color:#6366f1; margin-bottom:14px }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:32px 0 48px }}
  .stat {{ background:rgba(255,255,255,.04); padding:18px 16px; border-radius:8px; border-left:3px solid #6366f1 }}
  .stat-num {{ font-size:28px; font-weight:700; display:block; letter-spacing:-.02em }}
  .stat-lbl {{ font-family:ui-monospace,Menlo,monospace; font-size:11px; text-transform:uppercase; color:#94a3b8; margin-top:6px }}
  table {{ width:100%; border-collapse:collapse; margin:14px 0 28px }}
  th, td {{ padding:10px 12px; text-align:left; font-size:14px; border-bottom:1px solid rgba(255,255,255,.06) }}
  th {{ background:rgba(255,255,255,.03); font-family:ui-monospace,Menlo,monospace; font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:#94a3b8 }}
  a {{ color:#93c5fd }}
  .license-foot {{ background:rgba(16,185,129,.06); border-left:3px solid #10b981; padding:20px 24px; border-radius:6px; margin-top:60px; font-size:14px; color:#cbd5e1 }}
  .verdict-build {{ color:#10b981; font-weight:700 }}
  .verdict-avoid {{ color:#ef4444; font-weight:700 }}
  @media (max-width:700px) {{ h1 {{ font-size:30px }} .stats {{ grid-template-columns:1fr 1fr }} .wrap {{ padding:36px 20px 80px }} }}
</style></head><body>
<div class="wrap">
<div class="eyebrow">DC Hub · Energy Report · CC-BY-4.0 · {d.get('as_of_date','')}</div>
<h1>{label} Data Center Energy Report</h1>
<p class="lede">Live power and grid intelligence across {total} scored markets and 10 ISOs. Verdict shifts, BUILD/AVOID watch lists, interconnection-queue depth, and live fuel mix — refreshed daily, machine-readable, CC-BY-4.0.</p>

<div class="stats">
  <div class="stat"><span class="stat-num">{total}</span><span class="stat-lbl">Markets scored (DCPI)</span></div>
  <div class="stat"><span class="stat-num verdict-build">{verdicts.get('BUILD',0)}</span><span class="stat-lbl">BUILD verdicts</span></div>
  <div class="stat"><span class="stat-num" style="color:#f59e0b">{verdicts.get('CAUTION',0)}</span><span class="stat-lbl">CAUTION verdicts</span></div>
  <div class="stat"><span class="stat-num verdict-avoid">{verdicts.get('AVOID',0)}</span><span class="stat-lbl">AVOID verdicts</span></div>
</div>

{('<div class="narr"><div class="narr-meta">Executive summary · auto-generated · ' + narr.get('model','claude') + ' · ' + (narr.get('generated_at') or '')[:10] + '</div>' + narr_html + '</div>') if narr_text else ''}

<h2>Top BUILD markets</h2>
<table>
  <thead><tr><th>Market</th><th>ISO</th><th style="text-align:right">Composite</th><th style="text-align:right">TTP (mo)</th></tr></thead>
  <tbody>{build_rows}</tbody>
</table>

<h2>Top AVOID markets</h2>
<table>
  <thead><tr><th>Market</th><th>ISO</th><th style="text-align:right">Constraint</th></tr></thead>
  <tbody>{avoid_rows}</tbody>
</table>

<h2>ISO health rollup</h2>
<table>
  <thead><tr><th>ISO</th><th style="text-align:right">Markets</th><th style="text-align:right">BUILD %</th><th style="text-align:right">Avg Excess</th><th style="text-align:right">Avg Constraint</th><th style="text-align:right">Avg TTP (mo)</th></tr></thead>
  <tbody>{iso_rows}</tbody>
</table>

<div class="license-foot">
  <span style="display:inline-block;padding:3px 9px;background:#10b981;color:#0a0e1a;font-weight:700;border-radius:4px;font-size:11px;letter-spacing:.5px;margin-right:10px">CC-BY-4.0</span>
  Licensed under <a rel="license" href="https://creativecommons.org/licenses/by/4.0/">Creative Commons Attribution 4.0 International</a>.
  Quote freely with attribution to DC Hub.<br><br>
  <code style="background:rgba(255,255,255,.06);padding:2px 6px;border-radius:3px;font-size:12px;color:#c7d2fe">{(d.get('license') or {}).get('citation', '')}</code>
</div>

<p style="text-align:center;color:#64748b;font-size:13px;margin-top:60px">
  DC Hub · <a href="/">dchub.cloud</a> ·
  <a href="/reports/energy/{window}.md">Markdown</a> ·
  <a href="/api/v1/reports/energy/{window}">JSON</a> ·
  <a href="/sample">Sample landing</a>
</p>
</div>
</body></html>"""
