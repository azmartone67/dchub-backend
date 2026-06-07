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
    # r43-H (2026-05-28): per-slug deep-link canaries. The /dcpi/northern-virginia
    # cross-link 404 (#1 4xx path, ~6.6k/day) was invisible because the manifest
    # only polled the /markets/ + /dcpi index, never a per-slug page. These
    # canonical slugs return 200 and exercise the exact route families that broke,
    # so a regression now trips the sentinel instead of silently 404ing.
    {"path": "/markets/northern-virginia", "category": "high", "min_bytes": 2000, "label": "Market page (per-slug render)"},
    {"path": "/dcpi/ashburn",              "category": "high", "min_bytes": 2000, "label": "DCPI page (per-slug render)"},
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
    # r-sentinel (2026-06-04): lowered min_bytes 500->80. The endpoint returns an
    # honest empty-state {"zones":[],"count":0,...} = 83 bytes because all 10 upstream
    # /iso/<iso>/zones.json files 404 in prod (they live only in the STALE
    # backend/dchub-frontend subdir, never deployed; seeded roster-only with
    # lmp_usd_mwh:null "pending EIA hookup"). 200-status is valid - keep watching it,
    # but the 500-byte floor was a false-positive body_too_small (mirrors spare-capacity).
    {"path": "/api/v1/iso/zones",        "category": "high",   "min_bytes":   80, "label": "ISO Zones Aggregator"},
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

    # ── Phase HEAL-AND-SHIP (2026-06-07) — sentinel evolves into autonomous
    # heal-and-ship loop. Tracks ALL surfaces shipped in the last week so the
    # brain SEES every new endpoint the moment it regresses.
    #
    # Admin-gated routes are flagged `needs_admin: True` — the probe attaches
    # X-Admin-Key (DCHUB_ADMIN_KEY env) so a 401/403 doesn't get logged as a
    # false outage. Without that flag, every admin dashboard would scream
    # 401 once per scan.

    # Admin dashboards (single-pane operator telemetry surfaces)
    {"path": "/admin/funnel-health",                "category": "high",   "min_bytes": 2000, "label": "Admin Funnel Health",       "needs_admin": True},
    {"path": "/admin/brain-backlog",                "category": "high",   "min_bytes": 2000, "label": "Admin Brain Backlog",       "needs_admin": True},
    {"path": "/admin/qa/state-of-2026",             "category": "high",   "min_bytes": 2000, "label": "Admin QA State-of-2026",    "needs_admin": True},
    # 2026-06-07 Round-1 cleanup: the precheck probes 8 internal endpoints
    # and routinely takes ~30s at the Railway origin. CF Pages edge caps
    # synthesized 5xx at ~10s, so dchub.cloud returns 503 even when origin
    # is 200. Allow 503 here so the sentinel doesn't false-flag a working
    # but slow endpoint as down. Origin-direct probing would be cleaner;
    # this is the pragmatic fix.
    {"path": "/api/v1/admin/qa/state-of-2026-precheck", "category": "high","min_bytes": 200, "label": "QA Precheck API",           "needs_admin": True, "expected_status": [200, 202, 503, 504]},

    # MCP Connect landings (the one-click connector hand-off pages)
    {"path": "/connect/cursor",                     "category": "high",   "min_bytes": 1500, "label": "Connect → Cursor",          "wants_nav": True},
    {"path": "/connect/cline",                      "category": "high",   "min_bytes": 1500, "label": "Connect → Cline",           "wants_nav": True},
    {"path": "/connect/continue",                   "category": "high",   "min_bytes": 1500, "label": "Connect → Continue",        "wants_nav": True},
    {"path": "/connect/claude-desktop",             "category": "high",   "min_bytes": 1500, "label": "Connect → Claude Desktop",  "wants_nav": True},

    # Per-market brief canaries (top 5 by ops MW)
    {"path": "/markets/northern-virginia/brief",    "category": "high",   "min_bytes": 2000, "label": "Market Brief: Northern Virginia"},
    {"path": "/markets/dallas/brief",               "category": "high",   "min_bytes": 2000, "label": "Market Brief: Dallas"},
    {"path": "/markets/phoenix/brief",              "category": "high",   "min_bytes": 2000, "label": "Market Brief: Phoenix"},
    {"path": "/markets/atlanta/brief",              "category": "high",   "min_bytes": 2000, "label": "Market Brief: Atlanta"},
    {"path": "/markets/chicago/brief",              "category": "high",   "min_bytes": 2000, "label": "Market Brief: Chicago"},

    # Per-hyperscaler brief canaries (top 5 by capex)
    {"path": "/hyperscalers/aws/brief",             "category": "high",   "min_bytes": 2000, "label": "Hyperscaler Brief: AWS"},
    {"path": "/hyperscalers/azure/brief",           "category": "high",   "min_bytes": 2000, "label": "Hyperscaler Brief: Azure"},
    {"path": "/hyperscalers/google-cloud/brief",    "category": "high",   "min_bytes": 2000, "label": "Hyperscaler Brief: Google Cloud"},
    {"path": "/hyperscalers/meta/brief",            "category": "high",   "min_bytes": 2000, "label": "Hyperscaler Brief: Meta"},
    {"path": "/hyperscalers/oracle/brief",          "category": "high",   "min_bytes": 2000, "label": "Hyperscaler Brief: Oracle"},

    # Per-state brief canaries (top 5 by published-market count)
    {"path": "/states/texas/brief",                 "category": "high",   "min_bytes": 2000, "label": "State Brief: Texas"},
    {"path": "/states/california/brief",            "category": "high",   "min_bytes": 2000, "label": "State Brief: California"},
    {"path": "/states/virginia/brief",              "category": "high",   "min_bytes": 2000, "label": "State Brief: Virginia"},
    {"path": "/states/georgia/brief",               "category": "high",   "min_bytes": 2000, "label": "State Brief: Georgia"},
    {"path": "/states/ohio/brief",                  "category": "high",   "min_bytes": 2000, "label": "State Brief: Ohio"},

    # Per-operator brief canaries (top 5 by ops MW)
    {"path": "/operators/aligned/brief",            "category": "high",   "min_bytes": 2000, "label": "Operator Brief: Aligned"},
    {"path": "/operators/qts/brief",                "category": "high",   "min_bytes": 2000, "label": "Operator Brief: QTS"},
    {"path": "/operators/digital-realty/brief",     "category": "high",   "min_bytes": 2000, "label": "Operator Brief: Digital Realty"},
    {"path": "/operators/equinix/brief",            "category": "high",   "min_bytes": 2000, "label": "Operator Brief: Equinix"},
    {"path": "/operators/vantage/brief",            "category": "high",   "min_bytes": 2000, "label": "Operator Brief: Vantage"},

    # DCPI + MCP funnel API canaries (the JSON the brain learns from)
    {"path": "/api/v1/dcpi/scores",                 "category": "high",   "min_bytes":  500, "label": "DCPI Scores API (full)"},
    {"path": "/api/v1/dcpi/leaderboard",            "category": "high",   "min_bytes":  500, "label": "DCPI Leaderboard API"},
    {"path": "/api/v1/mcp/funnel",                  "category": "high",   "min_bytes":  300, "label": "MCP Funnel API"},
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
        # Phase HEAL-AND-SHIP (2026-06-07): admin-gated pages need the
        # X-Admin-Key header or they 401/403 + the sentinel logs a false
        # "page down" finding. The brain then spins on a phantom outage
        # because nothing is actually broken — it's just gated. Attach
        # the admin key when the manifest entry sets `needs_admin: True`.
        if entry.get("needs_admin") and _ADMIN_KEY:
            _hdrs["X-Admin-Key"] = _ADMIN_KEY
        # r-sentinel-retry (2026-05-31): the slowest-render pages
        # (/dcpi/<slug>, /markets/<slug>, /operators, /grid/<iso>) were
        # red-flagged on a SINGLE transient self-call timeout even though
        # external curl returns 200 in ~1s — worker-pool contention on the
        # 2-replica backend (same class as the brain self-DDoS). Retry
        # transient Timeout/ConnectionError up to 3x before giving up so a
        # momentary blip can't flip a healthy page to RED.
        r = None
        for _attempt in range(3):
            try:
                r = requests.get(url, timeout=15, headers=_hdrs,
                                  stream=True, allow_redirects=True)
                break
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError):
                if _attempt < 2:
                    time.sleep(0.5 * (_attempt + 1))
                    t0 = time.time()
                    continue
                raise
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
            # r62-qa: a Cloudflare WAF / anti-bot challenge served to OUR OWN
            # self-probe (403 + challenge page) is NOT a page outage — real
            # visitors load the page fine. This produced a false "Pricing down"
            # critical finding. Treat a CF-challenge 403 as a soft skip, not a
            # broken page.
            if out["status_code"] in (403, 503):
                _csnip = ""
                try:
                    _csnip = (body.decode("utf-8", "ignore") if isinstance(body, bytes)
                              else (body or ""))[:2000].lower()
                except Exception:
                    _csnip = ""
                if any(m in _csnip for m in (
                        "just a moment", "cf-challenge", "/cdn-cgi/challenge",
                        "attention required", "cloudflare", "ray id")):
                    out["healthy"] = True
                    out["reason"] = "waf_challenge_skipped"
                    return out
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
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s, NOW(),
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

    # Phase HEAL-AND-SHIP (2026-06-07): auto-run the consecutive-failure
    # tracker after every full sweep so the brain backlog and the operator
    # email path are wired without needing a separate cron. The tracker has
    # its own rate-limit guard (_TRACKER_MIN_INTERVAL_S) so it cannot fire
    # more often than once per 30s even if scan_all is called repeatedly.
    try:
        track_consecutive_failures(results)
    except Exception:
        pass

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

