"""
DC Hub Nav Config API Endpoint
═══════════════════════════════════════════
Add this route to your Replit Flask/FastAPI backend.

GET /api/nav-config
Returns the navigation structure for dchub-nav.js to consume.

Update the nav_links list below to change navigation across ALL pages.
Changes propagate within 5 minutes (client cache TTL).
"""

# ── For Flask ─────────────────────────────────────────────────

from flask import jsonify
from datetime import datetime


def register_nav_config_route(app, db=None):
    """Register the /api/nav-config endpoint on your Flask app."""

    @app.route('/api/nav-config', methods=['GET'])
    def get_nav_config():
        """
        Returns nav configuration consumed by dchub-nav.js.
        Edit nav_links to update navigation across all DC Hub pages.
        """

        # ── EDIT THESE TO UPDATE NAV EVERYWHERE ──────────────
        nav_links = [
            {"id": "home", "label": "Home", "href": "/", "type": "link"},
            {
                "id": "markets", "label": "Markets", "type": "dropdown",
                "items": [
                    {"icon": "🗺️", "label": "Global Markets", "desc": "Explore 140+ countries", "href": "/markets/"},
                    {"icon": "📊", "label": "Market Analysis", "desc": "Trends & insights", "href": "/market-intelligence"},
                    {"icon": "📈", "label": "Analytics", "desc": "Data dashboards", "href": "/analytics"},
                ]
            },
            {
                "id": "intelligence", "label": "Intelligence", "type": "dropdown",
                "items": [
                    {"icon": "🚀", "label": "AI Pipeline", "desc": "Capacity projects", "href": "/ai-pipeline", "badge": "Live"},
                    {"icon": "💰", "label": "AI Deals", "desc": "M&A tracker", "href": "/ai-deals", "badge": "Live"},
                    {"icon": "🏗️", "label": "Asset Explorer", "desc": "20,000+ facilities", "href": "/assets"},
                    {"icon": "📦", "label": "AI Inventory", "desc": "Supply analysis", "href": "/ai-inventory"},
                ]
            },
            {
                "id": "tools", "label": "Tools", "type": "dropdown",
                "items": [
                    {"icon": "⚡", "label": "Land & Power", "desc": "Site selection", "href": "/land-power", "badge": "New"},
                    {"icon": "💵", "label": "Transactions", "desc": "Deal flow", "href": "/transactions"},
                    {"icon": "⚖️", "label": "Comps", "desc": "Side-by-side analysis", "href": "/transaction-comps"},
                    {"icon": "💰", "label": "Tax Incentives", "desc": "50-state programs", "href": "/tax-incentives", "badge": "New"},
                    {"icon": "🔍", "label": "Map", "desc": "Interactive map", "href": "/#map-section"},
                ]
            },
            {"id": "news", "label": "News", "href": "/news", "type": "link"},
            {"id": "ai-wars", "label": "⚔️ AI Wars", "href": "/ai-wars", "type": "link", "style": "color:#a78bfa;font-weight:600"},
            {"id": "pricing", "label": "Pricing", "href": "/pricing", "type": "link", "style": "color:var(--accent,#6366f1)"},
            {
                "id": "about", "label": "About", "type": "dropdown",
                "items": [
                    {"icon": "ℹ️", "label": "About DC Hub", "desc": "Our mission", "href": "/about"},
                    {"icon": "🤖", "label": "AI Agents", "desc": "Research assistant", "href": "/ai-agents"},
                    {"icon": "⚔️", "label": "AI Wars", "desc": "Live AI showdowns", "href": "/ai-wars", "badge": "New"},
                    {"icon": "🌐", "label": "Ecosystem", "desc": "Vendors & partners", "href": "/ecosystem"},
                    {"icon": "📢", "label": "Advertise", "desc": "Sponsorship & media kit", "href": "/advertise"},
                ]
            },
        ]

        # Mobile bottom nav
        mobile_nav = [
            {"label": "Home", "href": "/", "icon": "home"},
            {"label": "Markets", "href": "/markets/", "icon": "bar-chart"},
            {"label": "Tools", "href": "/land-power", "icon": "zap"},
            {"label": "News", "href": "/news", "icon": "file-text"},
            {"label": "More", "href": "#", "icon": "menu", "action": "drawer"},
        ]

        # Founding member data (dynamic from your DB or Stripe)
        founding_data = get_founding_member_stats(db)

        # Platform stats for the nav badge
        stats = get_platform_stats(db)

        return jsonify({
            "success": True,
            "links": nav_links,
            # mobile and drawer are optional — omit to use JS defaults
            # "mobile": mobile_nav,
            # "drawer": drawer_links,
            "founding": founding_data,
            "stats": stats,
            "version": "1.0",
            "updated_at": datetime.utcnow().isoformat() + "Z"
        })

    return app


def get_founding_member_stats(db=None):
    """Get founding member stats from DB or Stripe."""
    # TODO: Replace with actual DB/Stripe query
    # Example: SELECT COUNT(*) FROM subscriptions WHERE plan = 'founding'
    total_spots = 50
    claimed = 5  # Update this from your Stripe webhook or DB count

    return {
        "total": total_spots,
        "claimed": claimed,
        "remaining": total_spots - claimed,
        "price": 99,
        "regular_price": 299,
    }


def get_platform_stats(db=None):
    """Get platform stats for display."""
    # TODO: Replace with actual DB queries
    return {
        "facilities": 20000,
        "deals": 241,
        "markets": 28,
        "pipeline_gw": 28.4,
    }


# ── INTEGRATION EXAMPLE ──────────────────────────────────────
#
# In your main Replit app.py or main.py:
#
#   from nav_config import register_nav_config_route
#   register_nav_config_route(app, db)
#
# Or if you prefer inline:
#
#   @app.route('/api/nav-config', methods=['GET'])
#   def nav_config():
#       return jsonify({"success": True, "links": [...], ...})
#
# ──────────────────────────────────────────────────────────────
