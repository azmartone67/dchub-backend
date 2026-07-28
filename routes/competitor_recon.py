"""Competitor Intelligence Recon (2026-07-25) — good / bad / gaps / win-moves.

Weekly deep-recon over the tracked rival set — DataCenterHawk, DC Byte*,
DataCenterDynamics, Data Center Frontier, Baxtel, CBRE (data centers),
JLL (data centers) — that goes BEYOND the two existing competitor loops:

  * routes/competitor_gap_crawler.py  → facility coverage-gap diffing
  * routes/competitor_intel.py        → homepage hash/title drift

This module probes each rival's PUBLIC posture (robots.txt AI-crawler
stance, llms.txt, homepage positioning + schema.org, pricing
transparency, editorial velocity, directory surface size), merges those
live signals into a curated capability matrix (DC Hub scored on the same
axes from canonical_stats), and emits a structured weekly assessment —

    good / bad / gaps / win_moves

— persisted to competitor_recon_reports and filed to the brain via the
canonical findings writer (routes/brain_findings_writer) so autopilot
lanes can act on the ranked win moves.

* DC Byte is NEVER fetched: its ToS prohibits automated crawling (the
  same reason competitor_gap_crawler SKIPs it). Zero requests go to
  dcbyte.com; its column comes from the curated profile plus that fact
  itself — which is strategic signal (AI agents cannot legally read
  them; DC Hub is MCP-native).

Crawl ethics: honest self-identifying UA, robots-prefix respect, ~2s
polite delay, hard fetch cap + time budget, weekly cadence.

Endpoints (blueprint "competitor_recon"):
  POST /api/v1/competitors/recon/run     admin (X-Admin-Key); ?force=1
                                         bypasses the weekly gate;
                                         ?sync=1 runs inline
  GET  /api/v1/competitors/recon/latest  admin — latest full report
  GET  /api/v1/competitors/recon/matrix  admin — capability matrix only

Wiring: STEP 3 in routes/competitor_intel.py:scan_competitors() spawns
run_competitor_recon() daily in a daemon thread; this module's own
7-day gate makes 6 of 7 spawns no-ops — no new cron (cron count is
already ~314 with dupes; see brain audit 2026-07-25).

v2 candidates (deliberately not in v1): mine our own news/deals tables
for rival mentions; derive DCHawk surface size from coverage_gaps.
"""

from __future__ import annotations

import os
import re
import json
import time
import logging
import datetime
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

competitor_recon_bp = Blueprint("competitor_recon", __name__)

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()

# Honest, transparent, non-AI-crawler UA (same rationale as
# competitor_gap_crawler.GAP_USER_AGENT: several rivals robots-block AI
# UAs; this bot is a posture probe, not a trainer or content scraper).
RECON_USER_AGENT = ("DCHub-Recon-Bot/1.0 (+https://dchub.cloud; "
                    "public-posture-recon; not-for-training)")

_POLITE_DELAY_S = float(os.environ.get("COMPETITOR_RECON_DELAY_S", "2.0"))
_FETCH_TIMEOUT_S = float(os.environ.get("COMPETITOR_RECON_FETCH_TIMEOUT_S", "12"))
_DEFAULT_BUDGET_S = float(os.environ.get("COMPETITOR_RECON_BUDGET_S", "240"))
_MAX_FETCHES = int(os.environ.get("COMPETITOR_RECON_MAX_FETCHES", "40"))
_GATE_DAYS = int(os.environ.get("COMPETITOR_RECON_GATE_DAYS", "6"))
_MAX_WIN_MOVE_FINDINGS = 5
_MAX_THREAT_FINDINGS = 3

_SYNTH_SLUG = "__synthesis__"

# AI/agent crawler UAs whose robots.txt stance we measure.
AI_BOT_UAS = (
    "gptbot", "claudebot", "claude-web", "anthropic-ai", "perplexitybot",
    "ccbot", "google-extended", "bytespider", "meta-externalagent",
)


# ─────────────────────────────────────────────────────────────────────
# TARGETS — public surfaces only. policy:
#   crawl              → robots + llms + home (+ pricing/feed/sitemap)
#   crawl_conservative → robots + llms + home + first-200 landing page
#   no_crawl_tos       → ZERO fetches (ToS prohibits automated access)
# ─────────────────────────────────────────────────────────────────────

TARGETS = [
    {"slug": "dchawk", "name": "DataCenterHawk",
     "category": "analyst_platform", "policy": "crawl",
     "base": "https://datacenterhawk.com",
     "paths": {"pricing": ["/pricing", "/platform"]},
     "disallowed_prefixes": ("/data", "/admin", "/api", "/rest", "/iam")},
    {"slug": "dcbyte", "name": "DC Byte",
     "category": "analyst_platform", "policy": "no_crawl_tos",
     "base": "https://www.dcbyte.com",
     "note": "ToS prohibits automated crawling/indexing — never fetched."},
    {"slug": "dcd", "name": "DataCenterDynamics",
     "category": "media", "policy": "crawl",
     "base": "https://www.datacenterdynamics.com",
     "feed": {"kind": "rss",
              "url": "https://www.datacenterdynamics.com/en/rss/"}},
    {"slug": "dcf", "name": "Data Center Frontier",
     "category": "media", "policy": "crawl",
     "base": "https://www.datacenterfrontier.com",
     "feed": {"kind": "news_sitemap",
              "url": "https://www.datacenterfrontier.com/sitemap/News.xml"}},
    {"slug": "baxtel", "name": "Baxtel",
     "category": "directory", "policy": "crawl",
     "base": "https://baxtel.com",
     "sitemap_index": "https://baxtel.com/sitemap.xml"},
    {"slug": "cbre", "name": "CBRE (data centers)",
     "category": "brokerage", "policy": "crawl_conservative",
     "base": "https://www.cbre.com",
     "paths": {"landing": ["/services/property-types/data-centers",
                            "/insights"]}},
    {"slug": "jll", "name": "JLL (data centers)",
     "category": "brokerage", "policy": "crawl_conservative",
     "base": "https://www.jll.com",
     "paths": {"landing": ["/en-us/industries/data-centers",
                            "/en-us/insights"]}},
]


# ─────────────────────────────────────────────────────────────────────
# CAPABILITY MATRIX — 0..3 per axis. Static seeds are CURATED editorial
# judgment (2026-07-25); axes marked dynamic are re-measured every run:
#   ai_agent_access      ← robots/llms/ToS probe
#   pricing_transparency ← pricing-page probe (can only raise the seed)
# ─────────────────────────────────────────────────────────────────────

AXES = [
    ("facilities_db",        "Facility directory depth"),
    ("market_analytics",     "Market analytics / rankings"),
    ("power_grid_data",      "Power & grid infrastructure data"),
    ("live_telemetry",       "Live grid telemetry"),
    ("news_editorial",       "Original news / editorial"),
    ("api_access",           "Programmatic API"),
    ("ai_agent_access",      "AI-agent access (MCP / robots / llms.txt)"),
    ("pricing_transparency", "Public pricing"),
    ("free_tier",            "Self-serve free tier"),
    ("deal_tracking",        "M&A / deal intelligence"),
]

