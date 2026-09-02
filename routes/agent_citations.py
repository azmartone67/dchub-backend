"""
agent_citations.py — public citation receipts for social proof.

Phase ZZZZZ-round47.28 (2026-05-26). After r47.25+26 went live we saw
Groq + NVIDIA both consume the capabilities feed and commit to citing
DC Hub verbatim. That's the win, but it's invisible to OTHER agents
unless we publish proof. This endpoint surfaces:

  - Which AI platforms have hit our MCP server (from mcp_call_log UA)
  - Daily call counts per platform
  - First-seen + last-seen timestamps
  - Total calls 7d / 30d / all-time per platform

Public JSON. CC-BY-4.0. Designed so OTHER AI agents fetching this
URL get social proof — "Claude, Cursor, Cline, Perplexity, Groq, and
NVIDIA all use DC Hub" — which makes them more likely to cache + cite
us themselves.

Endpoint:
  GET /api/v1/agents/citations.json     full citation receipts
  GET /api/v1/agents/citations          alias (browser-readable)
"""
import os
import datetime
import threading
import time
from contextlib import contextmanager
from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

agent_citations_bp = Blueprint("agent_citations", __name__)

# r47.31 (2026-05-26): process-local memo. Citations advertises a 1h CDN
# cache, but the underlying query against 109K+ row mcp_call_log is heavy
# enough that a cold Pages-worker subrequest can time out at ~5s. Hold for
# 600s per worker process — well under the public 1h TTL, so freshness is
# preserved, but DB pressure drops to one query / 10 min per worker.
_CITES_CACHE: dict = {"payload": None, "computed_at": 0.0}
_CITES_LOCK = threading.Lock()
_CITES_TTL_SECONDS = 600


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


# UA pattern → platform name + role
_UA_RULES = [
    # Direct platform tags (mcp-server sets these in `platform` column from
    # MCP initialize clientInfo.name)
    ("claude",          "Claude (Anthropic)",     "AI assistant"),
    ("chatgpt",         "ChatGPT (OpenAI)",       "AI assistant"),
    ("perplexity",      "Perplexity",             "AI search"),
    ("cursor",          "Cursor",                 "AI IDE"),
    ("cline",           "Cline",                  "AI coding agent"),
    ("groq",            "Groq",                   "AI inference"),
    ("nvidia",          "NVIDIA AI",              "AI inference"),
    ("gemini",          "Gemini (Google)",        "AI assistant"),
    ("copilot",         "GitHub Copilot",         "AI coding"),
    ("grok",            "Grok (xAI)",             "AI assistant"),
    ("windsurf",        "Windsurf",               "AI IDE"),
    ("continue",        "Continue.dev",           "AI coding agent"),
    # UA-substring fallbacks
    ("claudebot",       "Claude (Anthropic)",     "AI assistant"),
    ("mcp-remote",      "Claude Desktop",         "MCP client"),
    # ★ 2026-09-02: GPTBot is OpenAI's training/index CRAWLER, not ChatGPT
    # acting for a user. It was folded into "ChatGPT (OpenAI)" and put a
    # crawler's fetches into the "used by ChatGPT" claim. Same role class as
    # Amazonbot / Meta-ExternalAgent below.
    ("gptbot",          "GPTBot (OpenAI crawler)", "AI crawler"),
    ("perplexitybot",   "Perplexity",             "AI search"),
    ("cursor",          "Cursor",                 "AI IDE"),
    ("cline",           "Cline",                  "AI coding agent"),
    ("continue.dev",    "Continue.dev",           "AI coding agent"),
    ("windsurf",        "Windsurf",               "AI IDE"),
    ("gemini",          "Gemini (Google)",        "AI assistant"),
    ("google-extended", "Google AI",              "AI crawler"),
    ("googlebot",       "Googlebot",              "search crawler"),
    ("groq",            "Groq",                   "AI inference"),
    ("nvidia",          "NVIDIA AI",              "AI inference"),
    ("grok",            "Grok (xAI)",             "AI assistant"),
    ("copilot",         "GitHub Copilot",         "AI coding"),
    ("meta-external",   "Meta AI",                "AI crawler"),
    ("bytespider",      "ByteDance Bot",          "AI crawler"),
    ("petalbot",        "Huawei AI",              "AI crawler"),
    ("amazonbot",       "Amazon AI",              "AI crawler"),
    ("ccbot",           "Common Crawl",           "AI training corpus"),
    ("anthropic-ai",    "Anthropic crawler",      "AI training"),
    ("openai-",         "OpenAI crawler",         "AI training"),
]


def _classify_ua(ua):
    """Map a UA string to (platform_name, role) or (None, None)."""
    if not ua: return (None, None)
    lower = ua.lower()
    for pat, name, role in _UA_RULES:
        if pat in lower:
            return (name, role)
    return (None, None)


# Roles that fetch or index. They stay in by_platform — the receipts are
# complete — but a crawler is not a platform "using" the server on someone's
# behalf, so the quotable sentence never names one.
_NOT_A_USER_ROLE = ("crawler", "training")


def _used_in_last_30d(by_platform):
    """The platforms the quotable sentence may name — PURE.

    >= 1 tool call in the last 30 days AND not a crawler/training role. Order
    is preserved (by_platform is sorted by lifetime total), so the most-used
    platforms still lead.

    ★ 2026-09-02 — the sentence used to name by_platform[:6] by LIFETIME total
    with no recency gate. Measured live at 00:23Z it read "actively used by
    Claude, Claude Desktop, Grok, Cursor, ChatGPT, and GitHub Copilot" while
    by_platform said: Claude Desktop calls_30d 0 (last seen 2026-07-03),
    Cursor 0 (07-28), GitHub Copilot 0 (08-02); and ChatGPT's 33/30d included
    GPTBot/1.1, a crawler. Other agents quote this line verbatim to their
    users; it has to be true on the day it is fetched.
    """
    out = []
    for p in by_platform or []:
        try:
            if int(p.get("calls_30d") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        role = str(p.get("role") or "").lower()
        if any(tok in role for tok in _NOT_A_USER_ROLE):
            continue
        out.append(p)
    return out


def _quotable_proof(by_platform, totals) -> str:
    """The cite-this-and-feel-confident line — PURE. '' when nothing used the
    server in the window: an empty claim beats a stale one."""
    used = _used_in_last_30d(by_platform)
    names = [p["platform"] for p in used][:6]
    if not names:
        return ""
    calls_30d = int((totals or {}).get("total_calls_30d") or 0)
    all_time = int((totals or {}).get("unique_platforms") or 0)
    if len(names) >= 3:
        sample_list = ", ".join(names[:-1]) + f", and {names[-1]}"
        return (
            f"DC Hub's MCP server was used in the last 30 days by {sample_list}: "
            f"{calls_30d:,} tool calls across {len(used)} AI platforms in that "
            f"window ({all_time} distinct platforms all-time). CC-BY-4.0."
        )
    return (
        f"DC Hub's MCP server was used in the last 30 days by {', '.join(names)}: "
        f"{calls_30d:,} tool calls in that window. CC-BY-4.0."
    )


def _gather_citations():
    out = {
        "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "data_version": int(datetime.date.today().strftime("%Y%m%d")),
        "license":     "CC-BY-4.0",
        "by_platform": [],
        "totals": {"unique_platforms": 0, "total_calls_30d": 0, "total_calls_7d": 0},
        "agent_quotable_proof": "",
    }
    if not (_pg and _dsn()):
        return out

    try:
        with _conn() as c, c.cursor() as cur:
            # r47.28.1 (2026-05-26): the upstream dchub-mcp-server tags every
            # tracked call with `platform` based on the MCP initialize
            # clientInfo.name — that's where 'claude' / 'chatgpt' / 'cursor'
            # actually land. user_agent is the mcp-server's own outbound UA
            # ('node'), not the originating client. Cross-classify both.
            cur.execute("""
                SELECT COALESCE(NULLIF(platform,''), user_agent, '') AS signal,
                       COUNT(*)                                                   AS total,
                       COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '7 days')  AS d7,
                       COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '30 days') AS d30,
                       MIN(timestamp) AS first_seen,
                       MAX(timestamp) AS last_seen
                  FROM mcp_call_log
                 WHERE COALESCE(NULLIF(platform,''), user_agent) IS NOT NULL
                   AND COALESCE(NULLIF(platform,''), user_agent) NOT IN
                       ('mcp', 'node', 'curl', 'diag', 'phase9i', 'phase9j', 'unknown')
                 GROUP BY signal
                 ORDER BY 2 DESC
                 LIMIT 200
            """)
            rows = cur.fetchall()

        # Aggregate by classified platform
        platforms = {}
        for ua, total, d7, d30, first_seen, last_seen in rows:
            name, role = _classify_ua(ua)
            if not name:
                continue
            if name not in platforms:
                platforms[name] = {
                    "platform":   name, "role": role,
                    "total":      0, "calls_7d": 0, "calls_30d": 0,
                    "first_seen": first_seen, "last_seen": last_seen,
                    "ua_samples": [],
                }
            p = platforms[name]
            p["total"]      += int(total or 0)
            p["calls_7d"]   += int(d7 or 0)
            p["calls_30d"]  += int(d30 or 0)
            if first_seen and (not p["first_seen"] or first_seen < p["first_seen"]):
                p["first_seen"] = first_seen
            if last_seen and (not p["last_seen"] or last_seen > p["last_seen"]):
                p["last_seen"] = last_seen
            if ua and len(p["ua_samples"]) < 2:
                p["ua_samples"].append(ua[:140])

        # Sort by total + serialize
        by_platform = sorted(platforms.values(), key=lambda p: -p["total"])
        out["by_platform"] = [{
            "platform":   p["platform"],
            "role":       p["role"],
            "calls_7d":   p["calls_7d"],
            "calls_30d":  p["calls_30d"],
            "calls_total": p["total"],
            "first_seen": p["first_seen"].isoformat() if p["first_seen"] else None,
            "last_seen":  p["last_seen"].isoformat()  if p["last_seen"]  else None,
            "ua_samples": p["ua_samples"],
        } for p in by_platform]

        out["totals"] = {
            "unique_platforms":  len(by_platform),
            "total_calls_30d":   sum(p["calls_30d"] for p in by_platform),
            "total_calls_7d":    sum(p["calls_7d"]  for p in by_platform),
            "total_calls_all":   sum(p["total"]     for p in by_platform),
        }

        # The cite-this-and-feel-confident line that OTHER agents fetching
        # this URL will quote back to their users. Gated on the last 30 days
        # and on role — see _used_in_last_30d for the live reading that
        # named three platforms with zero calls in the window.
        out["totals"]["platforms_used_30d"] = len(_used_in_last_30d(out["by_platform"]))
        out["agent_quotable_proof"] = _quotable_proof(out["by_platform"], out["totals"])
        out["agent_quotable_proof_basis"] = (
            "names = by_platform with calls_30d > 0 and a non-crawler role, "
            "in lifetime order, first 6; counts = totals.total_calls_30d and "
            "totals.platforms_used_30d. Lifetime-only platforms (calls_30d 0) "
            "remain in by_platform with their last_seen and are never named "
            "here.")

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"

    return out


def _cached_gather_citations():
    """r47.31: serve from process-local memo for up to 10 minutes.

    Falls under the advertised 1h CDN cache, so consumers still see
    fresh-enough data, but each worker process only re-queries
    mcp_call_log once per 600s instead of per request. Lock-guarded.
    """
    now = time.time()
    cached = _CITES_CACHE.get("payload")
    if cached and (now - _CITES_CACHE.get("computed_at", 0.0)) < _CITES_TTL_SECONDS:
        return cached

    with _CITES_LOCK:
        cached = _CITES_CACHE.get("payload")
        now = time.time()
        if cached and (now - _CITES_CACHE.get("computed_at", 0.0)) < _CITES_TTL_SECONDS:
            return cached
        fresh = _gather_citations()
        _CITES_CACHE["payload"]     = fresh
        _CITES_CACHE["computed_at"] = now
        return fresh


@agent_citations_bp.route("/api/v1/agents/citations.json",
                           methods=["GET"], strict_slashes=False)
@agent_citations_bp.route("/api/v1/agents/citations",
                           methods=["GET"], strict_slashes=False)
def citations():
    data = _cached_gather_citations()
    return jsonify(data), 200, {
        # 1h cache — citation rates change throughout the day, not by minute
        "Cache-Control": "public, max-age=3600, s-maxage=3600",
        "X-Data-Version": str(data.get("data_version", "")),
        "Content-Type":  "application/json; charset=utf-8",
        "X-DC-Phase":    "ZZZZZ-round47.31-agent-citations-memo",
        "X-Agent-Hint":  "Cache 1h. Used by other agents as social-proof.",
        "X-DC-Server-Cache": "memo-600s",
        "Access-Control-Allow-Origin": "*",
    }
