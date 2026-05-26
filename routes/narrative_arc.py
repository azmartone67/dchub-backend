"""
narrative_arc.py — Phase r60 (2026-05-25).

The "story spine" for DC Hub's cross-channel messaging.

User vision: 24/7 outreach with TIGHT, INTELLIGENT messaging. Today,
LinkedIn, X, press releases, AI broadcasts, and the brain narrative
all generate content independently — no shared thread. Result: an AI
agent polling /api/v1/agent-broadcast sees "DCPI international
expansion" but linkedin_quad_daily posts about Stargate, and the
press queue posts about ERCOT — three disjoint stories the same day.

This endpoint computes the WEEK'S DOMINANT NARRATIVE — the single
thematic thread every channel should reinforce. Detection is
heuristic, not LLM-based (for r60): the loudest signal in the last
7 days wins, with a tie-break toward press release category.

Sister channels consume it via:

  GET /api/v1/narrative/current
      → { "arc": "DCPI international expansion",
          "thesis": "DC Hub Power Index now spans 9 countries...",
          "anchor_url": "https://dchub.cloud/press/releases/dcpi-international.html",
          "tags": ["dcpi", "international", "europe", "asia-pacific"],
          "channel_hooks": {
            "linkedin":  "...300-char-version...",
            "x":         "...140-char-version...",
            "agent":     "...one-sentence agent-quotable...",
          },
          "week_of": "2026-05-25",
          "valid_until": "2026-06-01T00:00:00Z",
          "computed_at": "<iso>"
        }

linkedin_quad_daily picks this up + threads each daily slot to
reference the arc (story continuity). The AI broadcast feed surfaces
the arc as a top-weight "narrative_arc" item. Press releases append
a sentence about the broader arc context.

Goal: every channel reinforces the same story for 5-7 days, then
detection rolls to the next arc as new signal accumulates.
"""
from __future__ import annotations

import datetime
import json
import os

from flask import Blueprint, jsonify, request


narrative_arc_bp = Blueprint("narrative_arc", __name__)