STATIC_PROFILES = {
    "dchawk": {
        "axes": {"facilities_db": 3, "market_analytics": 3,
                 "power_grid_data": 1, "live_telemetry": 0,
                 "news_editorial": 1, "api_access": 2,
                 "ai_agent_access": 0, "pricing_transparency": 0,
                 "free_tier": 0, "deal_tracking": 2},
        "strengths": ["Deep analyst-grade facility + absorption data",
                      "Strong enterprise/brokerage brand and sales motion"],
        "weaknesses": ["Demo-gated: no self-serve, no public pricing",
                       "Human-analyst delivery model — no agent surface"],
    },
    "dcbyte": {
        "axes": {"facilities_db": 3, "market_analytics": 3,
                 "power_grid_data": 1, "live_telemetry": 0,
                 "news_editorial": 1, "api_access": 2,
                 "ai_agent_access": 0, "pricing_transparency": 0,
                 "free_tier": 0, "deal_tracking": 2},
        "strengths": ["Supply/take-up analytics with strong EMEA+APAC depth",
                      "Trusted source for press market stats"],
        "weaknesses": ["ToS bans automated access — invisible to AI agents",
                       "Enterprise-only; no self-serve or free tier"],
    },
    "dcd": {
        "axes": {"facilities_db": 0, "market_analytics": 1,
                 "power_grid_data": 1, "live_telemetry": 0,
                 "news_editorial": 3, "api_access": 0,
                 "ai_agent_access": 1, "pricing_transparency": 2,
                 "free_tier": 2, "deal_tracking": 2},
        "strengths": ["Dominant editorial brand + events franchise",
                      "High-velocity global news desk"],
        "weaknesses": ["No structured data product an agent can query",
                       "Anti-AI-crawler stance limits agent citation"],
    },
    "dcf": {
        "axes": {"facilities_db": 0, "market_analytics": 1,
                 "power_grid_data": 1, "live_telemetry": 0,
                 "news_editorial": 3, "api_access": 0,
                 "ai_agent_access": 1, "pricing_transparency": 2,
                 "free_tier": 2, "deal_tracking": 1},
        "strengths": ["Respected US-focused editorial + analyst voices",
                      "Strong executive-audience newsletters"],
        "weaknesses": ["US-weighted coverage; no data product",
                       "No machine-readable surface for agents"],
    },
    "baxtel": {
        "axes": {"facilities_db": 2, "market_analytics": 1,
                 "power_grid_data": 0, "live_telemetry": 0,
                 "news_editorial": 1, "api_access": 0,
                 "ai_agent_access": 1, "pricing_transparency": 1,
                 "free_tier": 2, "deal_tracking": 0},
        "strengths": ["Free public facility directory with maps/photos",
                      "SEO reach on facility + market queries"],
        "weaknesses": ["Shallow attributes: no power/grid/queue layers",
                       "Unclear freshness; no API/agent access"],
    },
    "cbre": {
        "axes": {"facilities_db": 1, "market_analytics": 3,
                 "power_grid_data": 1, "live_telemetry": 0,
                 "news_editorial": 1, "api_access": 0,
                 "ai_agent_access": 0, "pricing_transparency": 0,
                 "free_tier": 2, "deal_tracking": 3},
        "strengths": ["Authoritative semiannual market reports (press-cited)",
                      "Unmatched brokerage deal flow + advisory reach"],
        "weaknesses": ["Quarterly/semiannual PDF cadence — stale between drops",
                       "Corporate site closed to AI crawlers; no data access"],
    },
    "jll": {
        "axes": {"facilities_db": 1, "market_analytics": 3,
                 "power_grid_data": 1, "live_telemetry": 0,
                 "news_editorial": 1, "api_access": 0,
                 "ai_agent_access": 0, "pricing_transparency": 0,
                 "free_tier": 2, "deal_tracking": 3},
        "strengths": ["Global data-center outlook reports with strong brand",
                      "Deep capital-markets + leasing intelligence"],
        "weaknesses": ["PDF/report cadence, not queryable data",
                       "No agent/API surface; research gated by forms"],
    },
}

# DC Hub self-assessment (honest: news_editorial=1 and deal_tracking=2
# are self-declared gaps). Evidence numbers come from canonical_stats at
# run time — never hardcoded counts.
_DCHUB_AXES = {"facilities_db": 3, "market_analytics": 3,
               "power_grid_data": 3, "live_telemetry": 3,
               "news_editorial": 1, "api_access": 3,
               "ai_agent_access": 3, "pricing_transparency": 3,
               "free_tier": 3, "deal_tracking": 2}


# ─────────────────────────────────────────────────────────────────────
# FETCH LAYER (honest UA, robots-prefix guard, polite, fail-soft)
# ─────────────────────────────────────────────────────────────────────

def _robots_allows(url: str, disallowed_prefixes: tuple) -> bool:
    try:
        path = urlsplit(url).path or "/"
    except Exception:
        return False
    for pref in (disallowed_prefixes or ()):
        if path.startswith(pref):
            return False
    return True


def _get(url: str, disallowed_prefixes: tuple = ()) -> dict:
    """GET with the honest UA. {status, text, error}. Never raises."""
    out = {"status": 0, "text": "", "error": None}
    if not _robots_allows(url, disallowed_prefixes):
        out["error"] = "robots_disallowed"
        return out
    try:
        import requests
    except Exception as e:  # pragma: no cover
        out["error"] = f"requests_unavailable: {e}"
        return out
    try:
        r = requests.get(url, timeout=_FETCH_TIMEOUT_S, headers={
            "User-Agent": RECON_USER_AGENT,
            "Accept": ("text/html, application/xml, text/xml, "
                       "application/rss+xml;q=0.9, text/plain;q=0.8, "
                       "*/*;q=0.5"),
            "Cache-Control": "no-cache",
        }, allow_redirects=True)
        out["status"] = r.status_code
        if r.status_code == 200:
            out["text"] = r.text[:400_000]
        else:
            out["error"] = f"http_{r.status_code}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


def planned_fetches(target: dict) -> list:
    """(label, url) fetch plan for one target. Pure — unit-testable.
    no_crawl_tos targets plan ZERO fetches."""
    if target.get("policy") == "no_crawl_tos":
        return []
    base = target["base"].rstrip("/")
    plan = [("robots", base + "/robots.txt"), ("llms", base + "/llms.txt"),
            ("home", base + "/")]
    paths = target.get("paths") or {}
    for p in (paths.get("pricing") or []):
        plan.append(("pricing", base + p))
    for p in (paths.get("landing") or []):
        plan.append(("landing", base + p))
    feed = target.get("feed")
    if feed:
        plan.append(("feed", feed["url"]))
    if target.get("sitemap_index"):
        plan.append(("sitemap", target["sitemap_index"]))
    return plan[:8]


# ─────────────────────────────────────────────────────────────────────
# PARSERS (pure functions)
# ─────────────────────────────────────────────────────────────────────

def parse_robots(text: str) -> dict:
    """Parse robots.txt → per-AI-bot stance + sitemap decls.
    Stance: blocked_all | scoped | allowed (explicit group), else falls
    back to the '*' group → default_blocked / default_open."""
    groups: dict = {}
    sitemaps: list = []
    current: list = []
    expecting_rules = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key == "sitemap":
            if val:
                sitemaps.append(val)
            continue
        if key == "user-agent":
            ua = val.lower()
            if expecting_rules:
                current = [ua]
                expecting_rules = False
            else:
                current.append(ua)
            for u in current:
                groups.setdefault(u, [])
            continue
        if key in ("disallow", "allow"):
            expecting_rules = True
            if key == "disallow" and val:
                for u in current:
                    groups.setdefault(u, []).append(val)
            continue

    def _stance(dis: list) -> str:
        if any(d.strip() == "/" for d in dis):
            return "blocked_all"
        return "scoped" if dis else "allowed"

    star = groups.get("*")
    ai_stance = {}
    blocks_ai = []
    for bot in AI_BOT_UAS:
        if bot in groups:
            st = _stance(groups[bot])
        elif star is not None:
            st = {"blocked_all": "default_blocked",
                  "scoped": "default_open",
                  "allowed": "default_open"}[_stance(star)]
        else:
            st = "unspecified"
        ai_stance[bot] = st
        if st in ("blocked_all", "default_blocked"):
            blocks_ai.append(bot)
    return {"fetched": True, "ai_stance": ai_stance,
            "blocks_ai": blocks_ai, "sitemaps": sitemaps[:10]}


_TITLE_RE = re.compile(r"<title[^>]*>\s*(.{1,300}?)\s*</title>", re.I | re.S)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{1,400})',
    re.I)
_META_DESC_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']{1,400})["\'][^>]+name=["\']description',
    re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.{1,300}?)</h1>", re.I | re.S)
_JSONLD_TYPE_RE = re.compile(r'"@type"\s*:\s*"([A-Za-z][A-Za-z0-9]{1,40})"')
_TAG_RE = re.compile(r"<[^>]+>")

_KEYWORD_PATTERNS = {
    "api": re.compile(r"\bapi\b", re.I),
    "mcp": re.compile(r"model context protocol|\bmcp\b", re.I),
    "ai_agent": re.compile(r"ai[- ]agents?\b|for agents\b|agent[- ]ready", re.I),
    "real_time": re.compile(r"real[- ]time|live data", re.I),
    "interconnection": re.compile(r"interconnection|grid queue", re.I),
    "power": re.compile(r"\bpower\b|\bmegawatt|\bMW\b", re.I),
    "pricing_word": re.compile(r"\bpricing\b|\bplans\b", re.I),
    "free_word": re.compile(r"\bfree\b", re.I),
    "sustainability": re.compile(r"sustainab|carbon|renewable", re.I),
}


def parse_home(html: str) -> dict:
    """Extract positioning signals from a homepage/landing HTML body."""
    out = {"title": None, "meta_description": None, "h1": [],
           "jsonld_types": [], "keywords": {}}
    if not html:
        return out
    m = _TITLE_RE.search(html)
    if m:
        out["title"] = _TAG_RE.sub("", m.group(1)).strip()[:200]
    m = _META_DESC_RE.search(html) or _META_DESC_RE2.search(html)
    if m:
        out["meta_description"] = m.group(1).strip()[:300]
    out["h1"] = [_TAG_RE.sub("", h).strip()[:160]
                 for h in _H1_RE.findall(html)[:3] if _TAG_RE.sub("", h).strip()]
    out["jsonld_types"] = sorted(set(_JSONLD_TYPE_RE.findall(html)))[:12]
    out["keywords"] = {k: bool(p.search(html))
                       for k, p in _KEYWORD_PATTERNS.items()}
    return out


_RSS_ITEM_RE = re.compile(r"<item[\s>].*?</item>", re.I | re.S)
_RSS_PUBDATE_RE = re.compile(r"<pubDate>\s*([^<]{6,60})\s*</pubDate>", re.I)
_RSS_TITLE_RE = re.compile(
    r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.I | re.S)
_NEWS_DATE_RE = re.compile(
    r"<(?:news:)?publication_date>\s*([^<\s]{8,40})\s*</(?:news:)?publication_date>"
    r"|<lastmod>\s*([^<\s]{8,40})\s*</lastmod>", re.I)
_NEWS_TITLE_RE = re.compile(
    r"<(?:news:)?title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</(?:news:)?title>",
    re.I | re.S)


def _parse_dt(s: str):
    if not s:
        return None
    s = s.strip()
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        if d is not None:
            return d.replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        pass
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        return None


def parse_feed_velocity(text: str, kind: str, now=None) -> dict:
    """Editorial velocity from an RSS feed or Google-News sitemap."""
    now = now or datetime.datetime.utcnow()
    out = {"kind": kind, "items": 0, "items_7d": 0, "items_30d": 0,
           "latest": []}
    if not text:
        return out
    dates, titles = [], []
    if kind == "rss":
        items = _RSS_ITEM_RE.findall(text)
        out["items"] = len(items)
        for it in items:
            dm = _RSS_PUBDATE_RE.search(it)
            d = _parse_dt(dm.group(1)) if dm else None
            if d:
                dates.append(d)
            tm = _RSS_TITLE_RE.search(it)
            if tm and tm.group(1).strip():
                titles.append((d or datetime.datetime.min,
                               _TAG_RE.sub("", tm.group(1)).strip()[:160]))
    else:  # news_sitemap
        for dm in _NEWS_DATE_RE.finditer(text):
            d = _parse_dt(dm.group(1) or dm.group(2))
            if d:
                dates.append(d)
        out["items"] = len(dates)
        for tm in _NEWS_TITLE_RE.finditer(text):
            t = _TAG_RE.sub("", tm.group(1)).strip()[:160]
            if t:
                titles.append((datetime.datetime.min, t))
    for d in dates:
        try:
            age = (now - d).days
        except Exception:
            continue
        if age <= 7:
            out["items_7d"] += 1
        if age <= 30:
            out["items_30d"] += 1
    titles.sort(key=lambda x: x[0], reverse=True)
    out["latest"] = [t for _, t in titles[:3]]
    return out


_LOC_RE = re.compile(r"<loc>\s*([^<\s][^<]*?)\s*</loc>", re.I)

_SITEMAP_BUCKETS = (
    ("facility", ("data-center", "datacenter", "facility", "colocation")),
    ("market", ("market", "metro", "region", "location")),
    ("provider", ("provider", "operator", "company", "carrier")),
    ("news", ("news", "blog", "article")),
)


def parse_sitemap_surface(text: str) -> dict:
    """Bucket a sitemap(-index)'s <loc> URLs into surface categories."""
    locs = _LOC_RE.findall(text or "")
    buckets = {name: 0 for name, _ in _SITEMAP_BUCKETS}
    for u in locs:
        low = u.lower()
        for name, keys in _SITEMAP_BUCKETS:
            if any(k in low for k in keys):
                buckets[name] += 1
                break
    return {"locs": len(locs), "buckets": buckets}


# ─────────────────────────────────────────────────────────────────────
# SCORING + ASSESSMENT (pure functions)
# ─────────────────────────────────────────────────────────────────────

