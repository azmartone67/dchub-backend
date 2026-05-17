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
    {"path": "/api/v1/spare-capacity/listings", "category": "normal", "min_bytes": 200, "label": "Spare Capacity API"},
    {"path": "/api/v1/developers/funnel","category": "normal", "min_bytes": 100, "label": "Developers Funnel API"},

    # Phase GGGG-JJJJ (2026-05-16) — new surfaces from master shell
    {"path": "/transparency",                  "category": "high",   "min_bytes": 3000, "label": "Transparency",       "wants_nav": True},
    {"path": "/api/v1/facilities/delta",       "category": "normal", "min_bytes": 100,  "label": "Facilities Delta API"},

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
        r = requests.get(url, timeout=15, headers={
            "User-Agent":  "DCHub-Site-Sentinel/1.0",
            "Cache-Control": "no-cache",
        }, stream=True)
        body = r.raw.read(64 * 1024, decode_content=True) if r.raw else r.content[:64*1024]
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        out["status_code"] = r.status_code
        out["bytes"] = len(body) if body else len(r.content)
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

        # HTTP status / size gates first (same as before)
        if out["status_code"] != 200:
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
                           last_healthy_at, has_nav, stale_days, data_age_src
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