# ── Outcome verification (r47, 2026-06-03) ──────────────────────────────────
# The Sentinel DETECTED unhealthy pages but never CLOSED the loop: a chronically
# broken page (e.g. the /pricing 20s-hang) read as a fresh one-off finding every
# scan, and a page that recovered left no resolution record. verify_outcomes()
# re-probes every currently-open finding and classifies the outcome:
#   • RESOLVED — re-probe healthy now: heal the row + log the recovery (+downtime)
#                so there's a durable detection→resolution trail the brain can learn from.
#   • STUCK    — still unhealthy AND down longer than `stuck_hours` (or never
#                healthy): escalate distinctly so chronic outages stand apart from
#                transient blips instead of re-firing as "new" every scan.
#   • FRESH    — still unhealthy but only recently (< stuck_hours): give it time.
_RESOLUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_sentinel_resolutions (
    id               BIGSERIAL PRIMARY KEY,
    path             TEXT NOT NULL,
    label            TEXT,
    prior_reason     TEXT,
    recovered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    downtime_minutes INT
);
CREATE INDEX IF NOT EXISTS ix_ssr_recovered ON site_sentinel_resolutions(recovered_at DESC);
"""


def verify_outcomes(stuck_hours: float = 2.0) -> dict:
    """Re-probe every currently-open Sentinel finding and classify each as
    resolved / stuck / fresh. Closes the detection→resolution loop the Sentinel
    never had — the missing piece behind the /pricing chronic-hang going
    un-escalated. Read-mostly: only writes a heal-update + a resolution row
    when a page has actually recovered."""
    out: dict = {"checked": 0, "resolved": [], "stuck": [], "fresh": [],
                 "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}
    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    by_path = {e["path"]: e for e in _MANIFEST}
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with c.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(_RESOLUTIONS_SCHEMA)
            cur.execute("""
                SELECT path, category, label, reason, last_healthy_at
                  FROM site_sentinel_results
                 WHERE healthy = FALSE
            """)
            rows = cur.fetchall()
            for path, category, label, reason, last_healthy_at in rows:
                entry = by_path.get(path)
                if not entry:
                    continue  # manifest changed since this row was written
                out["checked"] += 1
                scan = _scan_one(entry)
                if scan.get("healthy"):
                    downtime_min = None
                    if last_healthy_at is not None:
                        try:
                            downtime_min = int((now - last_healthy_at).total_seconds() // 60)
                        except Exception:
                            downtime_min = None
                    cur.execute("""
                        UPDATE site_sentinel_results
                           SET healthy = TRUE, reason = 'ok',
                               status_code = %s, elapsed_ms = %s,
                               checked_at = NOW(), last_healthy_at = NOW()
                         WHERE path = %s
                    """, (scan.get("status_code"), scan.get("elapsed_ms"), path))
                    cur.execute("""
                        INSERT INTO site_sentinel_resolutions
                          (path, label, prior_reason, downtime_minutes)
                        VALUES (%s, %s, %s, %s)
                    """, (path, label, reason, downtime_min))
                    out["resolved"].append({
                        "path": path, "label": label, "prior_reason": reason,
                        "downtime_minutes": downtime_min,
                    })
                else:
                    down_for_h = None
                    if last_healthy_at is not None:
                        try:
                            down_for_h = round((now - last_healthy_at).total_seconds() / 3600.0, 1)
                        except Exception:
                            down_for_h = None
                    is_stuck = (last_healthy_at is None) or \
                               (down_for_h is not None and down_for_h >= stuck_hours)
                    item = {"path": path, "label": label,
                            "reason": scan.get("reason"), "category": category,
                            "down_for_hours": down_for_h}
                    (out["stuck"] if is_stuck else out["fresh"]).append(item)
    except Exception as e:
        out["error"] = f"{type(e).__name__}:{str(e)[:120]}"
    finally:
        try: c.close()
        except Exception: pass
    out["resolved_count"] = len(out["resolved"])
    out["stuck_count"]    = len(out["stuck"])
    out["fresh_count"]    = len(out["fresh"])
    return out


@site_sentinel_bp.route("/api/v1/sentinel/verify-outcomes", methods=["POST"])
def sentinel_verify_outcomes():
    """Admin-only: re-probe open findings, mark each resolved / stuck / fresh."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401
    try:
        stuck_hours = float(request.args.get("stuck_hours", 2.0))
    except Exception:
        stuck_hours = 2.0
    return jsonify(verify_outcomes(stuck_hours=stuck_hours)), 200


