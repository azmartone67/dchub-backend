"""Phase JJJ (2026-05-16) — /transactions browser page.

DCHawk's main differentiator is their human-validated transaction comp
database. We have 14,509 deals in our `deals` table but no first-class
browser page — just API endpoints. This adds:

  GET /transactions                  HTML table + filters (server-rendered)
  GET /transactions/<id>             Per-deal page with schema.org markup
  GET /api/v1/transactions/list      JSON browser (already exists upstream,
                                      this is the page-friendly variant)

Server-rendered HTML so every transaction page gets indexed by Google
+ Perplexity + Gemini. Schema.org markup so AI agents extract the
deal facts cleanly. This is the SEO surface we've been missing.
"""

from __future__ import annotations

import os
import json
import datetime
from flask import Blueprint, jsonify, request, Response, abort
import psycopg2
import psycopg2.extras


transactions_browser_bp = Blueprint("transactions_browser", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _fetch_deals(limit: int = 100, offset: int = 0,
                  year: str | None = None, region: str | None = None,
                  buyer: str | None = None, min_mw: int | None = None) -> tuple[list[dict], int]:
    """Returns (deals, total_count).

    Phase JJJ-2 (2026-05-16): defensive query. Initial implementation
    assumed id+value+mw columns; live revealed only buyer/date/market/
    region/seller/type are returned by the deals API. The `deals` table
    schema varies across deploys — defensively SELECT * + extract what
    we find. Plus explicit error logging so failures surface."""
    c = _conn()
    if c is None: return [], 0
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where_clauses = []
            params: list = []
            if year:
                where_clauses.append("year = %s"); params.append(year)
            if region:
                where_clauses.append("LOWER(region) LIKE %s"); params.append(f"%{region.lower()}%")
            if buyer:
                where_clauses.append("LOWER(buyer) LIKE %s"); params.append(f"%{buyer.lower()}%")
            if min_mw:
                where_clauses.append("(mw IS NULL OR mw >= %s)"); params.append(min_mw)
            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            # Count — use the most-tolerant query
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM deals{where_sql}", params)
                r = cur.fetchone()
                total = int((r.get("n") if r else 0) or 0)
            except Exception as ce:
                print(f"[transactions_browser] count failed: {ce}")
                total = 0

            # SELECT * so any column shape works; we extract what we find
            try:
                cur.execute(f"""
                    SELECT * FROM deals{where_sql}
                     ORDER BY COALESCE(date, '1970-01-01'::date) DESC
                     LIMIT %s OFFSET %s
                """, params + [limit, offset])
                rows = cur.fetchall()
            except Exception as qe:
                print(f"[transactions_browser] select failed: {qe}")
                # Last-resort: drop ORDER BY (date column might not exist)
                try:
                    cur.execute(f"SELECT * FROM deals{where_sql} LIMIT %s OFFSET %s",
                                params + [limit, offset])
                    rows = cur.fetchall()
                except Exception as qe2:
                    print(f"[transactions_browser] fallback select failed: {qe2}")
                    rows = []
        return rows, total
    except Exception as e:
        print(f"[transactions_browser] _fetch_deals outer: {e}")
        return [], 0
    finally:
        try: c.close()
        except Exception: pass


def _fetch_deal(deal_id) -> dict | None:
    c = _conn()
    if c is None: return None
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, year, buyer, seller, value, mw, type, region, market
                  FROM deals WHERE id = %s LIMIT 1
            """, (deal_id,))
            return cur.fetchone()
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _fmt_value(v) -> str:
    if v is None: return "—"
    try:
        v_num = float(v)
        if v_num >= 1_000_000_000:  return f"${v_num/1_000_000_000:.1f}B"
        if v_num >= 1_000_000:      return f"${v_num/1_000_000:.0f}M"
        return f"${v_num:,.0f}"
    except (ValueError, TypeError):
        return str(v)[:30]


def _fmt_mw(mw) -> str:
    if mw is None: return "—"
    try:
        m = float(mw)
        return f"{m:,.0f} MW"
    except Exception:
        return "—"


def _fmt_date(d) -> str:
    if d is None: return "—"
    try: return d.strftime("%Y-%m-%d")
    except Exception: return str(d)[:10]


@transactions_browser_bp.route("/transactions", methods=["GET"], strict_slashes=False)
def transactions_index():
    """Server-rendered table with filters. Indexable by Google/AI agents."""
    try: page = max(1, int(request.args.get("page") or 1))
    except ValueError: page = 1
    limit  = 100
    offset = (page - 1) * limit

    year   = request.args.get("year")
    region = request.args.get("region")
    buyer  = request.args.get("buyer")
    try: min_mw = int(request.args.get("min_mw")) if request.args.get("min_mw") else None
    except ValueError: min_mw = None

    deals, total = _fetch_deals(limit, offset, year, region, buyer, min_mw)
    total_pages = (total + limit - 1) // limit if total else 1

    # Auto-log the visit (surface brain telemetry)
    try:
        from routes.surface_brain import auto_log
        auto_log("transactions", "view", target=f"page={page}")
    except Exception:
        pass

    # Phase XXX (2026-05-16) — inline soft paywall. User flagged 0 MCP
    # conversions across 4K+ calls; HTML side gave away even more for
    # free. After row N_FREE we redact $value + add a "sign up free"
    # callout to capture leads. Page 1 first 25 stays unredacted so SEO/
    # AI indexing still works AND the user gets a real teaser.
    N_FREE_ROWS    = 25
    # Treat any non-authenticated visitor as needing the gate.
    is_authed = False
    try:
        # Best-effort cookie check; the auth system uses dchub_token.
        if request.cookies.get("dchub_token"): is_authed = True
    except Exception:
        pass

    # Build the top banner outside the f-string — Python 3.11 disallows
    # backslash escapes inside f-string expressions, so we precompute
    # this HTML and interpolate the whole block as a single name.
    banner_html = ""
    if not is_authed:
        unlock_count = max(0, total - N_FREE_ROWS)
        banner_html = (
            '<div style="background:linear-gradient(135deg,#065f46,#0f766e);'
            'color:white;padding:1rem 1.25rem;border-radius:8px;'
            'margin:.5rem 0 1.5rem;display:flex;justify-content:space-between;'
            'align-items:center;gap:1rem;flex-wrap:wrap">'
            '<div><strong style="font-size:1.05rem">'
            '&#x1F513; You&#39;re seeing a free preview.</strong>'
            '<div style="font-size:.9rem;opacity:.85;margin-top:.15rem">'
            'Sign up free (no card) to unlock $value, deal type, CSV export, '
            f'and alerts on the next {unlock_count:,} deals.</div></div>'
            '<a href="/signup?next=/transactions&utm_source=transactions_banner" '
            'style="background:white;color:#065f46;padding:.5rem 1.1rem;'
            'border-radius:6px;font-weight:700;text-decoration:none">'
            'Sign up free &rarr;</a></div>'
        )

    rows_html = []
    for i, d in enumerate(deals):
        # Past N_FREE on page 1, OR any page past page 1, redact $value
        gated_row = (not is_authed) and (page > 1 or i >= N_FREE_ROWS)
        value_cell = (f'<td><span style="color:#9ca3af">🔒 <a href="/signup?next=/transactions&utm_source=transactions_browser" style="color:#1e40af;text-decoration:underline">Sign up free</a></span></td>'
                       if gated_row
                       else f'<td>{_fmt_value(d.get("value"))}</td>')
        type_cell = (f'<td><span style="color:#9ca3af">🔒</span></td>'
                      if gated_row
                      else f'<td>{(d.get("type") or "")[:25]}</td>')
        rows_html.append(
            f'<tr>'
            f'<td><a href="/transactions/{d["id"]}">#{d["id"]}</a></td>'
            f'<td>{_fmt_date(d.get("date")) }</td>'
            f'<td>{(d.get("buyer") or "")[:50]}</td>'
            f'<td>{(d.get("seller") or "")[:50]}</td>'
            f'{value_cell}'
            f'<td>{_fmt_mw(d.get("mw"))}</td>'
            f'{type_cell}'
            f'<td>{(d.get("region") or "")[:25]}</td>'
            f'<td>{(d.get("market") or "")[:25]}</td>'
            f'</tr>'
        )
        # Insert teaser callout immediately after the FREE rows on page 1.
        if (not is_authed) and page == 1 and i == N_FREE_ROWS - 1 and i < len(deals) - 1:
            rows_html.append(
                f'<tr style="background:linear-gradient(90deg,#fef3c7,#fde68a)">'
                f'<td colspan="9" style="text-align:center;padding:.8rem 1rem;font-weight:600;color:#78350f">'
                f'📊 Showing {N_FREE_ROWS} of {total:,} deals · '
                f'<a href="/signup?next=/transactions&utm_source=transactions_inline" '
                f'style="color:#92400e;text-decoration:underline">Sign up free</a> '
                f'to unlock $value, deal type, CSV export, alerts, and the next {total - N_FREE_ROWS:,} deals'
                f'</td></tr>'
            )

    # Filter form persistence
    yi  = year   or ""
    ri  = region or ""
    bi  = buyer  or ""
    mi  = min_mw if min_mw is not None else ""

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Data Center Transactions Browser — {total:,} deals tracked | DC Hub</title>
<meta name="description" content="Browse {total:,} data center M&amp;A transactions tracked by DC Hub. Filter by year, buyer, region, capacity. Free + indexable — schema.org markup, no signup.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/transactions">
<meta property="og:title" content="DC Hub Transactions Browser">
<meta property="og:description" content="{total:,} data center deals tracked. Free browsable database.">
<meta property="og:url" content="https://dchub.cloud/transactions">
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type":    "Dataset",
  "name":     "DC Hub Data Center M&A Transactions",
  "description": "{total} tracked data center transactions including buyer, seller, MW, dollar value, region, and type.",
  "url":      "https://dchub.cloud/transactions",
  "license":  "https://dchub.cloud/terms",
  "creator":  {{"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"}}
}}
</script>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        max-width:1300px;margin:1.5rem auto;padding:0 1rem;color:#1f2937;line-height:1.55}}
  h1{{margin:0 0 .25rem;font-size:1.75rem}}
  h1 + p{{color:#6b7280;margin:0 0 1.5rem}}
  .filters{{background:#f9fafb;padding:1rem 1.25rem;border-radius:8px;margin-bottom:1rem;
            display:flex;gap:.75rem;flex-wrap:wrap;align-items:flex-end}}
  .filters label{{display:flex;flex-direction:column;font-size:.85rem;color:#6b7280;gap:.2rem}}
  .filters input, .filters select{{padding:.4rem .6rem;border:1px solid #d1d5db;border-radius:4px;font-size:.95rem}}
  .filters button{{padding:.45rem 1rem;background:#1e40af;color:white;border:0;border-radius:4px;cursor:pointer;font-weight:600}}
  table{{width:100%;border-collapse:collapse;font-size:.92rem}}
  th,td{{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #f3f4f6;vertical-align:top}}
  th{{background:#f9fafb;font-weight:600;color:#374151;font-size:.82rem;text-transform:uppercase;letter-spacing:.04em}}
  tbody tr:hover{{background:#fafbfc}}
  td a{{color:#1e40af;text-decoration:none;font-weight:600}}
  .pagination{{display:flex;gap:.5rem;justify-content:center;margin:1.5rem 0;flex-wrap:wrap}}
  .pagination a, .pagination span{{padding:.4rem .8rem;border:1px solid #d1d5db;border-radius:4px;color:#374151;text-decoration:none}}
  .pagination .current{{background:#1e40af;color:white;border-color:#1e40af}}
  .summary{{color:#6b7280;font-size:.9rem;margin-top:1rem}}
</style>
</head>
<body>
<h1>Data Center Transactions Browser</h1>
<p><strong>{total:,} tracked deals</strong> across data center M&amp;A, infra rollups, and platform-level transactions. Free + indexable — every row links to a per-deal page with schema.org markup for AI agents.</p>

{banner_html}

<form class="filters" method="GET" action="/transactions">
  <label>Year<input type="text" name="year" value="{yi}" placeholder="2025" size="6"></label>
  <label>Buyer<input type="text" name="buyer" value="{bi}" placeholder="Equinix" size="14"></label>
  <label>Region<input type="text" name="region" value="{ri}" placeholder="North America" size="14"></label>
  <label>Min MW<input type="number" name="min_mw" value="{mi}" placeholder="50" size="6"></label>
  <button type="submit">Filter</button>
  <a href="/transactions" style="margin-left:.5rem;color:#6b7280">Reset</a>
</form>

<table>
  <thead>
    <tr><th>ID</th><th>Date</th><th>Buyer</th><th>Seller</th><th>Value</th><th>MW</th><th>Type</th><th>Region</th><th>Market</th></tr>
  </thead>
  <tbody>{"".join(rows_html) if rows_html else '<tr><td colspan="9" style="text-align:center;padding:2rem;color:#9ca3af">No deals match those filters.</td></tr>'}</tbody>
</table>

<div class="summary">Showing {len(deals):,} of {total:,} deals · page {page} of {total_pages}</div>

<div class="pagination">
  {('<a href="?page=' + str(page-1) + '">‹ Previous</a>') if page > 1 else ''}
  <span class="current">Page {page}</span>
  {('<a href="?page=' + str(page+1) + '">Next ›</a>') if page < total_pages else ''}
</div>

<p style="margin-top:3rem;color:#9ca3af;font-size:.85rem;text-align:center">
  Part of the <a href="/">DC Hub</a> intelligence platform.
  Programmatic access: <a href="/api/v1/transactions">/api/v1/transactions</a> ·
  MCP tool: <code>list_transactions</code>
</p>
</body>
</html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300"})


@transactions_browser_bp.route("/transactions/<deal_id>", methods=["GET"])
def transaction_detail(deal_id):
    """Per-deal page with schema.org markup. Indexable individually."""
    d = _fetch_deal(deal_id)
    if d is None: abort(404)

    try:
        from routes.surface_brain import auto_log
        auto_log("transactions", "view_detail", target=str(deal_id))
    except Exception:
        pass

    title = f"{d.get('buyer') or 'Buyer'} acquires {d.get('seller') or 'Seller'}"
    if d.get("mw"): title += f" ({_fmt_mw(d['mw'])})"

    ld_json = {
        "@context":   "https://schema.org",
        "@type":      "Action",
        "name":        title,
        "description": f"Data center transaction #{deal_id}: {title}. Value {_fmt_value(d.get('value'))}, capacity {_fmt_mw(d.get('mw'))}, region {d.get('region') or 'unspecified'}.",
        "agent":       {"@type": "Organization", "name": d.get("buyer") or "Buyer"},
        "participant": {"@type": "Organization", "name": d.get("seller") or "Seller"},
        "startTime":   _fmt_date(d.get("date")),
        "url":         f"https://dchub.cloud/transactions/{deal_id}",
    }

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title} — Deal #{deal_id} | DC Hub Transactions</title>
<meta name="description" content="Data center transaction #{deal_id}: {title}. Value {_fmt_value(d.get('value'))}, capacity {_fmt_mw(d.get('mw'))}, type {d.get('type') or 'data center'}, region {d.get('region') or 'unspecified'}.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/transactions/{deal_id}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="DC Hub Transaction #{deal_id}">
<script type="application/ld+json">{json.dumps(ld_json, indent=2)}</script>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        max-width:760px;margin:2rem auto;padding:0 1rem;color:#1f2937;line-height:1.55}}
  h1{{font-size:1.6rem;margin:0 0 .25rem}}
  .badge{{display:inline-block;background:#dbeafe;color:#1e40af;padding:2px 8px;border-radius:4px;font-size:.8rem;font-weight:600}}
  dl{{display:grid;grid-template-columns:140px 1fr;gap:.5rem 1rem;margin:1.5rem 0}}
  dt{{color:#6b7280;font-weight:600;font-size:.9rem}}
  dd{{margin:0;color:#1f2937;font-size:1rem}}
  .back{{color:#6b7280;text-decoration:none;font-size:.9rem}}
  .back:hover{{color:#1e40af}}
</style>
</head>
<body>
<p><a class="back" href="/transactions">‹ All transactions</a></p>
<h1>{title}</h1>
<p><span class="badge">Deal #{deal_id}</span> · {_fmt_date(d.get('date'))}</p>

<dl>
  <dt>Buyer</dt><dd>{(d.get('buyer') or '—')}</dd>
  <dt>Seller</dt><dd>{(d.get('seller') or '—')}</dd>
  <dt>Value</dt><dd>{_fmt_value(d.get('value'))}</dd>
  <dt>Capacity</dt><dd>{_fmt_mw(d.get('mw'))}</dd>
  <dt>Type</dt><dd>{(d.get('type') or '—')}</dd>
  <dt>Region</dt><dd>{(d.get('region') or '—')}</dd>
  <dt>Market</dt><dd>{(d.get('market') or '—')}</dd>
  <dt>Year</dt><dd>{d.get('year') or '—'}</dd>
</dl>

<p style="margin-top:2rem;color:#9ca3af;font-size:.85rem">
  Part of <a href="/transactions">DC Hub's transactions database</a> · {_fmt_date(datetime.datetime.utcnow())} ·
  API: <a href="/api/v1/transactions/{deal_id}">/api/v1/transactions/{deal_id}</a>
</p>
</body>
</html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600"})
