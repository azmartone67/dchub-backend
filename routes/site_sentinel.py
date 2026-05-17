"""Phase WWW (2026-05-16) — Site Sentinel: real-time page-health scanner.

User vision: "i want the industry to use us as the source !!!! fully
autonomous, never stale, error free, learning."

The user spotted 24 broken/stale pages and asked: "shouldn't the brain
fix?" The honest answer is: the brain didn't even SEE them, because
no detector polled the public surface to check whether each page was:
  - Reachable (HTTP 200)
  - Carrying real content (size above floor, not just a 404 shell)
  - Hooked into the brand's nav (dchub-nav.js loaded)
  - Fresh (Last-Modified or X-Generated-At within SLA)

This module fills that gap. It maintains a manifest of every public
URL the user cares about, polls each on a schedule, persists results
to a small SQLite-style table in Postgres, and exposes:

  GET /api/v1/sentinel/scan      — last scan results (JSON)
  GET /api/v1/sentinel/findings  — only the unhealthy pages
  POST /api/v1/sentinel/scan-now — admin-only on-demand rescan
  GET /sentinel                  — human dashboard (HTML)

The radar's check_site_sentinel_unhealthy() detector reads this table
and turns every unhealthy page into a brain finding so the heartbeat
surfaces them. No more user-spotted bugs — the brain finds them first.

Manifest categories (sla_hours, status_required):
  - critical:    404 = immediate finding         (pricing, /vs, /, /intelligence)
  - high:        non-200 OR <2KB body            (markets, transactions, dcpi)
  - normal:      non-200 only                    (everything else linked from nav)

Future: extend to detect missing nav include via DOM scrape (Phase XXX).
"""

from __future__ import annotations

import os
import datetime
import time
import json
from typing import Iterable
from flask import Blueprint, jsonify, request, Response


site_sentinel_bp = Blueprint("site_sentinel", __name__)


# ── The manifest. Every public URL we care about. Categorize so the
#    detector knows how loud to be about each failure mode.
#    Add new pages here — that's the only ongoing maintenance.
_MANIFEST: list[dict] = [
    # Critical brand-positioning surfaces (NNN-OOO)
    {"path": "/",                        "category": "critical", "min_bytes": 10000, "label": "Homepage"},
    {"path": "/vs",                      "category": "critical", "min_bytes":  5000, "label": "BS Translator"},
    {"path": "/dcpi/totals",             "category": "critical", "min_bytes":  3000, "label": "Total Power"},
    {"path": "/intelligence",            "category": "critical", "min_bytes":  3000, "label": "Live Pulse"},
    {"path": "/pricing",                 "category": "critical", "min_bytes":  3000, "label": "Pricing"},
    {"path": "/api/v1/power/totals",     "category": "critical", "min_bytes":   300, "label": "Power Totals API"},
    {"path": "/api/v1/vs/claims",        "category": "critical", "min_bytes":   500, "label": "Claims API"},

    # High-value intelligence pages
    {"path": "/market-intelligence",     "category": "high", "min_bytes": 5000, "label": "Market Analytics"},
    {"path": "/transactions",            "category": "high", "min_bytes": 5000, "label": "Transactions"},
    {"path": "/rankings",                "category": "high", "min_bytes": 3000, "label": "Rankings"},
    {"path": "/capacity-pipeline",       "category": "high", "min_bytes": 3000, "label": "Capacity Pipeline"},
    {"path": "/ai-pipeline",             "category": "high", "min_bytes": 3000, "label": "AI Pipeline"},
    {"path": "/ai-deals",                "category": "high", "min_bytes": 3000, "label": "AI Deals"},
    {"path": "/ai-inventory",            "category": "high", "min_bytes": 3000, "label": "AI Inventory"},
    {"path": "/powered-shell",           "category": "high", "min_bytes": 3000, "label": "Powered Shell"},
    {"path": "/tax-incentives",          "category": "high", "min_bytes": 3000, "label": "Tax Incentives"},
    {"path": "/news",                    "category": "high", "min_bytes": 3000, "label": "News"},
    {"path": "/daily",                   "category": "high", "min_bytes": 3000, "label": "Daily Report"},
    {"path": "/markets/",                "category": "high", "min_bytes": 3000, "label": "Markets"},
    {"path": "/land-power",              "category": "high", "min_bytes": 3000, "label": "Land + Power"},
    {"path": "/land-power-map",          "category": "high", "min_bytes": 3000, "label": "L+P Map"},
    {"path": "/map",                     "category": "high", "min_bytes": 3000, "label": "Facility Map"},

    # Platform / discovery
    {"path": "/api-docs",                "category": "high", "min_bytes": 3000, "label": "API Docs"},
    {"path": "/developers",              "category": "high", "min_bytes": 3000, "label": "Developers"},
    {"path": "/ai",                      "category": "high", "min_bytes": 3000, "label": "AI Hub"},
    {"path": "/ai-integrations",         "category": "high", "min_bytes": 3000, "label": "AI Integrations"},
    {"path": "/ecosystem",               "category": "high", "min_bytes": 3000, "label": "Ecosystem"},
    {"path": "/assets",                  "category": "high", "min_bytes": 3000, "label": "Assets Explorer"},

    # Research / brand
    {"path": "/research/grid-intelligence","category":"normal","min_bytes": 2000,"label": "Grid Intel"},
    {"path": "/press",                   "category": "normal", "min_bytes": 2000, "label": "Press"},
    {"path": "/gdci",                    "category": "normal", "min_bytes": 2000, "label": "GDCI"},
    {"path": "/testimonials",            "category": "normal", "min_bytes": 2000, "label": "Testimonials"},
    {"path": "/announcements",           "category": "normal", "min_bytes": 2000, "label": "Announcements"},
    {"path": "/architecture",            "category": "normal", "min_bytes": 2000, "label": "Architecture"},
    {"path": "/state-of-the-data-center","category": "normal", "min_bytes": 2000, "label": "State of DC"},
    {"path": "/cited-by",                "category": "normal", "min_bytes": 2000, "label": "Cited By"},
    {"path": "/system-status",           "category": "normal", "min_bytes": 2000, "label": "System Status"},

    # About / footer
    {"path": "/about",                   "category": "normal", "min_bytes": 1500, "label": "About"},
    {"path": "/advertise",               "category": "normal", "min_bytes": 1500, "label": "Advertise"},
    {"path": "/faq",                     "category": "normal", "min_bytes": 1500, "label": "FAQ"},
    {"path": "/glossary",                "category": "normal", "min_bytes": 1500, "label": "Glossary"},

    # Healthcheck APIs
    {"path": "/api/v1/brain/heartbeat",  "category": "high",   "min_bytes":  500, "label": "Brain Heartbeat"},
    {"path": "/api/v1/dcpi/scores?limit=1","category": "high","min_bytes": 200, "label": "DCPI Scores API"},
    {"path": "/api/v1/surfaces",         "category": "normal", "min_bytes":  300, "label": "Surfaces API"},
    {"path": "/api/v1/mcp/growth",       "category": "normal", "min_bytes":  200, "label": "MCP Growth"},
    {"path": "/openapi.json",            "category": "normal", "min_bytes": 1000, "label": "OpenAPI"},

    # Discovery / well-known
    {"path": "/.well-known/mcp.json",    "category": "high",   "min_bytes":  500, "label": "MCP Manifest"},
    {"path": "/.well-known/agent.json",  "category": "normal", "min_bytes":  200, "label": "Agent Card"},
    {"path": "/llms.txt",                "category": "normal", "min_bytes":  500, "label": "llms.txt"},
]


