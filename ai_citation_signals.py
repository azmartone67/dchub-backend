"""AI citation signals — who fetched us for a USER, who crawled us, and who
CLICKED THROUGH from an assistant's answer (2026-09-24).

WHY THIS EXISTS
===============
Every AI number we published before this mixed two different events under one
platform name. ai_tracking.detect_platform() maps GPTBot, OAI-SearchBot and
ChatGPT-User all to `chatgpt`, and ClaudeBot and Claude-User both to `claude`.
A training crawl and "a person asked ChatGPT and it fetched our page" are not
the same claim, and only the second one says an assistant is sending people
to us. The third signal — a human who clicked a cited link — was recorded by
nothing at all:

  · the Flask after_request hook only sees /api, /mcp and the discovery files,
    and skips browser UAs ('direct');
  · the edge beacon (dchub-frontend beaconOrganicCrawl) fires only for AI
    crawler UAs, so a Chrome UA arriving from chatgpt.com never reaches it;
  · Perplexity-User and Meta-ExternalFetcher resolve to 'direct' / 'seo_bot'
    in detect_platform and are dropped at write time.

This module is the separate, additive measurement. It does NOT touch
detect_platform, ai_requests, ai_daily_stats or ai_cumulative — the existing
roster keeps its meaning, and this table answers a different question.

CLASSES (bounded — CLASSES below is the whole set)
==================================================
  user_fetch          a fetch an assistant makes because a user asked it to
                      (ChatGPT-User, Claude-User, Perplexity-User,
                      Meta-ExternalFetcher, Google-GeminiNotebook, ...).
  search_crawler      automated crawl that feeds an assistant's SEARCH index,
                      documented by its vendor as NOT training
                      (OAI-SearchBot, Claude-SearchBot, PerplexityBot,
                      Meta-WebIndexer). Its own class on purpose: OpenAI's own
                      docs call OAI-SearchBot search indexing, not a user
                      action, so counting it as user_fetch would inflate the
                      one number meant to prove a person asked.
  training_crawler    crawl for model training (GPTBot, ClaudeBot,
                      Meta-ExternalAgent, CCBot, Bytespider, ...).
  assistant_referral  a NON-bot UA whose Referer host, or whose utm_source,
                      is an assistant (chatgpt.com, claude.ai, perplexity.ai,
                      gemini.google.com, copilot.microsoft.com, ...) on a
                      top-level navigation. This is the human click-through.
  other               a candidate the edge sent that is none of the above —
                      e.g. a bot UA carrying a chatgpt.com Referer. Recorded so
                      a rejected candidate is visible, never silently dropped.

Tokens were checked against the vendors' published pages on 2026-09-24:
  OpenAI     developers.openai.com/api/docs/bots
  Anthropic  support.claude.com (article 8896518)
  Perplexity docs.perplexity.ai/guides/bots
  Meta       developers.facebook.com/docs/sharing/webmasters/web-crawlers
  Google     developers.google.com/search/docs/crawling-indexing/...

★ Google-Extended and Applebot-Extended are robots.txt CONTROL tokens. Google
  says so in terms: "Google-Extended doesn't have a separate HTTP request user
  agent string." They are listed so the mapping is complete, and they will
  read 0 forever — that 0 is the absence of a UA, not the absence of Gemini
  training. Google's AI crawl arrives as Googlebot and cannot be separated
  from search by UA.

★ Microsoft publishes no Copilot fetch UA (Copilot grounds on Bingbot), so
  `microsoft` can only ever appear as assistant_referral. Plain bing.com is
  search and is NOT an assistant referral.

★ UA tokens are CLAIMED identities. Nothing here verifies them against the
  vendors' published IP ranges, so a spoofed ChatGPT-User counts. Stated in
  the read endpoint's `method` block, not left for a reader to discover.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

try:  # the self/probe + generic-lib markers the roster already maintains
    from ai_tracking import _GENERIC_LIB_UA_MARKERS, _INTERNAL_UA_MARKERS
except Exception:  # pragma: no cover — ai_tracking always importable in app
    _INTERNAL_UA_MARKERS = ("dchub", "dc-hub", "probe", "scanner", "uptime")
    _GENERIC_LIB_UA_MARKERS = ("python-requests", "python-httpx", "curl/",
                               "wget/", "go-http-client", "node-fetch", "axios")

USER_FETCH = "user_fetch"
SEARCH_CRAWLER = "search_crawler"
TRAINING_CRAWLER = "training_crawler"
ASSISTANT_REFERRAL = "assistant_referral"
OTHER = "other"
CLASSES = (USER_FETCH, SEARCH_CRAWLER, TRAINING_CRAWLER, ASSISTANT_REFERRAL,
           OTHER)

SOURCES = ("openai", "anthropic", "perplexity", "meta", "google", "microsoft",
           "apple", "bytedance", "commoncrawl", "none")

# (exact UA product token, class, source). Matched case-insensitively on token
# boundaries, so `Claude-User` never matches `ClaudeBot` and `PerplexityBot`
# never matches `Perplexity-User`. User-triggered tokens come first.
UA_TOKENS = (
    ("ChatGPT-User",          USER_FETCH,       "openai"),
    ("Claude-User",           USER_FETCH,       "anthropic"),
    ("Perplexity-User",       USER_FETCH,       "perplexity"),
    ("Meta-ExternalFetcher",  USER_FETCH,       "meta"),
    ("Google-GeminiNotebook", USER_FETCH,       "google"),
    ("Google-NotebookLM",     USER_FETCH,       "google"),
    ("Google-Agent",          USER_FETCH,       "google"),
    ("OAI-SearchBot",         SEARCH_CRAWLER,   "openai"),
    ("Claude-SearchBot",      SEARCH_CRAWLER,   "anthropic"),
    ("PerplexityBot",         SEARCH_CRAWLER,   "perplexity"),
    ("Meta-WebIndexer",       SEARCH_CRAWLER,   "meta"),
    ("GPTBot",                TRAINING_CRAWLER, "openai"),
    ("ClaudeBot",             TRAINING_CRAWLER, "anthropic"),
    ("anthropic-ai",          TRAINING_CRAWLER, "anthropic"),
    ("Meta-ExternalAgent",    TRAINING_CRAWLER, "meta"),
    ("Google-Extended",       TRAINING_CRAWLER, "google"),   # robots-only
    ("Applebot-Extended",     TRAINING_CRAWLER, "apple"),    # robots-only
    ("CCBot",                 TRAINING_CRAWLER, "commoncrawl"),
    ("Bytespider",            TRAINING_CRAWLER, "bytedance"),
)
AGENTS = tuple(t for t, _, _ in UA_TOKENS)

_TOKEN_RES = tuple(
    (re.compile(r"(?<![a-z0-9-])" + re.escape(tok.lower()) + r"(?![a-z0-9-])"),
     tok, klass, src)
    for tok, klass, src in UA_TOKENS
)

# Referer hosts (after stripping one leading "www.") and Android app referrers
# (android-app://<package>/ parses to the package as the host).
REFERRAL_HOSTS = {
    "chatgpt.com": "openai",
    "chat.openai.com": "openai",
    "com.openai.chatgpt": "openai",
    "claude.ai": "anthropic",
    "com.anthropic.claude": "anthropic",
    "perplexity.ai": "perplexity",
    "ai.perplexity.app.android": "perplexity",
    "gemini.google.com": "google",
    "bard.google.com": "google",
    "copilot.microsoft.com": "microsoft",
    "copilot.cloud.microsoft": "microsoft",
    "edgeservices.bing.com": "microsoft",   # Copilot in the Edge sidebar
    "meta.ai": "meta",
}

# utm_source values an assistant appends to cited links. EXACT values only:
# our own links carry utm_source=mcp / meta-ai / mcp_upgrade and friends
# (grep utm_source= in this repo), and a prefix match would count those.
REFERRAL_UTM = {
    "chatgpt.com": "openai",
    "chat.openai.com": "openai",
    "claude.ai": "anthropic",
    "perplexity.ai": "perplexity",
    "perplexity": "perplexity",
    "gemini.google.com": "google",
    "copilot.microsoft.com": "microsoft",
}

REFERRAL_AGENTS = ("referer", "utm_source", "referer+utm_source")
OTHER_AGENTS = ("bot_ua", "non_document", "no_signal")

_BOT_MARKERS = ("bot", "crawler", "spider", "scraper", "headless",
                "slurp", "fetcher", "preview", "lighthouse", "phantomjs")

# Fetch destinations that are a top-level page load. Sec-Fetch-Dest is absent
# on older browsers, so absence counts as a navigation.
_NAV_DESTS = ("", "document")

PATH_MAX = 200
_PATH_OK = re.compile(r"^/[A-Za-z0-9/_\-.~%]*$")
UNPARSEABLE_PATH = "(unrecognised)"


def _ua_token(ua_lower: str):
    for rx, tok, klass, src in _TOKEN_RES:
        if rx.search(ua_lower):
            return tok, klass, src
    return None


# Crawlers index or train; nobody is waiting on the fetch. USER_FETCH is a
# person's request and is deliberately NOT here.
CRAWLER_CLASSES = frozenset({SEARCH_CRAWLER, TRAINING_CRAWLER})


def ai_agent_for(user_agent: str):
    """(token, class, source) when the UA carries a known AI fetcher/crawler
    token (UA_TOKENS), else None. Public for callers outside this module
    (routes/auto_trial: no trial keys for these UAs)."""
    return _ua_token((user_agent or "").lower())


def _is_non_human_ua(ua_lower: str) -> bool:
    if not ua_lower:
        return True
    if any(m in ua_lower for m in _INTERNAL_UA_MARKERS):
        return True
    if any(m in ua_lower for m in _GENERIC_LIB_UA_MARKERS):
        return True
    return any(m in ua_lower for m in _BOT_MARKERS)


def referral_source_from_referer(referer: str):
    """Assistant source for a Referer, or None. Pure."""
    if not referer:
        return None
    try:
        host = (urlsplit(referer.strip()).hostname or "").lower()
    except Exception:
        return None
    if host.startswith("www."):
        host = host[4:]
    return REFERRAL_HOSTS.get(host)


def referral_source_from_query(query: str):
    """Assistant source for a query string's utm_source, or None. Pure."""
    if not query:
        return None
    try:
        vals = parse_qs(query.lstrip("?"), keep_blank_values=False).get(
            "utm_source") or []
    except Exception:
        return None
    for v in vals:
        v = (v or "").strip().lower()
        if v.startswith("www."):
            v = v[4:]
        if v in REFERRAL_UTM:
            return REFERRAL_UTM[v]
    return None


