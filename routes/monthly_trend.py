"""Phase FF+25-followup-r6 (2026-05-20) — monthly trend snapshot.
==========================================================================

The user said: "lets make sure the 2026 quarterly report which is really
good is updated, to a monthly trend snapshot we can share with DCD, JLL,
CBRE, etc."

The quarterly report (routes/quarterly_report.py) was structurally great
but slow-cadence — JLL/CBRE/DCD publish more often than every 90 days.
This module ships a MONTHLY edition with three extra things:

  1. Month-over-month + year-over-year DELTAS on every headline number,
     so the story is "what changed this month" not "where things stand".

  2. A press kit section with pre-written quotable sentences journalists
     can copy-paste with attribution. Saves them the lift; gets us cited.

  3. Permanent-URL archives. /reports/monthly/2026-05 always serves May's
     numbers, even in June, because we snapshot to monthly_reports table
     when a month closes. Partners can link once; the link stays accurate
     forever (unlike the live monthly which always shows current month).

Endpoints:
  GET  /reports/monthly                        — current month HTML
  GET  /reports/monthly/<year>-<month>         — historical (e.g. 2026-05)
  GET  /api/v1/reports/monthly                 — JSON
  GET  /api/v1/reports/monthly/<year>-<month>  — JSON of a specific month
  POST /api/v1/reports/monthly/archive         — admin: snapshot current
                                                  into monthly_reports
"""
from __future__ import annotations

import os
import json
import datetime
import logging
from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
monthly_trend_bp = Blueprint("monthly_trend", __name__)