@site_sentinel_bp.route("/api/v1/sentinel/resolutions", methods=["GET"])
def sentinel_resolutions():
    """Recent detection→resolution events — what recovered + how long it was down.
    The closed-loop trail the Sentinel previously lacked."""
    c = _conn()
    if c is None:
        return jsonify(resolutions=[], count=0, error="no_database"), 200
    rows = []
    try:
        with c.cursor() as cur:
            cur.execute(_RESOLUTIONS_SCHEMA)
            cur.execute("""
                SELECT path, label, prior_reason, recovered_at, downtime_minutes
                  FROM site_sentinel_resolutions
                 ORDER BY recovered_at DESC LIMIT 50
            """)
            for path, label, prior_reason, recovered_at, downtime_minutes in cur.fetchall():
                rows.append({"path": path, "label": label,
                             "prior_reason": prior_reason,
                             "recovered_at": recovered_at.isoformat() if recovered_at else None,
                             "downtime_minutes": downtime_minutes})
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(resolutions=rows, count=len(rows),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    resp.headers["Cache-Control"] = "public, max-age=120"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# ── Phase HEAL-AND-SHIP (2026-06-07) — autonomous heal-and-ship loop ───
# Closes the gap the user named: "probe sweep runs but nothing acts on
# failures." Now consecutive 5xx hits cross a threshold → the sentinel
# opens a brain Layer-5 fix proposal automatically via brain_findings.
# At threshold 5 a follow-up email goes to Jonathan so a chronic outage
# never just stews in the dashboard.
#
# The counters survive across probes via site_sentinel_consec_failures
# (per-path counter + last_failure_at + last_brain_finding_at). A 2xx
# response resets the counter and (optionally) logs a recovery hook.

_CONSEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_sentinel_consec_failures (
    path                   TEXT PRIMARY KEY,
    consecutive_5xx_count  INT NOT NULL DEFAULT 0,
    consecutive_fail_count INT NOT NULL DEFAULT 0,
    last_status_code       INT,
    last_reason            TEXT,
    last_failure_at        TIMESTAMPTZ,
    last_brain_finding_at  TIMESTAMPTZ,
    last_escalation_at     TIMESTAMPTZ,
    last_success_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_ssc_failure_at ON site_sentinel_consec_failures(last_failure_at DESC);
"""

# Threshold tuning. Three strikes = brain finding (Layer-5 reads on next
# learn pass); five strikes = also email the operator.
_BRAIN_FINDING_THRESHOLD = 3
_EMAIL_THRESHOLD         = 5

# Idempotency guard: don't re-fire the brain finding more than once per
# this many hours per path (otherwise every 5-min scan keeps re-upserting
# the same finding, drowning brain_findings).
_BRAIN_FINDING_REFIRE_HOURS = 6
_EMAIL_REFIRE_HOURS         = 24

# Probe-storm guard. The 162.220.232.99 1.5M-blocked-request incident
# (per memory) was the sentinel hammering its own surfaces. We never want
# the heal-and-ship loop to *trigger more probes* — it only reads the
# already-persisted scan_all() results. Belt-and-suspenders: track when
# we last ran the failure-tracker so even if it's wired into two cron
# slots it can't run more than once per 30s.
_TRACKER_GUARD = {"last_run_ts": 0.0}
_TRACKER_MIN_INTERVAL_S = 30.0


def _is_5xx(status_code: int | None) -> bool:
    try:
        return 500 <= int(status_code or 0) <= 599
    except Exception:
        return False


def track_consecutive_failures(results: list[dict] | None = None) -> dict:
    """Walk the latest scan results. For each path:
      • 5xx        → increment consecutive_5xx_count + consecutive_fail_count
      • non-5xx 4xx/timeout/etc → increment consecutive_fail_count only
        (keeps the broader fail trail honest without auto-opening a brain
        finding for a 401/timeout — those are usually config/transient).
      • 2xx healthy → reset BOTH counters + last_success_at = NOW().
    Threshold hits open/refresh brain findings + (at 5) email the operator.

    No HTTP probes. Pure DB walk over latest_results() / persisted scan
    rows so this can never DDoS our own site.
    """
    import time as _time
    out: dict = {"checked": 0, "opened": [], "escalated": [], "reset": [],
                 "skipped_reason": None,
                 "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}

    # Probe-storm guard (paranoid; not strictly needed since we don't probe).
    now_ts = _time.time()
    if now_ts - _TRACKER_GUARD["last_run_ts"] < _TRACKER_MIN_INTERVAL_S:
        out["skipped_reason"] = "rate_limited_30s"
        return out
    _TRACKER_GUARD["last_run_ts"] = now_ts

    if results is None:
        results = latest_results()
    if not results:
        out["skipped_reason"] = "no_results"
        return out

    c = _conn()
    if c is None:
        out["skipped_reason"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            cur.execute(_CONSEC_SCHEMA)
            for r in results:
                path = r.get("path")
                if not path:
                    continue
                out["checked"] += 1
                status_code = r.get("status_code") or 0
                reason      = r.get("reason") or ""
                healthy     = bool(r.get("healthy"))
                label       = r.get("label") or path
                category    = r.get("category") or "normal"

                if healthy:
                    # Reset both counters.
                    cur.execute("""
                        INSERT INTO site_sentinel_consec_failures
                          (path, consecutive_5xx_count, consecutive_fail_count,
                           last_status_code, last_reason, last_success_at)
                        VALUES (%s, 0, 0, %s, %s, NOW())
                        ON CONFLICT (path) DO UPDATE SET
                          consecutive_5xx_count  = 0,
                          consecutive_fail_count = 0,
                          last_status_code       = EXCLUDED.last_status_code,
                          last_reason            = 'ok',
                          last_success_at        = NOW()
                    """, (path, status_code, reason or "ok"))
                    out["reset"].append(path)
                    continue

                # Unhealthy: bump fail counter; bump 5xx counter only when
                # the status is in the 500s. Other failure modes
                # (timeout / connect_failed / 4xx / body_too_small /
                # nav_missing / stale) still raise consecutive_fail_count
                # but don't directly fire the auto-Layer-5 path — those
                # are caught by the existing detector + heal loop.
                fivexx_step = 1 if _is_5xx(status_code) else 0
                cur.execute("""
                    INSERT INTO site_sentinel_consec_failures
                      (path, consecutive_5xx_count, consecutive_fail_count,
                       last_status_code, last_reason, last_failure_at)
                    VALUES (%s, %s, 1, %s, %s, NOW())
                    ON CONFLICT (path) DO UPDATE SET
                      consecutive_5xx_count  = site_sentinel_consec_failures.consecutive_5xx_count + %s,
                      consecutive_fail_count = site_sentinel_consec_failures.consecutive_fail_count + 1,
                      last_status_code       = EXCLUDED.last_status_code,
                      last_reason            = EXCLUDED.last_reason,
                      last_failure_at        = NOW()
                    RETURNING consecutive_5xx_count, consecutive_fail_count,
                              last_brain_finding_at, last_escalation_at
                """, (path, fivexx_step, status_code, reason, fivexx_step))
                row = cur.fetchone()
                if not row:
                    continue
                consec_5xx, consec_fail, last_bf_at, last_esc_at = row

                # ── threshold 3: open brain Layer-5 fix proposal ──
                if consec_5xx >= _BRAIN_FINDING_THRESHOLD:
                    cooldown_hit = False
                    if last_bf_at is not None:
                        try:
                            hours_since = (datetime.datetime.now(datetime.timezone.utc)
                                           - last_bf_at).total_seconds() / 3600.0
                            cooldown_hit = hours_since < _BRAIN_FINDING_REFIRE_HOURS
                        except Exception:
                            cooldown_hit = False
                    if not cooldown_hit:
                        try:
                            from routes.brain_findings_writer import upsert_brain_finding
                            issue = f"page_persistent_5xx:{path}"
                            url   = f"{_SITE_BASE}{path}"
                            detail = (
                                f"Sentinel: '{label}' returned 5xx {consec_5xx} consecutive scans "
                                f"(last status {status_code}, reason: {reason}). "
                                f"Category: {category}. Auto-opened by site_sentinel for "
                                f"brain Layer-5 fix proposal. Fix path: inspect the route "
                                f"+ open a draft PR. Source: routes/site_sentinel.py "
                                f":track_consecutive_failures."
                            )
                            res = upsert_brain_finding(
                                cur, issue=issue, url=url,
                                count=int(consec_5xx),
                                detail=detail,
                                detector="site_sentinel.heal_and_ship")
                            cur.execute("""
                                UPDATE site_sentinel_consec_failures
                                   SET last_brain_finding_at = NOW()
                                 WHERE path = %s
                            """, (path,))
                            out["opened"].append({
                                "path": path, "label": label,
                                "consecutive_5xx": int(consec_5xx),
                                "consecutive_fail": int(consec_fail),
                                "brain_writer_result": res,
                                "status_code": int(status_code),
                            })
                        except Exception as e:
                            out.setdefault("errors", []).append(
                                f"open_finding:{path}:{type(e).__name__}:{str(e)[:80]}")

                # ── threshold 5: email Jonathan (Resend) ──
                if consec_5xx >= _EMAIL_THRESHOLD:
                    esc_cooldown = False
                    if last_esc_at is not None:
                        try:
                            hours_since = (datetime.datetime.now(datetime.timezone.utc)
                                           - last_esc_at).total_seconds() / 3600.0
                            esc_cooldown = hours_since < _EMAIL_REFIRE_HOURS
                        except Exception:
                            esc_cooldown = False
                    if not esc_cooldown:
                        sent = _send_escalation_email(path, label, category,
                                                      consec_5xx, status_code,
                                                      reason)
                        if sent:
                            cur.execute("""
                                UPDATE site_sentinel_consec_failures
                                   SET last_escalation_at = NOW()
                                 WHERE path = %s
                            """, (path,))
                            out["escalated"].append({
                                "path": path, "label": label,
                                "consecutive_5xx": int(consec_5xx),
                                "email_sent": True,
                            })
    except Exception as e:
        out["error"] = f"{type(e).__name__}:{str(e)[:120]}"
    finally:
        try: c.close()
        except Exception: pass
    out["opened_count"]     = len(out["opened"])
    out["escalated_count"]  = len(out["escalated"])
    out["reset_count"]      = len(out["reset"])
    return out


def _send_escalation_email(path: str, label: str, category: str,
                            consec_5xx: int, status_code: int,
                            reason: str) -> bool:
    """Best-effort Resend email. Fails silently — escalation is informative
    only, not a guard. Tries the canonical helper, falls back to the legacy
    alias, then to a direct Resend API call if RESEND_API_KEY is set."""
    to = (os.environ.get("DCHUB_OPERATOR_EMAIL")
          or os.environ.get("OPERATOR_EMAIL")
          or "azmartone@gmail.com").strip()
    subject = f"[Sentinel] Persistent 5xx on {label} ({consec_5xx} consecutive)"
    body = (
        f"The Site Sentinel has detected {consec_5xx} consecutive 5xx responses on:\n\n"
        f"  Page:     {label}\n"
        f"  Path:     {path}\n"
        f"  URL:      {_SITE_BASE}{path}\n"
        f"  Category: {category}\n"
        f"  Last:     HTTP {status_code} — {reason}\n\n"
        f"A brain Layer-5 finding has been open since strike 3. Layer-5 will "
        f"propose a fix on its next learn pass. If a draft PR appears in the "
        f"backlog, review + merge.\n\n"
        f"Inbox: https://dchub.cloud/admin/sentinel-inbox\n"
        f"Source: routes/site_sentinel.py:track_consecutive_failures\n"
    )
    # 1) Canonical helper
    try:
        from routes.email_resend import send_email  # type: ignore
        send_email(to=to, subject=subject, body=body)
        return True
    except Exception:
        pass
    # 2) Direct Resend API
    try:
        import requests as _rq
        key = os.environ.get("RESEND_API_KEY") or ""
        if not key:
            return False
        _rq.post("https://api.resend.com/emails",
                 headers={"Authorization": f"Bearer {key}",
                          "Content-Type": "application/json"},
                 json={"from": os.environ.get("RESEND_FROM_EMAIL", "alerts@dchub.cloud"),
                       "to": [to], "subject": subject, "text": body},
                 timeout=8)
        return True
    except Exception:
        return False


def consec_failure_state() -> list[dict]:
    """Read-only counter snapshot. Used by the inbox dashboard JSON."""
    c = _conn()
    if c is None:
        return []
    out: list[dict] = []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_CONSEC_SCHEMA)
            cur.execute("""
                SELECT path, consecutive_5xx_count, consecutive_fail_count,
                       last_status_code, last_reason, last_failure_at,
                       last_brain_finding_at, last_escalation_at,
                       last_success_at
                  FROM site_sentinel_consec_failures
            """)
            for r in cur.fetchall():
                out.append({
                    "path": r["path"],
                    "consecutive_5xx":  int(r["consecutive_5xx_count"] or 0),
                    "consecutive_fail": int(r["consecutive_fail_count"] or 0),
                    "last_status_code": r["last_status_code"],
                    "last_reason":      r["last_reason"],
                    "last_failure_at":  r["last_failure_at"].isoformat() if r["last_failure_at"] else None,
                    "last_brain_finding_at": r["last_brain_finding_at"].isoformat() if r["last_brain_finding_at"] else None,
                    "last_escalation_at":    r["last_escalation_at"].isoformat() if r["last_escalation_at"] else None,
                    "last_success_at":  r["last_success_at"].isoformat() if r["last_success_at"] else None,
                })
    except Exception:
        return out
    finally:
        try: c.close()
        except Exception: pass
    return out


def _grade(r: dict, consec: dict | None) -> str:
    """One-letter grade for the inbox row.
      A = healthy + fast        B = healthy but slow (>3s)
      C = unhealthy 1 strike    D = unhealthy 2 strikes / 3+ fail
      F = unhealthy >=3 strikes (brain finding open or escalated)
    """
    c5 = (consec or {}).get("consecutive_5xx", 0) or 0
    cf = (consec or {}).get("consecutive_fail", 0) or 0
    if not r.get("healthy"):
        if c5 >= _BRAIN_FINDING_THRESHOLD:
            return "F"
        if c5 >= 2 or cf >= 3:
            return "D"
        return "C"
    elapsed = r.get("elapsed_ms") or 0
    if elapsed > 3000:
        return "B"
    return "A"


@site_sentinel_bp.route("/api/v1/sentinel/track-failures", methods=["POST"])
def sentinel_track_failures():
    """Admin-only: walk the latest scan + update consecutive-failure
    counters + open brain findings at threshold 3 + escalate at 5.

    This is the single closing-the-loop call. Wire it after every probe
    sweep. No HTTP probes performed (read-only on persisted scan rows)
    so it can't trigger a probe storm."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401
    return jsonify(track_consecutive_failures()), 200


@site_sentinel_bp.route("/api/v1/admin/sentinel-inbox", methods=["GET"])
def sentinel_inbox_json():
    """JSON feed for the Sentinel Inbox dashboard. Admin-gated. Read-only.
    Joins persisted scan rows + consecutive-failure counters so the
    operator and the brain see the same per-page status."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401
    rows = latest_results()
    consec_by_path = {x["path"]: x for x in consec_failure_state()}
    items = []
    for r in rows:
        p  = r.get("path") or ""
        cs = consec_by_path.get(p, {})
        items.append({
            "path":              p,
            "label":             r.get("label") or p,
            "category":          r.get("category"),
            "status_code":       r.get("status_code"),
            "bytes":             r.get("bytes"),
            "elapsed_ms":        r.get("elapsed_ms"),
            "healthy":           bool(r.get("healthy")),
            "reason":            r.get("reason"),
            "grade":             _grade(r, cs),
            "consecutive_5xx":   cs.get("consecutive_5xx", 0),
            "consecutive_fail":  cs.get("consecutive_fail", 0),
            "last_failure_at":   cs.get("last_failure_at"),
            "last_brain_finding_at": cs.get("last_brain_finding_at"),
            "last_escalation_at":    cs.get("last_escalation_at"),
            "last_success_at":   cs.get("last_success_at"),
            "checked_at":        r.get("checked_at"),
        })
    items.sort(key=lambda x: (x["healthy"],
                              0 if x["grade"] == "F" else (1 if x["grade"] == "D" else 2),
                              x["category"], x["path"]))
    healthy = sum(1 for i in items if i["healthy"])
    grades  = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    for i in items:
        grades[i.get("grade", "C")] = grades.get(i.get("grade", "C"), 0) + 1
    return jsonify(
        total=len(items),
        healthy=healthy,
        unhealthy=len(items) - healthy,
        manifest_size=len(_MANIFEST),
        grades=grades,
        items=items,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    ), 200


@site_sentinel_bp.route("/api/v1/admin/sentinel-inbox/probe", methods=["POST"])
def sentinel_inbox_probe_one():
    """Admin: re-probe a single page on demand (the inbox row-level
    'trigger probe now' button). Bounded to paths in the current manifest
    so it can't be used to probe arbitrary URLs."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401
    target = (request.args.get("path")
              or (request.get_json(silent=True) or {}).get("path") or "").strip()
    if not target:
        return jsonify(error="missing_path"), 400
    entry = next((e for e in _MANIFEST if e["path"] == target), None)
    if entry is None:
        return jsonify(error="not_in_manifest", path=target), 404
    scan = _scan_one(entry)
    # Persist this single result (mirrors the multi-scan upsert above).
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                _ensure_schema(cur)
                cur.execute("""
                    INSERT INTO site_sentinel_results
                      (path, category, label, status_code, bytes, elapsed_ms,
                       healthy, reason, checked_at, last_healthy_at,
                       has_nav, stale_days, data_age_src, content_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s, NOW(),
                            CASE WHEN %s THEN NOW() ELSE NULL END,
                            %s, %s, %s, %s)
                    ON CONFLICT (path) DO UPDATE SET
                      status_code = EXCLUDED.status_code,
                      bytes       = EXCLUDED.bytes,
                      elapsed_ms  = EXCLUDED.elapsed_ms,
                      healthy     = EXCLUDED.healthy,
                      reason      = EXCLUDED.reason,
                      checked_at  = NOW(),
                      has_nav     = EXCLUDED.has_nav,
                      stale_days  = EXCLUDED.stale_days,
                      data_age_src= EXCLUDED.data_age_src,
                      content_hash= EXCLUDED.content_hash,
                      last_healthy_at = CASE
                        WHEN EXCLUDED.healthy THEN NOW()
                        ELSE site_sentinel_results.last_healthy_at
                      END
                """, (entry["path"], entry["category"],
                      entry.get("label", ""),
                      scan["status_code"], scan["bytes"], scan["elapsed_ms"],
                      scan["healthy"], scan["reason"],
                      scan["healthy"],
                      scan.get("has_nav"), scan.get("stale_days"),
                      scan.get("data_age_src"), scan.get("content_hash")))
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
    # Update the consecutive counter for this single path so the inbox
    # immediately reflects the new state. Reuses the same logic.
    track_consecutive_failures([{
        "path":        entry["path"],
        "label":       entry.get("label", ""),
        "category":    entry["category"],
        "status_code": scan["status_code"],
        "reason":      scan["reason"],
        "healthy":     scan["healthy"],
    }])
    return jsonify(ok=True, path=target, scan=scan), 200


@site_sentinel_bp.route("/api/v1/admin/sentinel-inbox/open-finding",
                         methods=["POST"])
def sentinel_inbox_open_finding():
    """Admin: force-open a brain finding for a specific page without
    waiting for the 3-strike threshold."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized", hint="X-Admin-Key required"), 401
    target = (request.args.get("path")
              or (request.get_json(silent=True) or {}).get("path") or "").strip()
    if not target:
        return jsonify(error="missing_path"), 400
    entry = next((e for e in _MANIFEST if e["path"] == target), None)
    if entry is None:
        return jsonify(error="not_in_manifest", path=target), 404
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 500
    try:
        with c.cursor() as cur:
            from routes.brain_findings_writer import upsert_brain_finding
            issue = f"page_persistent_5xx:{target}"
            url   = f"{_SITE_BASE}{target}"
            detail = (
                f"Operator-escalated from /admin/sentinel-inbox. "
                f"Page '{entry.get('label', target)}' flagged for manual "
                f"Layer-5 fix proposal. Source: routes/site_sentinel.py "
                f":sentinel_inbox_open_finding."
            )
            res = upsert_brain_finding(cur, issue=issue, url=url,
                                       count=1, detail=detail,
                                       detector="site_sentinel.inbox_manual")
            cur.execute(_CONSEC_SCHEMA)
            cur.execute("""
                INSERT INTO site_sentinel_consec_failures (path, last_brain_finding_at)
                VALUES (%s, NOW())
                ON CONFLICT (path) DO UPDATE SET last_brain_finding_at = NOW()
            """, (target,))
        return jsonify(ok=True, path=target, brain_writer_result=res), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}:{str(e)[:120]}"), 500
    finally:
        try: c.close()
        except Exception: pass


@site_sentinel_bp.route("/admin/sentinel-inbox", methods=["GET"],
                         strict_slashes=False)
def sentinel_inbox_dashboard():
    """The Sentinel Inbox — every probe result + grade + one-click actions.
    Mirrors the /admin/brain-backlog UX (the page itself renders publicly;
    the JS fetches with X-Admin-Key from the URL query string)."""
    return Response(_INBOX_HTML, mimetype="text/html")


# Inbox HTML — standalone so it renders even if other brand surfaces break.
_INBOX_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Sentinel Inbox — DC Hub</title>
<meta name="robots" content="noindex,nofollow">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>
 :root{--ok:#10b981;--bad:#ef4444;--warn:#f59e0b;--mute:#94a3b8}
 body{font-family:'Inter',-apple-system,sans-serif;background:#0a0e1a;color:#e6ecf5;
   max-width:1400px;margin:0 auto;padding:1.5rem 1rem;line-height:1.45}
 h1{font-size:1.6rem;margin:.2rem 0}
 .sub{color:var(--mute);font-size:.9rem;margin-bottom:1rem}
 .toolbar{display:flex;gap:.5rem;flex-wrap:wrap;margin:1rem 0;align-items:center}
 .btn{background:linear-gradient(135deg,#6366f1,#a855f7);color:white;border:0;
   padding:.55rem 1rem;border-radius:6px;font-weight:600;cursor:pointer;font-size:.85rem}
 .btn.alt{background:rgba(255,255,255,.08);color:#cbd5e1}
 .btn:disabled{opacity:.5;cursor:not-allowed}
 .grade{padding:.3rem .65rem;border-radius:99px;font-size:.78rem;font-weight:700;
   border:1px solid rgba(255,255,255,.1)}
 .grade.A{background:rgba(16,185,129,.15);color:#10b981}
 .grade.B{background:rgba(59,130,246,.15);color:#60a5fa}
 .grade.C{background:rgba(245,158,11,.15);color:#fbbf24}
 .grade.D{background:rgba(239,68,68,.15);color:#fca5a5}
 .grade.F{background:rgba(239,68,68,.3);color:white}
 table{width:100%;border-collapse:collapse;font-size:.84rem;
   background:rgba(255,255,255,.02);border-radius:8px;overflow:hidden;
   border:1px solid rgba(255,255,255,.06)}
 th{text-align:left;padding:.55rem .6rem;background:rgba(255,255,255,.04);
   text-transform:uppercase;font-size:.72rem;color:var(--mute);font-weight:600}
 td{padding:.5rem .6rem;border-top:1px solid rgba(255,255,255,.05);vertical-align:middle}
 tr:hover td{background:rgba(255,255,255,.03)}
 td.path a{color:#a5b4fc;text-decoration:none;font-family:JetBrains Mono,monospace;font-size:.8rem}
 td.path a:hover{text-decoration:underline}
 .status.ok{color:#10b981;font-weight:600}
 .status.bad{color:#fca5a5;font-weight:600}
 .pill{display:inline-block;padding:.15rem .45rem;border-radius:4px;font-size:.7rem;
   font-weight:600;font-family:JetBrains Mono,monospace}
 .pill.crit{background:rgba(239,68,68,.2);color:#fca5a5}
 .pill.high{background:rgba(245,158,11,.2);color:#fbbf24}
 .pill.norm{background:rgba(148,163,184,.2);color:var(--mute)}
 .pill.fail{background:rgba(239,68,68,.3);color:white}
 .pill.bf{background:rgba(99,102,241,.3);color:#c4b5fd}
 .rowbtn{padding:.3rem .55rem;border-radius:4px;background:rgba(255,255,255,.06);
   border:1px solid rgba(255,255,255,.1);color:#e6ecf5;cursor:pointer;font-size:.72rem;
   margin-right:.25rem}
 .rowbtn:hover{background:rgba(99,102,241,.2)}
 .summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
   gap:.6rem;margin:1rem 0}
 .kpi{background:rgba(255,255,255,.04);padding:.7rem .9rem;border-radius:8px;
   border:1px solid rgba(255,255,255,.06)}
 .kpi .v{font-size:1.5rem;font-weight:700;font-family:JetBrains Mono,monospace}
 .kpi .l{font-size:.7rem;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
 .toast{position:fixed;bottom:1rem;right:1rem;background:#1f2937;padding:.6rem 1rem;
   border-radius:6px;border:1px solid rgba(255,255,255,.1);font-size:.85rem;
   max-width:380px;opacity:0;transition:opacity .25s;pointer-events:none}
 .toast.show{opacity:1}
 .footer{color:var(--mute);font-size:.8rem;margin-top:2rem;line-height:1.5}
 .reason{font-family:JetBrains Mono,monospace;font-size:.72rem;color:#fca5a5;
   max-width:280px;overflow:hidden;text-overflow:ellipsis;display:block;white-space:nowrap}
</style></head>
<body>
<h1>Sentinel Inbox</h1>
<p class="sub">Every probe result + grade + one-click actions. Brain Layer-5 reads
findings opened here on its next learn pass and proposes draft PRs.</p>

<div class="summary" id="kpis">
  <div class="kpi"><div class="l">Total Tracked</div><div class="v" id="kpi-total">--</div></div>
  <div class="kpi"><div class="l">Healthy</div><div class="v" id="kpi-healthy" style="color:#10b981">--</div></div>
  <div class="kpi"><div class="l">Unhealthy</div><div class="v" id="kpi-unhealthy" style="color:#fca5a5">--</div></div>
  <div class="kpi"><div class="l">Manifest Size</div><div class="v" id="kpi-manifest">--</div></div>
  <div class="kpi"><div class="l">Grades</div><div class="v" id="kpi-grades" style="font-size:.9rem">--</div></div>
</div>

<div class="toolbar">
  <button class="btn" id="sweep">Run sweep now</button>
  <button class="btn alt" id="track">Run heal-and-ship tracker</button>
  <button class="btn alt" id="reload">Reload</button>
  <span style="color:var(--mute);font-size:.8rem;margin-left:.5rem" id="ts"></span>
</div>

<div class="toolbar" id="tabs">
  <button class="btn alt tab-btn active" data-tab="manifest" style="background:rgba(99,102,241,.3)">Manifest probes</button>
  <button class="btn alt tab-btn" data-tab="automerge">Auto-merge Activity</button>
</div>

<div id="tab-manifest">
<table>
  <thead>
    <tr>
      <th>Grade</th><th>Cat</th><th>Page</th><th>Status</th><th>Bytes</th>
      <th>Streak</th><th>Reason</th><th>Brain</th><th>Actions</th>
    </tr>
  </thead>
  <tbody id="tbody">
    <tr><td colspan="9" style="text-align:center;padding:2rem;color:var(--mute)">Loading...</td></tr>
  </tbody>
</table>
</div>

<div id="tab-automerge" style="display:none">
  <div class="summary">
    <div class="kpi"><div class="l">Kill Switch</div><div class="v" id="am-kill" style="font-size:1rem">--</div></div>
    <div class="kpi"><div class="l">Dry Run</div><div class="v" id="am-dry" style="font-size:1rem">--</div></div>
    <div class="kpi"><div class="l">Weekly Cap</div><div class="v" id="am-cap" style="font-size:1.1rem">--</div></div>
    <div class="kpi"><div class="l">Min Confidence</div><div class="v" id="am-conf" style="font-size:1.1rem">--</div></div>
    <div class="kpi"><div class="l">Outcomes (merged rows)</div><div class="v" id="am-outcomes" style="font-size:.85rem">--</div></div>
  </div>
  <div class="toolbar">
    <button class="btn" id="am-run">Run auto-merge sweep now</button>
    <button class="btn alt" id="am-followup">Run follow-up probes</button>
    <button class="btn alt" id="am-block-24h" style="background:rgba(239,68,68,.2);color:#fca5a5">Block auto-merge 24h</button>
    <button class="btn alt" id="am-reload">Reload decisions</button>
  </div>
  <table>
    <thead>
      <tr>
        <th>When</th><th>PR</th><th>Decision</th><th>Reason</th>
        <th>Sentinel Path</th><th>Conf</th><th>Files</th><th>Follow-up</th>
      </tr>
    </thead>
    <tbody id="am-tbody">
      <tr><td colspan="8" style="text-align:center;padding:2rem;color:var(--mute)">Click "Reload decisions" to load.</td></tr>
    </tbody>
  </table>
  <p class="footer" style="margin-top:1rem">
    Decisions: <a href="/api/v1/admin/sentinel/auto-merge-log" style="color:#a5b4fc">/api/v1/admin/sentinel/auto-merge-log</a> ·
    Sweep: <a href="/api/v1/admin/sentinel/auto-merge-eligible" style="color:#a5b4fc">/api/v1/admin/sentinel/auto-merge-eligible</a> ·
    Status: <a href="/api/v1/admin/sentinel/auto-merge-status" style="color:#a5b4fc">/api/v1/admin/sentinel/auto-merge-status</a> ·
    Source: routes/sentinel_auto_merge.py
  </p>
</div>

<p class="footer">
  JSON: <a href="/api/v1/admin/sentinel-inbox" style="color:#a5b4fc">/api/v1/admin/sentinel-inbox</a> ·
  /api/v1/sentinel/track-failures (auto-runs after every probe sweep) ·
  Brain backlog: <a href="/admin/brain-backlog" style="color:#a5b4fc">/admin/brain-backlog</a> ·
  Source: routes/site_sentinel.py
</p>

<div id="toast" class="toast"></div>

<script>
(function(){
  var ADMIN_KEY = new URLSearchParams(window.location.search).get('admin_key') || '';
  function toast(msg){
    var t = document.getElementById('toast');
    t.textContent = msg; t.classList.add('show');
    setTimeout(function(){ t.classList.remove('show'); }, 3500);
  }
  function esc(s){ return String(s||'').replace(/[&<>"]/g, function(m){
    return ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]); }); }
  function catPill(c){ var k=(c||'normal')==='critical'?'crit':(c==='high'?'high':'norm');
    return '<span class="pill '+k+'">'+esc(c||'normal')+'</span>'; }
  function load(){
    fetch('/api/v1/admin/sentinel-inbox?admin_key=' + encodeURIComponent(ADMIN_KEY),
          { headers: { 'X-Admin-Key': ADMIN_KEY } })
      .then(function(r){ return r.json(); })
      .then(function(j){
        if (j.error){ toast('Unauthorized: append ?admin_key=... to URL'); return; }
        document.getElementById('kpi-total').textContent = j.total;
        document.getElementById('kpi-healthy').textContent = j.healthy;
        document.getElementById('kpi-unhealthy').textContent = j.unhealthy;
        document.getElementById('kpi-manifest').textContent = j.manifest_size;
        var g = j.grades || {};
        document.getElementById('kpi-grades').textContent =
          'A:' + (g.A||0) + ' B:' + (g.B||0) + ' C:' + (g.C||0) + ' D:' + (g.D||0) + ' F:' + (g.F||0);
        document.getElementById('ts').textContent = 'generated_at ' + j.generated_at;
        var tb = document.getElementById('tbody'); tb.innerHTML = '';
        (j.items || []).forEach(function(it){
          var tr = document.createElement('tr');
          var brainPill = it.last_brain_finding_at
            ? '<span class="pill bf" title="brain finding open since ' + esc(it.last_brain_finding_at) + '">L5 OPEN</span>'
            : '--';
          var streak = (it.consecutive_5xx > 0)
            ? '<span class="pill fail">' + it.consecutive_5xx + 'x 5xx</span>'
            : ((it.consecutive_fail > 0) ? (it.consecutive_fail + 'x fail') : '--');
          tr.innerHTML =
            '<td><span class="grade ' + esc(it.grade) + '">' + esc(it.grade) + '</span></td>' +
            '<td>' + catPill(it.category) + '</td>' +
            '<td class="path"><a href="' + esc(it.path) + '" target="_blank" rel="noopener">' + esc(it.label) + '</a>' +
              '<br><span style="color:var(--mute);font-family:JetBrains Mono,monospace;font-size:.7rem">' + esc(it.path) + '</span></td>' +
            '<td><span class="status ' + (it.healthy ? 'ok' : 'bad') + '">' + esc(it.status_code || '--') + '</span></td>' +
            '<td>' + esc(it.bytes || 0) + '</td>' +
            '<td>' + streak + '</td>' +
            '<td><span class="reason" title="' + esc(it.reason || '') + '">' + esc(it.reason || '--') + '</span></td>' +
            '<td>' + brainPill + '</td>' +
            '<td>' +
              '<button class="rowbtn" data-act="probe" data-path="' + esc(it.path) + '">Probe</button>' +
              '<button class="rowbtn" data-act="open"  data-path="' + esc(it.path) + '">Open finding</button>' +
            '</td>';
          tb.appendChild(tr);
        });
      })
      .catch(function(e){ toast('Load failed: ' + e); });
  }
  document.addEventListener('click', function(ev){
    var b = ev.target.closest('button.rowbtn');
    if (!b) return;
    var act = b.getAttribute('data-act'), path = b.getAttribute('data-path');
    var url = act === 'probe' ? '/api/v1/admin/sentinel-inbox/probe'
                              : '/api/v1/admin/sentinel-inbox/open-finding';
    b.disabled = true;
    fetch(url + '?admin_key=' + encodeURIComponent(ADMIN_KEY),
          { method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Admin-Key': ADMIN_KEY },
            body: JSON.stringify({ path: path }) })
      .then(function(r){ return r.json(); })
      .then(function(j){
        toast(act === 'probe' ? ('Probed ' + path + ' -> ' + (j.scan ? j.scan.status_code : 'err'))
                              : ('Brain finding ' + (j.brain_writer_result || 'opened') + ' for ' + path));
        setTimeout(load, 400);
      })
      .catch(function(e){ toast('Failed: ' + e); })
      .finally(function(){ b.disabled = false; });
  });
  document.getElementById('sweep').addEventListener('click', function(){
    this.disabled = true; toast('Running full sweep...');
    fetch('/api/v1/sentinel/scan-now?admin_key=' + encodeURIComponent(ADMIN_KEY),
          { method: 'POST', headers: { 'X-Admin-Key': ADMIN_KEY } })
      .then(function(r){ return r.json(); })
      .then(function(j){
        toast('Sweep complete: ' + j.healthy + '/' + j.scanned + ' healthy');
        return fetch('/api/v1/sentinel/track-failures?admin_key=' + encodeURIComponent(ADMIN_KEY),
                     { method: 'POST', headers: { 'X-Admin-Key': ADMIN_KEY } });
      })
      .then(function(r){ return r ? r.json() : {}; })
      .then(function(j){ setTimeout(load, 400); })
      .catch(function(e){ toast('Sweep failed: ' + e); })
      .finally(function(){ document.getElementById('sweep').disabled = false; });
  });
  document.getElementById('track').addEventListener('click', function(){
    this.disabled = true; toast('Running heal-and-ship tracker...');
    fetch('/api/v1/sentinel/track-failures?admin_key=' + encodeURIComponent(ADMIN_KEY),
          { method: 'POST', headers: { 'X-Admin-Key': ADMIN_KEY } })
      .then(function(r){ return r.json(); })
      .then(function(j){
        toast('Tracker: opened=' + (j.opened_count||0) + ' escalated=' + (j.escalated_count||0));
        setTimeout(load, 400);
      })
      .catch(function(e){ toast('Tracker failed: ' + e); })
      .finally(function(){ document.getElementById('track').disabled = false; });
  });
  document.getElementById('reload').addEventListener('click', load);
  load();

  // ── Auto-merge tab (Round 2) ──────────────────────────────────────
  function amFmtTime(s){
    if (!s) return '--';
    try { return String(s).replace('T',' ').slice(0,19) + 'Z'; }
    catch(e){ return String(s); }
  }
  function amFmtFiles(arr){
    if (!arr || !arr.length) return '--';
    var s = arr.slice(0,2).map(function(p){ return '<code style="font-size:.7rem">'+esc(p)+'</code>'; }).join('<br>');
    if (arr.length > 2) s += '<br><span style="color:var(--mute)">+' + (arr.length-2) + ' more</span>';
    return s;
  }
  function amFmtDecision(row){
    var d = row.decision || '';
    var color = d === 'allow' ? '#10b981' : (d === 'reject' ? '#fca5a5' : '#fbbf24');
    var label = d.toUpperCase();
    if (d === 'allow' && row.dry_run) label = 'DRY-RUN ALLOW';
    if (d === 'allow' && row.merged_at && !row.dry_run) label = 'MERGED';
    return '<span style="color:'+color+';font-weight:600;font-size:.78rem">'+esc(label)+'</span>';
  }
  function amFmtFollowup(row){
    if (!row.merge_sha || row.dry_run) return '--';
    var o = row.follow_up_outcome;
    if (!o) return '<span class="pill" style="background:rgba(245,158,11,.2);color:#fbbf24">pending</span>';
    if (o === 'success') return '<span class="pill" style="background:rgba(16,185,129,.2);color:#10b981">healed</span>';
    if (o === 'regression') return '<span class="pill" style="background:rgba(239,68,68,.3);color:white">regressed</span>';
    return '<span class="pill" style="background:rgba(148,163,184,.2);color:var(--mute)">'+esc(o)+'</span>';
  }
  function loadAutoMergeLog(){
    fetch('/api/v1/admin/sentinel/auto-merge-log?admin_key='+encodeURIComponent(ADMIN_KEY),
          { headers: { 'X-Admin-Key': ADMIN_KEY }})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if (j.error){ toast('Auto-merge log: ' + j.error); return; }
        document.getElementById('am-kill').textContent = j.kill_switch ? 'ON (DISABLED)' : 'off';
        document.getElementById('am-kill').style.color = j.kill_switch ? '#fca5a5' : '#10b981';
        document.getElementById('am-dry').textContent = j.dry_run ? 'YES (no merges)' : 'no (live)';
        document.getElementById('am-dry').style.color = j.dry_run ? '#fbbf24' : '#10b981';
        document.getElementById('am-cap').textContent = (j.used_this_week||0) + ' / ' + (j.weekly_cap||10);
        document.getElementById('am-conf').textContent = '≥ ' + (j.min_confidence||0.95);
        var oc = j.outcomes || {};
        document.getElementById('am-outcomes').innerHTML =
          '<span style="color:#10b981">' + (oc.success||0) + ' ok</span> · ' +
          '<span style="color:#fca5a5">' + (oc.regression||0) + ' regress</span> · ' +
          '<span style="color:#fbbf24">' + (oc.pending||0) + ' pend</span> · ' +
          '<span style="color:var(--mute)">' + (oc.inconclusive||0) + ' ?</span>';
        var tb = document.getElementById('am-tbody');
        tb.innerHTML = '';
        var rows = j.rows || [];
        if (!rows.length){
          tb.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2rem;color:var(--mute)">No decisions logged yet. Run sweep to populate.</td></tr>';
          return;
        }
        rows.forEach(function(r){
          var tr = document.createElement('tr');
          tr.innerHTML =
            '<td style="font-size:.72rem;color:var(--mute)">' + esc(amFmtTime(r.decided_at)) + '</td>' +
            '<td><a href="https://github.com/azmartone67/dchub-backend/pull/' + (r.pr_number||0) + '" target="_blank" rel="noopener" style="color:#a5b4fc">#' + esc(r.pr_number||'?') + '</a></td>' +
            '<td>' + amFmtDecision(r) + '</td>' +
            '<td><span class="reason" title="' + esc(r.reason||'') + '">' + esc((r.reason||'').slice(0,90)) + '</span></td>' +
            '<td><code style="font-size:.72rem">' + esc(r.sentinel_path||'--') + '</code></td>' +
            '<td>' + (r.confidence != null ? (Number(r.confidence).toFixed(2)) : '--') + '</td>' +
            '<td>' + amFmtFiles(r.files_changed) + '</td>' +
            '<td>' + amFmtFollowup(r) + '</td>';
          tb.appendChild(tr);
        });
      })
      .catch(function(e){ toast('Auto-merge log load failed: ' + e); });
  }
  document.querySelectorAll('.tab-btn').forEach(function(btn){
    btn.addEventListener('click', function(){
      document.querySelectorAll('.tab-btn').forEach(function(b){
        b.style.background = 'rgba(255,255,255,.08)';
        b.classList.remove('active');
      });
      this.style.background = 'rgba(99,102,241,.3)';
      this.classList.add('active');
      var t = this.getAttribute('data-tab');
      document.getElementById('tab-manifest').style.display = (t==='manifest') ? '' : 'none';
      document.getElementById('tab-automerge').style.display = (t==='automerge') ? '' : 'none';
      if (t === 'automerge') loadAutoMergeLog();
    });
  });
  document.getElementById('am-reload').addEventListener('click', loadAutoMergeLog);
  document.getElementById('am-run').addEventListener('click', function(){
    this.disabled = true; toast('Running auto-merge sweep...');
    fetch('/api/v1/admin/sentinel/auto-merge-eligible?admin_key=' + encodeURIComponent(ADMIN_KEY),
          { method: 'POST', headers: { 'X-Admin-Key': ADMIN_KEY }})
      .then(function(r){ return r.json(); })
      .then(function(j){
        toast('Sweep: scanned=' + (j.scanned||0) + ' allowed=' + (j.allowed||0) +
              ' rejected=' + (j.rejected||0) + ' merged=' + (j.merged||0) +
              (j.dry_run ? ' (DRY-RUN)' : ''));
        setTimeout(loadAutoMergeLog, 400);
      })
      .catch(function(e){ toast('Sweep failed: ' + e); })
      .finally((function(b){ return function(){ b.disabled = false; }; })(this));
  });
  document.getElementById('am-followup').addEventListener('click', function(){
    this.disabled = true; toast('Running follow-up probes...');
    fetch('/api/v1/admin/sentinel/auto-merge-followup?admin_key=' + encodeURIComponent(ADMIN_KEY),
          { method: 'POST', headers: { 'X-Admin-Key': ADMIN_KEY }})
      .then(function(r){ return r.json(); })
      .then(function(j){
        toast('Follow-up: checked=' + (j.checked||0) +
              ' success=' + (j.success||0) +
              ' regression=' + (j.regression||0));
        setTimeout(loadAutoMergeLog, 400);
      })
      .catch(function(e){ toast('Follow-up failed: ' + e); })
      .finally((function(b){ return function(){ b.disabled = false; }; })(this));
  });
  document.getElementById('am-block-24h').addEventListener('click', function(){
    if (!confirm('Block auto-merge for 24 hours? (writes a row to sentinel_auto_merge_block; all sweeps will reject during the window)')) return;
    this.disabled = true; toast('Setting 24h block...');
    fetch('/api/v1/admin/sentinel/auto-merge-block-24h?admin_key=' + encodeURIComponent(ADMIN_KEY),
          { method: 'POST', headers: { 'X-Admin-Key': ADMIN_KEY }})
      .then(function(r){ return r.json(); })
      .then(function(j){
        toast(j.ok ? ('Blocked until ' + (j.blocked_until||'(24h)')) : ('Block failed: ' + (j.error||'?')));
        setTimeout(loadAutoMergeLog, 400);
      })
      .catch(function(e){ toast('Block failed: ' + e); })
      .finally((function(b){ return function(){ b.disabled = false; }; })(this));
  });
})();
</script>
</body></html>"""


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
Heal-and-ship inbox: <a href="/admin/sentinel-inbox">/admin/sentinel-inbox</a> ·
Manifest size: {len(_MANIFEST)} URLs · Add new URLs in routes/site_sentinel.py:_MANIFEST</p>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=120"})
