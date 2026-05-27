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
import time
import threading
import datetime
from flask import Blueprint, jsonify, request, Response, abort
import psycopg2
import psycopg2.extras


transactions_browser_bp = Blueprint("transactions_browser", __name__)

# r47.34 (2026-05-26): /transactions has been failing sentinel as a 15s
# timeout. Profile shows the cost is SELECT * FROM deals ORDER BY date DESC
# LIMIT 100 + an unbounded COUNT(*) — `deals.date` is TEXT with no index,
# so Postgres can't use an index-only scan; every render does a sort
# over 2K+ rows. ~90% of traffic hits page 1 with no filters (the SEO
# crawl path), so memoize that one shape for 60s and the heavy work
# happens once per minute instead of once per request.
_DEALS_MEMO: dict = {}
_DEALS_LOCK = threading.Lock()
_DEALS_TTL_SECONDS = 60


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

    r47.34: page-1 / no-filter requests (the SEO crawl path + default
    HTML view) hit a 60s memo before touching Postgres. Anything with a
    filter or pagination skips the memo — those are bespoke enough that
    caching them rarely helps and risks stale results.

    Phase JJJ-2 (2026-05-16): defensive query. Initial implementation
    assumed id+value+mw columns; live revealed only buyer/date/market/
    region/seller/type are returned by the deals API. The `deals` table
    schema varies across deploys — defensively SELECT * + extract what
    we find. Plus explicit error logging so failures surface."""
    is_cacheable = (offset == 0 and limit == 100
                    and not year and not region and not buyer and not min_mw)
    if is_cacheable:
        entry = _DEALS_MEMO.get('page1')
        if entry and (time.time() - entry['t']) < _DEALS_TTL_SECONDS:
            return entry['rows'], entry['total']

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

            # SELECT * so any column shape works; we extract what we find.
            # Phase FF+7-meta (2026-05-19): deals.date is TEXT (ISO-8601
            # strings), not DATE. The previous '1970-01-01'::date cast
            # produced "COALESCE types text and date cannot be matched"
            # every page render. ISO dates sort lexicographically =
            # chronologically, so plain text works.
            try:
                cur.execute(f"""
                    SELECT * FROM deals{where_sql}
                     ORDER BY COALESCE(date, '1970-01-01') DESC
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
        if is_cacheable:
            with _DEALS_LOCK:
                _DEALS_MEMO['page1'] = {'rows': rows, 'total': total, 't': time.time()}
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
    # r33-J round 10 (2026-05-21): user reported enterprise license
    # still seeing the free-preview banner. The old check ONLY looked
    # at the dchub_token cookie — but enterprise users authenticate
    # via X-API-Key header (and the key validates to a paid tier).
    # Now we ALSO check the API key tier via map_tier_gating helper
    # used by the rest of the site, so paid users see the unredacted
    # table with no upsell banner.
    is_authed = False
    try:
        if request.cookies.get("dchub_token"): is_authed = True
    except Exception:
        pass
    if not is_authed:
        try:
            from map_tier_gating import _detect_caller_tier
            tier = (_detect_caller_tier(request) or "anonymous").lower()
            if tier in ("identified", "developer", "pro",
                        "enterprise", "founding", "internal", "admin"):
                is_authed = True
        except Exception:
            pass

    # Build the top banner outside the f-string — Python 3.11 disallows
    # backslash escapes inside f-string expressions, so we precompute
    # this HTML and interpolate the whole block as a single name.
    # r33-J round 10: banner now uses brand indigo→violet gradient
    # instead of the off-brand emerald-teal that looked like a
    # leftover from an earlier site.
    banner_html = ""
    if not is_authed:
        unlock_count = max(0, total - N_FREE_ROWS)
        banner_html = (
            '<div style="background:linear-gradient(135deg,#6366f1,#a855f7);'
            'color:white;padding:1rem 1.25rem;border-radius:10px;'
            'margin:.5rem 0 1.5rem;display:flex;justify-content:space-between;'
            'align-items:center;gap:1rem;flex-wrap:wrap">'
            '<div><strong style="font-size:1.05rem">'
            '&#x1F513; You&#39;re seeing a free preview.</strong>'
            '<div style="font-size:.9rem;opacity:.9;margin-top:.15rem">'
            'Sign up free (no card) to unlock $value, deal type, CSV export, '
            f'and alerts on the next {unlock_count:,} deals.</div></div>'
            '<a href="/signup?next=/transactions&utm_source=transactions_banner" '
            'style="background:white;color:#6366f1;padding:.55rem 1.2rem;'
            'border-radius:6px;font-weight:700;text-decoration:none">'
            'Sign up free &rarr;</a></div>'
        )

    rows_html = []
    for i, d in enumerate(deals):
        # Past N_FREE on page 1, OR any page past page 1, redact $value
        gated_row = (not is_authed) and (page > 1 or i >= N_FREE_ROWS)
        value_cell = (f'<td><span style="color:#9ca3af">🔒 <a href="/signup?next=/transactions&utm_source=transactions_browser" style="color:#818cf8;text-decoration:underline">Sign up free</a></span></td>'
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
<!-- Phase r33-J (2026-05-21): unified brand styling. The previous
     inline CSS hardcoded a white page with #-apple-system font +
     #1e40af blue, which made /transactions look completely off-brand
     vs the rest of the site. Now uses brand.css tokens. -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
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
  body{{max-width:1300px;margin:1.5rem auto;padding:0 1rem;line-height:1.55}}
  h1{{margin:1.5rem 0 .25rem;font-size:1.75rem;letter-spacing:-.02em}}
  h1 + p{{color:var(--dch-text-mute);margin:0 0 1.5rem}}
  .filters{{background:var(--dch-surface);padding:1rem 1.25rem;border-radius:10px;margin-bottom:1rem;
            display:flex;gap:.75rem;flex-wrap:wrap;align-items:flex-end;
            border:1px solid var(--dch-border)}}
  .filters label{{display:flex;flex-direction:column;font-size:.78rem;color:var(--dch-text-mute);gap:.25rem;text-transform:uppercase;letter-spacing:.06em}}
  .filters input, .filters select{{padding:.5rem .7rem;border:1px solid var(--dch-border);border-radius:6px;font-size:.95rem;background:var(--dch-bg);color:var(--dch-text);font-family:inherit}}
  .filters input:focus, .filters select:focus{{outline:none;border-color:var(--dch-indigo);box-shadow:0 0 0 3px rgba(129,140,248,.15)}}
  .filters button{{padding:.55rem 1.2rem;background:var(--dch-grad-brand);color:white;border:0;border-radius:6px;cursor:pointer;font-weight:600;font-family:inherit}}
  .filters button:hover{{opacity:.9}}
  table{{width:100%;border-collapse:collapse;font-size:.92rem;background:transparent !important;border:1px solid var(--dch-border);border-radius:10px;overflow:hidden}}
  th,td{{text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--dch-border);vertical-align:top}}
  th{{background:var(--dch-surface) !important;font-weight:600;color:var(--dch-text) !important;font-size:.74rem;text-transform:uppercase;letter-spacing:.06em}}
  tbody tr:hover td{{background:var(--dch-surface-2) !important}}
  td a{{color:var(--dch-indigo) !important;text-decoration:none;font-weight:600}}
  td a:hover{{color:var(--dch-violet) !important}}
  .pagination{{display:flex;gap:.5rem;justify-content:center;margin:1.5rem 0;flex-wrap:wrap}}
  .pagination a, .pagination span{{padding:.45rem .9rem;border:1px solid var(--dch-border);border-radius:6px;color:var(--dch-text);text-decoration:none;font-size:.9rem}}
  .pagination a:hover{{border-color:var(--dch-indigo);color:var(--dch-indigo)}}
  .pagination .current{{background:var(--dch-grad-brand);color:white;border-color:transparent}}
  .summary{{color:var(--dch-text-mute);font-size:.9rem;margin-top:1rem}}
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
<!-- Phase QA-sweep (2026-05-16): include dchub-nav.js so users see
     the top nav instead of having to browser-back to escape. -->