def classify(user_agent: str, referer: str = "", query: str = "",
             fetch_dest=None) -> dict:
    """Classify one request. Pure; never raises.

    Returns {"signal_class", "source", "agent"}, every value drawn from a
    fixed tuple in this module (CLASSES / SOURCES / AGENTS + REFERRAL_AGENTS +
    OTHER_AGENTS), so the stored cardinality is bounded by construction.

    Precedence:
      1. a named assistant UA token wins (a ChatGPT-User fetch of a
         utm_source=chatgpt.com URL is a user_fetch, not a click);
      2. any other non-human UA (bot/crawler, generic HTTP lib, our own
         probes) can never be an assistant_referral;
      3. a human UA with an assistant Referer or utm_source on a top-level
         navigation is an assistant_referral;
      4. everything else is `other`.
    """
    ua_lower = (user_agent or "").lower()
    try:
        hit = _ua_token(ua_lower)
        if hit and not any(m in ua_lower for m in _INTERNAL_UA_MARKERS):
            tok, klass, src = hit
            return {"signal_class": klass, "source": src, "agent": tok}

        ref_src = referral_source_from_referer(referer)
        utm_src = referral_source_from_query(query)
        src = ref_src or utm_src
        if src is None:
            return {"signal_class": OTHER, "source": "none",
                    "agent": "no_signal"}
        if _is_non_human_ua(ua_lower):
            return {"signal_class": OTHER, "source": src, "agent": "bot_ua"}
        dest = (fetch_dest or "").strip().lower()
        if dest not in _NAV_DESTS:
            return {"signal_class": OTHER, "source": src,
                    "agent": "non_document"}
        if ref_src and utm_src:
            agent = "referer+utm_source"
        elif ref_src:
            agent = "referer"
        else:
            agent = "utm_source"
        return {"signal_class": ASSISTANT_REFERRAL, "source": src,
                "agent": agent}
    except Exception:
        return {"signal_class": OTHER, "source": "none", "agent": "no_signal"}


def normalize_landing_path(path: str) -> str:
    """Query and fragment stripped, trailing slash dropped (root kept), capped
    at PATH_MAX. Anything outside a plain URL-path alphabet collapses to one
    UNPARSEABLE_PATH bucket, so a hostile path cannot mint rows freely."""
    p = (path or "").split("?", 1)[0].split("#", 1)[0].strip()
    if not p:
        return "/"
    if not p.startswith("/") or not _PATH_OK.match(p):
        return UNPARSEABLE_PATH
    if len(p) > 1:
        p = p.rstrip("/") or "/"
    return p[:PATH_MAX]


# Classes whose landing path is stored. Crawler paths are not: their volume
# is thousands a day and the question for them is "how much", not "where".
PATH_CLASSES = (ASSISTANT_REFERRAL, USER_FETCH)


# ═══════════════════════════════════════════════════════════════
#  STORAGE — one daily aggregate row per (day, class, source, agent, path)
# ═══════════════════════════════════════════════════════════════

TABLE = "ai_citation_daily"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    day           DATE         NOT NULL,
    signal_class  VARCHAR(24)  NOT NULL,
    source        VARCHAR(16)  NOT NULL,
    agent         VARCHAR(40)  NOT NULL,
    landing_path  VARCHAR(200) NOT NULL DEFAULT '',
    hits          INTEGER      NOT NULL DEFAULT 0,
    first_seen    TIMESTAMPTZ,
    last_seen     TIMESTAMPTZ,
    PRIMARY KEY (day, signal_class, source, agent, landing_path)
)
"""

UPSERT = f"""
INSERT INTO {TABLE}
    (day, signal_class, source, agent, landing_path, hits, first_seen, last_seen)
VALUES (%s, %s, %s, %s, %s, 1, %s, %s)
ON CONFLICT (day, signal_class, source, agent, landing_path) DO UPDATE SET
    hits = {TABLE}.hits + 1,
    last_seen = EXCLUDED.last_seen
"""

_ddl_done = False


def row_for(cls: dict, path: str, now=None) -> tuple:
    """The UPSERT parameters for one classified hit. Pure."""
    now = now or datetime.now(timezone.utc)
    landing = (normalize_landing_path(path)
               if cls["signal_class"] in PATH_CLASSES else "")
    return (now.date(), cls["signal_class"], cls["source"], cls["agent"],
            landing, now, now)


def record(cls: dict, path: str, execute) -> bool:
    """Write one hit through `execute(sql, params)` (ai_tracking._execute in
    the app). Fail-soft: returns False instead of raising."""
    global _ddl_done
    try:
        if not _ddl_done:
            execute(DDL)
            _ddl_done = True
        execute(UPSERT, row_for(cls, path))
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  READ SQL — all windows are whole UTC days ending today
# ═══════════════════════════════════════════════════════════════

def window_counts_sql(days: int) -> str:
    days = max(1, min(int(days), 90))
    return (f"SELECT signal_class, source, agent, SUM(hits)::bigint AS hits "
            f"FROM {TABLE} "
            f"WHERE day > (NOW() AT TIME ZONE 'UTC')::date - {days} "
            f"GROUP BY signal_class, source, agent")


def daily_series_sql(days: int) -> str:
    days = max(1, min(int(days), 90))
    return (f"SELECT day, signal_class, source, SUM(hits)::bigint AS hits "
            f"FROM {TABLE} "
            f"WHERE day > (NOW() AT TIME ZONE 'UTC')::date - {days} "
            f"GROUP BY day, signal_class, source ORDER BY day, signal_class, source")


def top_paths_sql(signal_class: str, days: int, limit: int = 20) -> str:
    if signal_class not in PATH_CLASSES:
        raise ValueError(signal_class)
    days = max(1, min(int(days), 90))
    limit = max(1, min(int(limit), 100))
    return (f"SELECT landing_path, source, SUM(hits)::bigint AS hits "
            f"FROM {TABLE} "
            f"WHERE signal_class = '{signal_class}' "
            f"AND day > (NOW() AT TIME ZONE 'UTC')::date - {days} "
            f"GROUP BY landing_path, source "
            f"ORDER BY hits DESC, landing_path LIMIT {limit}")


def empty_window() -> dict:
    return {"total": 0,
            "by_class": {c: 0 for c in CLASSES},
            "by_class_source": {c: {} for c in CLASSES},
            "by_agent": {}}


def fold_window(rows) -> dict:
    """Fold (signal_class, source, agent, hits) rows into the window shape.
    Every class key is always present, so a zero is a zero, not a missing
    key a reader has to guess about. Pure."""
    w = empty_window()
    for r in rows or ():
        c, s, a, n = r["signal_class"], r["source"], r["agent"], int(r["hits"] or 0)
        if c not in w["by_class"]:
            continue
        w["total"] += n
        w["by_class"][c] += n
        w["by_class_source"][c][s] = w["by_class_source"][c].get(s, 0) + n
        w["by_agent"][a] = w["by_agent"].get(a, 0) + n
    return w
