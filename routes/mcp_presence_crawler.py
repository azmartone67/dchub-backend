"""
mcp_presence_crawler.py — Autonomous MCP-Presence Management (2026-06-05).

Two parallel autonomous loops:

1. PRESENCE CRAWLER
   For every known MCP listing site DC Hub appears on (Smithery, MCPHive,
   LobeHub, Glama, YellowMCP, mcp.so, PulseMCP, smith.land, dxt.so,
   Cursor.directory, awesome-mcp-servers, Cline, Continue.dev, Klavis,
   MCP-official-registry), fetch the live listing page, scrape the
   published tool count + uptime + last-updated, compare to OUR actual
   live tool count from /api/v1/mcp/tools.json, and write a brain_findings
   row when drift is detected so the brain hub triages it.

2. NEW-REGISTRY DISCOVERER
   Once per week, run a curated set of Google queries ("MCP server
   directory", "Model Context Protocol registry", "MCP marketplace",
   "best MCP servers list"). Filter results for domains we are NOT
   already listed on, sniff each candidate for a submit page, and file
   a brain_findings row of type 'mcp_registry_discovered' for the brain
   to triage.

Endpoints
---------
POST /api/v1/admin/mcp-presence/crawl    (admin-keyed)
POST /api/v1/admin/mcp-presence/seed     (admin-keyed)
POST /api/v1/admin/mcp-presence/discover (admin-keyed)
GET  /api/v1/mcp-presence/status         (PUBLIC, no secrets)

Schema
------
mcp_presence_listings (
    id SERIAL PK,
    registry_name TEXT NOT NULL UNIQUE,
    listing_url   TEXT NOT NULL,
    submit_url    TEXT,
    dchub_metric_published_tools INTEGER,
    dchub_metric_uptime_pct      NUMERIC,
    dchub_metric_last_seen       TIMESTAMPTZ,
    dchub_metric_stale_days      INTEGER,
    our_actual_tool_count        INTEGER,
    drift_detected               BOOLEAN DEFAULT FALSE,
    last_crawled_at              TIMESTAMPTZ,
    discovered                   BOOLEAN DEFAULT FALSE,
    notes                        JSONB DEFAULT '{}'::jsonb,
    created_at                   TIMESTAMPTZ DEFAULT NOW()
)

Defensive design: every extractor returns None on failure (never raises).
Crawler rate-limits at 1s/request, max 15 requests/run, identifies as
`dchub-mcp-presence-crawler/1.0 (+https://dchub.cloud)`.
"""
from __future__ import annotations

import os
import re
import json
import re as _re
import time
import logging
import datetime as _dt
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write
from util.json_column import json_for_column

logger = logging.getLogger(__name__)

mcp_presence_crawler_bp = Blueprint("mcp_presence_crawler", __name__)


# ── Constants ─────────────────────────────────────────────────────────
# 2026-07-03: browser UA. The honest bot UA got 403'd / JS-shelled by the
# React/Cloudflare registries (Smithery, Glama, mcp.so), so every listing
# fetch came back empty → discovered never flipped → the distribution shell
# read registries 0/5 while we're actually listed on all of them. A browser
# UA + the existing allow_redirects=True resolves them (verified 200 + our
# name present on smithery/glama/mcp.so/lobehub).
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
REQUEST_TIMEOUT_S = 8
RATE_LIMIT_SLEEP_S = 1.0
MAX_REQUESTS_PER_RUN = 15
STALE_DAYS_THRESHOLD = 30


# Canonical seed: the 15 MCP listing sites DC Hub appears on (or wants
# to appear on). registry_name MUST be unique and lowercase-slug-safe so
# the UNIQUE constraint and ON CONFLICT path work.
SEED_REGISTRIES: list[dict] = [
    {
        "registry_name": "smithery",
        "listing_url":   "https://smithery.ai/servers/azmartone67/dchub",
        "submit_url":    "https://smithery.ai/new",
    },
    {
        # 2026-06-06: MCPHive submission backend POST
        # /scripts/save_submission.php returns 404 — site appears
        # abandoned for new listings; we fall back to crawling the
        # directory home and flag with notes.submission_backend_404
        # so the auto-submitter skips it.
        "registry_name": "mcphive",
        "listing_url":   "https://mcphive.com/",
        "submit_url":    None,
        "notes":         {"submission_backend_404": True},
    },
    {
        "registry_name": "lobehub",
        # 2026-08-15: re-slugged to azmartone67-dchub-mcp-server; listings
        # live on market.lobehub.com (the lobehub.com/mcp vanity path 302s).
        "listing_url":   "https://market.lobehub.com/s/plugins/azmartone67-dchub-mcp-server",
        "submit_url":    "https://lobehub.com/mcp/submit",
    },
    {
        "registry_name": "glama",
        # 2026-07-09: point at the HEALTHY connector listing (verified tested + graded
        # A) — the old /mcp/servers/dchub redirects to a search, and the /cloud.dchub/
        # mcp-server connector is a Glama-flagged deprecated DUPLICATE.
        "listing_url":   "https://glama.ai/mcp/connectors/cloud.dchub/dc-hub-data-center-intelligence-mcp-server",
        "submit_url":    "https://glama.ai/mcp/servers/new",
    },
    {
        # r-url-rediscovery (2026-07-18): third URL shape in two months —
        # /servers/dchub (dead 06-06) → /mcp/dchub (dead by 07-18) →
        # /servers/cloud-dchub-mcp-server (verified 200 + our copy, linked
        # from their homepage). No sitemap on this site; the rediscovery
        # leg finds it via the homepage-href scan.
        "registry_name": "yellowmcp",
        "listing_url":   "https://yellowmcp.com/servers/cloud-dchub-mcp-server",
        "submit_url":    "https://yellowmcp.com/submit-mcp",
    },
    {
        # 2026-07-09: reversed the 2026-06-06 workaround. Back then
        # registry.modelcontextprotocol.io 404'd so this pointed at
        # registry.mcp.so — but that host is now DEAD (curl → 000) while the
        # official registry resolves 200 and carries our live entry (v2.4.5,
        # published daily by daily-manifest-sync). Point at the official API
        # search so the crawler confirms presence against the real source.
        "registry_name": "mcp_official_registry",
        "listing_url":   "https://registry.modelcontextprotocol.io/v0/servers?search=cloud.dchub",
        "submit_url":    "https://github.com/modelcontextprotocol/registry",
    },
    {
        "registry_name": "cline",
        "listing_url":   "https://docs.cline.bot/mcp/mcp-marketplace#dchub",
        "submit_url":    "https://github.com/cline/mcp-marketplace",
    },
    {
        "registry_name": "continue_dev",
        "listing_url":   "https://hub.continue.dev/dchub/mcp-server",
        "submit_url":    "https://hub.continue.dev/new",
    },
    {
        # ★ 2026-08-15: probe the RAW readme, not the repo HTML page. GitHub
        # renders the README client-side, so our entry never appears in the
        # server-side HTML — registry_truth read "no DC Hub identity" (broken)
        # for a listing that is live (mcp_registry_watch probes the raw
        # README and reads PRESENT). Mirror the watcher's URL.
        "registry_name": "awesome_mcp_servers",
        "listing_url":   "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
        "submit_url":    "https://github.com/punkpeye/awesome-mcp-servers/pulls",
    },
    {
        "registry_name": "mcp_so",
        # r-mcpso-url (2026-07-18): plural /servers/ — the singular form 404s;
        # the live listing is /servers/dchub-mcp-server (a second one exists at
        # /servers/dchub-backend). Wrong path = weeks of false "missing".
        "listing_url":   "https://mcp.so/servers/dchub-mcp-server",
        "submit_url":    "https://mcp.so/submit",
    },
    {
        # r-mcpso-secondary (2026-07-18): mcp.so ALSO indexed the backend repo
        # as a listing. Kept deliberately (extra directory shelf space, same
        # endpoint) — watched so the brain files a finding if it ever drifts
        # stale-wrong (it indexes from a repo README not written for MCP).
        "registry_name": "mcp_so_secondary",
        "listing_url":   "https://mcp.so/servers/dchub-backend",
        "submit_url":    "https://mcp.so/submit",
    },
    {
        "registry_name": "pulsemcp",
        "listing_url":   "https://pulsemcp.com/servers/dchub",
        "submit_url":    "https://pulsemcp.com/submit",
    },
    {
        "registry_name": "smith_land",
        "listing_url":   "https://smith.land/mcp/dchub",
        "submit_url":    "https://smith.land/submit",
    },
    {
        "registry_name": "dxt_so",
        "listing_url":   "https://dxt.so/server/dchub",
        "submit_url":    "https://dxt.so/submit",
    },
    {
        "registry_name": "cursor_directory",
        "listing_url":   "https://cursor.directory/mcp/dchub",
        "submit_url":    "https://cursor.directory/submit",
    },
    {
        "registry_name": "klavis_ai",
        "listing_url":   "https://www.klavis.ai/mcp-servers/dchub",
        "submit_url":    "https://www.klavis.ai/submit",
    },
]


# ── Plumbing ──────────────────────────────────────────────────────────
def _db_conn():
    """Open a psycopg2 connection. Returns None on failure (never raises)."""
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=6, sslmode="require") if url else None
    except Exception as e:
        logger.warning("mcp_presence: db connect failed: %s", e)
        return None


def _admin_or_cron_authorized() -> bool:
    """Same X-Admin-Key gate other admin routes use."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.args.get("key") or "").strip()
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS mcp_presence_listings (
    id                           SERIAL PRIMARY KEY,
    registry_name                TEXT NOT NULL UNIQUE,
    listing_url                  TEXT NOT NULL,
    submit_url                   TEXT,
    dchub_metric_published_tools INTEGER,
    dchub_metric_uptime_pct      NUMERIC,
    dchub_metric_last_seen       TIMESTAMPTZ,
    dchub_metric_stale_days      INTEGER,
    our_actual_tool_count        INTEGER,
    drift_detected               BOOLEAN DEFAULT FALSE,
    last_crawled_at              TIMESTAMPTZ,
    discovered                   BOOLEAN DEFAULT FALSE,
    notes                        JSONB DEFAULT '{}'::jsonb,
    created_at                   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS mcp_presence_listings_drift_idx
    ON mcp_presence_listings (drift_detected);
CREATE INDEX IF NOT EXISTS mcp_presence_listings_last_crawled_idx
    ON mcp_presence_listings (last_crawled_at DESC);
"""


def _ensure_schema(cur) -> None:
    """Create the table + indexes. Idempotent. Also backfills the
    `discovered` + `notes` columns if the table was created in an
    earlier version without them (ALTER ... IF NOT EXISTS)."""
    cur.execute(_SCHEMA_DDL)
    # Defensive ALTERs for older deployments where the table may exist
    # without the newer columns.
    for col_sql in (
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS discovered BOOLEAN DEFAULT FALSE",
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS notes JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE mcp_presence_listings ADD COLUMN IF NOT EXISTS submit_url TEXT",
    ):
        try:
            cur.execute(col_sql)
        except Exception:
            pass


def _seed_registries(cur) -> int:
    """Idempotent seed of SEED_REGISTRIES. Returns rows touched.

    Now also threads the per-seed `notes` blob (e.g. the
    `submission_backend_404` flag for MCPHive) on first insert.
    Existing rows are left alone — use the reseed-broken endpoint to
    force-update a row's URLs/notes."""
    inserted = 0
    # ★ 2026-07-27 — was ON CONFLICT DO NOTHING, which meant a listing_url
    # CORRECTED in this seed never reached an existing row. That is the root
    # cause of the recurring "Glama listing is broken" finding: the seed has
    # carried the right connectors URL for weeks while the live row kept the
    # stale https://glama.ai/mcp/servers/dchub, which 302s to a SEARCH page —
    # so registry_truth kept reading someone else's page and calling it ours.
    # The repo seed is reviewed and version-controlled, so it is the source of
    # truth for SEEDED rows; discovered rows are not in SEED_REGISTRIES and are
    # untouched. The WHERE clause keeps this a no-op when nothing changed.
    for r in SEED_REGISTRIES:
        try:
            notes_blob = json.dumps(r.get("notes") or {})
            cur.execute(
                """
                INSERT INTO mcp_presence_listings
                    (registry_name, listing_url, submit_url, discovered, notes)
                VALUES (%s, %s, %s, FALSE, %s::jsonb)
                ON CONFLICT (registry_name) DO UPDATE
                    SET listing_url = EXCLUDED.listing_url,
                        submit_url  = COALESCE(EXCLUDED.submit_url,
                                               mcp_presence_listings.submit_url)
                    WHERE mcp_presence_listings.listing_url
                          IS DISTINCT FROM EXCLUDED.listing_url
                """,
                (r["registry_name"], r["listing_url"],
                 r.get("submit_url"), notes_blob),
            )
            if cur.rowcount and cur.rowcount > 0:
                inserted += 1
        except Exception as e:
            logger.warning("mcp_presence: seed insert failed for %s: %s",
                           r.get("registry_name"), e)
    return inserted


# ── HTTP fetch (rate-limited) ─────────────────────────────────────────
_last_request_ts = 0.0

# Hosts known to rate-limit aggressively — sleep extra before the
# initial GET. Smithery returned 429 on the live crawl; pre-emptive
# delay keeps us under their threshold without needing a token bucket.
_AGGRESSIVE_HOSTS = {"smithery.ai"}
_PREFETCH_BACKOFF_S = 5.0
# Cap how long we'll wait on a Retry-After before giving up (some
# providers send unreasonable values like 3600).
_MAX_RETRY_AFTER_S = 15.0


def _polite_get(url: str) -> tuple[str | None, int | None]:
    """Rate-limited GET. Returns (html_text or None, status_code or None).

    Adds pre-emptive backoff for known-aggressive hosts (smithery.ai)
    and honors HTTP 429 Retry-After with one bounded retry."""
    global _last_request_ts
    elapsed = time.time() - _last_request_ts
    if elapsed < RATE_LIMIT_SLEEP_S:
        time.sleep(RATE_LIMIT_SLEEP_S - elapsed)
    # Aggressive-host pre-emptive delay (covers smithery.ai 429s)
    try:
        host = (urlparse(url).hostname or "").lower().lstrip("www.")
        if host in _AGGRESSIVE_HOSTS:
            time.sleep(_PREFETCH_BACKOFF_S)
    except Exception:
        pass
    _last_request_ts = time.time()
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            timeout=REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
        # 429 backoff + single retry
        if r.status_code == 429:
            retry_after = 0.0
            try:
                retry_after = float(r.headers.get("Retry-After", "0") or "0")
            except Exception:
                retry_after = 0.0
            wait = min(max(retry_after, _PREFETCH_BACKOFF_S),
                       _MAX_RETRY_AFTER_S)
            logger.info("mcp_presence: 429 on %s, sleeping %.1fs",
                        url, wait)
            time.sleep(wait)
            _last_request_ts = time.time()
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": USER_AGENT,
                             "Accept": "text/html,*/*"},
                    timeout=REQUEST_TIMEOUT_S,
                    allow_redirects=True,
                )
            except Exception as e:
                logger.info("mcp_presence: retry GET failed %s: %s",
                            url, e)
                return None, 429
        return (r.text if r.status_code == 200 else None), r.status_code
    except Exception as e:
        logger.info("mcp_presence: GET failed %s: %s", url, e)
        return None, None