<script src="/js/dchub-nav.js" defer></script>
</body>
</html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300"})


# Phase DDDD (2026-05-16) — DEVELOPER-gated CSV export. User
# question: "did we confirm the new gating will incent people to
# upgrade to developer for api data?" Answer: not until this endpoint
# existed. Now there's a concrete DEVELOPER-only value: full-dataset
# CSV download with $value + deal type + all rows.
@transactions_browser_bp.route("/api/v1/transactions/export.csv", methods=["GET"])
def transactions_export_csv():
    """DEVELOPER-only: full transactions CSV download. Anonymous gets
    a structured 402 with stripe checkout link + preview of 3 sample
    rows so they see exactly what they'd get."""
    from routes.tier_gate import require_tier as _rt, _resolve_caller_tier as _rc

    def _preview(req):
        # Cheap preview: 3 sample rows + total count + columns
        try:
            sample, total = _fetch_deals(limit=3, offset=0,
                                           year=None, region=None,
                                           buyer=None, min_mw=None)
            return {
                "total_rows":  total,
                "sample_rows": [
                    {"id": d.get("id"), "date": _fmt_date(d.get("date")),
                     "buyer": d.get("buyer"), "seller": d.get("seller"),
                     "value": _fmt_value(d.get("value")),
                     "mw":    d.get("mw"),
                     "type":  d.get("type"), "region": d.get("region")}
                    for d in (sample or [])[:3]
                ],
                "columns": ["id","date","buyer","seller","value","mw",
                            "type","region","market"],
                "format":  "CSV (UTF-8)",
                "value_proposition": ("Full dataset of all "
                    f"{total:,}+ tracked data-center M&A transactions, "
                    "including $value and deal-type fields that are "
                    "redacted on the free /transactions page."),
            }
        except Exception:
            return {"preview_unavailable": True}

    tier, _ = _rc()
    if (tier or "FREE").upper() not in ("DEVELOPER", "PRO", "ENTERPRISE"):
        from routes.tier_gate import _gate_response
        return _gate_response(tier, "DEVELOPER",
                              "transactions_csv_export", _preview(request))

    # Tier OK — stream the CSV. Cap at 10K rows per call (safety).
    try: limit = max(1, min(10000, int(request.args.get("limit") or 10000)))
    except (ValueError, TypeError): limit = 10000
    year   = request.args.get("year")
    region = request.args.get("region")
    buyer  = request.args.get("buyer")
    try: min_mw = int(request.args.get("min_mw")) if request.args.get("min_mw") else None
    except (ValueError, TypeError): min_mw = None

    deals, total = _fetch_deals(limit=limit, offset=0, year=year,
                                  region=region, buyer=buyer, min_mw=min_mw)
    import csv, io
    buf = io.StringIO()
    cols = ["id","date","buyer","seller","value","mw","type","region","market"]
    w = csv.writer(buf)
    w.writerow(cols)
    for d in (deals or []):
        w.writerow([d.get(c, "") for c in cols])
    csv_text = buf.getvalue()
    resp = Response(csv_text, mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=dchub-transactions.csv"
    resp.headers["Cache-Control"] = "private, max-age=300"
    resp.headers["X-Total-Rows"] = str(total)
    return resp, 200


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
<!-- Phase r33-J (2026-05-21): brand.css unification -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
<script type="application/ld+json">{json.dumps(ld_json, indent=2)}</script>
<style>
  body{{max-width:780px;margin:2rem auto;padding:0 1rem;line-height:1.55}}
  h1{{font-size:1.7rem;margin:1.5rem 0 .25rem;letter-spacing:-.02em}}
  .badge{{display:inline-block;background:rgba(129,140,248,.15);color:var(--dch-indigo);padding:3px 10px;border-radius:6px;font-size:.78rem;font-weight:600;letter-spacing:.04em}}
  dl{{display:grid;grid-template-columns:140px 1fr;gap:.6rem 1rem;margin:1.5rem 0;padding:1.2rem;background:var(--dch-surface);border:1px solid var(--dch-border);border-radius:10px}}
  dt{{color:var(--dch-text-mute);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}}
  dd{{margin:0;color:var(--dch-text);font-size:1rem}}
  .back{{color:var(--dch-text-mute);text-decoration:none;font-size:.88rem}}
  .back:hover{{color:var(--dch-indigo)}}
  a{{color:var(--dch-indigo)}}
  a:hover{{color:var(--dch-violet)}}
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
<script src="/js/dchub-nav.js" defer></script>
</body>
</html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600"})
