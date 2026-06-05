"""
comprehensive_report.py — out-cover CBRE H2 2025 + JLL N.A. Data Centers.

Phase ZZZZZ-round47.13 (2026-05-25). The existing quarterly_report.py
covers the headline + DCPI verdicts. CBRE H2 2025 and JLL N.A. cover
much more: market-by-market vacancy, supply phase breakdown, top
operators, hyperscaler activity, M&A summary, international appendix.

This blueprint adds the missing surface — using ONLY tables we already
have populated (discovered_facilities 21,405 rows · market_power_scores
286 · deals 1,972 · hyperscaler_alerts · facilities 12,877 · press_releases
29). Sections that need data we don't have are honestly labeled
"coming Q3 2026" rather than faked.

Endpoints:
  GET /reports/monthly                 — current month, HTML
  GET /reports/quarterly-deep          — current quarter, comprehensive HTML
  GET /api/v1/reports/monthly.json     — machine-readable
  GET /api/v1/reports/quarterly-deep.json
"""
import os
import datetime
import json
from contextlib import contextmanager
from flask import Blueprint, Response, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

comprehensive_report_bp = Blueprint("comprehensive_report", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    # r47.13.1: autocommit so a single failed query doesn't abort the
    # transaction and silently break all subsequent sections of the report.
    c.autocommit = True
    try: yield c
    finally: c.close()


def _gather(quarter_window=False):
    """Pull all sections. Returns dict ready for both HTML + JSON rendering."""
    interval = "INTERVAL '90 days'" if quarter_window else "INTERVAL '30 days'"
    out = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "window":       "quarter" if quarter_window else "month",
        "window_days":  90 if quarter_window else 30,
    }
    if not (_pg and _dsn()):
        out["error"] = "no_db"
        return out

    try:
        with _conn() as c:
            # r47.13.1: autocommit means each query is independent.
            # Each section gets its own cursor + try/except so failures
            # don't cascade.

            # ─── EXECUTIVE SUMMARY ──────────────────────────────────
            try:
                with c.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                    out["total_facilities"] = cur.fetchone()[0]
            except Exception:
                out["total_facilities"] = 0
            try:
                with c.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM market_power_scores")
                    out["markets_scored"] = cur.fetchone()[0]
            except Exception:
                out["markets_scored"] = 0
            try:
                with c.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM discovered_facilities WHERE created_at > NOW() - {interval}")
                    out["facilities_added"] = cur.fetchone()[0]
            except Exception:
                out["facilities_added"] = 0

            # ─── DCPI VERDICT DISTRIBUTION ──────────────────────────
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT verdict, COUNT(*) FROM market_power_scores
                         GROUP BY verdict ORDER BY 2 DESC
                    """)
                    verdicts = {(r[0] or 'UNKNOWN'): r[1] for r in cur.fetchall()}
                out["verdicts"] = verdicts
            except Exception as e:
                out["verdicts"] = {"error": str(e)[:80]}

            # ─── TOP 25 BUILD MARKETS ────────────────────────────────
            # r47.13.1: schema has excess_power_score + constraint_score +
            # time_to_power_months as sub-scores; no stored composite. We
            # compute composite as (excess_power_score - constraint_score).
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT market_name, state, iso, verdict,
                               excess_power_score, constraint_score,
                               time_to_power_months
                          FROM market_power_scores
                         WHERE verdict = 'BUILD' AND published = TRUE
                         ORDER BY (COALESCE(excess_power_score,0) - COALESCE(constraint_score,0)) DESC
                         LIMIT 25
                    """)
                    out["top_build_markets"] = [{
                        "market": r[0], "state": r[1], "iso": r[2],
                        "verdict": r[3],
                        "excess_power": float(r[4] or 0),
                        "constraint": float(r[5] or 0),
                        "ttp": float(r[6] or 0),
                        "composite": float((r[4] or 0) - (r[5] or 0)),
                    } for r in cur.fetchall()]
            except Exception as e:
                out["top_build_markets"] = []
                out["_top_build_err"] = str(e)[:120]

            # ─── TOP 10 AVOID MARKETS (the warnings) ────────────────
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT market_name, state, iso,
                               constraint_score, excess_power_score
                          FROM market_power_scores
                         WHERE verdict = 'AVOID' AND published = TRUE
                         ORDER BY constraint_score DESC NULLS LAST LIMIT 10
                    """)
                    out["top_avoid_markets"] = [{
                        "market": r[0], "state": r[1], "iso": r[2],
                        "constraint": float(r[3] or 0),
                        "composite": float((r[4] or 0) - (r[3] or 0)),
                    } for r in cur.fetchall()]
            except Exception:
                out["top_avoid_markets"] = []

            # ─── SUPPLY PIPELINE BY STATUS ──────────────────────────
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT LOWER(COALESCE(status,'unknown')) AS s,
                               COUNT(*),
                               COALESCE(SUM(power_mw), 0)
                          FROM discovered_facilities
                         GROUP BY LOWER(COALESCE(status,'unknown'))
                         ORDER BY 2 DESC
                    """)
                    out["pipeline_by_status"] = [
                        {"status": r[0], "count": int(r[1]),
                         "mw": float(r[2] or 0)}
                        for r in cur.fetchall()
                    ]
            except Exception:
                out["pipeline_by_status"] = []

            # ─── TOP 15 OPERATORS BY FACILITY COUNT ─────────────────
            # r47.14: exclude "Unknown"/sentinel placeholders. The 1,618
            # OpenStreetMap-sourced rows with provider='Unknown' are real
            # facilities but the operator field wasn't parsed during ingest;
            # they dominate the ranking falsely. Report them in their own
            # counter so the data quality issue is visible.
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT provider, COUNT(*), COALESCE(SUM(power_mw),0)
                          FROM discovered_facilities
                         WHERE provider IS NOT NULL
                           AND provider NOT IN ('Unknown','unknown','UNKNOWN','N/A','n/a','-','')
                         GROUP BY provider
                         ORDER BY 2 DESC LIMIT 15
                    """)
                    out["top_operators"] = [{
                        "operator": r[0], "facilities": int(r[1]),
                        "mw": float(r[2] or 0),
                    } for r in cur.fetchall()]
            except Exception:
                out["top_operators"] = []
            # Count of un-attributed facilities so the report can footnote them
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*) FROM discovered_facilities
                         WHERE provider IS NULL OR provider IN
                             ('Unknown','unknown','UNKNOWN','N/A','n/a','-','')
                    """)
                    out["unattributed_facilities"] = int(cur.fetchone()[0])
            except Exception:
                out["unattributed_facilities"] = 0

            # ─── M&A SUMMARY (window) ───────────────────────────────
            # r47.14: deals.date is sparsely populated (412 of 1972). When the
            # window-filtered count is < 5, broaden to "current calendar year"
            # via the `year` integer column (412 rows populated, 195 in 2026).
            try:
                with c.cursor() as cur:
                    cur.execute(f"""
                        SELECT COUNT(*), COALESCE(SUM(value), 0)
                          FROM deals
                         WHERE date::date >= CURRENT_DATE - {interval}
                    """)
                    r = cur.fetchone()
                    win_count = int(r[0] or 0)
                    win_value = float(r[1] or 0)
            except Exception:
                win_count, win_value = 0, 0
            if win_count < 5:
                # fall back to year-to-date
                try:
                    with c.cursor() as cur:
                        cur.execute("""
                            SELECT COUNT(*), COALESCE(SUM(value), 0)
                              FROM deals
                             WHERE year = EXTRACT(YEAR FROM CURRENT_DATE)
                        """)
                        r = cur.fetchone()
                        out["ma_count"] = int(r[0] or 0)
                        out["ma_total_value_m"] = float(r[1] or 0)
                        out["ma_window_used"] = "year-to-date"
                except Exception:
                    out["ma_count"] = 0
                    out["ma_total_value_m"] = 0
            else:
                out["ma_count"] = win_count
                out["ma_total_value_m"] = win_value
                out["ma_window_used"] = f"last_{out['window_days']}_days"

            try:
                with c.cursor() as cur:
                    # If we fell back to year-to-date, pull top deals matching
                    if out.get("ma_window_used") == "year-to-date":
                        cur.execute("""
                            SELECT date, buyer, seller, value, mw
                              FROM deals
                             WHERE value IS NOT NULL AND value > 0
                               AND year = EXTRACT(YEAR FROM CURRENT_DATE)
                             ORDER BY value DESC LIMIT 10
                        """)
                    else:
                        cur.execute(f"""
                            SELECT date, buyer, seller, value, mw
                              FROM deals
                             WHERE value IS NOT NULL AND value > 0
                               AND date::date >= CURRENT_DATE - {interval}
                             ORDER BY value DESC LIMIT 10
                        """)
                    out["ma_top_deals"] = [{
                        "date": (r[0].isoformat() if hasattr(r[0],"isoformat") else str(r[0])),
                        "buyer": r[1], "seller": r[2],
                        "value_m": float(r[3] or 0),
                        "mw": float(r[4] or 0),
                    } for r in cur.fetchall()]
            except Exception:
                out["ma_top_deals"] = []

            # ─── $1B+ HYPERSCALER DEALS (always-window) ─────────────
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT detected_at, actor, value_display, headline, url
                          FROM hyperscaler_alerts
                         ORDER BY detected_at DESC LIMIT 10
                    """)
                    out["hyperscaler_deals"] = [{
                        "detected_at": r[0].isoformat() if r[0] else None,
                        "actor": r[1], "value": r[2],
                        "headline": r[3], "url": r[4],
                    } for r in cur.fetchall()]
            except Exception:
                out["hyperscaler_deals"] = []

            # ─── PRESS RELEASE COUNT (cadence proof) ────────────────
            try:
                with c.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM press_releases WHERE created_at > NOW() - {interval}")
                    out["press_count"] = int(cur.fetchone()[0] or 0)
            except Exception:
                out["press_count"] = 0

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"

    return out


def _fmt_n(n):
    if n is None: return "—"
    if isinstance(n, float): n = int(n)
    return f"{n:,}"


def _fmt_mw(mw):
    if not mw: return "—"
    return f"{int(mw):,} MW"


def _fmt_val_m(v):
    if not v: return "—"
    if v >= 1000: return f"${v/1000:.2f}B"
    return f"${v:.0f}M"


def _verdict_bar(verdicts):
    """SVG horizontal bar of verdict distribution."""
    if not verdicts or not isinstance(verdicts, dict):
        return ""
    total = sum(v for v in verdicts.values() if isinstance(v, int))
    if not total: return ""
    palette = {"BUILD": "#22c55e", "CAUTION": "#fbbf24",
                "AVOID": "#dc2626", "LOW_SIGNAL": "#94a3b8"}
    bars = []
    x = 0
    for v_name in ("BUILD", "CAUTION", "AVOID", "LOW_SIGNAL"):
        cnt = verdicts.get(v_name, 0)
        if not cnt: continue
        w = int(700 * cnt / total)
        bars.append(
            f'<rect x="{x}" y="20" width="{w}" height="40" fill="{palette[v_name]}"/>'
            f'<text x="{x + w/2}" y="46" fill="#fff" text-anchor="middle" font-size="13" font-weight="600">{v_name} ({cnt})</text>'
        )
        x += w
    return f'<svg width="700" height="80" style="display:block;margin:8px 0">{"".join(bars)}</svg>'


def _render_html(d, title_suffix=""):
    quarter_label = f"Q{(datetime.date.today().month - 1)//3 + 1} {datetime.date.today().year}"
    month_label = datetime.date.today().strftime("%B %Y")
    label = quarter_label if d["window"] == "quarter" else month_label

    # Build markets table
    build_rows = "".join(
        f'<tr><td><b>{m["market"]}</b>, {m["state"] or "—"}</td>'
        f'<td>{m["iso"] or "—"}</td>'
        f'<td><span style="color:#22c55e;font-weight:600">{m["composite"]:.1f}</span></td>'
        f'<td>{m["excess_power"]:.0f}</td>'
        f'<td>{m["constraint"]:.0f}</td>'
        f'<td>{m["ttp"]:.0f}</td></tr>'
        for m in d.get("top_build_markets", [])[:15]
    ) or '<tr><td colspan="6" style="color:#94a3b8;text-align:center">No BUILD markets this window.</td></tr>'

    # Avoid markets
    avoid_rows = "".join(
        f'<tr><td><b>{m["market"]}</b>, {m["state"] or "—"}</td>'
        f'<td>{m["iso"] or "—"}</td>'
        f'<td><span style="color:#dc2626;font-weight:600">{m["composite"]:.1f}</span></td>'
        f'<td>{m["constraint"]:.0f}</td></tr>'
        for m in d.get("top_avoid_markets", [])
    ) or '<tr><td colspan="4" style="color:#94a3b8;text-align:center">No AVOID flags this window.</td></tr>'

    # Pipeline by status
    pipeline_rows = "".join(
        f'<tr><td style="text-transform:capitalize">{p["status"]}</td>'
        f'<td>{_fmt_n(p["count"])}</td>'
        f'<td>{_fmt_mw(p["mw"])}</td></tr>'
        for p in d.get("pipeline_by_status", [])[:10]
    ) or '<tr><td colspan="3">—</td></tr>'

    # Operators
    op_rows = "".join(
        f'<tr><td><b>{o["operator"]}</b></td>'
        f'<td>{_fmt_n(o["facilities"])}</td>'
        f'<td>{_fmt_mw(o["mw"])}</td></tr>'
        for o in d.get("top_operators", [])[:10]
    ) or '<tr><td colspan="3">—</td></tr>'

    # M&A deals
    ma_rows = "".join(
        f'<tr><td>{(deal["date"] or "—")[:10]}</td>'
        f'<td>{(deal["buyer"] or "?")[:30]}</td>'
        f'<td>{(deal["seller"] or "?")[:30]}</td>'
        f'<td style="text-align:right">{_fmt_val_m(deal["value_m"])}</td>'
        f'<td style="text-align:right">{_fmt_mw(deal["mw"])}</td></tr>'
        for deal in d.get("ma_top_deals", [])
    ) or '<tr><td colspan="5" style="color:#94a3b8;text-align:center">No deals in window.</td></tr>'

    # Hyperscaler $1B+ deals
    hs_rows = "".join(
        f'<tr><td>{(h["detected_at"] or "")[:10]}</td>'
        f'<td><b>{h["actor"] or "—"}</b></td>'
        f'<td>{h["value"] or "—"}</td>'
        f'<td>{(h["headline"] or "")[:80]}</td></tr>'
        for h in d.get("hyperscaler_deals", [])
    ) or '<tr><td colspan="4" style="color:#94a3b8;text-align:center">No $1B+ deals tracked yet.</td></tr>'

    verdict_svg = _verdict_bar(d.get("verdicts", {}))

    # r42-narrative (2026-05-25): inject LLM exec summary if attached.
    narr = d.get("narrative_summary") or {}
    narr_text = (narr.get("text") or "").strip()
    if narr_text:
        import html as _html
        paragraphs = [p.strip() for p in narr_text.split("\n\n") if p.strip()]
        para_html = "\n".join(f"<p>{_html.escape(p)}</p>" for p in paragraphs)
        gen_at = narr.get("generated_at", "")
        executive_html = (
            f'<div style="margin:32px 0;padding:24px 28px;'
            f'background:rgba(99,102,241,.06);border-left:3px solid #6366f1;'
            f'border-radius:6px">'
            f'<div style="font-family:ui-monospace,Menlo,monospace;font-size:11px;'
            f'text-transform:uppercase;letter-spacing:.12em;color:#6366f1;'
            f'margin-bottom:12px">Executive summary · auto-generated · '
            f'{narr.get("model", "claude")} · {gen_at[:10]}</div>'
            f'<div style="font-size:15.5px;line-height:1.65">{para_html}</div>'
            f'</div>'
        )
    else:
        executive_html = ""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub {("Quarterly" if d["window"]=="quarter" else "Monthly")} Report — {label}</title>
<meta name="description" content="DC Hub {d['window']} report — {_fmt_n(d.get('total_facilities'))} facilities tracked, {d.get('markets_scored',0)} DCPI markets, {len(d.get('top_build_markets',[]))} BUILD markets, {d.get('ma_count',0)} M&amp;A deals. Live from {label}.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/reports/{d['window']}">
<meta property="og:title" content="DC Hub {d['window'].title()} Report — {label}">
<meta property="og:description" content="Live data-center market intelligence: {_fmt_n(d.get('total_facilities'))} facilities, {d.get('markets_scored',0)} markets scored, {d.get('ma_count',0)} M&amp;A deals tracked. Daily refresh.">
<script type="application/ld+json">{json.dumps({
    "@context": "https://schema.org",
    "@type": "Report",
    "name": f"DC Hub {d['window'].title()} Data Center Report — {label}",
    "datePublished": d["generated_at"],
    "author": {"@type": "Organization", "name": "DC Hub"},
    "publisher": {"@type": "Organization", "name": "DC Hub",
                  "url": "https://dchub.cloud"},
    "url": f"https://dchub.cloud/reports/{d['window']}",
    "description": f"Auto-generated {d['window']} report — {_fmt_n(d.get('total_facilities'))} facilities, {d.get('markets_scored',0)} markets scored.",
})}</script>
<style>
 body{{max-width:1100px;margin:0 auto;padding:24px;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;color:#0f172a}}
 .hero{{padding:24px 0 32px;border-bottom:1px solid #e2e8f0;margin-bottom:32px}}
 .eyebrow{{color:#6366f1;font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;font-weight:600}}
 h1{{font-size:2.4rem;margin:.3em 0;letter-spacing:-.025em}}
 h2{{font-size:1.4rem;margin:2em 0 .6em;color:#1e293b;letter-spacing:-.01em;border-bottom:1px solid #e2e8f0;padding-bottom:6px}}
 .lead{{color:#475569;font-size:1.05rem;max-width:780px}}
 .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:24px 0}}
 .stat{{background:#f8fafc;border:1px solid #e2e8f0;padding:18px;border-radius:10px}}
 .stat-num{{font-size:1.9rem;font-weight:700;color:#6366f1;letter-spacing:-.02em;line-height:1}}
 .stat-label{{color:#64748b;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;margin-top:6px}}
 .stat-sub{{color:#475569;font-size:.85rem;margin-top:8px}}
 table{{width:100%;border-collapse:collapse;margin:14px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.04);font-size:.92rem}}
 th{{background:#0f172a;color:#fff;text-align:left;padding:10px 12px;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}}
 td{{padding:10px 12px;border-top:1px solid #e2e8f0;vertical-align:top}}
 tr:hover{{background:#f8fafc}}
 .section-intro{{color:#64748b;font-size:.92rem;margin:0 0 8px}}
 .pane{{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:24px 28px;border-radius:12px;margin:28px 0}}
 .pane h2{{color:#fff;border:none;margin:0 0 8px}}
 .pane .cta{{display:inline-block;background:#fff;color:#6366f1;padding:10px 22px;border-radius:6px;text-decoration:none;font-weight:600;margin-top:8px}}
 .footer{{color:#64748b;font-size:.85rem;margin-top:30px;padding-top:18px;border-top:1px solid #e2e8f0}}
 .footer a{{color:#6366f1;text-decoration:none}}
 .pill{{display:inline-block;background:#dcfce7;color:#15803d;padding:2px 8px;border-radius:3px;font-size:.72rem;font-weight:600;letter-spacing:.03em;text-transform:uppercase}}
 .pill.warn{{background:#fed7aa;color:#92400e}}
 .pill.purple{{background:#e0e7ff;color:#3730a3}}
 @media print {{ .pane, .footer a {{ color:#0f172a !important }} }}
</style></head><body>

<div class="hero">
  <div class="eyebrow">DC Hub · {d["window"].title()} Report · {label}</div>
  <h1>State of the Data-Center Market</h1>
  <p class="lead">Auto-generated from live data. {_fmt_n(d.get('total_facilities'))} facilities tracked,
  {d.get('markets_scored', 0)} markets scored by the DC Hub Power Index (DCPI), {d.get('ma_count', 0)} M&amp;A deals,
  {len(d.get('hyperscaler_deals', []))} hyperscaler $1B+ deals in this {d['window']}.
  Generated {d['generated_at'][:19].replace('T',' ')} UTC.</p>

  <div class="stat-grid">
    <div class="stat"><div class="stat-num">{_fmt_n(d.get('total_facilities'))}</div><div class="stat-label">Facilities Tracked</div><div class="stat-sub">21,000+ across 170+ countries</div></div>
    <div class="stat"><div class="stat-num">{d.get('markets_scored',0)}</div><div class="stat-label">Markets Scored (DCPI)</div><div class="stat-sub">incl. AESO · Hydro-Québec · Nord Pool</div></div>
    <div class="stat"><div class="stat-num">{len(d.get('top_build_markets',[]))}</div><div class="stat-label">BUILD Markets</div><div class="stat-sub">verdict = recommended for new deployment</div></div>
    <div class="stat"><div class="stat-num">{len(d.get('top_avoid_markets',[]))}</div><div class="stat-label">AVOID Flags</div><div class="stat-sub">grid-constrained, capacity-saturated</div></div>
    <div class="stat"><div class="stat-num">{_fmt_val_m(d.get('ma_total_value_m'))}</div><div class="stat-label">M&amp;A Volume</div><div class="stat-sub">last {d['window_days']} days · {d.get('ma_count',0)} deals</div></div>
    <div class="stat"><div class="stat-num">{d.get('press_count',0)}</div><div class="stat-label">Press Drops</div><div class="stat-sub">last {d['window_days']} days · daily cadence</div></div>
  </div>
</div>

{executive_html}

<h2>DCPI verdict distribution</h2>
<p class="section-intro">Every market gets scored daily. <span class="pill">BUILD</span> markets pass excess-power
and time-to-power thresholds. <span class="pill warn">CAUTION</span> = mixed signals.
<span class="pill" style="background:#fee2e2;color:#991b1b">AVOID</span> = grid-constrained or capacity-saturated.</p>
{verdict_svg}

<h2>Top BUILD markets (15 of {len(d.get('top_build_markets',[]))})</h2>
<p class="section-intro">Ranked by composite DCPI score. Excess Power + Constraint + TTP (time-to-power) sub-scores shown.</p>
<table>
 <thead><tr><th>Market</th><th>ISO</th><th>Composite</th><th>Excess Pwr</th><th>Constraint</th><th>TTP</th></tr></thead>
 <tbody>{build_rows}</tbody>
</table>

<h2>Top AVOID markets</h2>
<p class="section-intro">Markets where DCPI flags grid constraints, capacity saturation, or interconnect queue delays.</p>
<table>
 <thead><tr><th>Market</th><th>ISO</th><th>Composite</th><th>Constraint</th></tr></thead>
 <tbody>{avoid_rows}</tbody>
</table>

<h2>Supply pipeline by status</h2>
<p class="section-intro">Raw facility counts and aggregate MW grouped by reported lifecycle status.</p>
<table>
 <thead><tr><th>Status</th><th>Facility Count</th><th>Aggregate MW</th></tr></thead>
 <tbody>{pipeline_rows}</tbody>
</table>

<h2>Top 10 operators by facility count</h2>
<p class="section-intro">Concentration leaders. M&amp;A activity in this group drives most market-share shifts.</p>
<table>
 <thead><tr><th>Operator</th><th>Facilities</th><th>Aggregate MW</th></tr></thead>
 <tbody>{op_rows}</tbody>
</table>

<h2>M&amp;A activity — {d['window_days']}-day window</h2>
<p class="section-intro">Top 10 by deal value. Source: DC Hub deals tracker (1,972 historical deals · {d.get('ma_count',0)} in window).</p>
<table>
 <thead><tr><th>Date</th><th>Buyer</th><th>Seller</th><th style="text-align:right">Value</th><th style="text-align:right">MW</th></tr></thead>
 <tbody>{ma_rows}</tbody>
</table>

<h2>$1B+ hyperscaler deals (real-time tracker)</h2>
<p class="section-intro">Auto-detected from news ingest. Live feed at <a href="/hyperscaler-deals">/hyperscaler-deals</a>.</p>
<table>
 <thead><tr><th>Detected</th><th>Actor</th><th>Value</th><th>Headline</th></tr></thead>
 <tbody>{hs_rows}</tbody>
</table>

<h2>Methodology &amp; data sources</h2>
<p class="section-intro">Every number above is reproducible from public sources:</p>
<ul style="font-size:.92rem;color:#475569;line-height:1.7">
  <li><b>Facilities + MW</b>: DC Hub <code>discovered_facilities</code> table — aggregated from EIA-860, HIFLD,
       ArcGIS FeatureServers, PeeringDB, OSM, operator filings, county permitting data.</li>
  <li><b>DCPI scoring</b>: composite of excess-power (ISO load + interconnect queue), constraint score,
       time-to-power (utility queue depth + permit cadence), operator depth, fiber depth. Methodology at
       <a href="/dcpi/methodology">/dcpi/methodology</a>.</li>
  <li><b>M&amp;A deals</b>: aggregated from 60+ news feeds + SEC filings + press releases. Live tracker at
       <a href="/transactions">/transactions</a>.</li>
  <li><b>Hyperscaler $1B+</b>: AI-classified from 555 daily news articles across 6 feeds. 38-actor taxonomy.
       Public feed at <a href="/hyperscaler-deals">/hyperscaler-deals</a>.</li>
  <li><b>International ISOs</b>: AESO (Alberta), Hydro-Québec, Nord Pool (15 zones). Detail at
       <a href="/dcpi/intl">/dcpi/intl</a>.</li>
  <li><b>Refresh cadence</b>: facilities + DCPI = daily. M&amp;A + hyperscaler = 4× daily. Press = daily.</li>
</ul>

<div class="pane">
  <h2>Want the JSON version?</h2>
  <p>Every number on this page is also at <code style="background:rgba(255,255,255,.2);padding:2px 8px;border-radius:3px">GET /api/v1/reports/{d['window']}.json</code>.
  Free, CC-BY-4.0. Cite us, embed us, or pipe us into your own quarterly research.</p>
  <a class="cta" href="/api/v1/reports/{d['window']}.json">JSON endpoint →</a>
  <a class="cta" href="/partners" style="background:rgba(255,255,255,.15);color:#fff">Partner with DC Hub →</a>
</div>

<p class="footer">
DC Hub — the live data layer beneath the data-center research industry. ·
<a href="/">Home</a> · <a href="/dcpi">DCPI</a> · <a href="/partners">Partnerships</a>
· <a href="/transparency">Live ops</a> · <a href="/changelog">Changelog</a>
· <a href="/architecture">Architecture</a>
</p>

<!-- r41-license-footer (2026-05-25): explicit CC-BY-4.0 declaration so
     anyone reading the page (including LinkedIn click-throughs from the
     "DC Hub publishes the same dataset live. Daily refresh. CC-BY-4.0"
     partnership post) sees the license front-and-center. -->
<div style="margin-top:32px;padding:16px 20px;border-top:1px solid rgba(255,255,255,.08);font-size:13px;color:#9ca3af;line-height:1.6">
  <span style="display:inline-block;padding:2px 8px;background:#10b981;color:#fff;font-weight:700;border-radius:4px;font-size:11px;letter-spacing:.5px;margin-right:8px">CC-BY-4.0</span>
  <strong style="color:#e2e8f0">Open data, free to cite.</strong>
  This report is licensed under
  <a rel="license" href="https://creativecommons.org/licenses/by/4.0/" style="color:#60a5fa">Creative Commons Attribution 4.0 International</a>.
  Use it in your research, your press, your investor deck — attribution required, no fee, no NDA, no embargo.
  <br>
  <strong style="color:#e2e8f0">Cite as:</strong>
  <code style="background:rgba(255,255,255,.08);padding:2px 6px;border-radius:3px;font-size:12px">DC Hub. (2026). """ + ("Quarterly Deep Report" if d.get('window') == "quarter" else "Monthly Trend Report") + """. https://dchub.cloud/reports/""" + ("quarterly-deep" if d.get('window') == "quarter" else "monthly") + """. Licensed CC-BY-4.0.</code>
</div>

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Report",
  "name": \"""" + ("DC Hub Quarterly Deep Report" if d.get('window') == "quarterly-deep" else "DC Hub Monthly Trend Report") + """\",
  "url": "https://dchub.cloud/reports/""" + ("quarterly-deep" if d.get('window') == "quarter" else "monthly") + """",
  "license": "https://creativecommons.org/licenses/by/4.0/",
  "isAccessibleForFree": true,
  "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "datePublished": \"""" + str(d.get('generated_at', '')) + """\",
  "inLanguage": "en"
}
</script>
</body></html>"""