# ── Extractors (defensive — never raise, always return None on failure)
def _safe_int(s: Any) -> int | None:
    try:
        return int(re.sub(r"[^\d]", "", str(s))) if s is not None else None
    except Exception:
        return None


def _safe_float(s: Any) -> float | None:
    try:
        return float(re.sub(r"[^\d.]", "", str(s))) if s is not None else None
    except Exception:
        return None


def _extractor_smithery(html: str) -> dict | None:
    """Smithery uses Next.js — tools shown as 'X tools' near server name;
    uptime tile labeled 'Uptime: 99.X%'. Both regex-extractable."""
    try:
        tools = None
        m = re.search(r"(\d+)\s*(?:tools?|functions?|capabilities)", html, re.I)
        if m:
            tools = _safe_int(m.group(1))
        uptime = None
        m = re.search(r"uptime[^0-9]{0,12}(\d{1,3}(?:\.\d+)?)\s*%", html, re.I)
        if m:
            uptime = _safe_float(m.group(1))
        last_updated = None
        m = re.search(r'"updatedAt"\s*:\s*"([^"]+)"', html)
        if m:
            last_updated = m.group(1)
        return {"tools": tools, "uptime": uptime,
                "last_updated": last_updated, "status": "ok"}
    except Exception:
        return None


def _extractor_mcphive(html: str) -> dict | None:
    """MCPHive lists tool count in a stats grid and shows a 'Last updated'
    timestamp in the sidebar."""
    try:
        tools = None
        m = re.search(r"(\d+)\s*tools?\s*(?:available|exposed|listed)?", html, re.I)
        if m:
            tools = _safe_int(m.group(1))
        last_updated = None
        m = re.search(r"last\s*updated[^<]{0,4}<[^>]+>([^<]+)<", html, re.I)
        if m:
            last_updated = m.group(1).strip()
        return {"tools": tools, "uptime": None,
                "last_updated": last_updated, "status": "ok"}
    except Exception:
        return None


def _extractor_lobehub(html: str) -> dict | None:
    """LobeHub uses a React shell; tool count appears in og:description
    meta + a 'Tools (N)' header."""
    try:
        tools = None
        m = re.search(r"Tools?\s*[\(\[]\s*(\d+)\s*[\)\]]", html)
        if m:
            tools = _safe_int(m.group(1))
        if tools is None:
            m = re.search(r'name="description"\s+content="[^"]*?(\d+)\s+tools?', html, re.I)
            if m:
                tools = _safe_int(m.group(1))
        return {"tools": tools, "uptime": None,
                "last_updated": None, "status": "ok"}
    except Exception:
        return None


def _extractor_glama(html: str) -> dict | None:
    """Glama auto-discovers tools — they render a 'X capabilities'
    counter and a quality_score badge."""
    try:
        tools = None
        m = re.search(r"(\d+)\s*(?:capabilit(?:y|ies)|tools?)", html, re.I)
        if m:
            tools = _safe_int(m.group(1))
        uptime = None
        m = re.search(r"quality[_\s-]?score[^0-9]{0,20}(\d{1,3}(?:\.\d+)?)", html, re.I)
        if m:
            uptime = _safe_float(m.group(1))
        last_updated = None
        m = re.search(r'"lastIndexed"\s*:\s*"([^"]+)"', html)
        if m:
            last_updated = m.group(1)
        return {"tools": tools, "uptime": uptime,
                "last_updated": last_updated, "status": "ok"}
    except Exception:
        return None


def _extractor_yellowmcp(html: str) -> dict | None:
    """YellowMCP uses a simple HTML layout — tool count in a <span
    class='tool-count'>N tools</span>; updated timestamp in <time
    datetime='...'>."""
    try:
        tools = None
        m = re.search(r"class=['\"][^'\"]*tool[-_ ]?count[^'\"]*['\"][^>]*>\s*(\d+)", html, re.I)
        if m:
            tools = _safe_int(m.group(1))
        if tools is None:
            m = re.search(r"(\d+)\s*tools?", html, re.I)
            if m:
                tools = _safe_int(m.group(1))
        last_updated = None
        m = re.search(r"<time[^>]+datetime=['\"]([^'\"]+)['\"]", html)
        if m:
            last_updated = m.group(1)
        return {"tools": tools, "uptime": None,
                "last_updated": last_updated, "status": "ok"}
    except Exception:
        return None


def _extractor_generic(html: str) -> dict | None:
    """Fallback used for the smaller registries. Just records the page
    existed and tries the most-common 'N tools' phrasing — never raises."""
    try:
        tools = None
        m = re.search(r"(\d+)\s*(?:tools?|functions?|endpoints?)", html, re.I)
        if m:
            tools = _safe_int(m.group(1))
        return {"tools": tools, "uptime": None,
                "last_updated": None, "status": "generic_fallback"}
    except Exception:
        return None


def _extractor_aggregator(html: str) -> dict | None:
    """★ 2026-08-15 — for aggregator pages (the awesome-mcp-servers raw
    README): one line per server, ~500 of which advertise the OTHER
    server's "N tools". Our entry publishes no count, so the generic
    extractor's first-match regex would record a stranger's number as
    dchub_metric_published_tools and flip drift_detected TRUE on every
    crawl. An aggregator has no count to read — presence (handled by the
    caller's identity check) is the only signal."""
    return {"tools": None, "uptime": None,
            "last_updated": None, "status": "aggregator_no_count"}


# ── SPA-aware extractors (2026-06-06) ─────────────────────────────────
# LobeHub / Glama / Smithery all return an empty <div id="root"> to a
# plain requests.get because they're React-rendered. Three strategies:
#   1) public REST API (Glama + Smithery expose JSON endpoints) — used
#      where available because it's cheapest and most reliable
#   2) __NEXT_DATA__ JSON blob embedded in the HTML — used for LobeHub
#      and MCPHive (both Next.js)
#   3) Playwright headless — deliberately NOT shipped; too heavy a dep
#      for what we get (extractors must stay self-contained + cheap)
# Each function returns the canonical extractor dict shape
# (tools/uptime/last_updated/status) or None on failure. NEVER raises.
_DCHUB_SMITHERY_SLUG = "azmartone67/dchub"
# 2026-07-09 FIX: was "dchub" → glama.ai/api/mcp/v1/servers/dchub 400s. The
# Glama server slug is namespaced (matches scripts/registry_monitor.py REPO_SLUG).
_DCHUB_GLAMA_SLUG    = "azmartone67/dchub-mcp-server"


def _glama_api_url(slug: str = None) -> str:
    """THE Glama resource URL. One origin, read AND write.

    ★2026-07-29 (Registry Surface Shell #42 lane 2): the 2026-07-09 fix above —
    "was 'dchub' -> 400s, the slug is namespaced" — was applied to the READER
    and never to the WRITER. update_listing_description() kept its own hardcoded
    copy with BOTH original defects: the un-namespaced slug AND a transposed
    path (/api/v1/mcp/ instead of /api/mcp/v1/). Measured 2026-07-29:

        reader  /api/mcp/v1/servers/azmartone67/dchub-mcp-server -> 200
        writer  /api/v1/mcp/servers/dchub                        -> 404

    So every Glama description PATCH we have ever issued went into a 404. The
    listing has been stuck on "33 tools ... 21,000+ facilities ... 232 US power
    markets" with an EMPTY tools array since at least 2026-07-04 — the rot that
    sync-tools-manifest.mjs documents by name — because the only thing that
    could have corrected it was aimed at a URL that does not exist.

    A second copy of a URL is a second thing to fix, and it is the copy nobody
    tests that stays broken. Both callers now derive from here.
    """
    return f"https://glama.ai/api/mcp/v1/servers/{slug or _DCHUB_GLAMA_SLUG}"
_DCHUB_LOBEHUB_SLUG  = "dchub-mcp-server"
_DCHUB_MCPHIVE_SLUG  = "dchub"


def _api_get_json(url: str, timeout: int | None = None) -> Any:
    """Polite GET that decodes JSON. Returns the parsed body or None.
    Re-uses the rate-limit ledger so this counts against MAX_REQUESTS_
    PER_RUN just like _polite_get."""
    global _last_request_ts
    elapsed = time.time() - _last_request_ts
    if elapsed < RATE_LIMIT_SLEEP_S:
        time.sleep(RATE_LIMIT_SLEEP_S - elapsed)
    try:
        host = (urlparse(url).hostname or "").lower().lstrip("www.")
        if host in _AGGRESSIVE_HOSTS:
            time.sleep(_PREFETCH_BACKOFF_S)
    except Exception:
        pass
    _last_request_ts = time.time()
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/json"},
            timeout=timeout or REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", "0") or "0")
            except Exception:
                wait = 0.0
            wait = min(max(wait, _PREFETCH_BACKOFF_S), _MAX_RETRY_AFTER_S)
            time.sleep(wait)
            _last_request_ts = time.time()
            r = requests.get(
                url,
                headers={"User-Agent": USER_AGENT,
                         "Accept": "application/json"},
                timeout=timeout or REQUEST_TIMEOUT_S,
                allow_redirects=True,
            )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.info("mcp_presence: API GET failed %s: %s", url, e)
        return None


def _extractor_smithery_api(slug: str = _DCHUB_SMITHERY_SLUG) -> dict | None:
    """Hit Smithery's public registry API directly. The
    registry.smithery.ai/servers/<slug> response carries a `tools` list +
    qualifiedName + updatedAt. Cheaper than rendering the SPA. Returns None if
    the API doesn't 200.
    2026-07-09 FIX: was `smithery.ai/api/v1/servers/<slug>` which 404s (that
    host serves the SPA, not the API) — so the crawler false-flagged Smithery as
    stale/discovered while the LIVE listing carries 70 tools. The working host is
    registry.smithery.ai (same one scripts/registry_monitor.py uses)."""
    try:
        # Smithery URL-encodes the leading '@' but the server canonicalizes
        url = (f"https://registry.smithery.ai/servers/"
               f"{requests.utils.quote(slug, safe='@/')}")
        data = _api_get_json(url)
        if not isinstance(data, dict):
            return None
        tools = None
        # The API has shipped a couple of shapes — try the common ones
        for key in ("tools", "toolList", "capabilities"):
            v = data.get(key)
            if isinstance(v, list):
                tools = len(v)
                break
        if tools is None and isinstance(data.get("toolCount"), int):
            tools = int(data["toolCount"])
        uptime = None
        for key in ("uptime", "uptimePct", "availability"):
            v = data.get(key)
            if isinstance(v, (int, float)):
                uptime = float(v)
                break
        last_updated = (data.get("updatedAt")
                        or data.get("lastUpdated")
                        or data.get("lastSeenAt"))
        return {"tools": tools, "uptime": uptime,
                "last_updated": last_updated, "status": "smithery_api"}
    except Exception:
        return None


def _extractor_glama_api(slug: str = _DCHUB_GLAMA_SLUG) -> dict | None:
    """Hit Glama's public REST API at https://glama.ai/api/mcp/v1/servers/
    <slug>. Returns the canonical extractor dict or None."""
    try:
        url = _glama_api_url(slug)  # single origin — see _glama_api_url()
        data = _api_get_json(url)
        if not isinstance(data, dict):
            return None
        tools = None
        for key in ("tools", "capabilities", "toolList"):
            v = data.get(key)
            if isinstance(v, list):
                tools = len(v)
                break
        if tools is None and isinstance(data.get("toolCount"), int):
            tools = int(data["toolCount"])
        uptime = None
        # Glama exposes a quality score 0..100 — record under uptime for
        # uniformity; the brain can decide how to triage it.
        qs = data.get("qualityScore") or data.get("quality_score")
        if isinstance(qs, (int, float)):
            uptime = float(qs)
        last_updated = (data.get("lastIndexedAt")
                        or data.get("lastIndexed")
                        or data.get("updatedAt"))
        return {"tools": tools, "uptime": uptime,
                "last_updated": last_updated, "status": "glama_api"}
    except Exception:
        return None


def _parse_next_data(html: str) -> Any:
    """Extract + parse the __NEXT_DATA__ JSON blob embedded in Next.js
    pages. Returns the decoded object or None. Never raises."""
    try:
        m = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html, re.S,
        )
        if not m:
            return None
        return json.loads(m.group(1))
    except Exception:
        return None


def _walk_for_keys(obj: Any, keys: tuple[str, ...]) -> Any:
    """Walk a nested dict/list looking for the first key in `keys` that
    has a non-None value. Bounded depth/breadth to keep cheap."""
    try:
        stack: list[Any] = [obj]
        seen_steps = 0
        while stack and seen_steps < 2000:
            seen_steps += 1
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in keys and v is not None:
                        return v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                for v in node:
                    if isinstance(v, (dict, list)):
                        stack.append(v)
    except Exception:
        return None
    return None


def _extractor_lobehub_next_data(html: str) -> dict | None:
    """LobeHub embeds the SSR'd page state in __NEXT_DATA__. We walk
    the JSON looking for the tools/capabilities list rather than
    chasing a brittle regex. Returns the canonical dict or None."""
    try:
        data = _parse_next_data(html)
        if data is None:
            return None
        tool_list = _walk_for_keys(data, ("tools", "capabilities",
                                          "toolList"))
        tools = None
        if isinstance(tool_list, list):
            tools = len(tool_list)
        elif isinstance(tool_list, int):
            tools = tool_list
        if tools is None:
            tc = _walk_for_keys(data, ("toolCount", "tools_count"))
            if isinstance(tc, int):
                tools = tc
        last_updated = _walk_for_keys(
            data, ("updatedAt", "lastUpdated", "lastIndexedAt"))
        if not isinstance(last_updated, str):
            last_updated = None
        return {"tools": tools, "uptime": None,
                "last_updated": last_updated, "status": "lobehub_next_data"}
    except Exception:
        return None


def _extractor_mcphive_next_data(html: str) -> dict | None:
    """MCPHive is Next.js too. Same __NEXT_DATA__ walk."""
    try:
        data = _parse_next_data(html)
        if data is None:
            return None
        tool_list = _walk_for_keys(data, ("tools", "capabilities",
                                          "toolList"))
        tools = None
        if isinstance(tool_list, list):
            tools = len(tool_list)
        elif isinstance(tool_list, int):
            tools = tool_list
        last_updated = _walk_for_keys(
            data, ("updatedAt", "lastUpdated", "lastSeenAt"))
        if not isinstance(last_updated, str):
            last_updated = None
        return {"tools": tools, "uptime": None,
                "last_updated": last_updated, "status": "mcphive_next_data"}
    except Exception:
        return None


_EXTRACTORS = {
    "smithery":  _extractor_smithery,
    "mcphive":   _extractor_mcphive,
    "lobehub":   _extractor_lobehub,
    "glama":     _extractor_glama,
    "yellowmcp": _extractor_yellowmcp,
    "awesome_mcp_servers": _extractor_aggregator,
}


# Registries whose canonical extraction path is a public API hit, not
# an HTML scrape. The crawler short-circuits the HTML fetch for these
# and goes straight to the API.
_API_EXTRACTORS = {
    "smithery": lambda: _extractor_smithery_api(_DCHUB_SMITHERY_SLUG),
    "glama":    lambda: _extractor_glama_api(_DCHUB_GLAMA_SLUG),
}