def ai_access_score(sig: dict) -> tuple:
    """(score 0-2, evidence) — 3 is reserved for an actual agent surface
    (MCP), which no tracked rival has."""
    if sig.get("policy") == "no_crawl_tos":
        return 0, "ToS prohibits automated access — agents cannot read it"
    rb = sig.get("robots") or {}
    blocked = rb.get("blocks_ai") or []
    llms = bool((sig.get("llms_txt") or {}).get("present"))
    if llms and not blocked:
        return 2, "llms.txt present and no AI-crawler blocks"
    if len(blocked) >= 3:
        return 0, ("robots.txt blocks %d AI crawlers (%s)"
                   % (len(blocked), ", ".join(blocked[:4])))
    if blocked:
        return 1, "robots.txt blocks %s" % ", ".join(blocked)
    if rb.get("fetched"):
        return 1, "no explicit AI-crawler policy (default-open robots)"
    return 1, "robots.txt unreachable this run"


def build_target_row(slug: str, sig: dict) -> dict:
    """Static seed axes + dynamic overrides from this run's signals."""
    row = dict((STATIC_PROFILES.get(slug) or {}).get("axes") or
               {a: 0 for a, _ in AXES})
    score, evidence = ai_access_score(sig)
    row["ai_agent_access"] = score
    sig["ai_access_evidence"] = evidence
    pricing = sig.get("pricing") or {}
    if pricing.get("found") and pricing.get("tier_words"):
        row["pricing_transparency"] = max(row.get("pricing_transparency", 0), 2)
    return row


def dchub_row_with_evidence() -> tuple:
    """DC Hub's column + live evidence phrases from canonical_stats
    (fail-soft: axes are static, only the cited numbers are live)."""
    stats = {}
    try:
        from canonical_stats import get_canonical_stats
        stats = get_canonical_stats() or {}
    except Exception:
        stats = {}
    ev = []
    if stats.get("facilities"):
        ev.append("%s facilities listed" % stats["facilities"])
    if stats.get("markets"):
        ev.append("%s DCPI markets" % stats["markets"])
    if stats.get("countries"):
        ev.append("%s countries" % stats["countries"])
    ev.append("MCP-native (free key, public pricing, live ISO telemetry)")
    return dict(_DCHUB_AXES), ev


def assess_target(target: dict, sig: dict, row: dict) -> dict:
    """The per-rival good/bad lists: curated seeds + this run's probes."""
    slug = target["slug"]
    prof = STATIC_PROFILES.get(slug) or {}
    good = list(prof.get("strengths") or [])
    bad = list(prof.get("weaknesses") or [])

    feed = sig.get("feed") or {}
    if feed.get("items_7d", 0) >= 10:
        good.append("High editorial velocity: %d pieces in the last 7 days"
                    % feed["items_7d"])
    elif target.get("category") == "media" and feed.get("items") == 0 \
            and sig.get("fetches", 0) > 0:
        bad.append("Feed unreachable/empty this run — stale signal")

    if row.get("ai_agent_access", 0) == 0:
        bad.append("Closed to AI agents: %s"
                   % (sig.get("ai_access_evidence") or "no agent access"))
    elif row.get("ai_agent_access", 0) >= 2:
        good.append("Agent-open posture: %s"
                    % (sig.get("ai_access_evidence") or ""))

    pricing = sig.get("pricing") or {}
    if pricing.get("found"):
        good.append("Public pricing page (%s)" % pricing.get("url", ""))
    elif target.get("category") == "analyst_platform" \
            and sig.get("policy") != "no_crawl_tos":
        bad.append("No public pricing found — demo-gated sales motion")

    sm = sig.get("sitemap") or {}
    if sm.get("locs", 0) >= 200:
        good.append("Large public SEO surface (~%d sitemap URLs)" % sm["locs"])

    home = sig.get("home") or {}
    if home and sig.get("home_status") not in (None, 200):
        bad.append("Homepage fetch failed (HTTP %s)" % sig.get("home_status"))
    kw = (home.get("keywords") or {})
    if kw.get("mcp") or kw.get("ai_agent"):
        bad.append("MARKETING SHIFT: now pitching AI-agent language on its "
                   "homepage — direct move onto DC Hub's turf")
    return {"good": good[:8], "bad": bad[:8], "axes": row}