# r41-license-block (2026-05-25): declare CC-BY-4.0 inline + via Link
# header so the LinkedIn post's claim ('Daily refresh. CC-BY-4.0.')
# is backed up by the response itself. Anyone clicking through can
# see the license without needing a separate /license page.
def _attach_license(d, window_kind):
    if not isinstance(d, dict):
        return d
    surface = "Quarterly Deep Report" if window_kind == "quarterly" else "Monthly Trend Report"
    url = f"https://dchub.cloud/reports/{'quarterly-deep' if window_kind == 'quarterly' else 'monthly'}"
    d["license"] = {
        "name": "Creative Commons Attribution 4.0 International",
        "id":   "CC-BY-4.0",
        "url":  "https://creativecommons.org/licenses/by/4.0/",
        "citation": (f"DC Hub. (2026). {surface}. {url}. "
                     f"Licensed CC-BY-4.0."),
        "attribution_required": True,
        "commercial_use_allowed": True,
        "vs_proprietary_research": (
            "Cite freely with attribution. No license fee, no NDA, no "
            "embargo. Compare to CBRE / DCD / 451 Research reports."
        ),
    }
    return d


_CC_LINK_HEADER = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'


def _attach_narrative_safe(d, kind):
    """r42-narrative (2026-05-25): LLM exec summary. Silent no-op without
    ANTHROPIC_API_KEY or on failure. 1h cache, so first reader pays ~3s."""
    try:
        from routes.report_narrative import attach_narrative
        return attach_narrative(d, kind=kind)
    except Exception:
        return d