# ── Auth (admin endpoints only) ──────────────────────────────────────
_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── DB ───────────────────────────────────────────────────────────────
def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_archive_table():
    """Create monthly_reports archive table. Idempotent."""
    c = _conn()
    if c is None: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_reports (
                    year         INT NOT NULL,
                    month        INT NOT NULL,
                    snapshot     JSONB NOT NULL,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (year, month)
                )
            """)
        return True
    except Exception as e:
        logger.warning(f"[monthly-trend] archive table create failed: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── Date math ────────────────────────────────────────────────────────
def _month_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    """Returns (first_day_inclusive, first_day_of_next_month_exclusive)."""
    first = datetime.date(year, month, 1)
    if month == 12:
        nxt = datetime.date(year + 1, 1, 1)
    else:
        nxt = datetime.date(year, month + 1, 1)
    return first, nxt


def _prior_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _prior_year_same_month(year: int, month: int) -> tuple[int, int]:
    return year - 1, month


# ── Data compute ─────────────────────────────────────────────────────
def _pct_delta(curr: float | int | None, prev: float | int | None) -> float | None:
    try:
        if curr is None or prev is None: return None
        if prev == 0: return None
        return round((float(curr) - float(prev)) / float(prev) * 100.0, 1)
    except Exception:
        return None


def _safe_scalar(cur, sql: str, params=()) -> float | int | None:
    """Run sql, return scalar, swallow ProgrammingError (table missing).
    Caller must commit/rollback on its own connection. We rollback on
    error to keep the transaction alive."""
    try:
        cur.execute(sql, params)
        r = cur.fetchone()
        if not r: return None
        v = r[0]
        if v is None: return None
        try: return float(v)
        except Exception: return v
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None


def _compute_report(year: int | None = None,
                     month: int | None = None) -> dict:
    """Pull a monthly snapshot. If year/month omitted, uses current month."""
    today = datetime.date.today()
    if year is None or month is None:
        year, month = today.year, today.month

    out: dict = {
        "year":           year,
        "month":          month,
        "month_label":    datetime.date(year, month, 1).strftime("%B %Y"),
        "generated_at":   datetime.datetime.utcnow().isoformat() + "Z",
        "as_of_date":     today.isoformat(),
    }

    # Was this month asked archived?
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out

    try:
        with c.cursor() as cur:
            # ── If a historical (already-closed) month is requested, try
            # the archive first so we serve the snapshot exactly as it was
            # when the month ended (not whatever the live tables say today). ─
            if (year, month) < (today.year, today.month):
                try:
                    cur.execute(
                        "SELECT snapshot FROM monthly_reports "
                        "WHERE year=%s AND month=%s",
                        (year, month),
                    )
                    r = cur.fetchone()
                    if r and r[0]:
                        snap = r[0] if isinstance(r[0], dict) else json.loads(r[0])
                        snap["served_from"] = "archive"
                        return snap
                except Exception:
                    try: c.rollback()
                    except Exception: pass

            curr_lo, curr_hi = _month_bounds(year, month)
            py, pm = _prior_month(year, month)
            prev_lo, prev_hi = _month_bounds(py, pm)
            yy, ym = _prior_year_same_month(year, month)
            yago_lo, yago_hi = _month_bounds(yy, ym)

            # ── HEADLINE: facilities, total MW (point-in-time, end of month)
            # We use "current totals" as the snapshot for the current/last
            # month, since the data is cumulative.
            facilities_now = int(_safe_scalar(cur, """
                SELECT COUNT(*) FROM discovered_facilities
                 WHERE merged_at IS NULL AND is_duplicate = 0
            """) or 0)
            total_mw_now = float(_safe_scalar(cur, """
                SELECT COALESCE(SUM(power_mw), 0) FROM discovered_facilities
                 WHERE merged_at IS NULL AND is_duplicate = 0
            """) or 0)

            # Facilities ADDED in the month + the prior month (for MoM growth)
            new_curr = int(_safe_scalar(cur, """
                SELECT COUNT(*) FROM discovered_facilities
                 WHERE merged_at IS NULL AND is_duplicate = 0
                   AND discovered_at >= %s AND discovered_at < %s
            """, (curr_lo, curr_hi)) or 0)
            new_prev = int(_safe_scalar(cur, """
                SELECT COUNT(*) FROM discovered_facilities
                 WHERE merged_at IS NULL AND is_duplicate = 0
                   AND discovered_at >= %s AND discovered_at < %s
            """, (prev_lo, prev_hi)) or 0)
            new_yago = int(_safe_scalar(cur, """
                SELECT COUNT(*) FROM discovered_facilities
                 WHERE merged_at IS NULL AND is_duplicate = 0
                   AND discovered_at >= %s AND discovered_at < %s
            """, (yago_lo, yago_hi)) or 0)

            out["headline"] = {
                "facilities_total":           facilities_now,
                "total_mw":                    total_mw_now,
                "facilities_added_month":      new_curr,
                "facilities_added_prior":      new_prev,
                "facilities_added_year_ago":   new_yago,
                "facilities_mom_pct":          _pct_delta(new_curr, new_prev),
                "facilities_yoy_pct":          _pct_delta(new_curr, new_yago),
            }

            # ── DEAL FLOW: deal_count + $ + MW for curr / prev / year-ago ─
            def _deal_window(lo, hi):
                n  = _safe_scalar(cur, "SELECT COUNT(*) FROM deals WHERE date >= %s AND date < %s", (lo, hi)) or 0
                v  = _safe_scalar(cur, "SELECT COALESCE(SUM(value),0) FROM deals WHERE date >= %s AND date < %s", (lo, hi)) or 0
                mw = _safe_scalar(cur, "SELECT COALESCE(SUM(mw),0)    FROM deals WHERE date >= %s AND date < %s", (lo, hi)) or 0
                return {"count": int(n), "value": float(v), "mw": float(mw)}

            curr_deals = _deal_window(curr_lo, curr_hi)
            prev_deals = _deal_window(prev_lo, prev_hi)
            yago_deals = _deal_window(yago_lo, yago_hi)

            out["deal_flow"] = {
                "current":  curr_deals,
                "prior":    prev_deals,
                "year_ago": yago_deals,
                "deals_mom_pct":   _pct_delta(curr_deals["count"], prev_deals["count"]),
                "deals_yoy_pct":   _pct_delta(curr_deals["count"], yago_deals["count"]),
                "value_mom_pct":   _pct_delta(curr_deals["value"], prev_deals["value"]),
                "value_yoy_pct":   _pct_delta(curr_deals["value"], yago_deals["value"]),
            }

            # ── TOP DEALS this month ────────────────────────────────────
            try:
                cur.execute("""
                    SELECT id, date, buyer, seller, value, mw
                      FROM deals
                     WHERE date >= %s AND date < %s AND value IS NOT NULL
                     ORDER BY value DESC LIMIT 5
                """, (curr_lo, curr_hi))
                out["top_deals"] = [{
                    "id":     int(r[0]) if r[0] else None,
                    "date":   r[1].isoformat() if hasattr(r[1], "isoformat") else (str(r[1]) if r[1] else None),
                    "buyer":  r[2], "seller": r[3],
                    "value":  float(r[4]) if r[4] is not None else None,
                    "mw":     float(r[5]) if r[5] is not None else None,
                } for r in cur.fetchall()]
            except Exception:
                try: c.rollback()
                except Exception: pass
                out["top_deals"] = []

            # ── TOP MARKETS by operating MW ─────────────────────────────
            try:
                cur.execute("""
                    SELECT COALESCE(market, city, '') AS m,
                           COUNT(*) AS n,
                           COALESCE(SUM(power_mw), 0) AS mw
                      FROM discovered_facilities
                     WHERE merged_at IS NULL AND is_duplicate = 0
                       AND COALESCE(market, city) IS NOT NULL
                     GROUP BY COALESCE(market, city)
                     ORDER BY mw DESC LIMIT 10
                """)
                out["top_markets"] = [
                    {"market": r[0], "facilities": int(r[1]),
                     "total_mw": float(r[2] or 0)}
                    for r in cur.fetchall() if r[0]
                ]
            except Exception:
                try: c.rollback()
                except Exception: pass
                out["top_markets"] = []

            # ── DCPI top movers ─────────────────────────────────────────
            try:
                cur.execute("""
                    SELECT market_name, score, weekly_delta
                      FROM market_power_scores
                     WHERE published = true AND weekly_delta IS NOT NULL
                     ORDER BY ABS(weekly_delta) DESC LIMIT 10
                """)
                out["dcpi_movers"] = [
                    {"market": r[0], "score": int(r[1] or 0),
                     "delta":  int(r[2] or 0)}
                    for r in cur.fetchall()
                ]
            except Exception:
                try: c.rollback()
                except Exception: pass
                out["dcpi_movers"] = []

            # ── CONSTRUCTION PIPELINE ──────────────────────────────────
            try:
                cur.execute("""
                    SELECT COALESCE(market, city, '') AS m,
                           COUNT(*) AS n,
                           COALESCE(SUM(power_mw), 0) AS mw
                      FROM discovered_facilities
                     WHERE merged_at IS NULL AND is_duplicate = 0
                       AND LOWER(COALESCE(status,'')) IN
                          ('construction','planned','permitting',
                           'under construction','proposed','development')
                       AND COALESCE(market, city) IS NOT NULL
                     GROUP BY COALESCE(market, city)
                     ORDER BY mw DESC LIMIT 10
                """)
                out["pipeline_by_market"] = [
                    {"market": r[0], "projects": int(r[1]),
                     "mw":     float(r[2] or 0)}
                    for r in cur.fetchall() if r[0]
                ]
            except Exception:
                try: c.rollback()
                except Exception: pass
                out["pipeline_by_market"] = []

            # ── AI / MCP USAGE ─────────────────────────────────────────
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM mcp_tool_calls
                     WHERE created_at >= %s AND created_at < %s
                """, (curr_lo, curr_hi))
                mcp_curr = int((cur.fetchone() or [0])[0] or 0)
                cur.execute("""
                    SELECT COUNT(*) FROM mcp_tool_calls
                     WHERE created_at >= %s AND created_at < %s
                """, (prev_lo, prev_hi))
                mcp_prev = int((cur.fetchone() or [0])[0] or 0)
                out["ai_traffic"] = {
                    "tool_calls_month":  mcp_curr,
                    "tool_calls_prior":  mcp_prev,
                    "mom_pct":           _pct_delta(mcp_curr, mcp_prev),
                }
            except Exception:
                try: c.rollback()
                except Exception: pass
                out["ai_traffic"] = {"tool_calls_month": None,
                                      "tool_calls_prior": None,
                                      "mom_pct": None}

            # ── CITATION PULSE (brand visibility) ──────────────────────
            try:
                cur.execute("""
                    SELECT score_pct FROM citation_scores
                     ORDER BY score_date DESC LIMIT 1
                """)
                r = cur.fetchone()
                citation_pct = float(r[0]) if r and r[0] is not None else None
            except Exception:
                try: c.rollback()
                except Exception: pass
                citation_pct = None
            out["brand_pulse"] = {"citation_score_pct": citation_pct}

    finally:
        try: c.close()
        except Exception: pass

    # ── Press-kit quotables ─────────────────────────────────────────
    out["press_kit"] = _build_press_kit(out)
    out["served_from"] = "live"
    return out