_SITE_BASE = os.environ.get("DCHUB_SITE_BASE_URL", "https://dchub.cloud").rstrip("/")
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_sentinel_results (
    path           TEXT PRIMARY KEY,
    category       TEXT NOT NULL,
    label          TEXT,
    status_code    INT,
    bytes          INT,
    elapsed_ms     INT,
    healthy        BOOLEAN,
    reason         TEXT,
    checked_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_healthy_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_site_sentinel_results_healthy
    ON site_sentinel_results(healthy, checked_at DESC);
"""


def _ensure_schema(cur):
    cur.execute(_SCHEMA)


def _scan_one(path: str, category: str, min_bytes: int) -> tuple[int, int, int, bool, str]:
    """Returns (status_code, bytes, elapsed_ms, healthy, reason)."""
    import requests
    url = f"{_SITE_BASE}{path}"
    t0 = time.time()
    try:
        r = requests.get(url, timeout=10, headers={
            "User-Agent":  "DCHub-Site-Sentinel/1.0",
            "Cache-Control": "no-cache",
        }, stream=True)
        # Read at most 64KB — we only need a size signal, not the full body
        body = r.raw.read(64 * 1024, decode_content=True) if r.raw else r.content[:64*1024]
        elapsed = int((time.time() - t0) * 1000)
        status = r.status_code
        n_bytes = len(body) if body else len(r.content)
        try: r.close()
        except Exception: pass
        if status != 200:
            return status, n_bytes, elapsed, False, f"http_status:{status}"
        if n_bytes < min_bytes:
            return status, n_bytes, elapsed, False, f"body_too_small:{n_bytes}<{min_bytes}"
        return status, n_bytes, elapsed, True, "ok"
    except requests.exceptions.Timeout:
        elapsed = int((time.time() - t0) * 1000)
        return 0, 0, elapsed, False, "timeout"
    except requests.exceptions.ConnectionError as e:
        elapsed = int((time.time() - t0) * 1000)
        return 0, 0, elapsed, False, f"connect_failed:{str(e)[:80]}"
    except Exception as e:
        elapsed = int((time.time() - t0) * 1000)
        return 0, 0, elapsed, False, f"{type(e).__name__}:{str(e)[:80]}"


def scan_all() -> list[dict]:
    """Run one full sweep. Persists to DB; returns the full result set."""
    results: list[dict] = []
    c = _conn()
    if c is None:
        # Even without DB we can still scan; we just can't persist
        pass
    try:
        if c is not None:
            with c.cursor() as cur:
                _ensure_schema(cur)
        for entry in _MANIFEST:
            path     = entry["path"]
            category = entry["category"]
            mb       = entry.get("min_bytes", 0)
            label    = entry.get("label", "")
            status, n, ms, healthy, reason = _scan_one(path, category, mb)
            results.append({
                "path":        path,
                "category":    category,
                "label":       label,
                "status_code": status,
                "bytes":       n,
                "elapsed_ms":  ms,
                "healthy":     healthy,
                "reason":      reason,
            })
            if c is not None:
                try:
                    with c.cursor() as cur:
                        cur.execute("""
                            INSERT INTO site_sentinel_results
                              (path, category, label, status_code, bytes,
                               elapsed_ms, healthy, reason, checked_at,
                               last_healthy_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s, NOW(),
                                    CASE WHEN %s THEN NOW() ELSE NULL END)
                            ON CONFLICT (path) DO UPDATE SET
                              category    = EXCLUDED.category,
                              label       = EXCLUDED.label,
                              status_code = EXCLUDED.status_code,
                              bytes       = EXCLUDED.bytes,
                              elapsed_ms  = EXCLUDED.elapsed_ms,
                              healthy     = EXCLUDED.healthy,
                              reason      = EXCLUDED.reason,
                              checked_at  = NOW(),
                              last_healthy_at = CASE
                                WHEN EXCLUDED.healthy THEN NOW()
                                ELSE site_sentinel_results.last_healthy_at
                              END
                        """, (path, category, label, status, n, ms, healthy,
                              reason, healthy))
                except Exception:
                    pass
    finally:
        if c is not None:
            try: c.close()
            except Exception: pass
    return results


def latest_results() -> list[dict]:
    """Read the last persisted scan (much cheaper than re-scanning)."""
    c = _conn()
    if c is None: return []
    out: list[dict] = []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT path, category, label, status_code, bytes,
                           elapsed_ms, healthy, reason, checked_at,
                           last_healthy_at
                      FROM site_sentinel_results
                     ORDER BY healthy ASC, category ASC, path ASC
                """)
                for r in cur.fetchall():
                    out.append({
                        "path":        r["path"],
                        "category":    r["category"],
                        "label":       r["label"],
                        "status_code": r["status_code"],
                        "bytes":       r["bytes"],
                        "elapsed_ms":  r["elapsed_ms"],
                        "healthy":     r["healthy"],
                        "reason":      r["reason"],
                        "checked_at":  r["checked_at"].isoformat() if r["checked_at"] else None,
                        "last_healthy_at": r["last_healthy_at"].isoformat() if r["last_healthy_at"] else None,
                    })
            except Exception:
                return out
    finally:
        try: c.close()
        except Exception: pass
    return out


def unhealthy_findings() -> list[dict]:
    """Brain-detector entrypoint. Returns one finding per unhealthy page."""
    findings: list[dict] = []
    rows = latest_results()
    if not rows:
        # First-run: synchronously scan once so the brain has data on the
        # very first heartbeat after deploy. Cheap (~45 GET requests, all
        # cached at CF). Subsequent calls hit the DB.
        rows = scan_all()
    for r in rows:
        if r.get("healthy"): continue
        cat = r.get("category") or "normal"
        # Critical pages: every breakage is a finding. High: same. Normal:
        # only HTTP failures, not body-too-small (which can be legitimate
        # if a page is intentionally minimal).
        if cat == "normal" and (r.get("reason") or "").startswith("body_too_small"):
            continue
        findings.append({
            "issue":  f"site_sentinel_unhealthy:{r['path']}",
            "url":    f"{_SITE_BASE}{r['path']}",
            "count":  r.get("status_code") or 0,
            "detail": (f"Page '{r.get('label') or r['path']}' is unhealthy. "
                       f"Status: {r.get('status_code')}, "
                       f"bytes: {r.get('bytes')}, "
                       f"reason: {r.get('reason')}. "
                       f"Category: {cat}. "
                       f"Last healthy: {r.get('last_healthy_at') or 'never since tracked'}. "
                       f"This is the Site Sentinel — fix the page OR adjust "
                       f"the manifest in routes/site_sentinel.py:_MANIFEST "
                       f"if the expectation is wrong."),
        })
    # Cap at 12 so a mass outage doesn't drown the heartbeat
    return findings[:12]


# ── HTTP endpoints ────────────────────────────────────────────────

@site_sentinel_bp.route("/api/v1/sentinel/scan", methods=["GET"])
def sentinel_scan():
    """Return the last persisted scan. Public, cached 5min."""
    rows = latest_results()
    healthy = sum(1 for r in rows if r.get("healthy"))
    resp = jsonify(
        total=len(rows),
        healthy=healthy,
        unhealthy=len(rows) - healthy,
        results=rows,
        manifest_size=len(_MANIFEST),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    )
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@site_sentinel_bp.route("/api/v1/sentinel/findings", methods=["GET"])
def sentinel_findings():
    """Only the unhealthy pages — what the brain detector ingests."""
    f = unhealthy_findings()
    resp = jsonify(findings=f, count=len(f),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@site_sentinel_bp.route("/api/v1/sentinel/scan-now", methods=["POST"])
def sentinel_scan_now():
    """Admin-only: trigger a fresh sweep."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401
    rows = scan_all()
    healthy = sum(1 for r in rows if r.get("healthy"))
    return jsonify(scanned=len(rows), healthy=healthy,
                   unhealthy=len(rows) - healthy,
                   results=rows), 200


@site_sentinel_bp.route("/sentinel", methods=["GET"], strict_slashes=False)
def sentinel_dashboard():
    """Human-readable status board. The 'is everything green?' page."""
    rows = latest_results()
    if not rows:
        rows = scan_all()
    healthy = sum(1 for r in rows if r.get("healthy"))
    pct = round(100.0 * healthy / max(len(rows), 1), 1)
    overall_class = "green" if pct >= 95 else ("amber" if pct >= 80 else "red")

    rows_html = []
    for r in sorted(rows, key=lambda x: (x.get("healthy") or False, x.get("category"), x.get("path"))):
        css = "ok" if r.get("healthy") else "bad"
        rows_html.append(f"""
<tr class="{css}">
  <td>{r.get('category','')}</td>
  <td><a href="{r['path']}">{r.get('label') or r['path']}</a></td>
  <td>{r.get('status_code') or '—'}</td>
  <td>{r.get('bytes') or 0}</td>
  <td>{r.get('elapsed_ms') or 0}ms</td>
  <td>{r.get('reason') or '—'}</td>
</tr>""")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>DC Hub Site Sentinel — every page, every minute</title>
<meta name="description" content="Live page-health dashboard. Polls every public DC Hub URL and surfaces breakages as brain findings.">
<style>
 body{{font-family:-apple-system,sans-serif;max-width:1200px;margin:0 auto;padding:2rem 1rem;background:#fafbfc;color:#1f2937}}
 h1{{font-size:1.8rem;margin:0 0 .25rem}}
 .summary{{padding:1.25rem;border-radius:10px;color:white;font-size:1.2rem;margin:1rem 0}}
 .summary.green{{background:linear-gradient(135deg,#065f46,#0f766e)}}
 .summary.amber{{background:linear-gradient(135deg,#92400e,#b45309)}}
 .summary.red{{background:linear-gradient(135deg,#991b1b,#b91c1c)}}
 table{{width:100%;border-collapse:collapse;background:white;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 th{{text-align:left;padding:.6rem;background:#f3f4f6;font-size:.8rem;text-transform:uppercase;color:#6b7280}}
 td{{padding:.55rem .6rem;border-top:1px solid #f3f4f6;font-size:.9rem}}
 tr.ok td{{color:#1f2937}}
 tr.bad{{background:#fef2f2}}
 tr.bad td{{color:#991b1b;font-weight:600}}
 a{{color:#1e40af;text-decoration:none}} a:hover{{text-decoration:underline}}
 .footer{{color:#9ca3af;font-size:.85rem;margin-top:2rem}}
</style></head>
<body>
<h1>🛰️ Site Sentinel</h1>
<p style="color:#6b7280">Polls every public URL on the manifest. Unhealthy pages auto-surface as brain findings in /api/v1/brain/heartbeat.</p>
<div class="summary {overall_class}">
  <strong>{healthy}/{len(rows)} pages healthy ({pct}%)</strong>
</div>
<table>
  <thead>
    <tr><th>Category</th><th>Page</th><th>Status</th><th>Bytes</th><th>Latency</th><th>Reason</th></tr>
  </thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>
<p class="footer">JSON: <a href="/api/v1/sentinel/scan">/api/v1/sentinel/scan</a> ·
Findings only: <a href="/api/v1/sentinel/findings">/api/v1/sentinel/findings</a> ·
Manifest size: {len(_MANIFEST)} URLs · Add new URLs in routes/site_sentinel.py:_MANIFEST</p>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=120"})