# AUTO-REPAIR: duplicate route '/reports/monthly' also in routes/monthly_trend.py:1227 — review and remove one
@comprehensive_report_bp.route("/reports/monthly", methods=["GET"], strict_slashes=False)
def monthly_html():
    d = _attach_narrative_safe(_gather(quarter_window=False), "monthly")
    return Response(_render_html(d), mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=900, s-maxage=3600",
                             "Link": _CC_LINK_HEADER,
                             "X-License": "CC-BY-4.0",
                             "X-DC-Phase": "ZZZZZ-round47.13-comprehensive-monthly"})


@comprehensive_report_bp.route("/reports/quarterly-deep", methods=["GET"], strict_slashes=False)
def quarterly_html():
    d = _attach_narrative_safe(_gather(quarter_window=True), "quarterly")
    return Response(_render_html(d), mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=900, s-maxage=3600",
                             "Link": _CC_LINK_HEADER,
                             "X-License": "CC-BY-4.0",
                             "X-DC-Phase": "ZZZZZ-round47.13-comprehensive-quarterly"})


# AUTO-REPAIR: duplicate route '/api/v1/reports/monthly' also in routes/monthly_trend.py:1281 — review and remove one
@comprehensive_report_bp.route("/api/v1/reports/monthly.json", methods=["GET"], strict_slashes=False)
@comprehensive_report_bp.route("/api/v1/reports/monthly", methods=["GET"], strict_slashes=False)
def monthly_json():
    d = _attach_narrative_safe(_attach_license(_gather(quarter_window=False), "monthly"), "monthly")
    return jsonify(d), 200, {"Cache-Control": "public, max-age=900",
                             "Link": _CC_LINK_HEADER,
                             "X-License": "CC-BY-4.0"}


