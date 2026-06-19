"""Map-CTA click tracking via a logging redirect (2026-06-18).

The MCP gateway surfaces a "see this on the live Land & Power map" CTA on
grid/fiber/infra tool responses (server.mjs MAP_TOOLS). A bare https link in a
tool response is invisible to us — we can't tell whether the map upsell ever
drives a human click. This endpoint closes that blind spot: the CTA points here,
we log one funnel event, then 302 to the real map. Fail-safe: the redirect ALWAYS
fires even if logging throws, so a logging hiccup never strands the human.

GET /api/v1/go/map?tool=<name>&src=<surface>  →  302  /land-power-map
"""
from flask import Blueprint, request, redirect

map_click_bp = Blueprint("map_click_redirect", __name__)

_MAP_DEST = "https://dchub.cloud/land-power-map"


@map_click_bp.route("/api/v1/go/map", methods=["GET"])
def go_map():
    tool = (request.args.get("tool") or "")[:64]
    src = (request.args.get("src") or "mcp_upsell")[:48]
    try:
        from routes.redeem_tracking import record_funnel_event
        record_funnel_event(
            "map_cta_click",
            tool=tool or None,
            source=src,
            ip=request.headers.get("CF-Connecting-IP") or request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
            metadata={"surface": src, "tool": tool},
        )
    except Exception:
        pass  # logging is best-effort; the human must still reach the map
    # preserve any extra query the map page cares about, plus an attribution ref
    sep = "&" if "?" in _MAP_DEST else "?"
    return redirect(f"{_MAP_DEST}{sep}ref={src}", code=302)


def register(app):
    app.register_blueprint(map_click_bp)
    return True
