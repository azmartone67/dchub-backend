from flask import jsonify
from datetime import datetime

def register_nav_config_route(app, db=None):
    if 'get_nav_config' in [rule.endpoint for rule in app.url_map.iter_rules()]:
        return app
    @app.route('/api/nav-config', methods=['GET'])
    def get_nav_config():
        nav_links = [
            {"id": "home", "label": "Home", "href": "/", "type": "link"},
            {"id": "markets", "label": "Markets", "type": "dropdown", "items": [
                {"icon": "\ud83d\uddfa\ufe0f", "label": "Global Markets", "desc": "Explore 140+ countries", "href": "/markets/"},
                {"icon": "\ud83d\udcca", "label": "Market Analysis", "desc": "Trends & insights", "href": "/market-intelligence"},
                {"icon": "\ud83d\udcc8", "label": "Analytics", "desc": "Data dashboards", "href": "/analytics"}
            ]},
            {"id": "intelligence", "label": "Intelligence", "type": "dropdown", "items": [
                {"icon": "\ud83d\ude80", "label": "AI Pipeline", "desc": "Capacity projects", "href": "/ai-pipeline", "badge": "Live"},
                {"icon": "\ud83d\udcb0", "label": "AI Deals", "desc": "M&A tracker", "href": "/ai-deals", "badge": "Live"},
                {"icon": "\ud83c\udfd7\ufe0f", "label": "Asset Explorer", "desc": "20,000+ facilities", "href": "/assets"},
                {"icon": "\ud83d\udce6", "label": "AI Inventory", "desc": "Supply analysis", "href": "/ai-inventory"}
            ]},
            {"id": "tools", "label": "Tools", "type": "dropdown", "items": [
                {"icon": "\u26a1", "label": "Land & Power", "desc": "Site selection", "href": "/land-power", "badge": "New"},
                {"icon": "\ud83d\udcb5", "label": "Transactions", "desc": "Deal flow", "href": "/transactions"},
                {"icon": "\u2696\ufe0f", "label": "Comps", "desc": "Side-by-side analysis", "href": "/transaction-comps"},
                {"icon": "\ud83d\udcb0", "label": "Tax Incentives", "desc": "50-state programs", "href": "/tax-incentives", "badge": "New"},
                {"icon": "\ud83d\udd0d", "label": "Map", "desc": "Interactive map", "href": "/#map-section"}
            ]},
            {"id": "news", "label": "News", "href": "/news", "type": "link"},
            {"id": "ai-hub", "label": "\ud83e\udd16 AI Hub", "href": "/ai-hub", "type": "link", "style": "color:#06b6d4;font-weight:600"},
            {"id": "ai-wars", "label": "\u2694\ufe0f AI Wars", "href": "/ai-wars", "type": "link", "style": "color:#a78bfa;font-weight:600"},
            {"id": "pricing", "label": "Pricing", "href": "/pricing", "type": "link", "style": "color:var(--accent,#6366f1)"}, {"id": "press", "label": "Press", "href": "/press", "type": "link"},
            {"id": "about", "label": "About", "type": "dropdown", "items": [
        {"icon": "⭐", "label": "AI Validation", "desc": "What AI says about us", "href": "/testimonials", "badge": "New"},
                {"icon": "\u2139\ufe0f", "label": "About DC Hub", "desc": "Our mission", "href": "/about"},
                {"icon": "\ud83e\udd16", "label": "AI Hub", "desc": "AI ecosystem & integrations", "href": "/ai-hub", "badge": "New"},
                {"icon": "\u2694\ufe0f", "label": "AI Wars", "desc": "Live AI showdowns", "href": "/ai-wars", "badge": "New"},
                {"icon": "\ud83c\udf10", "label": "Ecosystem", "desc": "Vendors & partners", "href": "/ecosystem"},
                {"icon": "\ud83d\udce2", "label": "Advertise", "desc": "Sponsorship & media kit", "href": "/advertise"}
            ]}
        ]
        founding = {"total": 50, "claimed": 5, "remaining": 45, "price": 99, "regular_price": 299}
        stats = {"facilities": 16806, "deals": 756, "markets": 28, "pipeline_gw": 17.4}
        return jsonify({
            "success": True,
            "links": nav_links,
            "founding": founding,
            "stats": stats,
            "version": "1.0",
            "updated_at": datetime.utcnow().isoformat() + "Z"
        })
    return app