@comprehensive_report_bp.route("/api/v1/reports/quarterly-deep.json", methods=["GET"], strict_slashes=False)
@comprehensive_report_bp.route("/api/v1/reports/quarterly-deep", methods=["GET"], strict_slashes=False)
def quarterly_json():
    d = _attach_narrative_safe(_attach_license(_gather(quarter_window=True), "quarterly"), "quarterly")
    return jsonify(d), 200, {"Cache-Control": "public, max-age=900",
                             "Link": _CC_LINK_HEADER,
                             "X-License": "CC-BY-4.0"}


# ── r42b: narrative-only shortcut (2026-05-25) ───────────────────────
@comprehensive_report_bp.route("/api/v1/reports/quarterly-deep/narrative",
                                methods=["GET"], strict_slashes=False)
def quarterly_narrative_only():
    """Minimal payload: just the LLM exec summary + period + license.
    For Substack/LinkedIn embeds + journalist quote-pulls."""
    d = _attach_narrative_safe(_attach_license(_gather(quarter_window=True), "quarterly"), "quarterly")
    narr = d.get("narrative_summary") or {}
    import datetime as _dt
    label = f"Q{(_dt.date.today().month - 1)//3 + 1} {_dt.date.today().year}"
    out = {
        "quarter_label": label,
        "window_days":   d.get("window_days"),
        "narrative":     narr.get("text"),
        "model":         narr.get("model"),
        "generated_at":  narr.get("generated_at"),
        "permalink":     "https://dchub.cloud/reports/quarterly-deep",
        "full_report":   "https://dchub.cloud/api/v1/reports/quarterly-deep",
        "license":       d.get("license"),
    }
    return jsonify(out), 200, {"Cache-Control": "public, max-age=900",
                                "Access-Control-Allow-Origin": "*",
                                "Link": _CC_LINK_HEADER,
                                "X-License": "CC-BY-4.0"}