def synthesize(rows: dict, dchub_row: dict, dchub_evidence: list,
               signals: dict, prev_titles: dict) -> dict:
    """Cross-rival synthesis: gaps (ours + whitespace), ranked win moves,
    AI-access exhibit, positioning-shift watch, TL;DR."""
    axes_keys = [a for a, _ in AXES]
    labels = dict(AXES)

    dchub_gaps, whitespace = [], []
    for ax in axes_keys:
        rival_best = max((rows[s].get(ax, 0) for s in rows), default=0)
        ours = dchub_row.get(ax, 0)
        if rival_best >= 2 and ours <= 1:
            leaders = sorted([s for s in rows if rows[s].get(ax, 0) >= 2],
                             key=lambda s: -rows[s].get(ax, 0))
            dchub_gaps.append({"axis": ax, "label": labels[ax],
                               "dchub": ours, "rival_best": rival_best,
                               "leaders": leaders[:4]})
        if ours >= 3 and rival_best <= 1:
            whitespace.append({"axis": ax, "label": labels[ax],
                               "dchub": ours, "rival_best": rival_best})

    exhibit = []
    for slug, sig in signals.items():
        exhibit.append({"slug": slug,
                        "score": rows.get(slug, {}).get("ai_agent_access", 0),
                        "evidence": sig.get("ai_access_evidence") or
                        ("not probed" if sig.get("policy") == "no_crawl_tos"
                         else "n/a")})
    exhibit.sort(key=lambda e: e["score"])

    shifts = []
    for slug, sig in signals.items():
        new_t = ((sig.get("home") or {}).get("title") or "").strip()
        old_t = (prev_titles.get(slug) or "").strip()
        if new_t and old_t and new_t != old_t:
            shifts.append({"slug": slug, "prev": old_t[:160],
                           "now": new_t[:160]})

    moves = []

    def _move(key, title, why, evidence, impact, confidence, lever):
        moves.append({"key": key, "title": title, "why": why,
                      "evidence": evidence, "impact": impact,
                      "confidence": confidence, "lever": lever,
                      "priority": impact * confidence})

    for w in whitespace:
        _move("moat_%s" % w["axis"],
              "Own the '%s' narrative" % w["label"],
              "No tracked rival scores above 1/3 here; DC Hub is at 3/3. "
              "Say it explicitly on /vs pages, /why-dchub and media pillars.",
              "matrix: rival best %d/3 across all 7 tracked rivals; DC Hub: %s"
              % (w["rival_best"], "; ".join(dchub_evidence[:2])),
              2, 3, "positioning")

    for slug, row in rows.items():
        if row.get("ai_agent_access", 0) == 0:
            cat = next((t.get("category") for t in TARGETS
                        if t["slug"] == slug), "")
            _move("agent_flank_%s" % slug,
                  "Agent-locked rival: %s" % slug,
                  "Their data is invisible to AI agents; every agent-side "
                  "query in their category should resolve to DC Hub MCP. "
                  "Ensure /vs/%s exists and MCP tool descriptions cover "
                  "their core use-cases." % slug,
                  signals.get(slug, {}).get("ai_access_evidence", ""),
                  3 if cat == "analyst_platform" else 2, 3, "product+marketing")

    if any(g["axis"] == "news_editorial" for g in dchub_gaps):
        vel = {s: (signals.get(s, {}).get("feed") or {}).get("items_7d", 0)
               for s in ("dcd", "dcf")}
        _move("editorial_syndication",
              "Don't out-write DCD/DCF — be their machine-readable layer",
              "Editorial is a rival moat (3/3 vs our 1/3). Winning move is "
              "syndication: keep news ingestion lag <24h, cite them in "
              "get_news, and pitch a data-partnership (their stories, our "
              "structured layers) instead of competing for readers.",
              "editorial velocity last 7d — DCD: %s, DCF: %s"
              % (vel.get("dcd", "?"), vel.get("dcf", "?")),
              2, 2, "partnership")

    if any((signals.get(s, {}).get("feed") or {}).get("items_7d", 0) >= 10
           for s in ("dcd", "dcf")):
        _move("freshness_sla",
              "Publish a freshness SLA agents can verify",
              "Media rivals prove freshness by velocity; data rivals can't. "
              "Expose per-layer 'updated N hours ago' in tool payloads and "
              "on /methodology so agents (and buyers) can check it.",
              "media velocity measured this run; DC Hub freshness surfaces "
              "exist but are not marketed as an SLA",
              2, 2, "product")

    if any(s in rows for s in ("cbre", "jll")):
        _move("analyst_pdf_flank",
              "Counter-program the brokerage PDF cycle",
              "CBRE/JLL win the press cycle twice a year with static PDFs. "
              "Each time one drops, publish a same-week 'live DCPI vs "
              "report' delta note — live data vs stale snapshot is our "
              "structural advantage.",
              "brokerage cadence is semiannual/quarterly; DCPI is continuous",
              2, 2, "media")

    for sh in shifts[:3]:
        _move("positioning_shift_%s" % sh["slug"],
              "Positioning shift at %s" % sh["slug"],
              "Homepage title changed since last recon — review whether it "
              "moves onto DC Hub territory and refresh /vs/%s." % sh["slug"],
              "'%s' -> '%s'" % (sh["prev"], sh["now"]),
              1, 3, "watch")

    moves.sort(key=lambda m: (-m["priority"], m["key"]))
    seen, uniq = set(), []
    for m in moves:
        if m["key"] in seen:
            continue
        seen.add(m["key"])
        uniq.append(m)

    tldr = [
        "%d rivals scanned, %d fetches, robots respected; DC Byte not "
        "crawled (ToS)." % (len(signals),
                            sum(s.get("fetches", 0) for s in signals.values())),
        "Moats no rival touches: %s."
        % (", ".join(w["label"] for w in whitespace) or "none this run"),
        "Top move: %s." % (uniq[0]["title"] if uniq else "none"),
    ]
    return {"gaps": {"dchub_gaps": dchub_gaps, "whitespace_moats": whitespace},
            "win_moves": uniq, "ai_access_exhibit": exhibit,
            "positioning_shifts": shifts, "tldr": tldr}


def render_report_md(run_date: str, rows: dict, dchub_row: dict,
                     dchub_evidence: list, assessments: dict,
                     synthesis: dict) -> str:
    """Compact weekly report (stored in the synthesis row; the brain and
    humans read the same artifact)."""
    L = []
    L.append("# Competitor Recon — %s" % run_date)
    L.append("")
    for t in synthesis.get("tldr") or []:
        L.append("- %s" % t)
    L.append("")
    L.append("## Capability matrix (0-3)")
    slugs = list(rows.keys())
    L.append("| Axis | dchub | " + " | ".join(slugs) + " |")
    L.append("|---" * (len(slugs) + 2) + "|")
    for ax, label in AXES:
        L.append("| %s | **%d** | %s |"
                 % (label, dchub_row.get(ax, 0),
                    " | ".join(str(rows[s].get(ax, 0)) for s in slugs)))
    L.append("")
    L.append("DC Hub evidence: %s" % "; ".join(dchub_evidence))
    L.append("")
    L.append("## AI-agent access exhibit")
    for e in synthesis.get("ai_access_exhibit") or []:
        L.append("- **%s** %d/3 — %s" % (e["slug"], e["score"], e["evidence"]))
    L.append("")
    L.append("## Per-rival: the good / the bad")
    for slug, a in assessments.items():
        L.append("### %s" % slug)
        for g in a.get("good") or []:
            L.append("- GOOD: %s" % g)
        for b in a.get("bad") or []:
            L.append("- BAD: %s" % b)
        L.append("")
    L.append("## Gaps")
    for g in (synthesis.get("gaps") or {}).get("dchub_gaps") or []:
        L.append("- OURS TO CLOSE: %s — rivals best %d/3 (%s), DC Hub %d/3"
                 % (g["label"], g["rival_best"], ", ".join(g["leaders"]),
                    g["dchub"]))
    for w in (synthesis.get("gaps") or {}).get("whitespace_moats") or []:
        L.append("- MOAT (nobody else has it): %s" % w["label"])
    L.append("")
    L.append("## Win moves (ranked)")
    for i, m in enumerate((synthesis.get("win_moves") or [])[:10], 1):
        L.append("%d. **%s** [P%d, %s] — %s Evidence: %s"
                 % (i, m["title"], m["priority"], m["lever"], m["why"],
                    m["evidence"]))
    L.append("")
    L.append("_Method: public pages only, honest UA (%s), robots.txt "
             "respected, DC Byte never fetched (ToS). Static axis seeds are "
             "curated; ai_agent_access + pricing_transparency + velocity are "
             "re-measured each run._" % RECON_USER_AGENT)
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS competitor_recon_reports (
    id          BIGSERIAL PRIMARY KEY,
    run_date    DATE NOT NULL,
    target_slug TEXT NOT NULL,
    signals     JSONB,
    assessment  JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_competitor_recon_run
    ON competitor_recon_reports (run_date, target_slug);
CREATE INDEX IF NOT EXISTS ix_competitor_recon_recent
    ON competitor_recon_reports (target_slug, run_date DESC);
"""


def _conn():
    """Transactional conn (NO autocommit: the canonical findings writer
    is savepoint-wrapped and savepoints are no-ops under autocommit)."""
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _ran_recently(gate_days: int = None) -> bool:
    """Weekly gate. Fail-open on missing table (first run must proceed);
    fail-CLOSED on connect failure (don't crawl if we can't persist)."""
    days = _GATE_DAYS if gate_days is None else gate_days
    c = _conn()
    if c is None:
        return True
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM competitor_recon_reports "
                "WHERE run_date > CURRENT_DATE - %s::int LIMIT 1", (days,))
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────

def _collect_signals(budget_s: float) -> tuple:
    """Fetch phase. NO db connection is held here (fetches are slow and
    rate-limited; a held conn would be force-reclaimed mid-run)."""
    t0 = time.monotonic()
    signals: dict = {}
    total_fetches = 0
    for target in TARGETS:
        slug = target["slug"]
        sig = {"policy": target["policy"], "category": target["category"],
               "fetches": 0, "errors": []}
        signals[slug] = sig
        if target["policy"] == "no_crawl_tos":
            sig["note"] = target.get("note", "")
            continue
        landing_done = False
        for label, url in planned_fetches(target):
            if time.monotonic() - t0 > budget_s or total_fetches >= _MAX_FETCHES:
                sig["errors"].append("budget_exhausted")
                break
            if label == "landing" and landing_done:
                continue
            r = _get(url, target.get("disallowed_prefixes", ()))
            total_fetches += 1
            sig["fetches"] += 1
            time.sleep(_POLITE_DELAY_S)
            if label == "robots":
                sig["robots"] = (parse_robots(r["text"])
                                 if r["status"] == 200 else {"fetched": False})
            elif label == "llms":
                sig["llms_txt"] = {"present": r["status"] == 200,
                                   "status": r["status"]}
            elif label == "home":
                sig["home_status"] = r["status"]
                sig["home"] = parse_home(r["text"])
            elif label == "pricing":
                if r["status"] == 200 and not (sig.get("pricing") or {}).get("found"):
                    txt = r["text"]
                    sig["pricing"] = {
                        "found": True, "url": url,
                        "tier_words": bool(re.search(
                            r"per (?:month|user|seat)|/mo\b|tier|plan",
                            txt, re.I))}
                elif "pricing" not in sig:
                    sig["pricing"] = {"found": False, "status": r["status"]}
            elif label == "landing":
                if r["status"] == 200:
                    landing_done = True
                    sig["landing"] = {"url": url, "status": 200,
                                      "title": parse_home(r["text"]).get("title")}
                else:
                    sig.setdefault("landing", {"url": url,
                                               "status": r["status"]})
            elif label == "feed":
                sig["feed"] = (parse_feed_velocity(
                    r["text"], target["feed"]["kind"])
                    if r["status"] == 200 else {"kind": target["feed"]["kind"],
                                                "items": 0, "items_7d": 0,
                                                "items_30d": 0, "latest": [],
                                                "error": r["error"]})
            elif label == "sitemap":
                if r["status"] == 200:
                    sig["sitemap"] = parse_sitemap_surface(r["text"])
            if r.get("error"):
                sig["errors"].append("%s:%s" % (label, r["error"]))
    return signals, total_fetches, round(time.monotonic() - t0, 1)


def _persist_and_file(run_date, signals, rows, dchub_row, dchub_evidence,
                      assessments, synthesis, report_md) -> dict:
    """Persist phase: one short-lived transactional conn for everything
    (prev-run read happened before assessment; here we write rows, file
    findings via the canonical writer, verify landing, commit)."""
    out = {"rows": 0, "findings_filed": 0, "findings_landed": None,
           "errors": []}
    c = _conn()
    if c is None:
        out["errors"].append("no_database")
        return out
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
            for slug, sig in signals.items():
                cur.execute(
                    "INSERT INTO competitor_recon_reports "
                    " (run_date, target_slug, signals, assessment) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (run_date, target_slug) DO UPDATE SET "
                    " signals = EXCLUDED.signals, "
                    " assessment = EXCLUDED.assessment, "
                    " created_at = NOW()",
                    (run_date, slug, json.dumps(sig, default=str),
                     json.dumps(assessments.get(slug) or {}, default=str)))
                out["rows"] += 1
            synth_payload = {"matrix": {"dchub": dchub_row, "rivals": rows},
                             "dchub_evidence": dchub_evidence,
                             **synthesis, "report_md": report_md}
            cur.execute(
                "INSERT INTO competitor_recon_reports "
                " (run_date, target_slug, signals, assessment) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (run_date, target_slug) DO UPDATE SET "
                " signals = EXCLUDED.signals, "
                " assessment = EXCLUDED.assessment, "
                " created_at = NOW()",
                (run_date, _SYNTH_SLUG, json.dumps({"targets": len(signals)}),
                 json.dumps(synth_payload, default=str)))
            out["rows"] += 1
        c.commit()

        try:
            from routes.brain_findings_writer import upsert_brain_finding
            with c.cursor() as cur:
                for m in (synthesis.get("win_moves") or [])[:_MAX_WIN_MOVE_FINDINGS]:
                    upsert_brain_finding(
                        cur,
                        issue="competitor_recon:win_move:%s" % m["key"],
                        url="dchub://competitor-recon/win/%s" % m["key"],
                        count=int(m["priority"]),
                        detail=("[P%d/%s] %s — %s Evidence: %s"
                                % (m["priority"], m["lever"], m["title"],
                                   m["why"], m["evidence"]))[:2000],
                        detector="competitor_recon", status="open")
                    out["findings_filed"] += 1
                # shell#35 (WS6): CBRE/JLL report-cycle counter-programming.
                # A brokerage positioning/report shift stages a DRAFT social
                # card (content_publisher.stage_draft → status='draft',
                # operator approves — never auto-sent).
                for sh in (synthesis.get("positioning_shifts") or []):
                    if sh.get("slug") not in ("cbre", "jll"):
                        continue
                    try:
                        from content_publisher import stage_draft
                        stage_draft(
                            ("%s just refreshed its data-center research "
                             "(\"%s\"). Analyst PDFs are snapshots — the DC Hub "
                             "Power Index is live, per-market, and agent-"
                             "queryable the moment conditions change: "
                             "https://dchub.cloud/dcpi") % (
                                sh["slug"].upper(), sh.get("now", "")[:90]),
                            platform="linkedin")
                    except Exception:
                        pass
                for sh in (synthesis.get("positioning_shifts") or [])[:_MAX_THREAT_FINDINGS]:
                    upsert_brain_finding(
                        cur,
                        issue="competitor_recon:threat:%s" % sh["slug"],
                        url="dchub://competitor-recon/threat/%s" % sh["slug"],
                        count=1,
                        detail=("[watch] homepage positioning changed: "
                                "'%s' -> '%s'" % (sh["prev"], sh["now"]))[:2000],
                        detector="competitor_recon", status="open")
                    out["findings_filed"] += 1
                upsert_brain_finding(
                    cur,
                    issue="competitor_recon:weekly_report",
                    url="dchub://competitor-recon/%s" % run_date,
                    count=len(signals),
                    detail=(" | ".join(synthesis.get("tldr") or []))[:2000],
                    detector="competitor_recon", status="resolved")
                out["findings_filed"] += 1
            c.commit()
            # Verify landing — never trust the filed counter (writer memo).
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM brain_findings "
                            "WHERE issue LIKE %s", ("competitor_recon:%",))
                out["findings_landed"] = int(cur.fetchone()[0])
        except Exception as e:
            try:
                c.rollback()
            except Exception:
                pass
            out["errors"].append("findings: %s" % str(e)[:160])
            logger.warning("competitor_recon: findings filing failed: %s", e)
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        out["errors"].append(str(e)[:200])
        logger.warning("competitor_recon: persist failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def _prev_titles() -> dict:
    """Homepage titles from the most recent PRIOR run (for shift watch)."""
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT target_slug, signals->'home'->>'title' "
                "FROM competitor_recon_reports "
                "WHERE target_slug <> %s AND run_date = ("
                "  SELECT MAX(run_date) FROM competitor_recon_reports "
                "  WHERE run_date < CURRENT_DATE)",
                (_SYNTH_SLUG,))
            for slug, title in cur.fetchall():
                if title:
                    out[slug] = title
    except Exception:
        return {}
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def reemit_findings() -> dict:
    """Re-file the CURRENT report's win moves without re-crawling.

    ★ WHY THIS EXISTS (two compounding gates, both discovered 2026-07-28):
    (1) `brain_consistency_radar` full-sweep RESOLVES any open finding whose
        `last_seen` is older than 24h, regardless of detector — correct for
        incident findings, fatal for a WEEKLY strategic detector, which is
        why all six recon findings read status=resolved seen=1.
    (2) the autopilot worklist only reads findings with
        `last_seen > NOW() - INTERVAL '10 minutes'`, so a weekly finding is
        actionable for ten minutes a week even when open.
    Re-emitting daily (cheap: reads the stored synthesis, no crawl) keeps
    the moves fresh, open, and inside the act window. Idempotent — the
    canonical writer upserts and bumps last_seen."""
    c = _conn()
    if c is None:
        return {"status": "no_database"}
    out = {"reemitted": 0}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT assessment FROM competitor_recon_reports "
                "WHERE target_slug = %s ORDER BY run_date DESC LIMIT 1",
                (_SYNTH_SLUG,))
            row = cur.fetchone()
        synth = (row[0] if row else None) or {}
        moves = (synth.get("win_moves") or [])[:_MAX_WIN_MOVE_FINDINGS]
        if not moves:
            return {"status": "no_report_yet"}
        from routes.brain_findings_writer import upsert_brain_finding
        with c.cursor() as cur:
            for m in moves:
                upsert_brain_finding(
                    cur,
                    issue="competitor_recon:win_move:%s" % m["key"],
                    url="dchub://competitor-recon/win/%s" % m["key"],
                    count=int(m.get("priority") or 1),
                    detail=("[P%s/%s] %s — %s Evidence: %s"
                            % (m.get("priority"), m.get("lever"),
                               m.get("title"), m.get("why"),
                               m.get("evidence")))[:2000],
                    detector="competitor_recon", status="open")
                out["reemitted"] += 1
        c.commit()
        out["status"] = "ok"
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        out["status"] = "error"
        out["error"] = str(e)[:160]
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def run_competitor_recon(budget_s: float = None, force: bool = False) -> dict:
    """Full recon pass. Weekly-gated unless force. Returns a summary dict
    (also the sync-endpoint response body)."""
    if not force and _ran_recently():
        # Gated day: still re-emit so the win moves stay inside the
        # autopilot's freshness window instead of aging into 'resolved'.
        return {"status": "skipped_recent", "gate_days": _GATE_DAYS,
                "reemit": reemit_findings()}
    budget = budget_s or _DEFAULT_BUDGET_S
    run_date = datetime.datetime.utcnow().date().isoformat()
    logger.info("competitor_recon: starting run %s (budget %ss)",
                run_date, budget)

    prev = _prev_titles()
    signals, fetches, fetch_secs = _collect_signals(budget)

    rows, assessments = {}, {}
    for target in TARGETS:
        slug = target["slug"]
        rows[slug] = build_target_row(slug, signals[slug])
        assessments[slug] = assess_target(target, signals[slug], rows[slug])
    dchub_row, dchub_evidence = dchub_row_with_evidence()
    synthesis = synthesize(rows, dchub_row, dchub_evidence, signals, prev)
    report_md = render_report_md(run_date, rows, dchub_row, dchub_evidence,
                                 assessments, synthesis)
    persist = _persist_and_file(run_date, signals, rows, dchub_row,
                                dchub_evidence, assessments, synthesis,
                                report_md)
    summary = {"status": "ok" if not persist["errors"] else "partial",
               "run_date": run_date, "targets": len(signals),
               "fetches": fetches, "fetch_secs": fetch_secs,
               "rows_written": persist["rows"],
               "findings_filed": persist["findings_filed"],
               "findings_landed": persist["findings_landed"],
               "win_moves": [m["key"] for m in
                             (synthesis.get("win_moves") or [])[:5]],
               "errors": persist["errors"]}
    logger.info("competitor_recon: done %s", summary)
    return summary


# ─────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────

def _authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    return not _ADMIN_KEY or provided == _ADMIN_KEY


@competitor_recon_bp.route("/api/v1/competitors/recon/run", methods=["POST"])
def recon_run_endpoint():
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    force = request.args.get("force") == "1"
    if request.args.get("sync") == "1":
        return jsonify(run_competitor_recon(force=force)), 200
    import threading

    def _bg():
        try:
            run_competitor_recon(force=force)
        except Exception as e:
            logger.warning("competitor_recon (bg): %s", str(e)[:160])

    threading.Thread(target=_bg, name="competitor-recon-manual",
                     daemon=True).start()
    return jsonify(status="spawned", force=force), 202


def act_on_win_moves() -> dict:
    """Perform the SAFE, mechanical half of the latest recon's win moves.

    The brain's act-loop calls this (pattern key `competitor_recon`). Before
    this existed the findings were a silent no_action: `_lookup_pattern`
    prefix-matches on `competitor_recon` and there was no entry, so the
    autopilot recognised nothing and the intelligence sat unused.

    ★ WHAT IS AUTOMATED vs NOT — the line matters:
      * agent_flank_<slug>  → VERIFY the /vs/<slug> comparison page actually
        serves. That is a fact check, safe to automate. If it is missing we
        file a specific, actionable finding; we do NOT auto-write a page.
      * moat_<axis>         → STAGE a draft positioning card via
        content_publisher.stage_draft (status='draft'). An operator approves
        it. Nothing is ever auto-published or auto-sent.
      * everything else (pricing, partnership, tightening a trial) is a
        COMMERCIAL judgement and is deliberately left to the owner.
    """
    out = {"checked_vs_pages": [], "missing_vs_pages": [],
           "drafts_staged": 0, "findings_filed": 0, "errors": []}
    c = _conn()
    if c is None:
        return {"status": "no_database"}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT assessment FROM competitor_recon_reports "
                "WHERE target_slug = %s ORDER BY run_date DESC LIMIT 1",
                (_SYNTH_SLUG,))
            row = cur.fetchone()
        synth = (row[0] if row else None) or {}
        moves = synth.get("win_moves") or []
        if not moves:
            return {"status": "no_report_yet"}

        import requests as _rq
        # ── agent-flank moves: is the comparison page actually live? ──
        _alias = {"dcbyte": "dc-byte", "dchawk": "datacenterhawk",
                  "dcd": "datacenterdynamics", "dcf": "data-center-frontier"}
        for m in moves:
            key = str(m.get("key") or "")
            if not key.startswith("agent_flank_"):
                continue
            slug = key.replace("agent_flank_", "")
            url = "https://dchub.cloud/vs/%s" % _alias.get(slug, slug)
            try:
                r = _rq.get(url, timeout=12, allow_redirects=True,
                            headers={"User-Agent": RECON_USER_AGENT})
                ok = r.status_code == 200 and len(r.text or "") > 2000
                out["checked_vs_pages"].append(
                    {"slug": slug, "url": url, "status": r.status_code,
                     "ok": ok})
                if not ok:
                    out["missing_vs_pages"].append(slug)
            except Exception as e:
                out["errors"].append("vs:%s:%s" % (slug, str(e)[:60]))

        # ── moat moves: stage ONE draft positioning card per moat ──
        # stage_draft dedups on a content hash, so re-running is idempotent
        # and a weekly re-emit cannot spam the queue.
        # ★ 2026-07-28 REWRITE. The first version pasted the win move's
        # title/why/evidence straight into the post body — but `why` is an
        # INTERNAL DIRECTIVE ("Say it explicitly on /vs pages…") and
        # `evidence` is internal scoring jargon ("matrix: rival best 1/3").
        # Both shipped into two drafts that read like leaked strategy notes.
        # A draft is aimed at a READER; the win move is aimed at US. Write
        # reader-facing copy from live facts instead, and never echo the
        # internal fields.
        try:
            from content_publisher import stage_draft
            _COPY = {
                "moat_live_telemetry": (
                    "Most data-center market data is a quarterly PDF. Grid "
                    "conditions are not quarterly.\n\n"
                    "DC Hub now serves measured headroom for all seven US "
                    "ISOs — generation minus demand, refreshed every 20 "
                    "minutes, with the source and the reading's age attached "
                    "to every answer.\n\n"
                    "It also publishes the awkward part. ERCOT's raw headroom "
                    "reads comfortable; corrected for a documented "
                    "measurement artifact in the feed, it is materially "
                    "tighter. Both numbers ship, with the method, because a "
                    "site decision made on the flattering one is a bad "
                    "decision.\n\n"
                    "Live, cited, and queryable by an AI agent directly."),
                "moat_power_grid_data": (
                    "\"Can this site get power?\" usually gets answered with "
                    "a distance to the nearest substation.\n\n"
                    "DC Hub now answers it with what the utility actually "
                    # ★ "records", NOT "feeders": _feeders is COUNT(*) on
                    # hosting_capacity_feeders, and that table stores one row per
                    # GIS geometry VERTEX — measured 2026-07-28 at ~15x (Ameren)
                    # to ~29x (Rhode Island Energy) rows per DISTINCT feeder. So
                    # the count is published RECORDS; calling them feeders
                    # over-claims the feeder count by more than an order of
                    # magnitude in public copy. (get_hosting_capacity reports
                    # distinct_feeders and geometry_rows_scanned separately for
                    # exactly this reason.)
                    "published: hosting capacity across 18 utility sources, %s "
                    "published records — including the utilities that publish "
                    "LOAD-serving capacity, which is the number a data "
                    "center needs, not the solar-hosting figure most maps "
                    "show.\n\n"
                    "Where a utility publishes thin data, we say so rather "
                    "than interpolating. Informational, not binding "
                    "interconnection guidance — verify with the utility.\n\n"
                    # ★ "over MCP" RESTORED 2026-07-28: the get_hosting_capacity
                    # tool now serves this layer (gateway v2.9.3, tool #81,
                    # free tier) — verified live in tools/list and via a keyless
                    # tools/call. The claim was correctly withheld until then;
                    # withdraw it again if that tool is ever removed.
                    "Live on the Land & Power map, the public API, and over "
                    "MCP — queryable by an AI agent directly."),
            }
            _feeders = "—"
            try:
                with c.cursor() as _fc:
                    _fc.execute("SELECT COUNT(*) FROM hosting_capacity_feeders")
                    _feeders = "{:,}".format(int(_fc.fetchone()[0]))
            except Exception:
                c.rollback()
            for m in moves:
                key = str(m.get("key") or "")
                if not key.startswith("moat_"):
                    continue
                copy = _COPY.get(key)
                if not copy:
                    continue          # no vetted copy → stage nothing
                if "%s" in copy:
                    copy = copy % _feeders
                res = stage_draft(
                    (copy + "\n\nhttps://dchub.cloud/dcpi")[:2800],
                    platform="linkedin")
                if (res or {}).get("action") == "inserted":
                    out["drafts_staged"] += 1
        except Exception as e:
            out["errors"].append("stage_draft:%s" % str(e)[:80])

        # ── file the outcome so the loop is measurable, not just executed ──
        try:
            from routes.brain_findings_writer import upsert_brain_finding
            with c.cursor() as cur:
                for slug in out["missing_vs_pages"]:
                    upsert_brain_finding(
                        cur,
                        issue="competitor_recon:vs_page_missing:%s" % slug,
                        url="dchub://competitor-recon/vs/%s" % slug,
                        count=1,
                        detail=("[action] %s is closed to AI agents, so its "
                                "category should resolve to DC Hub — but the "
                                "/vs comparison page does not serve. Create or "
                                "repair it (routes/competitive_seo.py is the "
                                "LIVE handler; competitive_vs.py is dead code)."
                                % slug)[:2000],
                        detector="competitor_recon", status="open")
                    out["findings_filed"] += 1
            c.commit()
        except Exception as e:
            c.rollback()
            out["errors"].append("findings:%s" % str(e)[:80])
    finally:
        try:
            c.close()
        except Exception:
            pass
    out["status"] = "ok"
    logger.info("competitor_recon act: %s", out)
    return out


@competitor_recon_bp.route("/api/v1/competitors/recon/act", methods=["POST"])
def recon_act_endpoint():
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    return jsonify(act_on_win_moves()), 200


@competitor_recon_bp.route("/api/v1/competitors/recon/latest", methods=["GET"])
def recon_latest_endpoint():
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT run_date, target_slug, signals, assessment "
                "FROM competitor_recon_reports WHERE run_date = "
                " (SELECT MAX(run_date) FROM competitor_recon_reports) "
                "ORDER BY target_slug")
            rows = cur.fetchall()
        if not rows:
            return jsonify(error="no_report"), 404
        out = {"run_date": rows[0][0].isoformat(), "targets": {},
               "synthesis": None}
        for run_date, slug, sig, assessment in rows:
            if slug == _SYNTH_SLUG:
                out["synthesis"] = assessment
            else:
                out["targets"][slug] = {"signals": sig,
                                        "assessment": assessment}
        return jsonify(out), 200
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


@competitor_recon_bp.route("/api/v1/competitors/recon/matrix", methods=["GET"])
def recon_matrix_endpoint():
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT run_date, assessment FROM competitor_recon_reports "
                "WHERE target_slug = %s ORDER BY run_date DESC LIMIT 1",
                (_SYNTH_SLUG,))
            row = cur.fetchone()
        if not row:
            return jsonify(error="no_report"), 404
        assessment = row[1] or {}
        return jsonify(run_date=row[0].isoformat(),
                       matrix=assessment.get("matrix"),
                       ai_access_exhibit=assessment.get("ai_access_exhibit"),
                       tldr=assessment.get("tldr")), 200
    except Exception as e:
        return jsonify(error=str(e)[:200]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
