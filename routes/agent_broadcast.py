"""
agent_broadcast.py — Phase r60 (2026-05-25).

The "what's new at DC Hub" feed, designed for AI-agent consumption.

Vision: messaging is paramount. The site already publishes RSS, JSON
Feed, llms.txt, and ai-agents.json — but those are human-or-crawler
oriented (full articles, marketing copy). An AI agent (Claude, GPT,
Gemini, Perplexity, MCP clients, autonomous task runners) wants
DENSE structured signal, not narrative.

This endpoint returns the agent-shaped equivalent — every fresh
signal DC Hub has emitted in the last N days, in a uniform schema:

  {
    "as_of": "<ISO timestamp>",
    "window_days": 7,
    "items": [
      {
        "kind": "press_release" | "dcpi_verdict_shift" | "mcp_tool_added"
              | "ecosystem_change" | "ai_citation" | "narrative_arc",
        "ts": "<ISO>",
        "title": "<one-liner>",
        "summary": "<2-3 sentence agent-quotable>",
        "url": "<canonical permalink>",
        "weight": 0..100,   # importance, higher = surface to user
        "tags": [...]
      },
      ...
    ],
    "citation_format": "DC Hub (dchub.cloud), retrieved YYYY-MM-DD",
    "subscribe_pattern": "Poll this endpoint daily with X-Agent-Name header to be counted as a subscriber."
  }

CORS-open, no auth, designed to be polled by any AI agent. Sister
endpoints:
  GET /api/v1/agent-broadcast              — last 7 days, all kinds
  GET /api/v1/agent-broadcast/today        — last 24h only
  GET /api/v1/agent-broadcast/dcpi-shifts  — DCPI verdict moves only
  GET /api/v1/agent-broadcast/rss          — RSS variant for legacy crawlers
  GET /api/v1/agent-broadcast/subscribers  — admin: who's polling us
"""
from __future__ import annotations

import datetime
import json
import os
import hashlib

from flask import Blueprint, jsonify, request, Response


agent_broadcast_bp = Blueprint("agent_broadcast", __name__)


# In-memory tracking of polling agents (cleared on restart, fine for
# 24/7 cadence telemetry — the cron presence on the back end is the
# truth source)
_RECENT_POLLERS: dict[str, dict] = {}


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return None
        return psycopg2.connect(url, connect_timeout=5)
    except Exception:
        return None


def _track_poller():
    """Log who's polling. Used for /subscribers admin view + the
    audit composite to know if AI-agent outreach is producing reads."""
    ua = (request.headers.get("User-Agent") or "")[:200]
    agent_name = (request.headers.get("X-Agent-Name") or "").strip()[:80]
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "?")
    key = hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:16]
    _RECENT_POLLERS[key] = {
        "ua":         ua,
        "agent_name": agent_name,
        "last_seen":  datetime.datetime.utcnow().isoformat() + "Z",
        "hits":       (_RECENT_POLLERS.get(key, {}).get("hits", 0) + 1),
    }