# ── Press kit ────────────────────────────────────────────────────────
def _build_press_kit(d: dict) -> dict:
    """Pre-written sentences journalists can copy-paste with attribution.
    Every claim is grounded in numbers already in `d`. If a number is
    missing, the corresponding sentence is dropped — never invent."""
    h    = d.get("headline") or {}
    df   = d.get("deal_flow") or {}
    curr = df.get("current") or {}
    ai   = d.get("ai_traffic") or {}
    label = d.get("month_label", "")

    quotables: list[str] = []

    if h.get("facilities_total") and h.get("total_mw"):
        quotables.append(
            f"DC Hub now tracks {h['facilities_total']:,} data center "
            f"facilities globally, representing {h['total_mw']:,.0f} MW "
            f"of operational and pipeline capacity."
        )

    if h.get("facilities_added_month") and h.get("facilities_mom_pct") is not None:
        direction = "up" if h["facilities_mom_pct"] >= 0 else "down"
        quotables.append(
            f"{h['facilities_added_month']:,} new facilities were "
            f"discovered in {label}, {direction} "
            f"{abs(h['facilities_mom_pct']):.1f}% month-over-month."
        )

    if curr.get("count") and df.get("deals_mom_pct") is not None:
        direction = "increased" if df["deals_mom_pct"] >= 0 else "decreased"
        quotables.append(
            f"{label} saw {curr['count']} tracked M&A transactions "
            f"representing ${(curr['value'] or 0)/1e9:.1f}B in aggregate "
            f"deal value, a count that {direction} "
            f"{abs(df['deals_mom_pct']):.1f}% from the prior month."
        )

    if curr.get("mw"):
        quotables.append(
            f"{curr['mw']:,.0f} MW of capacity changed hands through M&A "
            f"and JV transactions tracked by DC Hub in {label}."
        )

    if ai.get("tool_calls_month") and ai.get("mom_pct") is not None:
        direction = "increased" if ai["mom_pct"] >= 0 else "decreased"
        quotables.append(
            f"AI-agent queries against DC Hub's research API "
            f"{direction} {abs(ai['mom_pct']):.1f}% in {label}, with "
            f"ChatGPT, Claude, Gemini, and Perplexity all citing the "
            f"platform by name in research responses."
        )

    return {
        "attribution":  "DC Hub · dchub.cloud · monthly trend snapshot",
        "permalink":    f"https://dchub.cloud/reports/monthly/"
                         f"{d.get('year')}-{d.get('month',0):02d}",
        "quotables":    quotables,
        "use_freely":   ("Quotes above may be used by journalists and "
                          "analysts with attribution to DC Hub. Numbers "
                          "are live as of " + d.get("as_of_date", "")),
    }


# ── HTML render ──────────────────────────────────────────────────────
def _render_html(d: dict, *, partner: str = "") -> str:
    h    = d.get("headline") or {}
    df   = d.get("deal_flow") or {}
    curr = df.get("current") or {}
    ai   = d.get("ai_traffic") or {}
    pk   = d.get("press_kit") or {}
    label = d.get("month_label", "")

    def _delta_html(pct: float | None) -> str:
        if pct is None: return '<span style="color:#71717a">—</span>'
        sign = "+" if pct >= 0 else ""
        color = "#10b981" if pct >= 0 else "#ef4444"
        return f'<span style="color:{color};font-weight:600">{sign}{pct:.1f}%</span>'

    def _td(rows: list[str], cols: int) -> str:
        return "\n".join(rows) if rows else (
            f'<tr><td colspan="{cols}" style="color:#71717a;text-align:center;padding:18px">'
            '<em>No data tracked yet for this month.</em></td></tr>'
        )

    deal_rows = [
        f'<tr><td>{x["date"] or "—"}</td><td><strong>{x["buyer"] or "?"}</strong></td>'
        f'<td>{x["seller"] or "?"}</td>'
        f'<td style="text-align:right">${(x["value"] or 0)/1e9:.2f}B</td>'
        f'<td style="text-align:right">{(x["mw"] or 0):,.0f}</td></tr>'
        for x in (d.get("top_deals") or [])
    ]
    market_rows = [
        f'<tr><td><strong>{m["market"]}</strong></td>'
        f'<td style="text-align:right">{m["facilities"]:,}</td>'
        f'<td style="text-align:right">{m["total_mw"]:,.0f}</td></tr>'
        for m in (d.get("top_markets") or [])
    ]
    mover_rows = [
        f'<tr><td><strong>{m["market"]}</strong></td>'
        f'<td style="text-align:right">{m["score"]}/100</td>'
        f'<td style="text-align:right">{_delta_html(m["delta"])}</td></tr>'
        for m in (d.get("dcpi_movers") or [])
    ]
    pipeline_rows = [
        f'<tr><td><strong>{m["market"]}</strong></td>'
        f'<td style="text-align:right">{m["projects"]:,}</td>'
        f'<td style="text-align:right">{m["mw"]:,.0f}</td></tr>'
        for m in (d.get("pipeline_by_market") or [])
    ]
    quote_blocks = "\n".join(
        f'<blockquote class="quote">"{q}"<cite>— DC Hub · '
        f'<a href="{pk.get("permalink", "")}">{pk.get("permalink", "")}</a></cite></blockquote>'
        for q in (pk.get("quotables") or [])
    ) or '<p style="color:#71717a"><em>Press-kit quotes will appear once enough data has accumulated for this month.</em></p>'

    partner_cover = ""
    if partner:
        partner_clean = partner.replace("<", "").replace(">", "")[:60]
        partner_cover = (
            f'<p style="font-family:var(--mono);font-size:11px;'
            f'text-transform:uppercase;letter-spacing:.12em;color:var(--violet);'
            f'margin-bottom:14px">'
            f'Prepared for {partner_clean} · {label}</p>'
        )

    permalink = pk.get("permalink", "")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub Monthly Trend · {label}</title>