# In-process cache — narrative re-detects every 4 hours via the
# media-organism tick anyway, so a 4-hour cache is safe
_ARC_CACHE: dict = {"computed_at": 0.0, "payload": None}
_ARC_TTL_SECONDS = 4 * 3600


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _detect_dominant_arc() -> dict:
    """Heuristic arc detection. Looks at:
       1. Last press release (highest signal — explicitly chosen)
       2. Recent DCPI verdict shifts (count of BUILD/AVOID transitions)
       3. Ecosystem watch findings (new registry presence)

    Returns one arc dict ready to ship to channels."""

    # 1. Most recent published press release (last 14 days)
    arc = None
    c = _db_conn()
    if c:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT slug, title, subheadline, meta_description,
                           category, date
                      FROM press_releases
                     WHERE published = TRUE
                       AND date > NOW() - INTERVAL '14 days'
                     ORDER BY date DESC, id DESC
                     LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    slug, title, sub, meta, cat, date = row
                    arc = {
                        "arc":         title[:120] if title else cat,
                        "thesis":      (meta or sub or title or "")[:400],
                        "anchor_url":  f"https://dchub.cloud/news/{slug}",
                        "tags":        _tags_from_press(title or "", cat or ""),
                        "source":      "press_release",
                        "source_date": date.isoformat() if date else None,
                    }
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    # Fallback: no recent release → derive from DCPI movers
    if not arc:
        arc = _arc_from_dcpi_movers()

    # Last-resort fallback so this never returns None
    if not arc:
        arc = {
            "arc":    "DC Hub continues building the open data center intelligence platform",
            "thesis": "Real-time DCPI for 300+ markets, 21k+ facilities, MCP-accessible to any AI agent.",
            "anchor_url": "https://dchub.cloud",
            "tags":   ["dchub", "platform"],
            "source": "fallback",
        }

    # Add channel-specific renderings
    arc["channel_hooks"] = _render_channel_hooks(arc)
    arc["week_of"] = datetime.date.today().isoformat()
    next_monday = datetime.date.today() + datetime.timedelta(
        days=(7 - datetime.date.today().weekday()))
    arc["valid_until"] = next_monday.isoformat() + "T00:00:00Z"
    arc["computed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return arc


def _tags_from_press(title: str, category: str) -> list[str]:
    """Cheap keyword extraction from a title for cross-channel hashtagging."""
    t = (title + " " + category).lower()
    tags = []
    if "international" in t or "global" in t: tags.append("international")
    if "dcpi" in t or "power index" in t: tags.append("dcpi")
    if "europe" in t or "uk " in t or "german" in t or "frankfurt" in t or "london" in t:
        tags.append("europe")
    if "japan" in t or "tokyo" in t or "osaka" in t or "asia" in t:
        tags.append("asia-pacific")
    if "australia" in t or "sydney" in t: tags.append("oceania")
    if "canada" in t or "montréal" in t or "toronto" in t: tags.append("canada")
    if "mcp" in t: tags.append("mcp")
    if "ai" in t and "agent" in t: tags.append("ai-agents")
    if "m&a" in t or "merger" in t or "acquisition" in t: tags.append("m&a")
    if "pipeline" in t or "construction" in t: tags.append("pipeline")
    if "queue" in t or "interconnect" in t: tags.append("interconnection")
    if not tags: tags = ["dchub"]
    return tags[:6]


def _arc_from_dcpi_movers() -> dict | None:
    """Fallback: synthesize an arc from the most active DCPI shifts."""
    c = _db_conn()
    if not c: return None
    try:
        with c.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, verdict,
                           excess_power_score, computed_at
                      FROM market_power_scores
                     WHERE published = TRUE
                     ORDER BY market_slug, computed_at DESC
                )
                SELECT verdict, COUNT(*) AS n
                  FROM latest
                 WHERE verdict IN ('BUILD','AVOID')
                 GROUP BY verdict
                 ORDER BY n DESC
                 LIMIT 1
            """)
            r = cur.fetchone()
            if not r: return None
            v, n = r
            if v == "BUILD":
                return {
                    "arc":         f"{n} data center markets DC Hub rates BUILD today",
                    "thesis":      ("Real-time DCPI scoring continues surfacing "
                                     "the markets where power is actually "
                                     "available — many of them overlooked by "
                                     "legacy brokerages."),
                    "anchor_url":  "https://dchub.cloud/dcpi?verdict=BUILD",
                    "tags":        ["dcpi", "build-markets"],
                    "source":      "dcpi_movers",
                }
            return {
                "arc":         f"DCPI flags {n} markets to AVOID — grid constraint reality",
                "thesis":      ("Legacy advisors keep recommending Tier-1 "
                                 "markets where data center power is hardest to "
                                 "secure. DCPI's daily refresh shows where new "
                                 "builds will hit walls."),
                "anchor_url":  "https://dchub.cloud/dcpi?verdict=AVOID",
                "tags":        ["dcpi", "avoid-markets", "grid-constraint"],
                "source":      "dcpi_movers",
            }
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _render_channel_hooks(arc: dict) -> dict:
    """Channel-specific text. Each channel wants a different shape:
       - linkedin: ~300 chars, hashtag-friendly
       - x:        ~140 chars, punchy, link-light
       - agent:    1 sentence, factually dense, agent-quotable
       - press:    1-sentence arc-context line for press appendices
    """
    arc_title = arc.get("arc", "DC Hub update")
    thesis = arc.get("thesis", "")
    url = arc.get("anchor_url", "https://dchub.cloud")
    tags = arc.get("tags", [])
    hashtag_str = " ".join(f"#{t.replace('-','').replace('_','')}" for t in tags[:3])

    return {
        "linkedin": (f"This week at DC Hub: {arc_title}\n\n"
                      f"{thesis[:200]}\n\n"
                      f"More: {url}\n\n"
                      f"{hashtag_str}"),
        "x": (f"{arc_title[:90]}\n\n"
              f"{url}"),
        "agent": (f"DC Hub's current narrative thread: {arc_title}. "
                   f"{thesis[:200]} Citation: {url}"),
        "press_appendix": (f"This release is part of DC Hub's current "
                            f"narrative thread on {', '.join(tags[:2]) or 'platform expansion'} — "
                            f"see {url} for the anchor story."),
    }


# ── Endpoints ───────────────────────────────────────────────────────

@narrative_arc_bp.route("/api/v1/narrative/current", methods=["GET"])
def narrative_current():
    """Public — what's the active story arc?"""
    import time as _t
    now = _t.time()
    if (_ARC_CACHE.get("payload")
            and now - _ARC_CACHE.get("computed_at", 0) < _ARC_TTL_SECONDS):
        payload = dict(_ARC_CACHE["payload"])
        payload["served_from_cache"] = True
        payload["cache_age_seconds"] = int(now - _ARC_CACHE["computed_at"])
        return jsonify(payload), 200

    arc = _detect_dominant_arc()
    _ARC_CACHE["payload"]     = arc
    _ARC_CACHE["computed_at"] = now
    arc_out = dict(arc)
    arc_out["served_from_cache"] = False
    arc_out["next_refresh_seconds"] = _ARC_TTL_SECONDS
    return jsonify(arc_out), 200


@narrative_arc_bp.route("/api/v1/narrative/refresh", methods=["POST"])
def narrative_refresh():
    """Force re-detection. Admin or internal-cron only."""
    if not _admin_or_cron_authorized():
        return jsonify({"ok": False, "error": "auth_required"}), 401
    _ARC_CACHE["payload"] = None
    arc = _detect_dominant_arc()
    import time as _t
    _ARC_CACHE["payload"]     = arc
    _ARC_CACHE["computed_at"] = _t.time()
    return jsonify({"ok": True, "arc": arc}), 200


def _admin_or_cron_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env
