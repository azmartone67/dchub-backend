"""
DC Hub Nav Config API
─────────────────────
Serves /api/nav-config for dchub-nav.js dynamic nav updates.
Single source of truth — edit NAV_LINKS here to update nav site-wide
without touching Cloudflare Pages.

Register in main.py:
    from routes.nav_config_routes import nav_config_bp, _register_nav_config_routes
    _register_nav_config_routes(app, db)
"""

from flask import Blueprint, jsonify

nav_config_bp = Blueprint('nav_config', __name__)


# ── NAV LINKS (desktop top bar) ─────────────────────────────────
NAV_LINKS = [
    {"id": "home", "label": "Home", "href": "/", "type": "link"},
    {
        "id": "markets", "label": "Markets", "type": "dropdown",
        "items": [
            {"icon": "\U0001f5fa\ufe0f", "label": "Global Markets", "desc": "Explore 178 countries", "href": "/markets/"},
            {"icon": "\U0001f4ca", "label": "Market Analysis", "desc": "Trends & insights", "href": "/market-intelligence"},
            {"icon": "\U0001f4c8", "label": "Analytics", "desc": "Data dashboards", "href": "/analytics"}
        ]
    },
    {
        "id": "intelligence", "label": "Intelligence", "type": "dropdown",
        "items": [
            {"icon": "\U0001f310", "label": "GDCI", "desc": "Global market index", "href": "/gdci", "badge": "New"},
            {"icon": "\U0001f680", "label": "AI Pipeline", "desc": "Capacity projects", "href": "/ai-pipeline", "badge": "Live"},
            {"icon": "\U0001f4b0", "label": "AI Deals", "desc": "M&A tracker", "href": "/ai-deals", "badge": "Live"},
            {"icon": "\U0001f3d7\ufe0f", "label": "Asset Explorer", "desc": "20,000+ facilities", "href": "/assets"},
            {"icon": "\U0001f4e6", "label": "AI Inventory", "desc": "Supply analysis", "href": "/ai-inventory"},
            {"icon": "\U0001f3c6", "label": "Rankings", "desc": "State infrastructure rankings", "href": "/rankings", "badge": "New"}
        ]
    },
    {
        "id": "tools", "label": "Tools", "type": "dropdown",
        "items": [
            {"icon": "\u26a1", "label": "Land & Power", "desc": "Site selection", "href": "/land-power", "badge": "New"},
            {"icon": "\U0001f4b5", "label": "Transactions", "desc": "Deal flow", "href": "/transactions"},
            {"icon": "\u2696\ufe0f", "label": "Comps", "desc": "Side-by-side analysis", "href": "/transaction-comps"},
            {"icon": "\U0001f4b0", "label": "Tax Incentives", "desc": "50-state programs", "href": "/tax-incentives", "badge": "New"},
            {"icon": "\U0001f50d", "label": "Map", "desc": "Interactive map", "href": "/#map-section"},
            {"icon": "\U0001f916", "label": "AI Analytics", "desc": "Live AI agent tracking", "href": "/ai-analytics", "badge": "Live"},
            {"icon": "\U0001f50c", "label": "Connect", "desc": "MCP & API setup", "href": "/connect"}
        ]
    },
    {"id": "news", "label": "News", "href": "/news", "type": "link"},
    {"id": "rankings", "label": "\U0001f3c6 Rankings", "href": "/rankings", "type": "link", "style": "color:#fbbf24;font-weight:600"},
    {"id": "ai-wars", "label": "\u2694\ufe0f AI Wars", "href": "/ai-wars", "type": "link", "style": "color:#a78bfa;font-weight:600"},
    {"id": "pricing", "label": "Pricing", "href": "/pricing", "type": "link", "style": "color:var(--accent,#6366f1)"},
    {"id": "press", "label": "\U0001f4e2 Press", "href": "/press", "type": "link"},
    {
        "id": "about", "label": "About", "type": "dropdown",
        "items": [
            {"icon": "\u2b50", "label": "AI Validation", "desc": "What AI says about us", "href": "/testimonials", "badge": "New"},
            {"icon": "\u2139\ufe0f", "label": "About DC Hub", "desc": "Our mission", "href": "/about"},
            {"icon": "\U0001f3d7\ufe0f", "label": "Architecture", "desc": "Platform infrastructure", "href": "/architecture"},
            {"icon": "\U0001f916", "label": "AI Agents", "desc": "Research assistant", "href": "/ai-agents"},
            {"icon": "\u2694\ufe0f", "label": "AI Wars", "desc": "Live AI showdowns", "href": "/ai-wars", "badge": "New"},
            {"icon": "\U0001f310", "label": "Ecosystem", "desc": "Vendors & partners", "href": "/ecosystem"},
            {"icon": "\U0001f4e2", "label": "Advertise", "desc": "Sponsorship & media kit", "href": "/advertise"}
        ]
    }
]

# ── MOBILE BOTTOM NAV ───────────────────────────────────────────
MOBILE_NAV = [
    {"label": "Home", "href": "/", "icon": "<circle cx=\"12\" cy=\"12\" r=\"10\"/><path d=\"M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z\"/>"},
    {"label": "Markets", "href": "/markets/", "icon": "<path d=\"M18 20V10M12 20V4M6 20v-6\"/>"},
    {"label": "Tools", "href": "/land-power", "icon": "<polygon points=\"13 2 3 14 12 14 11 22 21 10 12 10\"/>"},
    {"label": "News", "href": "/news", "icon": "<path d=\"M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2\"/>"},
    {"label": "More", "href": "#", "icon": "<line x1=\"3\" y1=\"12\" x2=\"21\" y2=\"12\"/><line x1=\"3\" y1=\"6\" x2=\"21\" y2=\"6\"/><line x1=\"3\" y1=\"18\" x2=\"21\" y2=\"18\"/>", "action": "drawer"}
]

# ── MOBILE DRAWER LINKS ────────────────────────────────────────
DRAWER_LINKS = [
    {"icon": "<path d=\"M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z\"/>", "label": "Dashboard", "href": "/"},
    {"icon": "<circle cx=\"12\" cy=\"12\" r=\"10\"/><path d=\"M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10\"/>", "label": "GDCI", "href": "/gdci"},
    {"icon": "<polygon points=\"12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2\"/>", "label": "AI Validation", "href": "/testimonials"},
    {"icon": "<circle cx=\"12\" cy=\"12\" r=\"10\"/><path d=\"M2 12h20\"/>", "label": "Land & Power", "href": "/land-power"},
    {"icon": "<rect x=\"1\" y=\"4\" width=\"22\" height=\"16\" rx=\"2\"/><line x1=\"1\" y1=\"10\" x2=\"23\" y2=\"10\"/>", "label": "Transactions", "href": "/transactions"},
    {"icon": "<path d=\"M18 20V10M12 20V4M6 20v-6\"/>", "label": "Analytics", "href": "/analytics"},
    {"icon": "<polygon points=\"12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2\"/>", "label": "Rankings", "href": "/rankings"},
    {"icon": "<path d=\"M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16\"/>", "label": "News", "href": "/news"},
    {"icon": "<path d=\"M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z\"/><polyline points=\"14 2 14 8 20 8\"/>", "label": "API Docs", "href": "/api-docs"},
    {"icon": "<path d=\"M18 20V10M12 20V4M6 20v-6\"/>", "label": "AI Analytics", "href": "/ai-analytics"},
    {"icon": "<polygon points=\"13 2 3 14 12 14 11 22 21 10 12 10\"/>", "label": "Connect", "href": "/connect"},
    {"icon": "<path d=\"M19 20H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v1m2 13a2 2 0 0 1-2-2V7m2 13a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-2\"/>", "label": "Press", "href": "/press"}
]

# ── FOUNDING MEMBER CONFIG ──────────────────────────────────────
FOUNDING_CONFIG = {
    "remaining": 12,
    "total": 50,
    "price": "$99/month",
    "normal_price": "$199/month"
}


def _register_nav_config_routes(app, db=None):
    """Register nav config routes with late-binding pattern."""

    @nav_config_bp.route('/api/nav-config', methods=['GET'])
    def get_nav_config():
        return jsonify({
            "success": True,
            "links": NAV_LINKS,
            "mobile": MOBILE_NAV,
            "drawer": DRAWER_LINKS,
            "founding": FOUNDING_CONFIG,
            "stats": {
                "facilities": "20,000+",
                "deals": "473",
                "pipeline_gw": "61.8",
                "markets": "178"
            }
        })

    try:
        app.register_blueprint(nav_config_bp)
        print("[NAV CONFIG] Registered /api/nav-config")
    except Exception as e:
        print(f"[NAV CONFIG] Blueprint registration note: {e}")