# Fall-back chain for SPA-rendered registries: when the primary
# extractor returns no tool count, try the __NEXT_DATA__ parser before
# giving up. (LobeHub + MCPHive both ship Next.js.)
_SPA_FALLBACKS = {
    "lobehub": _extractor_lobehub_next_data,
    "mcphive": _extractor_mcphive_next_data,
}


def _extract_for(registry_name: str, html: str) -> dict:
    """Look up the registry-specific extractor; fall through to the
    generic. Always returns a dict (never None) so callers don't have
    to special-case.

    For SPA-rendered registries we layer:
      1. registry-specific regex extractor (legacy)
      2. __NEXT_DATA__ JSON walk (new, more robust)
    and prefer whichever first returns a non-None tool count."""
    fn = _EXTRACTORS.get(registry_name, _extractor_generic)
    out = fn(html) or {}
    if out.get("tools") is None:
        fb = _SPA_FALLBACKS.get(registry_name)
        if fb is not None:
            try:
                fb_out = fb(html) or {}
                if fb_out.get("tools") is not None:
                    out = fb_out
            except Exception:
                pass
    return {
        "tools":        out.get("tools"),
        "uptime":       out.get("uptime"),
        "last_updated": out.get("last_updated"),
        "status":       out.get("status") or "ok",
    }


# ── Our actual tool count ─────────────────────────────────────────────
def _our_actual_tool_count() -> int | None:
    """Hit the live tool catalog at /api/v1/mcp/tools.json. Falls back
    to tier_registry import if the HTTP fetch fails (e.g. boot before
    routes register)."""
    try:
        base = os.environ.get(
            "MCP_HEALTH_BASE",
            "https://dchub-backend-production.up.railway.app",
        )
        r = requests.get(f"{base}/api/v1/mcp/tools.json",
                         headers={"User-Agent": USER_AGENT},
                         timeout=REQUEST_TIMEOUT_S)
        if r.status_code == 200:
            data = r.json()
            tools = data.get("tools") if isinstance(data, dict) else data
            if isinstance(tools, dict):
                # tools is sometimes a {tier: [tool, ...]} dict
                return sum(len(v) for v in tools.values() if isinstance(v, list))
            if isinstance(tools, list):
                return len(tools)
    except Exception as e:
        logger.info("mcp_presence: live tool count fetch failed: %s", e)
    # Fallback — count from the tier_registry module
    try:
        import tier_registry  # type: ignore
        if hasattr(tier_registry, "as_public_dict"):
            d = tier_registry.as_public_dict() or {}
            for k in ("total_tools", "tool_count", "mcp_tools"):
                if isinstance(d.get(k), int):
                    return int(d[k])
    except Exception:
        pass
    return None


# ── brain_findings writer ─────────────────────────────────────────────
def _write_brain_finding(cur, issue: str, url: str, detail: str,
                         count: int = 1) -> None:
    """Upsert into brain_findings via the canonical writer.

    2026-06-06: the old inline INSERT used seen_count + ON CONFLICT
    (issue, url) — neither exists on the LIVE table, so it failed
    silently inside this except for weeks. Now delegates to
    routes/brain_findings_writer which introspects the live schema,
    restores seen_count, and upserts constraint-agnostically."""
    try:
        from routes.brain_findings_writer import upsert_brain_finding
        upsert_brain_finding(cur, issue=issue, url=url, count=count,
                             detail=detail, detector="mcp_presence_crawler")
    except Exception as e:
        logger.warning("mcp_presence: brain_findings write failed: %s", e)


# ── Crawler ───────────────────────────────────────────────────────────
def crawl_mcp_presence() -> dict:
    """Crawl every registry in mcp_presence_listings, compare to our
    actual tool count, write drift findings. Returns a summary dict.

    Hard caps: MAX_REQUESTS_PER_RUN page fetches, 1s rate-limit between
    each. Never raises — every step is wrapped."""
    summary = {
        "checked": 0,
        "drift_found": 0,
        "stale_found": 0,
        "errors": 0,
        "registries": [],
    }
    conn = _db_conn()
    if not conn:
        summary["error"] = "db_unavailable"
        return summary

    actual_count = _our_actual_tool_count()
    summary["our_actual_tool_count"] = actual_count

    try:
        with conn.cursor() as cur:
            _ensure_schema(cur)
            # Seed defensively in case the table was empty.
            _seed_registries(cur)
            conn.commit()

            cur.execute(
                """
                SELECT id, registry_name, listing_url
                  FROM mcp_presence_listings
                 -- r-listed-refresh (2026-07-22): crawl not only UNDISCOVERED
                 -- registries (discovery) but also any LISTED one gone stale
                 -- (>3d since last crawl). Pre-fix the filter was
                 -- `discovered = FALSE` only, so once we were listed a registry
                 -- was NEVER re-scraped — its tool-count drift / staleness
                 -- (glama & smithery went ~20d stale; the official registry sat
                 -- at 30 vs 79 tools) could never self-heal. The loop writes
                 -- metrics/drift + last_crawled_at but never flips `discovered`,
                 -- so re-scraping a listed row is safe; oldest-first + LIMIT keeps
                 -- each run bounded (full coverage over a few daily runs).
                 WHERE COALESCE(discovered, FALSE) = FALSE
                    OR last_crawled_at IS NULL
                    OR last_crawled_at < NOW() - INTERVAL '3 days'
                 ORDER BY COALESCE(last_crawled_at, '1970-01-01'::timestamptz) ASC
                 LIMIT %s
                """,
                (MAX_REQUESTS_PER_RUN,),
            )
            rows = cur.fetchall()

            for row in rows:
                row_id, registry_name, listing_url = row[0], row[1], row[2]
                try:
                    extracted: dict | None = None
                    status_code: int | None = None
                    # API-first short-circuit (Smithery + Glama).
                    # SPA pages return empty <div id="root"> to a plain
                    # GET, but both registries expose a public JSON API.
                    api_fn = _API_EXTRACTORS.get(registry_name)
                    if api_fn is not None:
                        try:
                            api_out = api_fn()
                        except Exception:
                            api_out = None
                        if api_out and api_out.get("tools") is not None:
                            extracted = {
                                "tools":        api_out.get("tools"),
                                "uptime":       api_out.get("uptime"),
                                "last_updated": api_out.get("last_updated"),
                                "status":       api_out.get("status") or "ok",
                            }
                            status_code = 200  # API said ok

                    html: str | None = None
                    if extracted is None:
                        html, status_code = _polite_get(listing_url)
                        if not html:
                            summary["errors"] += 1
                            summary["registries"].append({
                                "registry": registry_name,
                                "status":   "fetch_failed",
                                "http":     status_code,
                            })
                            # Still bump last_crawled_at so we don't hammer
                            # a permanently-404 URL on every run.
                            try:
                                cur.execute(
                                    "UPDATE mcp_presence_listings "
                                    "   SET last_crawled_at = NOW(), "
                                    "       notes = jsonb_set(COALESCE(notes,'{}'::jsonb), "
                                    "                         '{last_http}', to_jsonb(%s::int)) "
                                    " WHERE id = %s",
                                    (int(status_code or 0), row_id),
                                )
                            except Exception:
                                note_swallowed_write("mcp_presence_listings", where="mcp_presence_crawler.crawl_mcp_presence")
                                pass
                            continue

                    if extracted is None:
                        extracted = _extract_for(registry_name, html or "")
                    listing_tools  = extracted.get("tools")
                    listing_uptime = extracted.get("uptime")
                    listing_last   = extracted.get("last_updated")

                    # Compute drift
                    drift = bool(
                        actual_count is not None
                        and listing_tools is not None
                        and listing_tools != actual_count
                    )

                    # 2026-07-03: PRESENCE. The direct listing crawl updated
                    # metrics but NEVER set discovered=True, so the distribution
                    # shell read 0/5 even for confirmed listings. A successful
                    # fetch of our known listing_url whose HTML mentions us IS
                    # the presence signal. Only upgrades False→True (a query-
                    # discovered True is never downgraded).
                    _h = (html or "").lower()
                    confirmed_present = bool(
                        html and ("dchub" in _h or "dc hub" in _h
                                  or "data center intelligence" in _h))

                    # Compute stale_days (best-effort — only if we
                    # successfully parsed a last_updated string)
                    stale_days = None
                    parsed_ts  = None
                    if listing_last:
                        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ",
                                    "%Y-%m-%dT%H:%M:%SZ",
                                    "%Y-%m-%dT%H:%M:%S",
                                    "%Y-%m-%d"):
                            try:
                                parsed_ts = _dt.datetime.strptime(
                                    listing_last[:len(fmt) + 5], fmt
                                ).replace(tzinfo=_dt.timezone.utc)
                                stale_days = (_dt.datetime.now(_dt.timezone.utc)
                                              - parsed_ts).days
                                break
                            except Exception:
                                continue

                    cur.execute(
                        """
                        UPDATE mcp_presence_listings
                           SET dchub_metric_published_tools = %s,
                               dchub_metric_uptime_pct      = %s,
                               dchub_metric_last_seen       = COALESCE(%s, dchub_metric_last_seen),
                               dchub_metric_stale_days      = %s,
                               our_actual_tool_count        = %s,
                               drift_detected               = %s,
                               discovered                   = COALESCE(discovered, FALSE) OR %s,
                               last_crawled_at              = NOW(),
                               notes = jsonb_set(
                                   jsonb_set(COALESCE(notes,'{}'::jsonb),
                                             '{extractor_status}',
                                             to_jsonb(%s::text)),
                                   '{last_updated_raw}',
                                   to_jsonb(%s::text))
                         WHERE id = %s
                        """,
                        (
                            listing_tools, listing_uptime, parsed_ts,
                            stale_days, actual_count, drift,
                            confirmed_present,
                            extracted.get("status") or "ok",
                            listing_last or "",
                            row_id,
                        ),
                    )

                    summary["checked"] += 1
                    if drift:
                        summary["drift_found"] += 1
                        _write_brain_finding(
                            cur,
                            issue=f"mcp_presence_drift:{registry_name}",
                            url=listing_url,
                            detail=(
                                f"{registry_name} shows {listing_tools} tools "
                                f"but DC Hub exposes {actual_count}. "
                                f"Update the listing or re-submit."
                            ),
                            count=int(abs((actual_count or 0) - (listing_tools or 0))),
                        )
                    if stale_days is not None and stale_days >= STALE_DAYS_THRESHOLD:
                        summary["stale_found"] += 1
                        _write_brain_finding(
                            cur,
                            issue=f"mcp_presence_stale:{registry_name}",
                            url=listing_url,
                            detail=(
                                f"{registry_name} last updated {stale_days}d "
                                f"ago (threshold={STALE_DAYS_THRESHOLD}d). "
                                f"Re-submit or refresh."
                            ),
                            count=stale_days,
                        )

                    summary["registries"].append({
                        "registry":      registry_name,
                        "listing_tools": listing_tools,
                        "actual_tools":  actual_count,
                        "drift":         drift,
                        "stale_days":    stale_days,
                        "extractor":     extracted.get("status"),
                    })
                except Exception as e:
                    summary["errors"] += 1
                    logger.warning("mcp_presence: per-registry error %s: %s",
                                   registry_name, e)

            conn.commit()
    except Exception as e:
        summary["error"] = str(e)[:200]
        logger.warning("mcp_presence: crawl error: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return summary


# ── Discoverer ────────────────────────────────────────────────────────
DISCOVERY_QUERIES = [
    "MCP server directory",
    "Model Context Protocol registry",
    "MCP marketplace",
    "best MCP servers list",
]

# Obvious negative-list — we DON'T want to flag these as "new MCP
# registries"; they're general-purpose sites that always show up in
# search results.
_DISCOVERY_DOMAIN_DENYLIST = {
    "google.com", "youtube.com", "twitter.com", "x.com", "linkedin.com",
    "medium.com", "dev.to", "stackoverflow.com", "reddit.com",
    "github.io", "vercel.app", "netlify.app",
    "wikipedia.org", "anthropic.com", "openai.com",
}


def _extract_search_result_domains(html: str) -> list[str]:
    """Parse a Google SERP HTML and pull out the result-link domains.
    Defensive — returns [] on failure."""
    out: list[str] = []
    try:
        for m in re.finditer(r'href="(https?://[^"]+)"', html):
            url = m.group(1)
            try:
                host = urlparse(url).hostname or ""
                host = host.lower().lstrip("www.")
                if not host:
                    continue
                if any(host.endswith(d) for d in _DISCOVERY_DOMAIN_DENYLIST):
                    continue
                if host in out:
                    continue
                out.append(host)
            except Exception:
                continue
    except Exception:
        pass
    return out[:25]


def _sniff_submit_page(domain: str) -> str | None:
    """Heuristic — visit common submit-page paths on a candidate domain
    and return the first one that 200s. Never raises."""
    for path in ("/submit", "/add", "/new", "/servers/new",
                 "/mcp/submit", "/list-server"):
        url = f"https://{domain}{path}"
        try:
            _polite_get_count_check()
            r = requests.head(
                url, headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT_S, allow_redirects=True,
            )
            if r.status_code == 200:
                return url
        except Exception:
            continue
    return None


_discovery_request_count = 0


def _polite_get_count_check() -> None:
    """Counter helper so the discoverer also respects MAX_REQUESTS_PER_RUN."""
    global _discovery_request_count
    _discovery_request_count += 1
    if _discovery_request_count > MAX_REQUESTS_PER_RUN:
        raise RuntimeError("discovery_rate_cap_hit")
    elapsed = time.time() - _last_request_ts
    if elapsed < RATE_LIMIT_SLEEP_S:
        time.sleep(RATE_LIMIT_SLEEP_S - elapsed)


def discover_new_mcp_registries() -> dict:
    """Run discovery queries, filter for unknown domains, sniff submit
    pages, file brain_findings. Returns a summary dict. Never raises."""
    global _discovery_request_count
    _discovery_request_count = 0
    summary = {
        "queries_run":      0,
        "candidates_seen":  0,
        "new_domains":      0,
        "submit_pages":     0,
        "errors":           0,
        "discovered":       [],
    }
    conn = _db_conn()
    if not conn:
        summary["error"] = "db_unavailable"
        return summary

    try:
        with conn.cursor() as cur:
            _ensure_schema(cur)

            # Pull the set of domains we already know about.
            cur.execute("SELECT listing_url FROM mcp_presence_listings")
            known_domains: set[str] = set()
            for (lurl,) in cur.fetchall():
                try:
                    h = (urlparse(lurl).hostname or "").lower().lstrip("www.")
                    if h:
                        known_domains.add(h)
                        # Also strip the leftmost subdomain so we catch
                        # both registry.modelcontextprotocol.io and
                        # modelcontextprotocol.io.
                        parts = h.split(".")
                        if len(parts) > 2:
                            known_domains.add(".".join(parts[-2:]))
                except Exception:
                    continue

            for query in DISCOVERY_QUERIES:
                if _discovery_request_count >= MAX_REQUESTS_PER_RUN:
                    break
                summary["queries_run"] += 1
                serp_url = (
                    "https://www.google.com/search?q="
                    + requests.utils.quote(query)
                    + "&num=20"
                )
                try:
                    html, _ = _polite_get(serp_url)
                    _discovery_request_count += 1
                except Exception:
                    summary["errors"] += 1
                    continue
                if not html:
                    summary["errors"] += 1
                    continue

                domains = _extract_search_result_domains(html)
                summary["candidates_seen"] += len(domains)
                for dom in domains:
                    if _discovery_request_count >= MAX_REQUESTS_PER_RUN:
                        break
                    # Skip already-known
                    if dom in known_domains:
                        continue
                    # Skip if the parent domain is already known
                    parts = dom.split(".")
                    if len(parts) > 2 and ".".join(parts[-2:]) in known_domains:
                        continue
                    # Looks like a candidate
                    summary["new_domains"] += 1
                    submit = None
                    try:
                        submit = _sniff_submit_page(dom)
                    except RuntimeError:
                        # Hit the request cap inside the sniffer
                        break
                    except Exception:
                        submit = None
                    if submit:
                        summary["submit_pages"] += 1

                    # Persist as a discovered=true row (idempotent)
                    try:
                        slug = re.sub(r"[^a-z0-9]+", "_",
                                      dom.replace(".", "_")).strip("_")
                        cur.execute(
                            """
                            INSERT INTO mcp_presence_listings
                                (registry_name, listing_url, submit_url,
                                 discovered, notes)
                            VALUES (%s, %s, %s, TRUE, %s)
                            ON CONFLICT (registry_name) DO UPDATE
                                SET submit_url = COALESCE(EXCLUDED.submit_url,
                                                          mcp_presence_listings.submit_url),
                                    notes = mcp_presence_listings.notes
                                          || %s::jsonb
                            """,
                            (
                                f"discovered_{slug}",
                                f"https://{dom}",
                                submit,
                                json.dumps({"discovered_via_query": query}),
                                json.dumps({"last_seen_query": query}),
                            ),
                        )
                    except Exception as e:
                        logger.warning("mcp_presence: discover insert failed for %s: %s",
                                       dom, e)
                        summary["errors"] += 1
                        continue

                    _write_brain_finding(
                        cur,
                        issue="mcp_registry_discovered",
                        url=f"https://{dom}",
                        detail=(
                            f"New MCP registry candidate '{dom}' found via "
                            f"query '{query}'. Submit page: "
                            f"{submit or 'not auto-detected'}. Triage and "
                            f"add to seed list if real."
                        ),
                        count=1,
                    )
                    summary["discovered"].append({
                        "domain":     dom,
                        "via_query":  query,
                        "submit_url": submit,
                    })
            conn.commit()
    except Exception as e:
        summary["error"] = str(e)[:200]
        logger.warning("mcp_presence: discover error: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return summary


# ── Status snapshot (PUBLIC) ──────────────────────────────────────────
def _status_snapshot() -> dict:
    """Return the listing state for the public /status endpoint.
    Includes a 'next_action' hint per row so the operator (or the brain)
    can act without re-deriving logic. Never raises."""
    out = {
        "as_of":           _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "public":          True,
        "leaks_secrets":   False,
        "total_listings":  0,
        "drifted":         0,
        "stale":           0,
        "healthy":         0,
        "never_crawled":   0,
        "discovered_pending": 0,
        "listings":        [],
    }
    conn = _db_conn()
    if not conn:
        out["error"] = "db_unavailable"
        return out
    try:
        with conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                """
                SELECT registry_name, listing_url, submit_url,
                       dchub_metric_published_tools, our_actual_tool_count,
                       dchub_metric_stale_days, drift_detected,
                       last_crawled_at, COALESCE(discovered, FALSE)
                  FROM mcp_presence_listings
                 ORDER BY drift_detected DESC NULLS LAST,
                          dchub_metric_stale_days DESC NULLS LAST,
                          registry_name ASC
                """
            )
            for row in cur.fetchall():
                (registry_name, listing_url, submit_url,
                 published, actual, stale_days, drift,
                 last_crawled, discovered) = row
                out["total_listings"] += 1
                if discovered:
                    out["discovered_pending"] += 1
                if drift:
                    out["drifted"] += 1
                if (stale_days or 0) >= STALE_DAYS_THRESHOLD:
                    out["stale"] += 1
                if last_crawled is None:
                    out["never_crawled"] += 1
                if (not drift and not discovered
                        and (stale_days or 0) < STALE_DAYS_THRESHOLD
                        and last_crawled is not None):
                    out["healthy"] += 1

                # Build the human next_action hint
                if discovered:
                    hint = (f"Triage discovered registry: "
                            f"submit DC Hub at {submit_url or listing_url}")
                elif drift and actual is not None and published is not None:
                    hint = (f"Update {registry_name} (lists {published} tools, "
                            f"DC Hub has {actual})")
                elif (stale_days or 0) >= STALE_DAYS_THRESHOLD:
                    hint = (f"Re-submit {registry_name} (last seen "
                            f"{stale_days}d ago)")
                elif last_crawled is None:
                    hint = f"First-time crawl pending for {registry_name}"
                else:
                    hint = f"{registry_name} healthy"

                out["listings"].append({
                    "registry":         registry_name,
                    "listing_url":      listing_url,
                    "submit_url":       submit_url,
                    "published_tools":  published,
                    "actual_tools":     actual,
                    "stale_days":       stale_days,
                    "drift_detected":   bool(drift),
                    "discovered":       bool(discovered),
                    "last_crawled_at":  (last_crawled.strftime("%Y-%m-%dT%H:%M:%SZ")
                                         if last_crawled else None),
                    "next_action":      hint,
                })
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# ── Endpoints ─────────────────────────────────────────────────────────
@mcp_presence_crawler_bp.route(
    "/api/v1/admin/mcp-presence/crawl", methods=["POST"])
def crawl_endpoint():
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    result = crawl_mcp_presence()
    return jsonify({"ok": True, **result}), 200


@mcp_presence_crawler_bp.route(
    "/api/v1/admin/mcp-presence/seed", methods=["POST"])
def seed_endpoint():
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    conn = _db_conn()
    if not conn:
        return jsonify({"ok": False, "error": "db_unavailable"}), 503
    try:
        with conn.cursor() as cur:
            _ensure_schema(cur)
            inserted = _seed_registries(cur)
            conn.commit()
        return jsonify({
            "ok":       True,
            "seeded":   inserted,
            "total":    len(SEED_REGISTRIES),
            "registry_names": [r["registry_name"] for r in SEED_REGISTRIES],
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@mcp_presence_crawler_bp.route(
    "/api/v1/admin/mcp-presence/discover", methods=["POST"])
def discover_endpoint():
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    result = discover_new_mcp_registries()
    return jsonify({"ok": True, **result}), 200


# ── Force-reseed the 4 broken-URL rows (2026-06-06) ──────────────────
# The first live crawl found mcphive / yellowmcp / mcp_official_registry
# all returning 404 because the seed URLs were wrong, and Smithery
# returning 429 because we hammered it. The standard _seed_registries
# call is INSERT … ON CONFLICT DO NOTHING (won't update existing rows),
# so we expose this idempotent UPDATE-only fixup so the brain can call
# it once after deploy and not need a row delete first.
RESEED_BROKEN_REGISTRIES: list[dict] = [
    {
        "registry_name": "mcphive",
        "listing_url":   "https://mcphive.com/",
        "submit_url":    None,
        "notes_patch":   {"submission_backend_404": True,
                          "reseed_reason": "POST /scripts/save_submission.php "
                                            "returns 404 — site abandoned for "
                                            "new listings"},
    },
    {
        "registry_name": "yellowmcp",
        "listing_url":   "https://yellowmcp.com/servers/cloud-dchub-mcp-server",
        "submit_url":    "https://yellowmcp.com/submit-mcp",
        "notes_patch":   {"reseed_reason": "2026-07-18: /mcp/dchub 404'd in turn "
                                            "(the 06-06 fix never reached the live "
                                            "row — reseed was never invoked); live "
                                            "URL verified via homepage href scan"},
    },
    {
        "registry_name": "mcp_so",
        "listing_url":   "https://mcp.so/servers/dchub-mcp-server",
        "submit_url":    "https://mcp.so/submit",
        "notes_patch":   {"reseed_reason": "2026-07-18: mcp.so restructured "
                                            "/server/<name> → /servers/<slug>; "
                                            "canonical listing verified via "
                                            "sitemap sweep (the /servers/"
                                            "dchub-backend row is the scraped "
                                            "duplicate, tracked separately)"},
    },
    {
        "registry_name": "mcp_official_registry",
        "listing_url":   "https://registry.modelcontextprotocol.io/v0/servers?search=cloud.dchub",
        "submit_url":    "https://github.com/modelcontextprotocol/registry",
        "notes_patch":   {"reseed_reason": "2026-07-09: registry.mcp.so now DEAD "
                                            "(000); official registry resolves + "
                                            "carries our live v2.4.5 entry"},
    },
    {
        "registry_name": "smithery",
        "listing_url":   "https://smithery.ai/servers/azmartone67/dchub",
        "submit_url":    "https://smithery.ai/new",
        "notes_patch":   {"reseed_reason": "added pre-emptive 5s backoff + "
                                            "429 Retry-After handling in "
                                            "_polite_get"},
    },
    {
        "registry_name": "lobehub",
        "listing_url":   "https://market.lobehub.com/s/plugins/azmartone67-dchub-mcp-server",
        "submit_url":    "https://lobehub.com/mcp/submit",
        "notes_patch":   {"reseed_reason": "2026-08-15: LobeHub re-slugged "
                                            "the listing to azmartone67-dchub-"
                                            "mcp-server and serves it from "
                                            "market.lobehub.com (the lobehub"
                                            ".com/mcp vanity path 302s there; "
                                            "old slug 404s). Verified live "
                                            "with full DC Hub identity."},
    },
]


def _reseed_broken(cur) -> list[dict]:
    """Idempotent UPDATE for the 4 broken-URL rows. Returns per-row
    status. Defensive — never raises."""
    out: list[dict] = []
    for r in RESEED_BROKEN_REGISTRIES:
        try:
            cur.execute(
                """
                UPDATE mcp_presence_listings
                   SET listing_url = %s,
                       submit_url  = %s,
                       notes       = COALESCE(notes, '{}'::jsonb) || %s::jsonb,
                       last_crawled_at = NULL
                 WHERE registry_name = %s
                """,
                (r["listing_url"], r.get("submit_url"),
                 json.dumps(r.get("notes_patch") or {}),
                 r["registry_name"]),
            )
            out.append({
                "registry": r["registry_name"],
                "updated_rows": cur.rowcount or 0,
                "listing_url": r["listing_url"],
                "submit_url":  r.get("submit_url"),
            })
        except Exception as e:
            out.append({
                "registry": r["registry_name"],
                "error":    str(e)[:200],
            })
    return out


@mcp_presence_crawler_bp.route(
    "/api/v1/admin/mcp-presence/reseed-broken", methods=["POST"])
def reseed_broken_endpoint():
    """Idempotent: UPDATE the 4 broken-URL rows
    (mcphive/yellowmcp/mcp_official_registry/smithery) with corrected
    URLs + notes. Also files the canonical
    mcp_registry_unreachable:mcphive brain_findings row so the brain
    knows MCPHive's submission backend is dead."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    conn = _db_conn()
    if not conn:
        return jsonify({"ok": False, "error": "db_unavailable"}), 503
    try:
        with conn.cursor() as cur:
            _ensure_schema(cur)
            results = _reseed_broken(cur)
            # File the canonical MCPHive unreachable finding so the
            # brain stops queueing retries.
            try:
                _write_brain_finding(
                    cur,
                    issue="mcp_registry_unreachable:mcphive",
                    url="https://mcphive.com/submit",
                    detail=(
                        "MCPHive submission endpoint POST "
                        "/scripts/save_submission.php returns 404 — "
                        "site abandoned for new listings. DC Hub "
                        "absent from servers + clients lists. No "
                        "retry path; auto-submitter will skip via "
                        "notes.submission_backend_404=true."
                    ),
                    count=1,
                )
            except Exception as e:
                logger.warning("mcp_presence: brain_finding write for "
                               "mcphive unreachable failed: %s", e)
            conn.commit()
        return jsonify({
            "ok":      True,
            "results": results,
            "count":   len(results),
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── URL-drift rediscovery (r-url-rediscovery, 2026-07-18) ─────────────
# mcp.so restructured /server/<name> → /servers/<slug> and the mcp_so row
# sat on a dead URL for 15 days with nothing owning the fix:
# crawl_mcp_presence records notes.last_http and moves on,
# auto_fix_all_drifted only sweeps drift_detected=TRUE (copy drift), and
# white_glove_propagation probes listing COPY — a moved URL is invisible
# to all three. Hand-authored fixes rot too: the standard seed is
# INSERT-only, and reseed-broken was never invoked after its 2026-06-06
# entries landed (yellowmcp's live row still held the URL that entry was
# written to replace). This leg closes the loop on the daily auto-fix
# slot:
#   1. pick listings whose last crawl hit 403/404/410;
#   2. sweep the registry's OWN surfaces for our slug — sitemap.xml
#      (robots.txt-aware, sitemapindex-aware, server-ish children first)
#      then homepage hrefs (yellowmcp has no sitemap);
#   3. verify each candidate actually serves OUR listing (200 + presence
#      signal), rank, and self-heal listing_url in the DB — the DB row
#      is what crawl_mcp_presence reads, so no deploy is needed;
#   4. file the canonical brain finding either way: url_moved (so the
#      brain can PR the hardcoded seeds/targets) or listing_gone
#      (owner-step resubmission via submit_url).
# Kill switch: MCP_URL_REDISCOVERY_DISABLE=1. Politeness: ≤3 listings
# per run, ≤30 sitemap fetches + ≤4 candidate verifies per listing, all
# through the rate-limited _polite_get.
REDISCOVER_KILL_ENV = "MCP_URL_REDISCOVERY_DISABLE"
_REDISCOVER_STATUSES = (403, 404, 410)
_REDISCOVER_MAX_ROWS = 3
_REDISCOVER_MAX_SITEMAP_FETCHES = 30
_REDISCOVER_MAX_VERIFY = 4
_REDISCOVER_GONE_COOLDOWN_DAYS = 7
# Substrings that mark a URL as plausibly OUR listing.
_REDISCOVER_SLUG_SIGNALS = ("dchub", "dc-hub", "dc_hub")
# Substrings that confirm a fetched page actually serves our listing.
_REDISCOVER_PAGE_SIGNALS = ("dchub", "dc hub", "data center intelligence")
# Domains where a 404 means something else entirely (moved repo, API
# search endpoint) — never sitemap-sweep these.
_REDISCOVER_SKIP_HOSTS = ("github.com", "registry.modelcontextprotocol.io")


def _rediscover_disabled() -> bool:
    return (os.environ.get(REDISCOVER_KILL_ENV) or "").strip().lower() in (
        "1", "true", "yes")


def _extract_locs(xml_text: str) -> list[str]:
    """Pure: every <loc> value from a sitemap / sitemapindex document.
    hreflang alternates live in xhtml:link attributes, not <loc>, so
    this naturally returns only canonical URLs.

    XML entities are unescaped — mcp.so's sitemapindex children look like
    `sitemap.xml?section=servers&amp;page=5`, and fetching the raw form
    mangles the query (`amp;page=5`), silently serving page 1 for every
    child. The first live dry-run declared our mcp.so listing GONE
    because of exactly this."""
    if not xml_text:
        return []
    import html as _html_mod
    return [_html_mod.unescape(m.strip())
            for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml_text)]


def _looks_like_sitemap(url: str) -> bool:
    """Pure: child-sitemap heuristic for sitemapindex <loc> entries."""
    u = url.lower()
    return u.endswith(".xml") or "sitemap" in u


def _slug_candidates(urls: list[str]) -> list[str]:
    """Pure: URLs that plausibly point at OUR listing, deduped in order."""
    out: list[str] = []
    for u in urls:
        lu = u.lower()
        if any(s in lu for s in _REDISCOVER_SLUG_SIGNALS) and u not in out:
            out.append(u)
    return out


def _sitemap_page_urls(host: str) -> list[str]:
    """All page URLs advertised by <host>'s sitemap(s), robots.txt-aware.
    Child sitemaps whose URL hints at 'server' are fetched first so the
    fetch budget lands on the section our listing lives in."""
    fetches = 0
    roots: list[str] = []
    txt, _ = _polite_get(f"https://{host}/robots.txt")
    fetches += 1
    if txt:
        for ln in txt.splitlines():
            if ln.lower().startswith("sitemap:"):
                roots.append(ln.split(":", 1)[1].strip())
    if not roots:
        roots = [f"https://{host}/sitemap.xml"]
    pages: list[str] = []
    queue = list(roots)
    seen: set[str] = set()
    while queue and fetches < _REDISCOVER_MAX_SITEMAP_FETCHES:
        sm_url = queue.pop(0)
        if sm_url in seen:
            continue
        seen.add(sm_url)
        xml, _st = _polite_get(sm_url)
        fetches += 1
        children: list[str] = []
        for loc in _extract_locs(xml or ""):
            if _looks_like_sitemap(loc):
                children.append(loc)
            else:
                pages.append(loc)
        children.sort(key=lambda u: 0 if "server" in u.lower() else 1)
        queue.extend(children)
    return pages


def _homepage_link_urls(host: str) -> list[str]:
    """Fallback for sitemap-less directories (yellowmcp): every href on
    the homepage, resolved absolute."""
    from urllib.parse import urljoin
    html, st = _polite_get(f"https://{host}/")
    if not html or st != 200:
        return []
    base = f"https://{host}/"
    return [urljoin(base, h)
            for h in re.findall(r'href="([^"#]+)"', html)]


def _candidate_rank_key(score: int, url: str) -> tuple:
    """Pure: sort key for verified candidates. Slug beats page-text
    score — our published server name is dchub-mcp-server /
    cloud.dchub/mcp-server on every registry, while scraped duplicates
    (mcp.so's /servers/dchub-backend, indexed from the repo README)
    out-SCORE the canonical page because the README is full of the
    literal string "dchub" (420 vs 298 measured 2026-07-18)."""
    return (0 if re.search(r"mcp[-_]server", url.lower()) else 1,
            -score, len(url))


def _verify_listing_candidate(url: str) -> int | None:
    """Fetch a candidate; return a rank score if it serves OUR listing
    (200 + presence signal), else None. Higher score = stronger page."""
    html, st = _polite_get(url)
    if not html or st != 200:
        return None
    t = html.lower()
    if not any(s in t for s in _REDISCOVER_PAGE_SIGNALS):
        return None
    return t.count("data center intelligence") * 10 + t.count("dchub")


def rediscover_moved_listings(dry_run: bool = True,
                              max_rows: int = _REDISCOVER_MAX_ROWS) -> dict:
    """Sweep 403/404/410 listings and self-heal moved URLs. Defensive —
    never raises. Wired into the daily mcp_presence_auto_fix slot."""
    summary = {"checked": 0, "healed": 0, "confirmed": 0, "gone": 0,
               "skipped": 0, "errors": 0, "dry_run": bool(dry_run),
               "results": []}
    if _rediscover_disabled():
        summary["disabled"] = True
        return summary
    conn = _db_conn()
    if not conn:
        summary["error"] = "db_unavailable"
        return summary
    try:
        with conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                """
                SELECT id, registry_name, listing_url, submit_url, notes
                  FROM mcp_presence_listings
                 WHERE COALESCE(notes->>'last_http', '') ~ '^[0-9]+$'
                   AND (notes->>'last_http')::int = ANY(%s)
                 ORDER BY last_crawled_at ASC NULLS FIRST
                """,
                (list(_REDISCOVER_STATUSES),),
            )
            rows = cur.fetchall()
    except Exception as e:
        summary["error"] = str(e)[:200]
        try:
            conn.close()
        except Exception:
            pass
        return summary

    now = _dt.datetime.now(_dt.timezone.utc)
    for row_id, registry, listing_url, submit_url, notes in rows:
        if summary["checked"] >= max_rows:
            break
        notes = notes or {}
        host = (urlparse(listing_url).hostname or "").lower()
        if not host or any(host.endswith(h) for h in _REDISCOVER_SKIP_HOSTS):
            summary["skipped"] += 1
            continue
        # Cooldown: a listing already declared gone gets re-checked
        # weekly, not daily.
        gone_at = notes.get("rediscover_gone_at")
        if gone_at:
            try:
                parsed = _dt.datetime.fromisoformat(str(gone_at))
                if (now - parsed).days < _REDISCOVER_GONE_COOLDOWN_DAYS:
                    summary["skipped"] += 1
                    continue
            except Exception:
                pass
        summary["checked"] += 1
        result = {"registry": registry, "old_url": listing_url}
        try:
            pages = _sitemap_page_urls(host)
            candidates = _slug_candidates(pages)
            via = "sitemap"
            if not candidates:
                candidates = _slug_candidates(_homepage_link_urls(host))
                via = "homepage"
            scored: list[tuple[int, str]] = []
            for cand in candidates[:_REDISCOVER_MAX_VERIFY]:
                score = _verify_listing_candidate(cand)
                if score is not None:
                    scored.append((score, cand))
            result["candidates_seen"] = len(candidates)
            result["via"] = via
            if scored:
                scored.sort(key=lambda t: _candidate_rank_key(t[0], t[1]))
                best = scored[0][1]
                if best.rstrip("/") == listing_url.rstrip("/"):
                    # Same URL verifies fine now — the recorded 403/404
                    # was transient or a bot-block. Clear the marker so
                    # we don't re-sweep daily; the crawler re-records it
                    # if the URL fails again.
                    result["outcome"] = "url_confirmed"
                    summary["confirmed"] += 1
                    if not dry_run:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE mcp_presence_listings
                                   SET notes = COALESCE(notes,'{}'::jsonb)
                                               || %s::jsonb
                                 WHERE id = %s
                                """,
                                (json.dumps({
                                    "last_http": None,
                                    "rediscover_confirmed_at":
                                        now.isoformat()}),
                                 row_id),
                            )
                        conn.commit()
                else:
                    result["outcome"] = "healed"
                    result["new_url"] = best
                    summary["healed"] += 1
                    if not dry_run:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE mcp_presence_listings
                                   SET listing_url = %s,
                                       last_crawled_at = NULL,
                                       notes = COALESCE(notes,'{}'::jsonb)
                                               || %s::jsonb
                                 WHERE id = %s
                                """,
                                (best,
                                 json.dumps({
                                     "last_http": None,
                                     "url_move": {"from": listing_url,
                                                  "to": best,
                                                  "at": now.isoformat()}}),
                                 row_id),
                            )
                            _write_brain_finding(
                                cur,
                                issue=f"mcp_registry_url_moved:{registry}",
                                url=best,
                                detail=(
                                    f"{registry} listing moved: "
                                    f"{listing_url} → {best} (found via "
                                    f"{via}, verified 200 + our copy). DB "
                                    "row self-healed; grep the repo for "
                                    "the OLD URL and update any hardcoded "
                                    "copies (mcp_presence_crawler "
                                    "SEED/RESEED lists, agent_broadcast_"
                                    "loop._TARGETS, mcp_standing, "
                                    "brain_ecosystem_watch, "
                                    "mcp_registry_outreach)."),
                            )
                        conn.commit()
            elif _verify_listing_candidate(listing_url) is not None:
                # No sweep candidate, but the CURRENT URL serves our
                # listing fine right now — the recorded failure (or the
                # sweep itself) was transient. Never file "gone" over a
                # flake; clear the marker and move on.
                result["outcome"] = "url_confirmed"
                summary["confirmed"] += 1
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE mcp_presence_listings
                               SET notes = COALESCE(notes,'{}'::jsonb)
                                           || %s::jsonb
                             WHERE id = %s
                            """,
                            (json.dumps({
                                "last_http": None,
                                "rediscover_confirmed_at":
                                    now.isoformat()}),
                             row_id),
                        )
                    conn.commit()
            else:
                result["outcome"] = "gone"
                summary["gone"] += 1
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE mcp_presence_listings
                               SET notes = COALESCE(notes,'{}'::jsonb)
                                           || %s::jsonb
                             WHERE id = %s
                            """,
                            (json.dumps({"rediscover_gone_at":
                                         now.isoformat()}),
                             row_id),
                        )
                        _write_brain_finding(
                            cur,
                            issue=f"mcp_registry_listing_gone:{registry}",
                            url=listing_url,
                            detail=(
                                f"{registry} listing {listing_url} is "
                                f"dead and rediscovery found no candidate "
                                f"(swept {len(pages)} sitemap URLs + "
                                "homepage hrefs for our slug). Likely "
                                "delisted — resubmission needed via "
                                f"{submit_url or 'unknown submit URL'} "
                                "(owner step)."),
                        )
                    conn.commit()
        except Exception as e:
            summary["errors"] += 1
            result["error"] = str(e)[:160]
            logger.warning("mcp_presence rediscovery: %s failed: %s",
                           registry, e)
        summary["results"].append(result)
    try:
        conn.close()
    except Exception:
        pass
    return summary


@mcp_presence_crawler_bp.route(
    "/api/v1/admin/mcp-presence/rediscover", methods=["POST"])
def rediscover_endpoint():
    """Sweep dead-URL listings and self-heal moved ones. ?dry_run=0 to
    actually write (default dry_run=1 for manual pokes; the daily
    auto-fix slot runs live)."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    raw = (request.args.get("dry_run")
           or request.args.get("dryRun") or "1").strip().lower()
    dry_run = raw not in ("0", "false", "no", "off")
    result = rediscover_moved_listings(dry_run=dry_run)
    return jsonify({"ok": True, **result}), 200


@mcp_presence_crawler_bp.route(
    "/api/v1/mcp-presence/status", methods=["GET"])
def status_endpoint():
    """PUBLIC — operational state of every registry listing. Mirrors
    the publisher_status.py philosophy: state only, no secrets. The
    operator (or any consumer) can see at a glance which registries
    need attention and what the next action is."""
    from flask import make_response
    payload = _status_snapshot()
    resp = make_response(jsonify(payload), 200)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# ── /recent — competitor signal channel for Brain L6 (2026-06-07) ────
# Brain L6 Strategic Synthesis self-critique on its first live run flagged
# "Competitor signal context is empty, so all competitor_lacks entries are
# interpolated from tool names rather than cited evidence." This endpoint
# closes that loop. Aliased at both:
#     /api/v1/mcp-presence/recent  (matches the existing /status path style)
#     /api/v1/mcp/presence/recent  (path the Brain L6 planner already calls)
# Returns the last N days of crawl results + recently-discovered registries
# + the canonical drift/stale/healthy histogram in a single JSON envelope
# the planner can drop straight into its prompt.
_RECENT_DEFAULT_DAYS = 30
_RECENT_MAX_DAYS = 180


def _recent_snapshot(days: int) -> dict:
    """Build the competitor-intel envelope the Brain L6 planner consumes.

    Shape (kept stable so the planner's evidence_keys stay portable):
        {
          "as_of":           ISO-8601 UTC,
          "window_days":     <int>,
          "total_listings":  <int>,
          "drifted":         <int>,
          "stale":           <int>,
          "healthy":         <int>,
          "discovered_pending": <int>,
          "active_registries":      [ {registry, listing_url, ...}, ... ],
          "recently_discovered":    [ {registry, listing_url, discovered}, ... ],
          "competitor_features":    [ {registry, features:[...]}, ... ],
          "submission_blockers":    [ {registry, blocker, next_action} ],
          "scrape_window_count":    <int>,  # rows last_crawled_at within N days
        }

    Defensive — never raises. Returns the partial snapshot on DB error so
    the planner can still cite *something* (an explicit `error` key)."""
    days = max(1, min(int(days or _RECENT_DEFAULT_DAYS), _RECENT_MAX_DAYS))
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=days)
    out = {
        "as_of":               now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days":         days,
        "total_listings":      0,
        "drifted":             0,
        "stale":               0,
        "healthy":             0,
        "discovered_pending":  0,
        "scrape_window_count": 0,
        "active_registries":   [],
        "recently_discovered": [],
        "competitor_features": [],
        "submission_blockers": [],
    }
    conn = _db_conn()
    if not conn:
        out["error"] = "db_unavailable"
        return out
    try:
        with conn.cursor() as cur:
            _ensure_schema(cur)
            # Headline histogram across ALL listings (mirrors /status)
            cur.execute(
                """
                SELECT
                  COUNT(*),
                  COUNT(*) FILTER (WHERE COALESCE(drift_detected, FALSE)),
                  COUNT(*) FILTER (WHERE COALESCE(dchub_metric_stale_days, 0)
                                        >= %s),
                  COUNT(*) FILTER (WHERE COALESCE(discovered, FALSE)),
                  COUNT(*) FILTER (WHERE last_crawled_at >= %s)
                  FROM mcp_presence_listings
                """,
                (STALE_DAYS_THRESHOLD, cutoff),
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0)
            out["total_listings"]      = int(row[0] or 0)
            out["drifted"]             = int(row[1] or 0)
            out["stale"]               = int(row[2] or 0)
            out["discovered_pending"]  = int(row[3] or 0)
            out["scrape_window_count"] = int(row[4] or 0)

            # Active known registries (the canonical seed list — the
            # competitor universe the brain compares us against).
            cur.execute(
                """
                SELECT registry_name, listing_url, submit_url,
                       dchub_metric_published_tools, our_actual_tool_count,
                       dchub_metric_stale_days, drift_detected,
                       last_crawled_at, COALESCE(notes, '{}'::jsonb)
                  FROM mcp_presence_listings
                 WHERE COALESCE(discovered, FALSE) = FALSE
                 ORDER BY drift_detected DESC NULLS LAST,
                          dchub_metric_stale_days DESC NULLS LAST,
                          registry_name ASC
                 LIMIT 30
                """
            )
            for r in cur.fetchall() or []:
                (registry_name, listing_url, submit_url,
                 published, actual, stale_days, drift,
                 last_crawled, notes) = r
                # Mirror /status' healthy bucketing
                is_healthy = (
                    not drift
                    and (stale_days or 0) < STALE_DAYS_THRESHOLD
                    and last_crawled is not None
                )
                if is_healthy:
                    out["healthy"] += 1
                notes_dict = (notes if isinstance(notes, dict)
                              else (json.loads(notes) if notes else {}))
                out["active_registries"].append({
                    "registry":         registry_name,
                    "listing_url":      listing_url,
                    "submit_url":       submit_url,
                    "published_tools":  published,
                    "actual_tools":     actual,
                    "stale_days":       stale_days,
                    "drift_detected":   bool(drift),
                    "last_crawled_at":  (last_crawled.strftime(
                                            "%Y-%m-%dT%H:%M:%SZ")
                                         if last_crawled else None),
                })
                # Submission blockers — for the planner's "DC Hub lacks Z"
                # framing. Surfaces the canonical reasons each registry
                # is dead/blocked so the brain can cite them.
                if notes_dict.get("submission_backend_404"):
                    out["submission_blockers"].append({
                        "registry":    registry_name,
                        "blocker":     "submission_backend_404",
                        "next_action": ("MCPHive-style abandoned backend "
                                        "— no retry path"),
                    })

            # Recently-discovered candidates (the brain treats these as
            # "competitor registries DC Hub is NOT listed on yet")
            cur.execute(
                """
                SELECT registry_name, listing_url, submit_url,
                       last_crawled_at, COALESCE(notes, '{}'::jsonb)
                  FROM mcp_presence_listings
                 WHERE COALESCE(discovered, FALSE) = TRUE
                   AND created_at >= %s
                 ORDER BY created_at DESC
                 LIMIT 25
                """,
                (cutoff,),
            )
            for r in cur.fetchall() or []:
                (registry_name, listing_url, submit_url,
                 last_crawled, notes) = r
                notes_dict = (notes if isinstance(notes, dict)
                              else (json.loads(notes) if notes else {}))
                out["recently_discovered"].append({
                    "registry":         registry_name,
                    "listing_url":      listing_url,
                    "submit_url":       submit_url,
                    "discovered_via":   notes_dict.get(
                                            "discovered_via_query"),
                    "last_crawled_at":  (last_crawled.strftime(
                                            "%Y-%m-%dT%H:%M:%SZ")
                                         if last_crawled else None),
                })

            # Competitor features hint — pulled from notes blobs that
            # carry sniffed-feature tags. Best-effort. If notes lacks
            # `features` (the discoverer hasn't filled it yet), fall
            # back to a curated baseline so the brain L6 planner has
            # CITED competitor evidence to draw on instead of [].
            cur.execute(
                """
                SELECT registry_name, COALESCE(notes->'features',
                                                '[]'::jsonb)
                  FROM mcp_presence_listings
                 WHERE notes ? 'features'
                 LIMIT 15
                """
            )
            for r in cur.fetchall() or []:
                feats = r[1] if isinstance(r[1], list) else []
                if feats:
                    out["competitor_features"].append({
                        "registry": r[0],
                        "features": feats[:10],
                    })

            # 2026-06-07 Round-1 cleanup: when the discoverer hasn't
            # populated notes.features yet (first runs), surface a curated
            # baseline for each ACTIVE registry the brain can compare
            # DC Hub against. Each list is the registry's own observed
            # differentiators (gathered from their landing pages during
            # the 2026-06-05 audit). Brain L6 self-critique explicitly
            # asked for "competitor signal context"; an empty array
            # silently degrades the planner to interpolating from tool
            # names. Curated baseline > empty array.
            if not out["competitor_features"]:
                _CURATED_FEATURES = {
                    "smithery": [
                        "quality_score badge", "auto-discovered tool list",
                        "uptime tracking", "OAuth flow tester",
                        "MCP Inspector UI", "one-click install in Claude Desktop",
                    ],
                    "glama": [
                        "auto-discovered capabilities", "quality_score 0-100",
                        "tool_count badge", "Dockerfile build status",
                        "security audit grade", "GitHub-linked deploys",
                    ],
                    "lobehub": [
                        "Chinese-language interface", "one-click install",
                        "screenshot gallery", "category taxonomy",
                        "user reviews + stars", "auto-translate descriptions",
                    ],
                    "cline": [
                        "marketplace inside VS Code", "auto-install on click",
                        "JSON manifest schema", "GitHub PR submission",
                        "developer-first surface", "free + paid tool tags",
                    ],
                    "continue_dev": [
                        "IDE hub integration", "tool sharing across teams",
                        "MCP + plugin unified directory",
                        "team workspace permissions",
                    ],
                    "cursor_directory": [
                        "Cursor IDE installer link", "rule + MCP combined surface",
                        "fast-search of 1000+ MCPs", "curated 'Best of' lists",
                    ],
                    "mcp_so": [
                        "PR-based submission (low friction)",
                        "category + tag taxonomy", "homepage carousel",
                        "GitHub stars displayed", "weekly trending list",
                    ],
                    "pulsemcp": [
                        "RSS feed of new MCPs", "uptime + latency probes",
                        "free tier filter", "discord-friendly cards",
                    ],
                    "awesome_mcp_servers": [
                        "GitHub-stars-as-signal", "curated by community",
                        "single README file", "PR-reviewed quality bar",
                    ],
                    "mcphive": [
                        "stats grid (tools/users/uptime)",
                        "submission backend dead (auto-discovered only)",
                    ],
                    "yellowmcp": [
                        "Chinese-MCP surface", "WeChat share buttons",
                        "tool_count badge", "submission form (low traffic)",
                    ],
                    "klavis_ai": [
                        "enterprise MCP hosting", "managed-runtime tier",
                        "OAuth-out-of-box", "paid tier directory",
                    ],
                    "mcp_official_registry": [
                        "Anthropic-aligned canonical registry",
                        "PR-based submission to modelcontextprotocol/registry",
                        "DNS TXT verification of ownership",
                    ],
                    "smith_land": [
                        "simple submit form", "category filter",
                    ],
                    "dxt_so": [
                        "Anthropic Desktop Extension format support",
                        "one-click .dxt installer",
                    ],
                }
                # Emit features only for ACTIVE registries we surfaced — the
                # planner cites these alongside the registry rows above.
                active_names = {a.get("registry") for a in out["active_registries"]}
                for reg_name, feats in _CURATED_FEATURES.items():
                    if reg_name in active_names and feats:
                        out["competitor_features"].append({
                            "registry": reg_name,
                            "features": feats[:10],
                            "source":   "curated_baseline_2026_06_07",
                        })
                # Bound to top 15 to mirror the SELECT cap and keep the
                # planner's prompt budget predictable.
                out["competitor_features"] = out["competitor_features"][:15]
    except Exception as e:
        out["error"] = str(e)[:200]
        logger.info("mcp_presence /recent snapshot error: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


@mcp_presence_crawler_bp.route(
    "/api/v1/mcp-presence/recent", methods=["GET"])
@mcp_presence_crawler_bp.route(
    "/api/v1/mcp/presence/recent", methods=["GET"])
def recent_endpoint():
    """PUBLIC — competitor signal channel for Brain L6 Strategic Synthesis.

    Returns the last N days of crawl results so the brain's strategic
    planner has CITED competitor evidence (not interpolated tool names).
    Two aliases keep both old (/mcp-presence/...) and new (/mcp/presence/...)
    callers happy. ?days=N (1..180, default 30)."""
    from flask import make_response
    try:
        days = int(request.args.get("days") or _RECENT_DEFAULT_DAYS)
    except Exception:
        days = _RECENT_DEFAULT_DAYS
    payload = _recent_snapshot(days)
    resp = make_response(jsonify(payload), 200)
    # 5-min edge cache — the underlying snapshot only changes when the
    # crawler fires (twice/day), so a short cache is safe and saves DB.
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# ╭───────────────────────────────────────────────────────────────────────╮
# │ AUTO-SUBMITTER (2026-06-05 extension)                                 │
# │                                                                       │
# │ A live audit of 7 registries found:                                   │
# │   - 2 NOT LISTED at all      (MCPHive, Cursor.directory)              │
# │   - 3 stale tool/facility counts (Smithery, LobeHub, PulseMCP)        │
# │   - 1 still has deprecated "$324B" text (Glama)                       │
# │                                                                       │
# │ This block resolves drift autonomously:                               │
# │   • auto_resubmit_listing()       — per-registry submitter dispatch   │
# │   • update_listing_description()  — for registries w/ an update API   │
# │   • auto_fix_all_drifted()        — sweep WHERE drift_detected=true   │
# │   • _build_canonical_description()— per-registry char-capped copy     │
# │                                                                       │
# │ Defensive: every submitter catches, returns structured error, and     │
# │ honors dry_run=True (the default). Real POSTs require dry_run=False.  │
# │ Rate-limit: in-memory token, max 1 submission per registry per hour.  │
# ╰───────────────────────────────────────────────────────────────────────╯

# Auto-submitter UA so server logs differentiate from the crawler
AUTOSUBMIT_USER_AGENT = "dchub-mcp-presence-autosubmit/1.0 (+https://dchub.cloud)"
AUTOSUBMIT_TIMEOUT_S = 10
AUTOSUBMIT_RATE_LIMIT_SECONDS = 3600  # 1 submission per registry per hour

# {registry_name: epoch_seconds_of_last_submit}
_autosubmit_last_run: dict[str, float] = {}


def _rate_limit_ok(registry_name: str) -> tuple[bool, int]:
    """Return (ok, seconds_until_next_allowed). Mirrors the drain-now
    in-memory token used elsewhere in the repo. NOT cross-process safe
    (single Railway replica) — fine for the autopilot scale we're at."""
    now = time.time()
    last = _autosubmit_last_run.get(registry_name, 0.0)
    elapsed = now - last
    if elapsed >= AUTOSUBMIT_RATE_LIMIT_SECONDS:
        return True, 0
    return False, int(AUTOSUBMIT_RATE_LIMIT_SECONDS - elapsed)


def _mark_submitted(registry_name: str) -> None:
    _autosubmit_last_run[registry_name] = time.time()


# ── Canonical numbers (lazy-load from honest_numbers if available) ───
_CANONICAL_FALLBACK = {
    # ★2026-07-28: these were tools=33 / facilities=21,433 / "1,400+" — every
    # one of them a number canon had moved past. They are only reached when the
    # honest-numbers import FAILS, which means the failure mode was to publish
    # a confident stale claim to every registry rather than to go quiet. Kept
    # in step with ai_surface_canon; a fallback that lies is worse than none.
    "tools":        81,         # live tool count (matches /mcp/tools.json)
    "facilities":   12650,      # discovered_facilities, DEDUPED (07-24 rebase)
    "markets":      311,        # DCPI live market count
    "deals":        1500,       # DISTINCT tracked deals (deduped; see canonical_stats.deals_phrase)
    "deals_phrase": "1,500+ tracked deals",
    "countries":    178,
    "countries_phrase": "170+ countries",
}


def _canonical_numbers() -> dict:
    """Read canonical numbers from routes/mcp_honest_numbers.py if it
    exists; otherwise fall back to the in-file defaults. The honest-
    numbers module is the source of truth (HEALTH_BASELINE.md §canon)
    so when it ships, the submitter automatically picks up updates."""
    try:
        from routes import mcp_honest_numbers as _hn  # type: ignore
        if hasattr(_hn, "as_dict"):
            d = _hn.as_dict() or {}
            return {**_CANONICAL_FALLBACK, **d}
        if hasattr(_hn, "CANONICAL"):
            return {**_CANONICAL_FALLBACK, **dict(_hn.CANONICAL)}
    except Exception:
        pass
    return dict(_CANONICAL_FALLBACK)


# ── Per-registry character-capped description builder ────────────────
_DESCRIPTION_CHAR_CAPS = {
    "mcphive":          1500,
    "cursor_directory":  280,
    "smithery":          500,
    "lobehub":           800,
    "glama":             600,
    "pulsemcp":          500,
    # default fallback for unknown registries
    "_default":          500,
}


def _floor_phrase(value, default: str) -> str:
    """Render a pinned integer floor back into canon phrase shape.
    21433 -> "21,433+". The trailing + matters: these are FLOORS, and a
    bare "18,500 discovered facilities" states an exact count we do not
    have."""
    try:
        return f"{int(value):,}+" if value else default
    except Exception:
        return default


def _canon_floor(phrase) -> int | None:
    """'18,500+' -> 18500 · '300+' -> 300 · junk -> None."""
    digits = _re.sub(r"[^\d]", "", str(phrase or ""))
    return int(digits) if digits else None


def _resolve_canon_public() -> dict:
    """The LIVE-resolved public phrases — the SAME origin the white-glove
    drift detector compares listings against.

    ★2026-08-23: `_canonical_numbers()` bridges to
    `ai_surface_canon.PINNED`, and that module's own docstring says so:
    "Values are the pinned floors; consumers that need live-resolved
    numbers should use ai_surface_canon.resolve_canon() directly." This
    builder is exactly such a consumer, and it was not doing it. See
    `_build_canonical_description` for what that cost.

    ★★★ resolve_canon() is fail-soft PER RESOLVER, and only against
    EXCEPTIONS. A resolver that SUCCEEDS against a degraded or empty
    database returns a perfectly valid-looking phrase computed from
    near-zero rows and OVERWRITES the pinned floor it deep-copied. Measured
    with no database reachable:

        facilities  "400+"    (pinned 18,500+)   ~46x under-claim
        deals       "1,400+"  (pinned  1,800+)   and a known stale_marker

    Nothing raises; `public` just quietly describes an empty DB. Publishing
    that into registry copy is worse than never resolving at all.

    Pinned floors are a RATCHET — canonical_stats floors round DOWN, so a
    published floor only ever moves up as the fleet grows. A live value
    BELOW the pinned floor is therefore evidence of a broken resolver, never
    of shrinkage, and is rejected so the pinned floor stands. Equal or
    higher wins, which is the whole point of resolving live.

    resolve_canon() touches /api/v1/stats and canonical_stats, so it must
    never be on an import-time path — this is called from job contexts
    (white-glove propagation, the registry submitter) only.
    """
    try:
        from ai_surface_canon import PINNED, resolve_canon
        live = (resolve_canon() or {}).get("public") or {}
        pinned = (PINNED.get("public") or {})
    except Exception as e:
        logger.warning("[canon] resolve_canon unavailable (%s) — "
                       "falling back to pinned floors", e)
        return {}
    out: dict = {}
    for key, value in live.items():
        lo = _canon_floor(value)
        if lo is None:
            # a phrase with no digits at all is not a number we can compare
            continue
        floor = _canon_floor(pinned.get(key))
        if floor is not None and lo < floor:
            logger.warning(
                "[canon] live %s=%r is BELOW the pinned floor %r — treating "
                "as a degraded resolver, keeping the floor", key, value,
                pinned.get(key))
            continue
        out[key] = value
    return out


def _build_canonical_description(registry_name: str) -> str:
    """Assemble a registry-appropriate, character-capped pitch.
    Always returns a non-empty string. Uses the LIVE-resolved canon
    (falling back to the pinned floors from honest_numbers)."""
    n = _canonical_numbers()
    live = _resolve_canon_public()
    # ★2026-07-28: this used the PINNED advertised count while white-glove's
    # drift DETECTOR compares against the LIVE count. They disagreed (80 vs
    # 81), so the loop handed the operator paste-ready copy that its own
    # detector would flag as drift the next morning — the loop could not
    # converge by construction. One quantity, one origin: prefer live.
    tools = _our_actual_tool_count() or n.get("tools", 81)
    # ★2026-08-23: the SAME class as `tools` above, left unfixed for
    # `deals`/`facilities`/`markets` for four weeks. The white-glove drift
    # DETECTOR reads resolve_canon() (live); this builder read the PINNED
    # floors — so issue #1872 printed, five lines apart:
    #     "Canonical numbers (ai_surface_canon): … 1,900+ tracked deals"
    #     paste-ready copy:                       … 1,800+ tracked deals
    # An operator who pasted the remedy re-drifted the listing on the next
    # morning's run. The human-gated lane could not converge by
    # construction, which is why three registries sat at drifted 5-6 with
    # human_gated 3 unchanged.
    # Prefer the live phrase; fall back to the pinned floor. Floors round
    # DOWN, so the fallback can only ever under-claim.
    facs_p  = live.get("facilities") or _floor_phrase(n.get("facilities"), "20,100+")
    mkts_p  = live.get("markets") or _floor_phrase(n.get("markets"), "300+")
    # ★ the old literal default here was "1,400+ tracked deals" — which the
    # drift detector itself flags as a stale_marker. A fallback must never be
    # a claim we already know is stale; derive it from the pinned floor.
    _deals = live.get("deals")
    if _deals:
        deals_p = f"{_deals} tracked deals"
    elif n.get("deals_phrase"):
        deals_p = n["deals_phrase"]
    else:
        deals_p = f"{_floor_phrase(n.get('deals'), '1,800+')} tracked deals"
    cap = _DESCRIPTION_CHAR_CAPS.get(registry_name,
                                     _DESCRIPTION_CHAR_CAPS["_default"])

    full = (
        f"DC Hub is the data layer for data-center infrastructure: "
        f"{tools} live MCP tools covering {facs_p} discovered facilities, "
        f"{mkts_p} DCPI markets, {deals_p}, ISO-grid headroom, "
        f"interconnection-queue snapshots, fiber intel, energy prices, "
        f"tax incentives, water risk, and renewable mix. Real-time data, "
        f"versioned, cited. Free tier exposes ~10 tools; paid tiers unlock "
        f"the full {tools}."
    )
    medium = (
        f"DC Hub MCP: {tools} tools, {facs_p} facilities, {mkts_p} markets, "
        f"{deals_p}. ISO-grid, interconnection, fiber, energy, water, tax."
    )
    short = (
        f"{tools} MCP tools for data-center infra: {facs_p} facilities, "
        f"{mkts_p} markets, {deals_p}."
    )
    micro = f"DC Hub: {tools} MCP tools for data-center infrastructure."

    for candidate in (full, medium, short, micro):
        if len(candidate) <= cap:
            return candidate
    # Last resort — hard truncate the micro line at the cap with ellipsis
    return micro[:max(0, cap - 1)] + ("…" if cap > 0 else "")


# ── Canonical payload (filled at call time so numbers stay live) ─────
def _canonical_payload(registry_name: str) -> dict:
    return {
        "name":        "DC Hub MCP Server",
        "description": _build_canonical_description(registry_name),
        "url":         "https://dchub.cloud/mcp",
        "github":      "https://github.com/azmartone67/dchub-mcp-server",
        "homepage":    "https://dchub.cloud",
        "category":    "data,infrastructure,energy",
        "license":     "MIT",
        "manifest":    "https://dchub.cloud/.well-known/mcp.json",
    }


# ── Submitter callables ──────────────────────────────────────────────
def _submitter_mcphive(payload: dict, dry_run: bool) -> dict:
    """Real submitter — POST to mcphive.com submission endpoint.
    MCPHive expects TYPE=SERVER + JSON body. Defensive."""
    target = "https://mcphive.com/api/submit"
    body = {
        "type":        "SERVER",
        "name":        payload["name"],
        "description": payload["description"],
        "url":         payload["url"],
        "repository":  payload["github"],
        "homepage":    payload["homepage"],
        "categories":  payload["category"].split(","),
        "license":     payload["license"],
        "manifest_url": payload["manifest"],
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "target": target,
                "body_preview": body, "submitter": "mcphive"}
    try:
        r = requests.post(
            target,
            json=body,
            headers={"User-Agent": AUTOSUBMIT_USER_AGENT,
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
            timeout=AUTOSUBMIT_TIMEOUT_S,
        )
        return {
            "ok":          200 <= r.status_code < 300,
            "dry_run":     False,
            "target":      target,
            "http_status": r.status_code,
            "response":    (r.text or "")[:500],
            "submitter":   "mcphive",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200],
                "target": target, "submitter": "mcphive"}


def _submitter_cursor_directory(payload: dict, dry_run: bool) -> dict:
    """Real submitter — POST to cursor.directory plugin-submit endpoint.
    Cursor.directory's form expects a slightly different shape (plugin
    metadata, 280-char description cap is already enforced by the
    builder)."""
    target = "https://cursor.directory/api/plugin-submit"
    body = {
        "kind":         "mcp",
        "name":         payload["name"],
        "shortDescription": payload["description"],
        "url":          payload["url"],
        "github":       payload["github"],
        "homepage":     payload["homepage"],
        "tags":         payload["category"].split(","),
        "license":      payload["license"],
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "target": target,
                "body_preview": body, "submitter": "cursor_directory"}
    try:
        r = requests.post(
            target,
            json=body,
            headers={"User-Agent": AUTOSUBMIT_USER_AGENT,
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
            timeout=AUTOSUBMIT_TIMEOUT_S,
        )
        return {
            "ok":          200 <= r.status_code < 300,
            "dry_run":     False,
            "target":      target,
            "http_status": r.status_code,
            "response":    (r.text or "")[:500],
            "submitter":   "cursor_directory",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200],
                "target": target, "submitter": "cursor_directory"}


def _submitter_manifest_refresh(registry_name: str) -> dict:
    """For registries that auto-discover from GitHub README +
    /.well-known/mcp.json (Smithery, LobeHub, Glama, PulseMCP).
    The 'submission' is upstream — bump the manifest and let the
    registry re-crawl. Returns a structured 'requires_manifest_update'
    so the brain knows the path is upstream, not a form POST."""
    # r-nofakepush (2026-07-17): the per-registry "refresh webhook" URLs here
    # (smithery.ai/api/refresh, glama.ai/.../reindex, pulsemcp.com/.../refresh)
    # were SPECULATIVE — none are real endpoints, so every POST 404'd and this
    # "auto-fix push" was a silent NO-OP that only LOOKED automated (worse than
    # honest "escalate to owner"). Removed. The HONEST path for these
    # README/manifest-crawled registries is upstream: bump the GitHub manifest +
    # About (the daily-manifest-sync heal does this — it now heals the deal count
    # too) and let the registry re-crawl on its own cadence.
    # ★2026-07-28 r-closeloop: this returned an UNCONDITIONAL ok=True. That is
    # what kept the Smithery listing stale for days behind a green loop.
    # White-glove classifies these registries as AUTO_PATH and therefore
    # EXCLUDES them from the human-gated drift issue — on the promise that an
    # automated path will fix them. This function is that promise, and it did
    # nothing but assert itself. So Smithery fell in the gap: too "automated"
    # for the human loop, and the automation was a no-op that reported success.
    #
    # The claim "updates land on next crawl" is testable, and which way it
    # fails decides who should act:
    #
    #   upstream manifest != canon  -> the auto-path CAN still work. Heal the
    #                                  manifest and wait for the re-crawl. ok.
    #   upstream manifest == canon  -> we already did our half and the listing
    #     but listing drifts           is STILL wrong, so the re-crawl is not
    #                                  landing. No amount of waiting fixes it.
    #                                  Escalate to the human loop.
    #
    # Reporting ok=True in the second case is the same defect in a different
    # place: claiming success for work that was never verified.
    upstream_ok, upstream_detail = _upstream_manifest_matches_canon()
    if not upstream_ok:
        return {
            "ok": True,
            "submitter": f"{registry_name}_manifest_refresh",
            "requires_manifest_update": True,
            "upstream_manifest": upstream_detail,
            # ★2026-08-25: this said "Upstream manifest is itself stale" —
            # an ASSERTION, and one the check could not support (it also
            # fires on an unreadable manifest and on unresolved canon).
            # State the DETECTION; `upstream_detail` now names both numbers.
            # The heal is two commands, not one: sync-tools-manifest.mjs
            # regenerates the tool list, but the canon PHRASES come from
            # refresh-canon-phrases.mjs — which is why daily-manifest-sync.yml
            # runs them in that order.
            "next_action": (
                f"{registry_name} auto-discovers from GitHub README + "
                f"manifest, and the manifest did not verify as carrying "
                f"canon — {upstream_detail}. Heal it (dchub-mcp-server: "
                "node scripts/refresh-canon-phrases.mjs && node "
                "scripts/sync-tools-manifest.mjs --fix), then the next "
                "crawl carries canon."
            ),
        }
    return {
        "ok": False,
        "escalate": True,
        "submitter": f"{registry_name}_manifest_refresh",
        "requires_manifest_update": False,
        "upstream_manifest": upstream_detail,
        "next_action": (
            f"{registry_name} listing still shows stale numbers, but our "
            f"upstream manifest checks out — {upstream_detail}. The "
            "re-crawl is not landing, so waiting cannot fix this: correct "
            "the listing by hand on the registry's edit surface."
        ),
    }


# Quantities we publish in registry-facing prose. Compared against the
# upstream manifest so "the crawl will fix it" is a checked claim, not an
# assumption. Read from canon — never transcribed here.
_UPSTREAM_MANIFEST_URL = (
    "https://raw.githubusercontent.com/azmartone67/dchub-mcp-server/main/smithery.yaml"
)


# "18,800+ facilities", "1,900+ tracked M&A deals" — a floor and the noun it
# claims, with up to three intervening words so an editorial modifier ("tracked
# M&A") cannot hide the number.
#
# ★ THE GAP CLASS IS THE LOAD-BEARING GUARD, and it is letters-only on purpose.
# The real smithery.yaml packs eight figures into one prose sentence, so a gap
# that admits digits lets a number bridge PAST the noun it belongs to:
#   "126,000+ substations 18800 facilities"  -> letters-only: 18800   (right)
#                                            -> \w-class:     126000  (wrong)
# and a version string becomes a claim: `version 2.4.4 facilities` reads as 2.
# A trailing `(?![\d.,])` on the number was tried here and REMOVED — with a
# letters-only gap it cannot change any outcome, because a number followed by
# a digit/dot/comma has no way to reach the noun (the next token is neither a
# letters gap-word nor the noun itself). An unfalsifiable guard is the thing
# this whole function exists to stop shipping. Widen the class and you must
# put that guard back, plus a test for it.
_MANIFEST_FLOOR_NUM = r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)"


def _manifest_floors(text: str, noun: str) -> list[int]:
    """Every floor the manifest states for `noun`, ascending. [] if none."""
    rx = _re.compile(
        _MANIFEST_FLOOR_NUM + r"\s*\+?\s*(?:[a-zA-Z&/-]+\s+){0,3}" + noun + r"\b",
        _re.I)
    return sorted({int(m.replace(",", "")) for m in rx.findall(text or "")})


def _upstream_manifest_matches_canon() -> tuple[bool, str]:
    """Does the manifest the registries crawl already carry canon?

    Returns (matches, human-readable detail). FAIL-CLOSED on any error: if
    we cannot read the manifest we return False, which routes to the
    "heal the manifest" branch rather than escalating to a human on the
    strength of a failed HTTP call. An unreadable manifest is not evidence
    that a registry's crawler is broken.

    ★2026-08-25 — TWO defects, both of which made this report a manifest
    STALE while it was in fact correct, and both already diagnosed elsewhere
    in this file:

    (1) WRONG CANON ORIGIN. It read `_canonical_numbers()`, which bridges to
        `ai_surface_canon.PINNED` — the hand-bumped DB-DOWN floor. The white-
        glove lane that consumes this verdict resolves canon LIVE, so ONE run
        carried two different canons: `payload.canon.facilities_floor` = 18800
        while this function's detail line said 18,500+. That is verbatim the
        class `_build_canonical_description` fixed on 08-23 (its ★ note: the
        detector reads live, the builder read pinned, "the loop could not
        converge by construction") — left unfixed in this one function, and
        it costs more here: the builder produced copy an operator could see
        was odd, this produces an INSTRUCTION to go heal a healthy file.

    (2) EXACT SUBSTRING MATCH ON A FLOOR. `"18,500+" in text` is False when
        the manifest reads "18,800+" — a manifest AHEAD of the pinned floor,
        i.e. strictly fresher than what we were comparing it to. A floor is a
        one-sided claim: only an UNDER-claim is stale. And because PINNED lags
        `resolve_canon()` by design (see the ★ bump history on
        ai_surface_canon.PINNED["public"]) while dchub-mcp-server's manifest
        is generated from /api/v1/canon/phrases — the LIVE resolver — the two
        diverge on every canon bump. So (1) alone would only reset the clock;
        the comparison itself had to become directional.

    Measured 2026-08-25 against the live manifest, before the fix:
        (False, "manifest missing canon facilities=18,500+")
    while smithery.yaml carried "18,800+ facilities" and `node
    scripts/sync-tools-manifest.mjs` exited 0 with
    "✓ all manifest + facts surfaces consistent".

    The detail string now reports what was actually DETECTED — the manifest's
    own figure against canon's — rather than asserting a mismatch.
    """
    try:
        # ★ Same origin as the drift detector and as white-glove's own
        # payload.canon: live phrases first, pinned floor only as fallback.
        # Floors round DOWN, so the fallback can only ever under-claim, which
        # is the safe direction for a >= comparison.
        n = _canonical_numbers()
        live = _resolve_canon_public()
        want = {
            "facilities": _canon_floor(live.get("facilities")) or n.get("facilities"),
            "deals": _canon_floor(live.get("deals")) or n.get("deals"),
        }
        r = requests.get(_UPSTREAM_MANIFEST_URL,
                         headers={"User-Agent": AUTOSUBMIT_USER_AGENT},
                         timeout=AUTOSUBMIT_TIMEOUT_S)
        if r.status_code != 200:
            return False, f"manifest unreadable (HTTP {r.status_code})"
        text = r.text or ""
        # Only assert on figures we actually resolved — an unresolved canon
        # value must not silently pass by comparing against nothing.
        checked = {k: v for k, v in want.items() if isinstance(v, int) and v > 0}
        if not checked:
            return False, "canon figures unresolved — cannot compare"
        # ★ DIRECTIONAL, and on EVERY stated figure. A manifest at or above
        # canon is not stale. A manifest that states the noun nowhere, or
        # states it anywhere BELOW canon, is — reported with both numbers so
        # the remediation names the real gap instead of a generic "stale".
        behind, ahead = [], []
        for noun, floor in checked.items():
            found = _manifest_floors(text, noun)
            if not found:
                behind.append(f"{noun} stated nowhere (canon {floor:,}+)")
            elif found[0] < floor:
                behind.append(f"{noun} {found[0]:,}+ < canon {floor:,}+")
            else:
                ahead.append(f"{noun} {found[0]:,}+ ≥ canon {floor:,}+")
        if behind:
            return False, "manifest under-claims: " + "; ".join(behind)
        return True, "manifest carries canon: " + ", ".join(ahead)
    except Exception as e:
        return False, f"manifest check failed: {str(e)[:80]}"


def _submitter_human_loop(registry_name: str, reason: str) -> dict:
    """For registries with captcha / login walls. Opens a brain_findings
    row so a human picks it up."""
    conn = _db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                _write_brain_finding(
                    cur,
                    issue=f"mcp_presence_human_loop:{registry_name}",
                    url=f"submit:{registry_name}",
                    detail=(
                        f"{registry_name} needs a human-loop submission "
                        f"({reason}). Open the submit URL from the listings "
                        "table and submit manually."
                    ),
                    count=1,
                )
            conn.commit()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return {
        "ok": True,
        "submitter": f"{registry_name}_human_loop",
        "requires_human_loop": True,
        "reason": reason,
        "next_action": (
            f"Open brain_findings row 'mcp_presence_human_loop:"
            f"{registry_name}' for the next operator pass."
        ),
    }


# Dispatch table: registry_name → callable(payload, dry_run) -> dict
SUBMITTERS: dict[str, Any] = {
    # Real form POSTs
    "mcphive":          lambda p, dr: _submitter_mcphive(p, dr),
    "cursor_directory": lambda p, dr: _submitter_cursor_directory(p, dr),
    # Upstream manifest-refresh (registry auto-discovers from GitHub +
    # /.well-known/mcp.json)
    "smithery":  lambda p, dr: _submitter_manifest_refresh("smithery"),
    "lobehub":   lambda p, dr: _submitter_manifest_refresh("lobehub"),
    "glama":     lambda p, dr: _submitter_manifest_refresh("glama"),
    "pulsemcp":  lambda p, dr: _submitter_manifest_refresh("pulsemcp"),
    # Human-loop fallbacks (captcha / login wall / GitHub PR required)
    "mcp_so":               lambda p, dr: _submitter_human_loop(
        "mcp_so", "form has hCaptcha"),
    "smith_land":           lambda p, dr: _submitter_human_loop(
        "smith_land", "login wall (no public API)"),
    "dxt_so":               lambda p, dr: _submitter_human_loop(
        "dxt_so", "form has Cloudflare Turnstile"),
    "yellowmcp":            lambda p, dr: _submitter_human_loop(
        "yellowmcp", "login wall"),
    "klavis_ai":            lambda p, dr: _submitter_human_loop(
        "klavis_ai", "manual review queue"),
    "awesome_mcp_servers":  lambda p, dr: _submitter_human_loop(
        "awesome_mcp_servers", "GitHub PR required"),
    "cline":                lambda p, dr: _submitter_human_loop(
        "cline", "GitHub PR required"),
    "continue_dev":         lambda p, dr: _submitter_human_loop(
        "continue_dev", "login wall"),
    "mcp_official_registry": lambda p, dr: _submitter_human_loop(
        "mcp_official_registry", "DNS-TXT verification + manual review"),
}


# ── Public API ───────────────────────────────────────────────────────
def auto_resubmit_listing(registry_name: str, dry_run: bool = True) -> dict:
    """Resolve drift for a single registry. Looks up the submit_url
    from mcp_presence_listings, dispatches the appropriate submitter,
    returns a structured result. Defaults to dry_run=True — real POSTs
    require an explicit dry_run=False. Defensive: catches everything,
    rate-limits at 1/registry/hour, identifies as
    dchub-mcp-presence-autosubmit/1.0."""
    result: dict[str, Any] = {
        "ok":            False,
        "registry":      registry_name,
        "dry_run":       bool(dry_run),
        "submitter":     None,
        "rate_limited":  False,
    }
    # Rate-limit gate
    ok, wait_s = _rate_limit_ok(registry_name)
    if not ok:
        result["rate_limited"] = True
        result["wait_seconds"] = wait_s
        result["error"] = (
            f"rate_limited: next attempt allowed in {wait_s}s "
            f"(1 submission/registry/hour)"
        )
        return result

    # Look up the submit_url + notes so the result row carries them
    # (useful for the human-loop path even when we don't POST). Best-
    # effort — the submitter dispatch doesn't actually need submit_url
    # because each submitter knows its own endpoint, but notes carries
    # the submission_backend_404 / dead-endpoint flag (MCPHive).
    submit_url = None
    listing_notes: dict = {}
    conn = _db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT submit_url, COALESCE(notes,'{}'::jsonb) "
                    "  FROM mcp_presence_listings "
                    " WHERE registry_name = %s",
                    (registry_name,),
                )
                row = cur.fetchone()
                if row:
                    submit_url = row[0]
                    if isinstance(row[1], dict):
                        listing_notes = row[1]
                    elif isinstance(row[1], str):
                        try:
                            listing_notes = json.loads(row[1])
                        except Exception:
                            listing_notes = {}
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    result["submit_url_from_db"] = submit_url

    # Short-circuit: if the registry is marked as having a dead
    # submission backend (e.g. MCPHive 404s on POST), skip the
    # submitter entirely and return a structured "skipped" result.
    # The brain reads this and stops queueing retries.
    if listing_notes.get("submission_backend_404") is True:
        result["ok"] = True
        result["submitter"] = f"{registry_name}_skipped_backend_404"
        result["skipped"] = True
        result["skip_reason"] = "submission_backend_404"
        result["next_action"] = (
            f"{registry_name} submission backend is 404 — "
            "no retry path. See mcp_registry_unreachable finding."
        )
        try:
            _persist_auto_fix_outcome(registry_name, result)
        except Exception:
            pass
        return result

    # Dispatch
    fn = SUBMITTERS.get(registry_name)
    if fn is None:
        result["error"] = f"unknown_registry: {registry_name}"
        return result

    try:
        payload = _canonical_payload(registry_name)
        if dry_run:
            print(f"[auto_resubmit_listing] DRY-RUN registry={registry_name}")
            print(f"  payload={json.dumps(payload, indent=2)[:1200]}")
        sub_result = fn(payload, dry_run) or {}
        result.update(sub_result)
        result["ok"] = bool(sub_result.get("ok"))
        result["submitter"] = sub_result.get("submitter") or registry_name
        # Only burn the rate-limit token on a non-dry-run POST OR a
        # successful human_loop / manifest_refresh path (they do real
        # work — open findings rows / hit webhooks).
        if not dry_run or sub_result.get("requires_human_loop") \
                or sub_result.get("requires_manifest_update"):
            _mark_submitted(registry_name)
    except Exception as e:
        result["error"] = str(e)[:200]
        logger.warning("auto_resubmit_listing %s failed: %s",
                       registry_name, e)

    # Persist outcome on the listings row (idempotent ALTERs + UPDATE)
    try:
        _persist_auto_fix_outcome(registry_name, result)
    except Exception as e:
        logger.info("auto_fix outcome persist failed for %s: %s",
                    registry_name, e)
    return result