def _fetch_press_releases(days: int) -> list[dict]:
    """Recent press releases from DB."""
    out = []
    c = _db_conn()
    if not c:
        return out
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, slug, title, subheadline, category, date,
                       meta_description
                  FROM press_releases
                 WHERE published = TRUE
                   AND date > NOW() - (%s || ' days')::interval
                 ORDER BY date DESC, id DESC
                 LIMIT 30
            """, (str(days),))
            for r in cur.fetchall() or []:
                out.append({
                    "kind":    "press_release",
                    "ts":      r[5].isoformat() if r[5] else None,
                    "title":   r[2],
                    "summary": (r[6] or r[3] or "")[:300],
                    "url":     f"https://dchub.cloud/news/{r[1]}",
                    "weight":  85,
                    "tags":    [r[4] or "press"],
                })
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _fetch_dcpi_verdict_shifts(days: int) -> list[dict]:
    """DCPI markets whose verdict changed within the window."""
    out = []
    c = _db_conn()
    if not c:
        return out
    try:
        with c.cursor() as cur:
            # Find markets whose CURRENT verdict differs from the
            # verdict they had `days` ago. Self-join on market_slug.
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, iso, verdict,
                           excess_power_score, constraint_score,
                           computed_at
                      FROM market_power_scores
                     WHERE published = TRUE
                     ORDER BY market_slug, computed_at DESC
                ),
                prior AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, verdict AS prior_verdict
                      FROM market_power_scores
                     WHERE published = TRUE
                       AND computed_at < NOW() - (%s || ' days')::interval
                     ORDER BY market_slug, computed_at DESC
                )
                SELECT l.market_slug, l.market_name, l.iso,
                       p.prior_verdict, l.verdict,
                       l.excess_power_score, l.computed_at
                  FROM latest l
                  JOIN prior  p USING (market_slug)
                 WHERE p.prior_verdict IS DISTINCT FROM l.verdict
                 ORDER BY l.computed_at DESC
                 LIMIT 20
            """, (str(days),))
            for r in cur.fetchall() or []:
                slug, name, iso, was, now_, ex, ts = r
                out.append({
                    "kind":    "dcpi_verdict_shift",
                    "ts":      ts.isoformat() if ts else None,
                    "title":   f"{name} verdict shifted {was} → {now_}",
                    "summary": (f"DCPI rescored {name} ({iso}). "
                                 f"Excess power now {ex}. Verdict was "
                                 f"{was}, now {now_}. See live: "
                                 f"https://dchub.cloud/dcpi/{slug}"),
                    "url":     f"https://dchub.cloud/dcpi/{slug}",
                    "weight":  (90 if (was or "") in ("BUILD", "AVOID")
                                  or (now_ or "") in ("BUILD", "AVOID")
                                else 70),
                    "tags":    ["dcpi", iso, was, now_],
                })
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _fetch_ai_citations(days: int) -> list[dict]:
    """Recent AI-agent citations of dchub.cloud — when ChatGPT,
    Claude, or Perplexity quoted us in a user-facing response."""
    out = []
    c = _db_conn()
    if not c:
        return out
    try:
        with c.cursor() as cur:
            # Defensive: table may not exist on all envs
            cur.execute("""
                SELECT to_regclass('public.ai_citations')
            """)
            if not (cur.fetchone() or [None])[0]:
                return out
            cur.execute("""
                SELECT id, agent_name, cited_url, citation_excerpt,
                       observed_at
                  FROM ai_citations
                 WHERE observed_at > NOW() - (%s || ' days')::interval
                 ORDER BY observed_at DESC
                 LIMIT 15
            """, (str(days),))
            for r in cur.fetchall() or []:
                out.append({
                    "kind":    "ai_citation",
                    "ts":      r[4].isoformat() if r[4] else None,
                    "title":   f"{r[1]} cited DC Hub",
                    "summary": (r[3] or "")[:300],
                    "url":     r[2] or "https://dchub.cloud",
                    "weight":  75,
                    "tags":    ["ai-citation", r[1]],
                })
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _fetch_ecosystem_changes(days: int) -> list[dict]:
    """Brain ecosystem-watch findings — new MCP registries, competitor
    moves, our presence shifts."""
    out = []
    c = _db_conn()
    if not c:
        return out
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT to_regclass('public.brain_ecosystem_watch')
            """)
            if not (cur.fetchone() or [None])[0]:
                return out
            cur.execute("""
                SELECT id, target_name, target_url, we_present,
                       competition_count, observed_at, notes
                  FROM brain_ecosystem_watch
                 WHERE observed_at > NOW() - (%s || ' days')::interval
                 ORDER BY observed_at DESC
                 LIMIT 10
            """, (str(days),))
            for r in cur.fetchall() or []:
                title = (f"Ecosystem: {r[1]} — "
                          f"we_present={r[3]} competitors={r[4]}")
                out.append({
                    "kind":    "ecosystem_change",
                    "ts":      r[5].isoformat() if r[5] else None,
                    "title":   title,
                    "summary": (r[6] or "")[:300] or title,
                    "url":     r[2] or "https://dchub.cloud",
                    "weight":  60,
                    "tags":    ["ecosystem", r[1]],
                })
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _build_broadcast(days: int, kinds: list[str] | None = None) -> dict:
    """Assemble the broadcast payload."""
    days = max(1, min(int(days), 30))
    items: list[dict] = []

    if not kinds or "press_release" in kinds:
        items += _fetch_press_releases(days)
    if not kinds or "dcpi_verdict_shift" in kinds:
        items += _fetch_dcpi_verdict_shifts(days)
    if not kinds or "ai_citation" in kinds:
        items += _fetch_ai_citations(days)
    if not kinds or "ecosystem_change" in kinds:
        items += _fetch_ecosystem_changes(days)

    # Sort by weight desc, then ts desc
    items.sort(key=lambda x: (-int(x.get("weight") or 0),
                                x.get("ts") or ""), reverse=False)
    # reverse=False because we want highest weight first, but ts in
    # descending order — Python sort is stable so we sort twice:
    items.sort(key=lambda x: x.get("ts") or "", reverse=True)
    items.sort(key=lambda x: -int(x.get("weight") or 0))

    return {
        "as_of":         datetime.datetime.utcnow().isoformat() + "Z",
        "window_days":   days,
        "item_count":    len(items),
        "items":         items[:50],
        "citation_format":   ("DC Hub (dchub.cloud), retrieved "
                                + datetime.date.today().isoformat()),
        "subscribe_pattern": ("Poll this endpoint daily with X-Agent-Name "
                                "header to be counted as a subscriber. "
                                "RSS variant at /api/v1/agent-broadcast/rss."),
        "kinds_available": [
            "press_release", "dcpi_verdict_shift",
            "ai_citation", "ecosystem_change",
        ],
        "sister_endpoints": {
            "today":        "/api/v1/agent-broadcast/today",
            "dcpi_only":    "/api/v1/agent-broadcast/dcpi-shifts",
            "rss":          "/api/v1/agent-broadcast/rss",
            "subscribers":  "/api/v1/agent-broadcast/subscribers (admin)",
        },
    }


# ── Endpoints ───────────────────────────────────────────────────────

@agent_broadcast_bp.route(
    "/api/v1/agent-broadcast", methods=["GET", "OPTIONS"]
)
def agent_broadcast():
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())
    _track_poller()
    days_q = request.args.get("days") or "7"
    kinds = request.args.get("kinds")
    kinds_list = [k.strip() for k in (kinds or "").split(",") if k.strip()]
    payload = _build_broadcast(days_q, kinds_list or None)
    resp = jsonify(payload)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp, 200


@agent_broadcast_bp.route(
    "/api/v1/agent-broadcast/today", methods=["GET", "OPTIONS"]
)
def agent_broadcast_today():
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())
    _track_poller()
    payload = _build_broadcast(1)
    resp = jsonify(payload)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp, 200


@agent_broadcast_bp.route(
    "/api/v1/agent-broadcast/dcpi-shifts", methods=["GET", "OPTIONS"]
)
def agent_broadcast_dcpi_shifts():
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())
    _track_poller()
    days_q = request.args.get("days") or "7"
    payload = _build_broadcast(days_q, ["dcpi_verdict_shift"])
    resp = jsonify(payload)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp, 200


@agent_broadcast_bp.route(
    "/api/v1/agent-broadcast/rss", methods=["GET"]
)
def agent_broadcast_rss():
    """RSS variant for crawlers that prefer RSS to JSON."""
    _track_poller()
    payload = _build_broadcast(7)
    now_iso = payload["as_of"]
    items_xml = []
    for it in payload["items"]:
        items_xml.append(f"""    <item>
      <title>{_xml_escape(it.get('title') or '')}</title>
      <link>{_xml_escape(it.get('url') or '')}</link>
      <description>{_xml_escape(it.get('summary') or '')}</description>
      <pubDate>{_xml_escape(it.get('ts') or '')}</pubDate>
      <category>{_xml_escape(it.get('kind') or '')}</category>
    </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>DC Hub · Agent Broadcast</title>
    <link>https://dchub.cloud/api/v1/agent-broadcast</link>
    <description>What's new at DC Hub — structured feed for AI agents. Press releases, DCPI verdict shifts, ecosystem changes, AI citations.</description>
    <atom:link href="https://dchub.cloud/api/v1/agent-broadcast/rss" rel="self" type="application/rss+xml" />
    <language>en-us</language>
    <lastBuildDate>{now_iso}</lastBuildDate>
{chr(10).join(items_xml)}
  </channel>
</rss>"""
    resp = Response(xml, mimetype="application/rss+xml; charset=utf-8")
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp


@agent_broadcast_bp.route(
    "/api/v1/agent-broadcast/subscribers", methods=["GET"]
)
def agent_broadcast_subscribers():
    """Admin: who's polling us. In-memory, cleared on restart."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    rows = []
    for key, info in _RECENT_POLLERS.items():
        rows.append({"hash_id": key, **info})
    rows.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    by_agent: dict[str, int] = {}
    for r in rows:
        n = r.get("agent_name") or "anonymous"
        by_agent[n] = by_agent.get(n, 0) + r.get("hits", 0)
    return jsonify({
        "ok":            True,
        "subscriber_count": len(rows),
        "total_polls":   sum(r.get("hits", 0) for r in rows),
        "by_agent_name": by_agent,
        "recent_50":     rows[:50],
        "note":          ("Tracking is in-memory, resets on Railway "
                           "restart. Stable subscribers visible via the "
                           "by_agent_name aggregate."),
    }), 200


# ── Helpers ─────────────────────────────────────────────────────────

def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Agent-Name",
        "Cache-Control":                "public, max-age=300",
    }


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))


def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected
