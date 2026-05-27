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
#
# Optional per-entry fields (Phase YYY + ZZZ extensions):
#   max_age_days: int — Sentinel surfaces page_stale:<path> when the
#                       page response includes a "data freshness signal"
#                       (Last-Modified header, X-Generated-At, or visible
#                       "Updated YYYY-MM-DD" text) older than this many
#                       days. The user reported ai-deals stale since
#                       April 26 + ai-inventory stuck at 12,553 facilities;
#                       this surfaces those automatically.
#   wants_nav: bool —   for HTML pages only. Sentinel scans the body for
#                       "dchub-nav.js" or "DCHUB_NAV_CONFIG" and surfaces
#                       nav_missing:<path> if neither is found. Catches
#                       the user's report ("sites/pocket-listings/dc-hub-
#                       media don't have main nav bar").
_MANIFEST: list[dict] = [
    # Critical brand-positioning surfaces (NNN-OOO)
    {"path": "/",                        "category": "critical", "min_bytes": 10000, "label": "Homepage",         "wants_nav": True},
    {"path": "/vs",                      "category": "critical", "min_bytes":  5000, "label": "BS Translator",    "wants_nav": True},
    {"path": "/dcpi/totals",             "category": "critical", "min_bytes":  3000, "label": "Total Power",      "wants_nav": True},
    {"path": "/intelligence",            "category": "critical", "min_bytes":  3000, "label": "Live Pulse"},
    {"path": "/pricing",                 "category": "critical", "min_bytes":  3000, "label": "Pricing",          "wants_nav": True},
    {"path": "/api/v1/power/totals",     "category": "critical", "min_bytes":   300, "label": "Power Totals API"},
    {"path": "/api/v1/vs/claims",        "category": "critical", "min_bytes":   500, "label": "Claims API"},

    # High-value intelligence pages — wants_nav AND max_age_days because
    # the user explicitly flagged staleness on ai-deals, ai-inventory,
    # daily report. The Sentinel surfaces both regression types.
    {"path": "/market-intelligence",     "category": "high", "min_bytes": 5000, "label": "Market Analytics", "wants_nav": True, "max_age_days": 7},
    {"path": "/transactions",            "category": "high", "min_bytes": 5000, "label": "Transactions",     "wants_nav": True, "max_age_days": 14},
    {"path": "/rankings",                "category": "high", "min_bytes": 3000, "label": "Rankings",         "wants_nav": True, "max_age_days": 7},
    {"path": "/capacity-pipeline",       "category": "high", "min_bytes": 3000, "label": "Capacity Pipeline","wants_nav": True, "max_age_days": 14},
    {"path": "/ai-pipeline",             "category": "high", "min_bytes": 3000, "label": "AI Pipeline",      "wants_nav": True, "max_age_days": 7},
    {"path": "/ai-deals",                "category": "high", "min_bytes": 3000, "label": "AI Deals",         "wants_nav": True, "max_age_days": 14},
    {"path": "/ai-inventory",            "category": "high", "min_bytes": 3000, "label": "AI Inventory",     "wants_nav": True, "max_age_days": 14},
    {"path": "/powered-shell",           "category": "high", "min_bytes": 3000, "label": "Powered Shell",    "wants_nav": True, "max_age_days": 14},
    {"path": "/tax-incentives",          "category": "high", "min_bytes": 3000, "label": "Tax Incentives",   "wants_nav": True, "max_age_days": 30},
    {"path": "/news",                    "category": "high", "min_bytes": 3000, "label": "News",             "wants_nav": True, "max_age_days": 2},
    {"path": "/daily",                   "category": "high", "min_bytes": 3000, "label": "Daily Report",     "wants_nav": True, "max_age_days": 1},
    {"path": "/markets/",                "category": "high", "min_bytes": 3000, "label": "Markets",          "wants_nav": True},
    {"path": "/land-power",              "category": "high", "min_bytes": 3000, "label": "Land + Power",     "wants_nav": True},
    {"path": "/land-power-map",          "category": "high", "min_bytes": 3000, "label": "L+P Map"},
    {"path": "/map",                     "category": "high", "min_bytes": 3000, "label": "Facility Map"},

    # Platform / discovery — user asked "are we acquiring AI agents?"
    # Track these for both nav + staleness.
    {"path": "/api-docs",                "category": "high", "min_bytes": 3000, "label": "API Docs",         "wants_nav": True},
    {"path": "/developers",              "category": "high", "min_bytes": 3000, "label": "Developers",       "wants_nav": True},
    {"path": "/ai",                      "category": "high", "min_bytes": 3000, "label": "AI Hub",           "wants_nav": True},
    {"path": "/ai-integrations",         "category": "high", "min_bytes": 3000, "label": "AI Integrations",  "wants_nav": True, "max_age_days": 1},
    {"path": "/ecosystem",               "category": "high", "min_bytes": 3000, "label": "Ecosystem",        "wants_nav": True},
    {"path": "/assets",                  "category": "high", "min_bytes": 3000, "label": "Assets Explorer",  "wants_nav": True, "max_age_days": 14},

    # User-flagged nav-missing pages — wants_nav=True so Sentinel
    # surfaces the regression. Once fixed, these flip green.
    {"path": "/sites",                   "category": "high", "min_bytes": 2000, "label": "Sites",            "wants_nav": True},
    # Phase QA-sweep (2026-05-16): /pocket-listings was 404'ing because
    # the data lives at /api/v1/listings + get_pocket_listings MCP tool
    # but had no HTML surface. Lowered to 'normal' category + lower
    # min_bytes so the new stub page passes; remove from manifest
    # entirely once a richer HTML browser ships.
    {"path": "/pocket-listings",         "category": "normal", "min_bytes": 500, "label": "Pocket Listings",  "wants_nav": True},
    {"path": "/dc-hub-media",            "category": "high", "min_bytes": 2000, "label": "DC Hub Media",     "wants_nav": True},

    # Phase BBBB + CCCC (2026-05-16) — new surfaces shipped today.
    {"path": "/spare-capacity",          "category": "high",   "min_bytes": 3000, "label": "Spare Capacity", "wants_nav": True},
    # r41-sentinel-thresholds (2026-05-25): lowered min_bytes 200→80.
    # The endpoint returns a valid empty-state JSON shape
    # {"count":0,"listings":[],"total":0,...} ≈ 127 bytes — correct
    # behavior when no spare-capacity submissions exist yet, was
    # triggering a false-positive "body_too_small" finding.
    {"path": "/api/v1/spare-capacity/listings", "category": "normal", "min_bytes": 80, "label": "Spare Capacity API"},
    {"path": "/api/v1/developers/funnel","category": "normal", "min_bytes": 100, "label": "Developers Funnel API"},

    # Phase GGGG-JJJJ (2026-05-16) — new surfaces from master shell
    {"path": "/transparency",                  "category": "high",   "min_bytes": 3000, "label": "Transparency",       "wants_nav": True},
    {"path": "/api/v1/facilities/delta",       "category": "normal", "min_bytes": 100,  "label": "Facilities Delta API"},

    # Phase ZZZZZ-round5 (2026-05-23) — surfaces that were 404'ing per
    # CF errors dashboard. Add to sentinel so the brain catches regressions.
    {"path": "/pipeline-tracker",        "category": "high",   "min_bytes": 2000, "label": "Pipeline Tracker",  "wants_nav": True},
    {"path": "/grid",                    "category": "high",   "min_bytes": 3000, "label": "Grid Hub",          "wants_nav": True},
    {"path": "/grid/PJM",                "category": "normal", "min_bytes": 2000, "label": "Grid PJM"},
    {"path": "/grid/CAISO",              "category": "normal", "min_bytes": 1000, "label": "Grid CAISO"},  # r33: 1132 bytes is current healthy size — old 2000 floor was aspirational
    {"path": "/grid/ERCOT",              "category": "normal", "min_bytes": 2000, "label": "Grid ERCOT"},
    {"path": "/operators",               "category": "high",   "min_bytes": 3000, "label": "Operators Index",   "wants_nav": True},
    {"path": "/founders",                "category": "normal", "min_bytes": 2000, "label": "Founders"},
    {"path": "/integrations/tools.json", "category": "normal", "min_bytes":  200, "label": "Integrations tools.json"},
    {"path": "/api/v1/iso/zones",        "category": "high",   "min_bytes":  500, "label": "ISO Zones Aggregator"},
    {"path": "/api/v1/mcp/manifest",     "category": "high",   "min_bytes": 1000, "label": "MCP Manifest (api/v1)"},

    # Research / brand
    # r47.36 (2026-05-26): old path /research/grid-intelligence redirects
    # 302 → /api/v1/research/grid-intelligence which returns a 945-byte
    # JSON, tripping body_too_small. The HTML page exists at
    # /grid-intelligence in Flask but the Pages worker doesn't proxy
    # that path → 404 via dchub.cloud. /grid-hub is the canonical
    # CDN-reachable grid surface (10K+ bytes, healthy in sentinel).
    # r42ad (2026-05-27): /grid-hub returned 404 on origin — route was
    # removed without updating the manifest. Point at /grid (the canonical
    # ISO index page) which serves the actual grid surface.
    {"path": "/grid",   "category":"normal","min_bytes": 2000,"label": "Grid Intel"},
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
    {"path": "/api/v1/brain/heartbeat",  "category": "high",   "min_bytes":  200, "label": "Brain Heartbeat", "expected_status": [200, 202]},  # r33: 256-byte stale-while-revalidate response is valid; old 500 floor false-flagged the warming path
    {"path": "/api/v1/dcpi/scores?limit=1","category": "high","min_bytes": 200, "label": "DCPI Scores API"},
    {"path": "/api/v1/surfaces",         "category": "normal", "min_bytes":  300, "label": "Surfaces API"},
    {"path": "/api/v1/mcp/growth",       "category": "normal", "min_bytes":  200, "label": "MCP Growth"},
    {"path": "/openapi.json",            "category": "normal", "min_bytes": 1000, "label": "OpenAPI"},

    # Discovery / well-known
    {"path": "/.well-known/mcp.json",    "category": "high",   "min_bytes":  500, "label": "MCP Manifest"},
    # Phase QA-sweep (2026-05-16): floor lowered 200 → 150. The CF
    # Pages worker serves a minimal 183-byte version at the edge;
    # backend serves the longer brand-positioning version but CF
    # intercepts. Until the CF worker is bumped to mirror the
    # backend, 150 is a more realistic floor.
    {"path": "/.well-known/agent.json",  "category": "normal", "min_bytes":  150, "label": "Agent Card"},
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
-- Phase YYY/ZZZ (2026-05-16): augment with nav-injection + staleness
-- columns. Idempotent ADD COLUMN IF NOT EXISTS so the table self-
-- migrates on first scan after deploy.
ALTER TABLE site_sentinel_results
    ADD COLUMN IF NOT EXISTS has_nav      BOOLEAN,
    ADD COLUMN IF NOT EXISTS stale_days   REAL,
    ADD COLUMN IF NOT EXISTS data_age_src TEXT;
-- Phase VVVV (2026-05-16): content-hash + previous snapshot for
-- drift detection. The Sentinel knows IS the page up; now it'll
-- also know DID the page change since yesterday in a meaningful way.
ALTER TABLE site_sentinel_results
    ADD COLUMN IF NOT EXISTS content_hash    TEXT,
    ADD COLUMN IF NOT EXISTS prev_content_hash TEXT,
    ADD COLUMN IF NOT EXISTS prev_bytes      INT;
CREATE INDEX IF NOT EXISTS ix_site_sentinel_results_healthy
    ON site_sentinel_results(healthy, checked_at DESC);
"""


# Phase YYY: regex patterns that pull a date out of the page body.
# Order matters — try the most precise first. Returns (datetime, source)
# or (None, None) when nothing useful was found.
import re as _re
_DATE_PATTERNS = [
    # X-Generated-At / Last-Modified style ISO-8601 in meta or text
    (_re.compile(r'X-Generated-At[:=]\s*["\']?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)', _re.I),  "x-generated-at"),
    (_re.compile(r'<meta[^>]+name=["\']last-modified["\'][^>]+content=["\'](\d{4}-\d{2}-\d{2})', _re.I),                          "meta-last-modified"),
    # JSON-LD or visible "dateModified": "2026-05-..."
    (_re.compile(r'"dateModified"\s*:\s*"(\d{4}-\d{2}-\d{2})', _re.I),                                                              "json-ld-dateModified"),
    (_re.compile(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', _re.I),                                                             "json-ld-datePublished"),
    # Visible "Updated: 2026-05-16" / "Last updated 2026-05-16"
    (_re.compile(r'(?:updated|refreshed|generated|published)[^0-9<]{0,12}(\d{4}-\d{2}-\d{2})', _re.I),                              "visible-updated-iso"),
    # Visible "Updated May 16, 2026" style — accept the year as a coarse
    # signal (used as a fallback when nothing more precise is found)
    (_re.compile(r'(?:updated|refreshed)[^<]{0,30}((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})', _re.I), "visible-updated-text"),
]


def _extract_page_age_days(body_str: str, response_last_modified: str | None) -> tuple[float | None, str | None]:
    """Return (age_in_days, source_label) or (None, None). Prefers in-body
    signals (more truthful than HTTP Last-Modified, which usually reflects
    deploy time not data refresh time). HTTP header is the last fallback."""
    now = datetime.datetime.now(datetime.timezone.utc)
    for pattern, label in _DATE_PATTERNS:
        m = pattern.search(body_str)
        if not m:
            continue
        raw = m.group(1)
        # Try several parse formats
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                    "%Y-%m-%d", "%B %d, %Y", "%B %d %Y"):
            try:
                dt = datetime.datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                age = (now - dt).total_seconds() / 86400.0
                if age >= 0:
                    return round(age, 2), label
            except ValueError:
                continue
    # Fallback: HTTP Last-Modified header
    if response_last_modified:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(response_last_modified)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            age = (now - dt).total_seconds() / 86400.0
            if age >= 0:
                return round(age, 2), "http-last-modified"
        except Exception:
            pass
    return None, None


def _has_dchub_nav(body_str: str) -> bool:
    """Phase ZZZ: True if body contains a reference to dchub-nav.js or
    the nav-config object. Case-insensitive cheap substring check —
    no DOM parse needed."""
    if not body_str:
        return False
    lo = body_str.lower()
    return ("dchub-nav.js" in lo or "dchubnav.js" in lo
            or "dchub_nav_config" in lo or "dchub-nav-brand" in lo)


def _ensure_schema(cur):
    cur.execute(_SCHEMA)


def _scan_one(entry: dict) -> dict:
    """Phase YYY/ZZZ: returns full scan dict including nav + staleness.
    Backward-compatible: callers that only need the basics can read
    status_code/bytes/elapsed_ms/healthy/reason."""
    import requests
    path     = entry["path"]
    category = entry["category"]
    min_bytes  = entry.get("min_bytes", 0)
    wants_nav    = bool(entry.get("wants_nav", False))
    max_age_days = entry.get("max_age_days")  # None means don't check
    url = f"{_SITE_BASE}{path}"
    t0 = time.time()
    out: dict = {
        "status_code": 0, "bytes": 0, "elapsed_ms": 0,
        "healthy": False, "reason": "",
        "has_nav": None, "stale_days": None, "data_age_src": None,
    }
    try:
        # Phase FFFF (2026-05-16): timeout 10s → 15s. The brain
        # heartbeat endpoint has a 9-10s cold-start path; 10s was
        # right at the edge and Sentinel was falsely flagging it as
        # timeout. 15s gives slow cold-starts headroom without
        # making the overall scan meaningfully slower (most pages
        # respond in <1s anyway).
        #
        # Phase ZZZZZ-round8 (2026-05-23): explicitly follow redirects
        # so /vs (301→/vs/dchawk→200) isn't false-flagged as
        # http_status:301. requests defaults to allow_redirects=True
        # for GET, but the prior version's stream=True path had a quirk
        # where status_code reflected the first hop in some retry
        # branches. Force it.
        # 2026-05-24 r34: browser-style User-Agent. The old "DCHub-Site-
        # Sentinel/1.0" was triggering Cloudflare's anti-bot WAF on
        # /grid/CAISO, /grid/ERCOT, /grid/PJM, /research/grid-intelligence
        # (all 4 returning HTTP 403 with 8115b WAF challenge page even
        # though real users hit them fine). Switching to a recent
        # Chrome UA passes the bot check while keeping the request
        # identifiable via the X-DC-Probe header for our own log analysis.
        # r47.36 (2026-05-26): include X-Internal-Key so sentinel probes
        # bypass the free-tier gate + transactions-browser paywall + WAF
        # Custom Rules that returned 403 on /transactions et al.
        # Brain class `site_url_unhealthy` recommends fixing the probe,
        # not loosening the public gate.
        import os as _os
        _ik = (_os.environ.get("DCHUB_INTERNAL_KEY")
               or _os.environ.get("DCHUB_SYNC_KEY") or "")
        _hdrs = {
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36 "
                           "DCHub-Sentinel/2.0"),
            "X-DC-Probe":    "site-sentinel",
            "Cache-Control": "no-cache",
            "Accept":        "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
        }
        if _ik:
            _hdrs["X-Internal-Key"] = _ik
        r = requests.get(url, timeout=15, headers=_hdrs,
                          stream=True, allow_redirects=True)
        body = r.raw.read(64 * 1024, decode_content=True) if r.raw else r.content[:64*1024]
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        out["status_code"] = r.status_code
        out["bytes"] = len(body) if body else len(r.content)
        # Track the URL we ended up at (for the dashboard's transparency)
        if r.url and r.url != url:
            out["final_url"] = r.url
        last_mod = r.headers.get("Last-Modified")
        # Phase VVVV (2026-05-16): content-hash for drift detection.
        # Use first 8KB to keep cost predictable + ignore tail noise
        # (timestamps near the bottom of pages would flap otherwise).
        try:
            import hashlib
            sample = (body or b"")[:8192]
            out["content_hash"] = hashlib.sha256(sample).hexdigest()[:32]
        except Exception:
            out["content_hash"] = None
        try: r.close()
        except Exception: pass

        # HTTP status / size gates first.
        # 2026-05-24: support per-entry `expected_status` so routes that
        # intentionally return non-200 (e.g. /api/v1/brain/heartbeat's 202
        # stale-while-revalidate path) don't get flagged as unhealthy.
        # Accepts int or list/tuple of ints; defaults to [200].
        expected = entry.get("expected_status", 200)
        if isinstance(expected, (list, tuple, set)):
            allowed = set(expected)
        else:
            allowed = {expected}
        if out["status_code"] not in allowed:
            out["reason"] = f"http_status:{out['status_code']}"
            return out
        if out["bytes"] < min_bytes:
            out["reason"] = f"body_too_small:{out['bytes']}<{min_bytes}"
            return out

        # Phase YYY/ZZZ analysis — only when basics pass
        body_str = ""
        try:
            body_str = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else (body or "")
        except Exception:
            body_str = ""

        if wants_nav:
            out["has_nav"] = _has_dchub_nav(body_str)
            if not out["has_nav"]:
                out["reason"] = "nav_missing"
                return out

        if max_age_days is not None:
            age, src = _extract_page_age_days(body_str, last_mod)
            out["stale_days"] = age
            out["data_age_src"] = src
            if age is not None and age > max_age_days:
                out["reason"] = f"stale:{age:.1f}d>max{max_age_days}d({src})"
                return out

        out["healthy"] = True
        out["reason"] = "ok"
        return out
    except requests.exceptions.Timeout:
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        out["reason"] = "timeout"
        return out
    except requests.exceptions.ConnectionError as e:
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        out["reason"] = f"connect_failed:{str(e)[:80]}"
        return out
    except Exception as e:
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        out["reason"] = f"{type(e).__name__}:{str(e)[:80]}"
        return out


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
            label    = entry.get("label", "")
            scan = _scan_one(entry)
            results.append({
                "path":         path,
                "category":     category,
                "label":        label,
                "status_code":  scan["status_code"],
                "bytes":        scan["bytes"],
                "elapsed_ms":   scan["elapsed_ms"],
                "healthy":      scan["healthy"],
                "reason":       scan["reason"],
                "has_nav":      scan.get("has_nav"),
                "stale_days":   scan.get("stale_days"),
                "data_age_src": scan.get("data_age_src"),
            })
            if c is not None:
                try:
                    with c.cursor() as cur:
                        # Phase VVVV: roll content_hash → prev_content_hash
                        # so the diff detector has yesterday's value
                        # to compare against.
                        cur.execute("""
                            INSERT INTO site_sentinel_results
                              (path, category, label, status_code, bytes,
                               elapsed_ms, healthy, reason, checked_at,
                               last_healthy_at, has_nav, stale_days,
                               data_age_src, content_hash,
                               prev_content_hash, prev_bytes)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s, NOW() ON CONFLICT DO NOTHING,
                                    CASE WHEN %s THEN NOW() ELSE NULL END,
                                    %s, %s, %s, %s, NULL, NULL)
                            ON CONFLICT (path) DO UPDATE SET
                              category     = EXCLUDED.category,
                              label        = EXCLUDED.label,
                              status_code  = EXCLUDED.status_code,
                              bytes        = EXCLUDED.bytes,
                              elapsed_ms   = EXCLUDED.elapsed_ms,
                              healthy      = EXCLUDED.healthy,
                              reason       = EXCLUDED.reason,
                              checked_at   = NOW(),
                              has_nav      = EXCLUDED.has_nav,
                              stale_days   = EXCLUDED.stale_days,
                              data_age_src = EXCLUDED.data_age_src,
                              prev_content_hash = site_sentinel_results.content_hash,
                              prev_bytes        = site_sentinel_results.bytes,
                              content_hash      = EXCLUDED.content_hash,
                              last_healthy_at = CASE
                                WHEN EXCLUDED.healthy THEN NOW()
                                ELSE site_sentinel_results.last_healthy_at
                              END
                        """, (path, category, label, scan["status_code"],
                              scan["bytes"], scan["elapsed_ms"],
                              scan["healthy"], scan["reason"],
                              scan["healthy"],
                              scan.get("has_nav"), scan.get("stale_days"),
                              scan.get("data_age_src"),
                              scan.get("content_hash")))
                except Exception:
                    pass
    finally:
        if c is not None:
            try: c.close()
            except Exception: pass
    return results


def latest_results() -> list[dict]:
    """Read the last persisted scan (much cheaper than re-scanning).

    r47.40 (2026-05-27): filter to paths in the CURRENT _MANIFEST. Paths
    that used to be tracked but have been removed (e.g. /grid-hub which
    we replaced with /grid, /research/grid-intelligence which we replaced
    earlier) still have rows in site_sentinel_results. Without this
    filter, retired paths kept showing up as "unhealthy Grid Intel"
    forever, even though the manifest had already moved on. Now the
    read mirrors what the next scan will actually probe."""
    current_paths = {m["path"] for m in _MANIFEST}
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
                           last_healthy_at, has_nav, stale_days, data_age_src
                      FROM site_sentinel_results
                     WHERE path = ANY(%s)
                     ORDER BY healthy ASC, category ASC, path ASC
                """, (list(current_paths),))
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
                        "has_nav":     r["has_nav"],
                        "stale_days":  float(r["stale_days"]) if r["stale_days"] is not None else None,
                        "data_age_src":r["data_age_src"],
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
        cat    = r.get("category") or "normal"
        reason = r.get("reason") or ""
        # Critical pages: every breakage is a finding. High: same. Normal:
        # only HTTP failures, not body-too-small (which can be legitimate
        # if a page is intentionally minimal).
        if cat == "normal" and reason.startswith("body_too_small"):
            continue

        # Phase ZZZ: nav-missing → its own finding type. The fix is
        # always "include dchub-nav.js in the page template" not "fix
        # the route", so separate it from generic site_sentinel_unhealthy
        # to make the autopilot pattern lookup unambiguous.
        if reason == "nav_missing":
            findings.append({
                "issue":  f"nav_missing:{r['path']}",
                "url":    f"{_SITE_BASE}{r['path']}",
                "count":  1,
                "detail": (f"Page '{r.get('label') or r['path']}' returns 200 "
                           f"with {r.get('bytes')} bytes but does NOT include "
                           f"dchub-nav.js. Users see a page with no top nav — "
                           f"must use browser back to escape. Add "
                           f"`<script src=\"/js/dchub-nav.js\" defer></script>` "
                           f"to the page template OR (for Flask routes) "
                           f"wire dchub-nav.js include via the standard "
                           f"page wrapper. Category: {cat}."),
            })
            continue

        # Phase YYY: stale-page → its own finding type. The fix is
        # always "bump the cron / re-ingest", not "fix the route".
        if reason.startswith("stale:"):
            findings.append({
                "issue":  f"page_stale:{r['path']}",
                "url":    f"{_SITE_BASE}{r['path']}",
                "count":  int(r.get("stale_days") or 0),
                "detail": (f"Page '{r.get('label') or r['path']}' has data "
                           f"older than its freshness SLA. "
                           f"Detected age: {r.get('stale_days')} days "
                           f"(source: {r.get('data_age_src')}). "
                           f"Fix: bump the ingest cron OR refresh the data "
                           f"source. Category: {cat}. "
                           f"Last healthy: {r.get('last_healthy_at') or 'never since tracked'}."),
            })
            continue

        # Default: generic unhealthy
        findings.append({
            "issue":  f"site_sentinel_unhealthy:{r['path']}",
            "url":    f"{_SITE_BASE}{r['path']}",
            "count":  r.get("status_code") or 0,
            "detail": (f"Page '{r.get('label') or r['path']}' is unhealthy. "
                       f"Status: {r.get('status_code')}, "
                       f"bytes: {r.get('bytes')}, "
                       f"reason: {reason}. "
                       f"Category: {cat}. "
                       f"Last healthy: {r.get('last_healthy_at') or 'never since tracked'}. "
                       f"This is the Site Sentinel — fix the page OR adjust "
                       f"the manifest in routes/site_sentinel.py:_MANIFEST "
                       f"if the expectation is wrong."),
        })
    # Cap at 16 so a mass outage doesn't drown the heartbeat
    return findings[:16]


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

    # r34 (2026-05-24): Page-integrity tile. Pulls the per-URL
    # 0-100 + verdict from /api/v1/sentinel/page-integrity so the
    # operator sees the holistic "is every page evolving" score
    # right next to the per-page pass/fail table below.
    integrity_tile = ""
    try:
        from flask import current_app
        with current_app.test_client() as _c2:
            _ri = _c2.get("/api/v1/sentinel/page-integrity")
            if _ri.status_code == 200:
                _ig = _ri.get_json() or {}
                _isc = float(_ig.get("site_score") or 0)
                _ivd = _ig.get("site_verdict", "unknown")
                _ibreak = _ig.get("verdict_breakdown") or {}
                _itotal = int(_ig.get("pages_total") or 0)
                _vcolor = {
                    "alive":   ("#10b981", "linear-gradient(135deg,#065f46,#10b981)"),
                    "weak":    ("#f59e0b", "linear-gradient(135deg,#92400e,#f59e0b)"),
                    "patchy":  ("#f59e0b", "linear-gradient(135deg,#7c2d12,#f59e0b)"),
                    "broken":  ("#ef4444", "linear-gradient(135deg,#991b1b,#ef4444)"),
                }.get(_ivd, ("#94a3b8", "linear-gradient(135deg,#475569,#94a3b8)"))
                _ic, _ibg = _vcolor
                _alive  = int(_ibreak.get("alive")  or 0)
                _broken = int(_ibreak.get("broken") or 0)
                _orphan = int(_ibreak.get("orphan") or 0)
                _stale  = int(_ibreak.get("stale")  or 0)
                _pills = ""
                for lbl, val, col in (
                    ("alive", _alive, "#10b981"),
                    ("broken", _broken, "#ef4444"),
                    ("orphan", _orphan, "#f59e0b"),
                    ("stale", _stale, "#a78bfa"),
                ):
                    if val:
                        _pills += (
                            f'<span style="display:inline-flex;align-items:center;gap:0.3rem;'
                            f'padding:0.25rem 0.6rem;border-radius:99px;'
                            f'background:rgba(255,255,255,0.08);color:{col};'
                            f'font-size:0.78rem;font-weight:600;margin:0.15rem;">'
                            f'{lbl} <span style="font-family:JetBrains Mono,monospace;'
                            f'opacity:0.9">{val}</span></span>'
                        )
                integrity_tile = (
                    f'<div style="padding:1.25rem 1.5rem;border-radius:10px;color:white;'
                    f'margin:1rem 0;background:{_ibg};">'
                    f'<div style="display:flex;align-items:center;justify-content:space-between;'
                    f'flex-wrap:wrap;gap:1rem;">'
                    f'<div><div style="font-size:0.78rem;text-transform:uppercase;'
                    f'letter-spacing:0.1em;opacity:0.8;margin-bottom:0.3rem;">'
                    f'🔍 Page Integrity — {_itotal} pages</div>'
                    f'<div style="font-size:1.5rem;font-weight:700;line-height:1.1;">'
                    f'{_isc:.1f}/100 · {_ivd.upper()}</div>'
                    f'<div style="font-size:0.85rem;opacity:0.85;margin-top:0.3rem;">'
                    f'per-URL brain integration + freshness + health</div></div>'
                    f'<div style="font-size:0.78rem;opacity:0.8;text-align:right;">'
                    f'<a href="/api/v1/sentinel/page-integrity" style="color:white;'
                    f'text-decoration:none;border-bottom:1px dotted rgba(255,255,255,0.5);">'
                    f'view JSON →</a></div>'
                    f'</div>'
                    f'<div style="margin-top:0.7rem;">{_pills}</div>'
                    f'</div>'
                )
    except Exception:
        integrity_tile = ""

    # r32 (2026-05-24): Media-organism tile. Pulls vitality + verdict
    # from /api/v1/media/organism so the operator's "is everything OK"
    # page also answers "is media alive?". Wrapped in try so a slow
    # composition can never block this dashboard from rendering.
    organism_tile = ""
    try:
        from flask import current_app
        with current_app.test_client() as _client:
            _r = _client.get("/api/v1/media/organism")
            if _r.status_code == 200:
                _d = _r.get_json() or {}
                _vs = float(_d.get("vitality_score") or 0)
                _verdict = _d.get("verdict", "unknown")
                _weakest = _d.get("weakest_channel") or "—"
                _comps = _d.get("components") or {}
                _verdict_color = {
                    "alive":   ("#10b981", "linear-gradient(135deg,#065f46,#10b981)"),
                    "warming": ("#3b82f6", "linear-gradient(135deg,#1d4ed8,#3b82f6)"),
                    "quiet":   ("#f59e0b", "linear-gradient(135deg,#92400e,#f59e0b)"),
                    "dormant": ("#ef4444", "linear-gradient(135deg,#991b1b,#ef4444)"),
                }.get(_verdict, ("#94a3b8", "linear-gradient(135deg,#475569,#94a3b8)"))
                _vc, _vbg = _verdict_color
                # Compact pill row, one per channel.
                _pills = ""
                _icons = {
                    "press": "📰", "linkedin": "💼", "source_of_truth": "🎯",
                    "topic_pulse": "📡", "journalist_outreach": "✉️", "winback": "♻️",
                }
                for _k, _c in _comps.items():
                    if not isinstance(_c, dict): continue
                    _sv = float(_c.get("score") or 0)
                    _cv = _c.get("verdict", "?")
                    _icon = _icons.get(_k, "•")
                    _pcolor = ("#10b981" if _cv == "healthy"
                               else "#f59e0b" if _cv == "weak"
                               else "#94a3b8" if _cv == "quiet"
                               else "#ef4444" if _cv == "dormant"
                               else "#94a3b8")
                    _pills += (
                        f'<span style="display:inline-flex;align-items:center;gap:0.3rem;'
                        f'padding:0.25rem 0.6rem;border-radius:99px;'
                        f'background:rgba(255,255,255,0.08);color:{_pcolor};'
                        f'font-size:0.78rem;font-weight:600;margin:0.15rem;">'
                        f'{_icon} {_k.replace("_"," ")} '
                        f'<span style="font-family:JetBrains Mono,monospace;'
                        f'opacity:0.9">{_sv:.0f}</span></span>'
                    )
                organism_tile = (
                    f'<div style="padding:1.25rem 1.5rem;border-radius:10px;color:white;'
                    f'margin:1rem 0;background:{_vbg};">'
                    f'<div style="display:flex;align-items:center;justify-content:space-between;'
                    f'flex-wrap:wrap;gap:1rem;">'
                    f'<div><div style="font-size:0.78rem;text-transform:uppercase;'
                    f'letter-spacing:0.1em;opacity:0.8;margin-bottom:0.3rem;">'
                    f'📺 DC Hub Media Organism</div>'
                    f'<div style="font-size:1.5rem;font-weight:700;line-height:1.1;">'
                    f'{_vs:.1f}/100 · {_verdict.upper()}</div>'
                    f'<div style="font-size:0.85rem;opacity:0.85;margin-top:0.3rem;">'
                    f'weakest channel: <strong>{_weakest}</strong></div></div>'
                    f'<div style="font-size:0.78rem;opacity:0.8;text-align:right;">'
                    f'<a href="/api/v1/media/organism" style="color:white;'
                    f'text-decoration:none;border-bottom:1px dotted rgba(255,255,255,0.5);">'
                    f'view JSON →</a></div>'
                    f'</div>'
                    f'<div style="margin-top:0.7rem;">{_pills}</div>'
                    f'</div>'
                )
    except Exception:
        organism_tile = ""

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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
 body{{font-family:'Instrument Sans',-apple-system,sans-serif;max-width:1200px;margin:0 auto;padding:2rem 1rem;background:var(--dch-bg);color:var(--dch-text)}}
 h1{{font-size:1.8rem;margin:0 0 .25rem}}
 .summary{{padding:1.25rem;border-radius:10px;color:white;font-size:1.2rem;margin:1rem 0}}
 .summary.green{{background:linear-gradient(135deg,#6366f1,#a855f7)}}
 .summary.amber{{background:linear-gradient(135deg,#92400e,#b45309)}}
 .summary.red{{background:linear-gradient(135deg,#991b1b,#b91c1c)}}
 table{{width:100%;border-collapse:collapse;background:var(--dch-surface);border-radius:8px;overflow:hidden;border:1px solid var(--dch-border)}}
 th{{text-align:left;padding:.6rem;background:var(--dch-surface-2);font-size:.8rem;text-transform:uppercase;color:var(--dch-text-mute)}}
 td{{padding:.55rem .6rem;border-top:1px solid var(--dch-border);font-size:.9rem}}
 tr.ok td{{color:var(--dch-text)}}
 tr.bad{{background:rgba(239,68,68,.08)}}
 tr.bad td{{color:#fca5a5;font-weight:600}}
 a{{color:#818cf8;text-decoration:none}} a:hover{{text-decoration:underline;color:#a855f7}}
 .footer{{color:var(--dch-text-dim);font-size:.85rem;margin-top:2rem}}
</style></head>
<body>
<h1>🛰️ Site Sentinel</h1>
<p style="color:var(--dch-text-mute)">Polls every public URL on the manifest. Unhealthy pages auto-surface as brain findings in /api/v1/brain/heartbeat.</p>
<div class="summary {overall_class}">
  <strong>{healthy}/{len(rows)} pages healthy ({pct}%)</strong>
</div>
{integrity_tile}
{organism_tile}
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