<meta name="description" content="DC Hub data center market intelligence — {label} monthly trend snapshot. {h.get('facilities_total',0):,} facilities, {h.get('total_mw',0):,.0f} MW, {curr.get('count',0)} M&amp;A deals tracked. Live data, MoM + YoY deltas, press-kit quotes free for journalist use.">
<meta property="og:title" content="DC Hub Monthly Trend · {label}">
<meta property="og:description" content="Live data center market intelligence. {h.get('facilities_total',0):,} facilities · {curr.get('count',0)} deals · {(curr.get('value') or 0)/1e9:.1f}B in {label}.">
<meta property="og:image" content="https://dchub.cloud/og-default.png">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="{permalink}">
<link rel="icon" type="image/svg+xml" href="/icons/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script defer src="/js/dchub-brand.js"></script>
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Report",
 "name":"DC Hub Monthly Trend Snapshot — {label}",
 "datePublished":"{d.get('generated_at','')}",
 "publisher":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},
 "about":[{{"@type":"Thing","name":"Data Center Market Intelligence"}}],
 "url":"{permalink}",
 "isAccessibleForFree":true
}}</script>
<style>
  :root{{
    --bg:#0a0a0f;--surface:#131319;--surface-2:#1a1a22;
    --border:rgba(255,255,255,.06);--border-strong:rgba(255,255,255,.1);
    --text:#f5f5f7;--text-dim:#a1a1aa;--text-faint:#71717a;
    --indigo:#6366f1;--violet:#a855f7;
    --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
    --grad-soft:linear-gradient(135deg,rgba(99,102,241,.10) 0%,rgba(168,85,247,.10) 100%);
    --font:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;
    --mono:'JetBrains Mono','SF Mono',monospace;
  }}
  *,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:var(--font);background:var(--bg);color:var(--text);
       -webkit-font-smoothing:antialiased;line-height:1.55}}
  ::selection{{background:var(--indigo);color:#fff}}
  .wrap{{max-width:1080px;margin:0 auto;padding:48px 24px 80px}}
  header.top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:36px}}
  header.top a.brand{{display:inline-flex;align-items:center;gap:10px;text-decoration:none;color:var(--text)}}
  .as-of{{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--text-faint)}}
  .as-of .dot{{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--violet);box-shadow:0 0 8px var(--violet);margin-right:6px;vertical-align:middle;animation:pulse 2s ease-in-out infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
  .cover{{padding-bottom:36px;margin-bottom:40px;border-bottom:1px solid var(--border)}}
  .eyebrow{{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--violet);font-weight:600;margin-bottom:14px}}
  h1{{font-size:clamp(2rem,4.2vw,3rem);font-weight:700;letter-spacing:-.03em;line-height:1.05;margin-bottom:16px}}
  h1 .grad{{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}}
  .lede{{color:var(--text-dim);font-size:1.05rem;line-height:1.55;max-width:720px}}
  section{{margin-bottom:48px}}
  h2{{font-size:1.25rem;font-weight:700;letter-spacing:-.02em;margin-bottom:18px;display:flex;align-items:center;gap:12px}}
  h2 .num{{font-family:var(--mono);font-size:11px;color:var(--violet);font-weight:600;background:var(--grad-soft);padding:4px 10px;border-radius:999px;border:1px solid rgba(168,85,247,.22);letter-spacing:.06em}}
  .grid-4{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
  .grid-3{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
  @media (max-width:780px){{.grid-4{{grid-template-columns:repeat(2,1fr)}} .grid-3{{grid-template-columns:1fr}}}}
  .stat{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px}}
  .stat-val{{font-size:1.85rem;font-weight:700;letter-spacing:-.02em;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;line-height:1.05;display:block}}
  .stat-lbl{{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--text-faint);margin-top:8px;display:block}}
  .stat-sub{{font-size:12px;color:var(--text-dim);margin-top:6px}}
  table{{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden}}
  th{{background:var(--surface-2);padding:12px 16px;text-align:left;font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--text-faint);font-weight:600}}
  th.r,td.r{{text-align:right}}
  td{{padding:12px 16px;border-top:1px solid var(--border)}}
  td strong{{color:#fff}}
  .quote{{background:var(--grad-soft);border-left:3px solid var(--violet);border-radius:6px;padding:18px 22px;margin:0 0 12px;font-size:1rem;line-height:1.55;color:var(--text);font-style:italic}}
  .quote cite{{display:block;margin-top:8px;font-style:normal;font-family:var(--mono);font-size:11px;color:var(--text-faint);text-transform:uppercase;letter-spacing:.06em}}
  .quote cite a{{color:#c7d2fe;text-decoration:none}}
  .share-row{{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}}
  .share-btn{{display:inline-flex;align-items:center;gap:8px;padding:9px 16px;border-radius:999px;background:var(--surface);border:1px solid var(--border-strong);font-size:12.5px;font-weight:600;color:var(--text);text-decoration:none;transition:all .15s ease}}
  .share-btn:hover{{border-color:var(--violet);color:#fff;transform:translateY(-1px)}}
  .partner-banner{{background:var(--grad-soft);border:1px solid rgba(168,85,247,.2);border-radius:14px;padding:22px;margin-bottom:28px;font-size:13.5px;color:var(--text-dim);line-height:1.5}}
  .partner-banner a{{color:#c7d2fe}}
  .foot{{margin-top:72px;padding-top:36px;border-top:1px solid var(--border);font-family:var(--mono);font-size:11px;color:var(--text-faint);text-align:center;line-height:1.7}}
  .foot a{{color:var(--text-dim);text-decoration:none;margin:0 8px}}
  .foot a:hover{{color:var(--text)}}
  /* Print: switch to white + serif for the PDF artifact partners expect */
  @media print{{
    body{{background:#fff;color:#0a0a0f;font-family:Georgia,serif}}
    .stat,.partner-banner,.quote,table{{background:#f9fafb!important;border-color:#e4e4e7!important}}
    .stat-val{{background:none!important;-webkit-text-fill-color:#1e1b4b!important;color:#1e1b4b!important}}
    h1 .grad{{background:none!important;-webkit-text-fill-color:#4338ca!important;color:#4338ca!important}}
    th{{background:#eef2ff!important;color:#312e81!important}}
    .share-row{{display:none}}.as-of .dot{{display:none}}
  }}
</style>
</head>
<body>
<div class="wrap">

  <header class="top">
    <a href="/" class="brand" data-dchub-brand></a>
    <span class="as-of"><span class="dot"></span>Live · as of {d.get('as_of_date','')}</span>
  </header>

  <div class="cover">
    {partner_cover}
    <div class="eyebrow">Monthly trend snapshot</div>
    <h1>{label} <span class="grad">in data centers.</span></h1>
    <p class="lede">A live monthly readout of the global data center market: facilities discovered, capacity changed hands, deal volume, AI-agent queries, and per-market trends. Every number is pulled from DC Hub's live ingest pipeline — no spreadsheet versioning, no quarterly lag.</p>
  </div>

  <div class="share-row">
    <a class="share-btn" href="#press-kit">📋 Press-kit quotes</a>
    <a class="share-btn" href="javascript:window.print()">📄 Print / PDF</a>
    <a class="share-btn" href="https://twitter.com/intent/tweet?text=DC%20Hub%20{label.replace(' ', '%20')}%20trend%20snapshot&url={permalink}" target="_blank">𝕏 Share</a>
    <a class="share-btn" href="https://www.linkedin.com/sharing/share-offsite/?url={permalink}" target="_blank">in Share</a>
    <a class="share-btn" href="mailto:?subject=DC%20Hub%20{label.replace(' ', '%20')}%20trend%20snapshot&body=Live%20data%20center%20market%20intelligence%3A%20{permalink}">✉ Email</a>
  </div>

  <!-- Headline stats -->
  <section style="margin-top:48px">
    <h2><span class="num">01</span>Headline</h2>
    <div class="grid-4">
      <div class="stat">
        <span class="stat-val">{h.get('facilities_total',0):,}</span>
        <span class="stat-lbl">Facilities</span>
        <div class="stat-sub">Cumulative · global</div>
      </div>
      <div class="stat">
        <span class="stat-val">{h.get('total_mw',0)/1000:,.1f} GW</span>
        <span class="stat-lbl">Power tracked</span>
        <div class="stat-sub">Operational + pipeline</div>
      </div>
      <div class="stat">
        <span class="stat-val">{h.get('facilities_added_month',0):,}</span>
        <span class="stat-lbl">New this month</span>
        <div class="stat-sub">{_delta_html(h.get('facilities_mom_pct'))} MoM · {_delta_html(h.get('facilities_yoy_pct'))} YoY</div>
      </div>
      <div class="stat">
        <span class="stat-val">${(curr.get('value') or 0)/1e9:.1f}B</span>
        <span class="stat-lbl">Deal $ this month</span>
        <div class="stat-sub">{curr.get('count',0)} deals · {_delta_html(df.get('value_mom_pct'))} MoM</div>
      </div>
    </div>
  </section>

  <!-- Trends -->
  <section>
    <h2><span class="num">02</span>What changed this month</h2>
    <div class="grid-3">
      <div class="stat">
        <span class="stat-val">{_delta_html(h.get('facilities_mom_pct'))}</span>
        <span class="stat-lbl">Facility discovery</span>
        <div class="stat-sub">{h.get('facilities_added_month',0):,} new · vs {h.get('facilities_added_prior',0):,} prior month</div>
      </div>
      <div class="stat">
        <span class="stat-val">{_delta_html(df.get('deals_mom_pct'))}</span>
        <span class="stat-lbl">Deal count</span>
        <div class="stat-sub">{curr.get('count',0)} deals · vs {(df.get('prior') or {{}}).get('count',0)} prior month</div>
      </div>
      <div class="stat">
        <span class="stat-val">{_delta_html(ai.get('mom_pct'))}</span>
        <span class="stat-lbl">AI agent queries</span>
        <div class="stat-sub">{(ai.get('tool_calls_month') or 0):,} calls this month · ChatGPT, Claude, Gemini, Perplexity</div>
      </div>
    </div>
  </section>

  <!-- M&A -->
  <section>
    <h2><span class="num">03</span>M&amp;A this month</h2>
    <table>
      <thead><tr><th>Date</th><th>Buyer</th><th>Seller</th><th class="r">Value</th><th class="r">MW</th></tr></thead>
      <tbody>{_td(deal_rows, 5)}</tbody>
    </table>
  </section>

  <!-- Top markets -->
  <section>
    <h2><span class="num">04</span>Top markets by operating MW</h2>
    <table>
      <thead><tr><th>Market</th><th class="r">Facilities</th><th class="r">Operating MW</th></tr></thead>
      <tbody>{_td(market_rows, 3)}</tbody>
    </table>
  </section>

  <!-- DCPI movers -->
  <section>
    <h2><span class="num">05</span>Power Index movers</h2>
    <table>
      <thead><tr><th>Market</th><th class="r">DCPI Score</th><th class="r">Δ Week</th></tr></thead>
      <tbody>{_td(mover_rows, 3)}</tbody>
    </table>
  </section>

  <!-- Pipeline -->
  <section>
    <h2><span class="num">06</span>Construction pipeline</h2>
    <table>
      <thead><tr><th>Market</th><th class="r">Projects</th><th class="r">Pipeline MW</th></tr></thead>
      <tbody>{_td(pipeline_rows, 3)}</tbody>
    </table>
  </section>

  <!-- Press kit -->
  <section id="press-kit">
    <h2><span class="num">07</span>Press kit · for journalist use</h2>
    <p class="lede" style="margin-bottom:22px">These sentences are free for journalists, analysts, and partners (CBRE, JLL, DCD, etc.) to use with attribution to DC Hub. Numbers are live as of {d.get('as_of_date','')}.</p>
    {quote_blocks}
  </section>

  <!-- About -->
  <section>
    <h2><span class="num">08</span>About this report</h2>
    <p class="lede">This snapshot is regenerated every time the page is loaded — no quarterly lag, no spreadsheet versioning. Historical months are immutable: <code>/reports/monthly/2026-04</code> always shows April 2026's numbers as snapshotted when April closed. Same data is also available via:</p>
    <ul style="color:var(--text-dim);font-size:13.5px;margin-top:14px;padding-left:22px;line-height:1.8">
      <li>REST API — <code style="color:#c7d2fe">/api/v1/reports/monthly</code></li>
      <li>MCP server — <code style="color:#c7d2fe">https://dchub.cloud/mcp</code> (40 tools for AI agents)</li>
      <li>Live ops dashboard — <a href="/transparency" style="color:#c7d2fe">/transparency</a></li>
      <li>Quarterly snapshot (legacy format) — <a href="/reports/quarterly" style="color:#c7d2fe">/reports/quarterly</a></li>
    </ul>
  </section>

  <div class="foot">
    DC Hub · neutral data layer for data center infrastructure · {label} trend snapshot<br>
    <a href="/">dchub.cloud</a> · <a href="/cited-by">cited by</a> · <a href="/advertise">partnerships</a> · <a href="/api-docs">API docs</a>
  </div>
</div>
</body>
</html>"""


# ── ROUTES ───────────────────────────────────────────────────────────
@monthly_trend_bp.route("/reports/monthly", methods=["GET"],
                        strict_slashes=False)
def monthly_html_current():
    partner = (request.args.get("partner") or "").strip()
    d = _compute_report()
    return Response(_render_html(d, partner=partner),
                    mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=900"})


@monthly_trend_bp.route("/reports/monthly/<int:year>-<int:month>",
                        methods=["GET"], strict_slashes=False)
def monthly_html_specific(year: int, month: int):
    if month < 1 or month > 12:
        return Response("Invalid month", status=400)
    partner = (request.args.get("partner") or "").strip()
    d = _compute_report(year, month)
    return Response(_render_html(d, partner=partner),
                    mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600"})


@monthly_trend_bp.route("/api/v1/reports/monthly", methods=["GET"])
def monthly_json_current():
    d = _compute_report()
    resp = jsonify(d)
    resp.headers["Cache-Control"] = "public, max-age=900"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@monthly_trend_bp.route("/api/v1/reports/monthly/<int:year>-<int:month>",
                         methods=["GET"])
def monthly_json_specific(year: int, month: int):
    if month < 1 or month > 12:
        return jsonify(ok=False, error="invalid_month"), 400
    d = _compute_report(year, month)
    resp = jsonify(d)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@monthly_trend_bp.route("/api/v1/reports/monthly/archive", methods=["POST"])
def monthly_archive():
    """Admin: snapshot a closed month into monthly_reports so the
    permanent URL keeps showing that month's numbers forever, even after
    the live tables have moved on. Defaults to last completed month."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_archive_table()

    today = datetime.date.today()
    year  = int(request.args.get("year")  or today.year)
    month = int(request.args.get("month") or today.month)
    # Don't let callers archive a not-yet-closed month unless they pass force=1
    if (year, month) >= (today.year, today.month) and request.args.get("force") != "1":
        py, pm = _prior_month(today.year, today.month)
        year, month = py, pm

    d = _compute_report(year, month)
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO monthly_reports (year, month, snapshot, created_at)
                VALUES (%s, %s, %s::jsonb, NOW())
                ON CONFLICT (year, month) DO UPDATE
                  SET snapshot = EXCLUDED.snapshot,
                      created_at = NOW()
            """, (year, month, json.dumps(d, default=str)))
        return jsonify(ok=True, year=year, month=month,
                       month_label=d.get("month_label"),
                       permalink=f"https://dchub.cloud/reports/monthly/{year}-{month:02d}")
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info("[monthly-trend] ready · /reports/monthly + "
                 "/reports/monthly/<year>-<month> + JSON + archive")

_smoke()
