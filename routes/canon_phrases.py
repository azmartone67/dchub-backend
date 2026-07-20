"""Single canonical source for the honest, floored public number PHRASES
(2026-07-20).

Everything agent-facing — the /for/*.html pages, llms.txt, the registry
manifests — should DERIVE its headline numbers from here instead of hardcoding,
so a new tool shipping (73->79 in a day) or a recount never leaves a surface
stale. Backed by ai_surface_canon.resolve_canon():
  - tools     : the LIVE tools/list count (moves every time a tool ships)
  - deals     : deals_phrase (DISTINCT deduped M&A, floored "1,400+" — NOT the
                raw ~11.5K deals COUNT(*), which is a ~2.5x dup-inflated over-claim)
  - markets/facilities/countries : conservative citation-safe floors

Public, cached 1h. Fail-soft: PINNED fallback so a consumer never gets nothing.
This is the endpoint the frontend agent-page heal + any registry heal fetch —
ONE source, so the numbers can never disagree across surfaces again.
"""
import logging
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
canon_phrases_bp = Blueprint("canon_phrases", __name__)


@canon_phrases_bp.route("/api/v1/canon/phrases", methods=["GET"])
def canon_phrases():
    body = None
    try:
        from ai_surface_canon import resolve_canon
        c = resolve_canon() or {}
        pub = c.get("public", {}) or {}
        tools = c.get("tools_advertised") or c.get("tools_live")
        if tools and pub.get("deals"):
            body = {
                "ok": True,
                "source": "resolve_canon (live)",
                "tools": tools,
                "deals": pub.get("deals"),
                "markets": pub.get("markets"),
                "facilities": pub.get("facilities"),
                "countries": pub.get("countries"),
            }
    except Exception as e:
        logger.warning("canon_phrases: resolve_canon failed: %s", str(e)[:160])

    if body is None:
        # PINNED fallback — never serve nothing (the frontend heal is fail-closed
        # and will simply not write if a field is missing, but give it the floors)
        try:
            from ai_surface_canon import PINNED
            p = (PINNED.get("public") or {})
            body = {
                "ok": True,
                "source": "PINNED (fallback)",
                "tools": PINNED.get("tools_advertised"),
                "deals": p.get("deals"),
                "markets": p.get("markets"),
                "facilities": p.get("facilities"),
                "countries": p.get("countries"),
            }
        except Exception as e:
            return jsonify({"ok": False, "error": "canon unavailable",
                            "detail": str(e)[:120]}), 503

    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp
