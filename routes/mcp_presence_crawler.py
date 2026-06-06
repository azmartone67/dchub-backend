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
import time
import logging
import datetime as _dt
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

mcp_presence_crawler_bp = Blueprint("mcp_presence_crawler", __name__)


# ── Constants ─────────────────────────────────────────────────────────
USER_AGENT = "dchub-mcp-presence-crawler/1.0 (+https://dchub.cloud)"
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
        "listing_url":   "https://smithery.ai/server/@dchub/dchub-mcp-server",
        "submit_url":    "https://smithery.ai/new",
    },
    {
        "registry_name": "mcphive",
        "listing_url":   "https://mcphive.com/servers/dchub",
        "submit_url":    "https://mcphive.com/submit",
    },
    {
        "registry_name": "lobehub",
        "listing_url":   "https://lobehub.com/mcp/dchub-mcp-server",
        "submit_url":    "https://lobehub.com/mcp/submit",
    },
    {
        "registry_name": "glama",
        "listing_url":   "https://glama.ai/mcp/servers/dchub",
        "submit_url":    "https://glama.ai/mcp/servers/new",
    },
    {
        "registry_name": "yellowmcp",
        "listing_url":   "https://yellowmcp.com/servers/dchub",
        "submit_url":    "https://yellowmcp.com/submit",
    },
    {
        "registry_name": "mcp_official_registry",
        "listing_url":   "https://registry.modelcontextprotocol.io/servers/dchub",
        "submit_url":    "https://registry.modelcontextprotocol.io/submit",
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
        "registry_name": "awesome_mcp_servers",
        "listing_url":   "https://github.com/punkpeye/awesome-mcp-servers",
        "submit_url":    "https://github.com/punkpeye/awesome-mcp-servers/pulls",
    },
    {
        "registry_name": "mcp_so",
        "listing_url":   "https://mcp.so/server/dchub-mcp-server",
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
    """Idempotent seed of SEED_REGISTRIES. Returns rows touched."""
    inserted = 0
    for r in SEED_REGISTRIES:
        try:
            cur.execute(
                """
                INSERT INTO mcp_presence_listings
                    (registry_name, listing_url, submit_url, discovered)
                VALUES (%s, %s, %s, FALSE)
                ON CONFLICT (registry_name) DO NOTHING
                """,
                (r["registry_name"], r["listing_url"], r.get("submit_url")),
            )
            if cur.rowcount and cur.rowcount > 0:
                inserted += 1
        except Exception as e:
            logger.warning("mcp_presence: seed insert failed for %s: %s",
                           r.get("registry_name"), e)
    return inserted


# ── HTTP fetch (rate-limited) ─────────────────────────────────────────
_last_request_ts = 0.0


def _polite_get(url: str) -> tuple[str | None, int | None]:
    """Rate-limited GET. Returns (html_text or None, status_code or None)."""
    global _last_request_ts
    elapsed = time.time() - _last_request_ts
    if elapsed < RATE_LIMIT_SLEEP_S:
        time.sleep(RATE_LIMIT_SLEEP_S - elapsed)
    _last_request_ts = time.time()
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            timeout=REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
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


_EXTRACTORS = {
    "smithery":  _extractor_smithery,
    "mcphive":   _extractor_mcphive,
    "lobehub":   _extractor_lobehub,
    "glama":     _extractor_glama,
    "yellowmcp": _extractor_yellowmcp,
}


def _extract_for(registry_name: str, html: str) -> dict:
    """Look up the registry-specific extractor; fall through to the
    generic. Always returns a dict (never None) so callers don't have
    to special-case."""
    fn = _EXTRACTORS.get(registry_name, _extractor_generic)
    out = fn(html) or {}
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
    """Upsert into brain_findings — never raises. Mirrors the
    bug-squash pattern: (issue, url) is the UNIQUE key."""
    try:
        cur.execute(
            """
            INSERT INTO brain_findings
                (issue, url, count, detail, first_seen, last_seen, seen_count)
            VALUES (%s, %s, %s, %s, NOW(), NOW(), 1)
            ON CONFLICT (issue, url) DO UPDATE
                SET count      = EXCLUDED.count,
                    detail     = EXCLUDED.detail,
                    last_seen  = NOW(),
                    seen_count = brain_findings.seen_count + 1
            """,
            (issue[:200], (url or "")[:500], count, (detail or "")[:2000]),
        )
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
                 WHERE COALESCE(discovered, FALSE) = FALSE
                 ORDER BY COALESCE(last_crawled_at, '1970-01-01'::timestamptz) ASC
                 LIMIT %s
                """,
                (MAX_REQUESTS_PER_RUN,),
            )
            rows = cur.fetchall()

            for row in rows:
                row_id, registry_name, listing_url = row[0], row[1], row[2]
                try:
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
                            pass
                        continue

                    extracted = _extract_for(registry_name, html)
                    listing_tools  = extracted.get("tools")
                    listing_uptime = extracted.get("uptime")
                    listing_last   = extracted.get("last_updated")

                    # Compute drift
                    drift = bool(
                        actual_count is not None
                        and listing_tools is not None
                        and listing_tools != actual_count
                    )

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


def register_mcp_presence_crawler(app) -> None:
    """Idempotent blueprint registration. Called from main.py."""
    app.register_blueprint(mcp_presence_crawler_bp)
    logger.info(
        "MCP Presence Crawler registered: POST /api/v1/admin/mcp-presence/"
        "{crawl,seed,discover}, GET /api/v1/mcp-presence/status"
    )