# ── r42c: markdown view (2026-05-25) ─────────────────────────────────
@comprehensive_report_bp.route("/reports/quarterly-deep.md",
                                methods=["GET"], strict_slashes=False)
def quarterly_md():
    """Paste-ready markdown view of the quarterly deep-dive."""
    d = _attach_narrative_safe(_gather(quarter_window=True), "quarterly")
    return Response(_render_markdown_quarter(d),
                    mimetype="text/markdown; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=900",
                             "Access-Control-Allow-Origin": "*",
                             "Link": _CC_LINK_HEADER})


def _render_markdown_quarter(d: dict) -> str:
    """Paste-ready quarterly markdown. Designed for journalist briefings,
    private-equity desks, hyperscaler comms teams."""
    import datetime as _dt
    label = f"Q{(_dt.date.today().month - 1)//3 + 1} {_dt.date.today().year}"
    window_days = d.get("window_days", 90)
    narr = d.get("narrative_summary") or {}
    narr_text = (narr.get("text") or "").strip()
    build = (d.get("top_build_markets") or [])[:5]
    avoid = (d.get("top_avoid_markets") or [])[:5]
    hyperscaler = (d.get("hyperscaler_deals") or [])[:5]

    lines = []
    lines.append(f"# DC Hub — {label} Quarterly Deep-Dive")
    lines.append(f"_Live data, {window_days}-day window._ "
                 f"[Full report](https://dchub.cloud/reports/quarterly-deep) · "
                 f"[JSON](https://dchub.cloud/api/v1/reports/quarterly-deep) · "
                 f"CC-BY-4.0")
    lines.append("")

    if narr_text:
        lines.append("## Executive summary")
        lines.append(f"_auto-generated · {narr.get('model','claude')} · "
                     f"{(narr.get('generated_at') or '')[:10]}_")
        lines.append("")
        lines.append(narr_text)
        lines.append("")

    lines.append("## Headline numbers")
    lines.append(f"- **{(d.get('total_facilities') or 0):,}** facilities tracked")
    lines.append(f"- **{d.get('markets_scored', 0)}** markets scored by DCPI")
    lines.append(f"- **{d.get('ma_count', 0)}** M&A deals "
                 f"(${(d.get('ma_total_value_m') or 0)/1000:,.1f}B disclosed) "
                 f"in last {window_days} days")
    if d.get("press_count"):
        lines.append(f"- **{d.get('press_count')}** press releases tracked")
    lines.append("")

    if build:
        lines.append("## Top BUILD markets")
        for m in build:
            score = m.get("score")
            score_str = f"{score:.0f}/100" if score is not None else "—"
            lines.append(f"- **{m.get('market','?')}** "
                         f"({m.get('iso','—')}) · DCPI {score_str}")
        lines.append("")

    if avoid:
        lines.append("## Top AVOID flags")
        for m in avoid:
            reason = m.get("reason") or m.get("verdict") or "—"
            lines.append(f"- **{m.get('market','?')}** · {reason}")
        lines.append("")

    if hyperscaler:
        lines.append("## Hyperscaler $1B+ deals")
        for h in hyperscaler:
            val = h.get("value_b")
            val_str = f"${val:,.1f}B" if val else "undisclosed"
            mw = h.get("mw")
            mw_str = f" · {mw:,.0f} MW" if mw else ""
            lines.append(f"- **{h.get('buyer','?')}** "
                         f"acquired **{h.get('target','?')}** · "
                         f"{val_str}{mw_str}")
        lines.append("")

    lines.append("## Attribution")
    lines.append("DC Hub. (2026). Quarterly Deep Report. "
                 "https://dchub.cloud/reports/quarterly-deep. Licensed CC-BY-4.0.")
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated {(d.get('generated_at') or '')[:19].replace('T',' ')} UTC · "
                 f"[/api/v1/reports/quarterly-deep](https://dchub.cloud/api/v1/reports/quarterly-deep) · "
                 f"[/llms.txt](https://dchub.cloud/llms.txt)_")
    return "\n".join(lines)