def update_listing_description(registry_name: str,
                                new_description: str) -> dict:
    """Patch a listing's description on registries that support it via
    API. Glama exposes a server.patch endpoint per their docs. For
    everything else (including LobeHub since 2026-08-15 — see below),
    mark as requires_human_loop. Defensive — never raises."""
    result: dict[str, Any] = {
        "ok":          False,
        "registry":    registry_name,
        "description_preview": (new_description or "")[:200],
        "submitter":   "update_description",
    }
    if not new_description or not new_description.strip():
        result["error"] = "empty_description"
        return result

    if registry_name == "glama":
        target = _glama_api_url()   # single origin — see _glama_api_url()
        try:
            r = requests.patch(
                target,
                json={"description": new_description},
                headers={"User-Agent": AUTOSUBMIT_USER_AGENT,
                         "Content-Type": "application/json"},
                timeout=AUTOSUBMIT_TIMEOUT_S,
            )
            result.update({
                "ok": 200 <= r.status_code < 300,
                "http_status": r.status_code,
                "target": target,
                "response": (r.text or "")[:400],
            })
        except Exception as e:
            result["error"] = str(e)[:200]
            result["target"] = target
        return result

    # r-fix 2026-08-15: the LobeHub branch PUT to
    # https://lobehub.com/api/mcp/dchub-mcp-server, which 404s with no
    # redirect — every description refresh failed silently since LobeHub
    # consolidated onto market.lobehub.com. No public description API has
    # been verified on the market host, so LobeHub goes through the human
    # loop: the authenticated `market-cli plugin update` flow documented
    # in mcp_registry_outreach is the working path.

    # No API patch — human loop
    hl = _submitter_human_loop(
        registry_name,
        "no description-update API; submit edit manually",
    )
    result.update(hl)
    result["ok"] = True
    return result


# ── Persist outcome columns on mcp_presence_listings ─────────────────
_AUTOFIX_DDL = (
    "ALTER TABLE mcp_presence_listings "
    "  ADD COLUMN IF NOT EXISTS last_auto_fix_at TIMESTAMPTZ",
    "ALTER TABLE mcp_presence_listings "
    "  ADD COLUMN IF NOT EXISTS last_auto_fix_result JSONB",
)


def _persist_auto_fix_outcome(registry_name: str, outcome: dict) -> None:
    """Best-effort write of last_auto_fix_at + last_auto_fix_result to
    mcp_presence_listings. Idempotent ALTERs on first call. Never raises."""
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            for ddl in _AUTOFIX_DDL:
                try:
                    cur.execute(ddl)
                except Exception:
                    pass
            try:
                payload = json_for_column(
                    {k: v for k, v in outcome.items()
                     if k != "body_preview"},   # body_preview can be huge
                    6000,
                )
            except Exception:
                payload = json.dumps({"ok": bool(outcome.get("ok"))})
            cur.execute(
                """
                UPDATE mcp_presence_listings
                   SET last_auto_fix_at     = NOW(),
                       last_auto_fix_result = %s::jsonb
                 WHERE registry_name = %s
                """,
                (payload, registry_name),
            )
        conn.commit()
    except Exception as e:
        logger.info("persist_auto_fix_outcome %s: %s", registry_name, e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Sweep-all-drifted ────────────────────────────────────────────────
def auto_fix_all_drifted(dry_run: bool = True) -> dict:
    """Iterate listings WHERE drift_detected=true and run the submitter
    for each. Honors per-registry rate-limits. Defensive — never raises.
    Wired into a SCHEDULE entry (slot 19/19 UTC) named 'mcp_presence_auto_fix'."""
    summary = {
        "checked":       0,
        "submitted":       0,   # a REAL form/POST submitter returned 2xx
        "manifest_upstream": 0, # honest no-op: registry re-crawls our GitHub manifest/About
        "human_loop":        0, # queued a brain_finding for a captcha/login-wall registry
        "rate_limited":  0,
        "errors":        0,
        "dry_run":       bool(dry_run),
        "results":       [],
    }
    conn = _db_conn()
    if not conn:
        summary["error"] = "db_unavailable"
        return summary
    try:
        with conn.cursor() as cur:
            _ensure_schema(cur)
            cur.execute(
                """
                SELECT registry_name
                  FROM mcp_presence_listings
                 WHERE COALESCE(drift_detected, FALSE) = TRUE
                   AND COALESCE(discovered, FALSE) = FALSE
                 ORDER BY registry_name ASC
                """
            )
            registries = [r[0] for r in cur.fetchall()]
    except Exception as e:
        summary["error"] = str(e)[:200]
        try:
            conn.close()
        except Exception:
            pass
        return summary
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for reg in registries:
        summary["checked"] += 1
        try:
            r = auto_resubmit_listing(reg, dry_run=dry_run)
            summary["results"].append({
                "registry":   reg,
                "ok":         bool(r.get("ok")),
                "submitter":  r.get("submitter"),
                "rate_limited": bool(r.get("rate_limited")),
                "http_status": r.get("http_status"),
                "requires_human_loop": bool(r.get("requires_human_loop")),
                "requires_manifest_update": bool(
                    r.get("requires_manifest_update")),
            })
            if r.get("rate_limited"):
                summary["rate_limited"] += 1
            # r-honestcount (2026-07-17): after the speculative-webhook removal
            # (see _submitter_manifest_refresh), the manifest registries return
            # ok=True but did NOT push anything — the fix is upstream (re-crawl).
            # Counting those as "submitted" reported phantom automation. Split
            # the buckets so the daily log tells the truth: only a real form/POST
            # 2xx is "submitted".
            elif r.get("requires_manifest_update"):
                summary["manifest_upstream"] += 1
            elif r.get("requires_human_loop"):
                summary["human_loop"] += 1
            elif r.get("ok"):
                summary["submitted"] += 1
            else:
                summary["errors"] += 1
        except Exception as e:
            summary["errors"] += 1
            logger.warning("auto_fix_all_drifted: %s failed: %s", reg, e)
    return summary


# ── New endpoints ────────────────────────────────────────────────────
@mcp_presence_crawler_bp.route(
    "/api/v1/admin/mcp-presence/auto-fix/<registry>", methods=["POST"])
def auto_fix_endpoint(registry: str):
    """Run auto_resubmit_listing for a single registry. ?dry_run=0 to
    actually POST (default is dry_run=1 for safety). Records outcome
    on mcp_presence_listings and returns JSON with the next-action hint."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    raw = (request.args.get("dry_run")
           or request.args.get("dryRun") or "1").strip().lower()
    dry_run = raw not in ("0", "false", "no", "off")
    result = auto_resubmit_listing(registry, dry_run=dry_run)
    # Build a next_action hint
    if result.get("rate_limited"):
        hint = (f"Wait {result.get('wait_seconds')}s — registry is "
                "rate-limited (1/hour).")
    elif result.get("requires_human_loop"):
        hint = (f"Human loop required for {registry}: "
                f"{result.get('reason') or 'see brain_findings row'}.")
    elif result.get("requires_manifest_update"):
        hint = (f"{registry} auto-discovers from manifest. "
                "Verify next crawl.")
    elif result.get("ok"):
        hint = (f"Submitted to {registry} "
                f"({'dry-run' if dry_run else 'live'}).")
    else:
        hint = (f"Submitter failed for {registry}: "
                f"{result.get('error') or 'unknown'}.")
    return jsonify({**result, "next_action": hint}), 200


@mcp_presence_crawler_bp.route(
    "/api/v1/admin/mcp-presence/auto-fix-all", methods=["POST"])
def auto_fix_all_endpoint():
    """Sweep every drifted listing. ?dry_run=0 to actually POST."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    raw = (request.args.get("dry_run")
           or request.args.get("dryRun") or "1").strip().lower()
    dry_run = raw not in ("0", "false", "no", "off")
    result = auto_fix_all_drifted(dry_run=dry_run)
    return jsonify({"ok": True, **result}), 200


def register_mcp_presence_crawler(app) -> None:
    """Idempotent blueprint registration. Called from main.py."""
    app.register_blueprint(mcp_presence_crawler_bp)
    logger.info(
        "MCP Presence Crawler registered: POST /api/v1/admin/mcp-presence/"
        "{crawl,seed,discover,reseed-broken,auto-fix/<registry>,"
        "auto-fix-all}, GET /api/v1/mcp-presence/{status,recent}, "
        "GET /api/v1/mcp/presence/recent (Brain L6 alias)"
    )
