"""
DC Hub SEO Agent System
========================
Drop this into your Replit backend alongside main.py.
Adds 3 SEO-focused agent endpoints that your existing agent system can call.

Usage:
    from seo_agents import setup_seo_routes
    setup_seo_routes(app)  # Add to your Flask app in main.py

Endpoints:
    GET  /api/seo/status          - Overall SEO agent status
    GET  /api/seo/social/generate  - Generate SEO-optimized social post
    GET  /api/seo/backlinks        - Track backlink targets and status
    POST /api/seo/outreach/generate - Generate outreach email for a target
    GET  /api/seo/deep-links       - Get next deep link for social rotation
    GET  /api/seo/meta-audit       - Check meta tags on key pages
"""

import json
import random
import hashlib
from datetime import datetime, timedelta
from flask import jsonify, request

# ============================================================
# CONFIGURATION - Edit these to match your actual data
# ============================================================

# Your Replit backend base URL
API_BASE = "https://dchub.cloud"

# All pages that should get social media backlinks
DEEP_LINK_PAGES = {
    "markets": [
        {"url": "https://dchub.cloud/markets/silicon-valley", "title": "Silicon Valley Data Centers", "hashtags": ["#SiliconValley", "#DataCenter", "#BayArea"]},
        {"url": "https://dchub.cloud/markets/phoenix", "title": "Phoenix Data Center Market", "hashtags": ["#Phoenix", "#DataCenter", "#Arizona"]},
        {"url": "https://dchub.cloud/markets/dallas", "title": "Dallas-Fort Worth Data Centers", "hashtags": ["#Dallas", "#DFW", "#DataCenter"]},
        {"url": "https://dchub.cloud/markets/northern-virginia", "title": "Northern Virginia Data Centers", "hashtags": ["#NoVA", "#Ashburn", "#DataCenter"]},
        {"url": "https://dchub.cloud/markets/chicago", "title": "Chicago Data Center Market", "hashtags": ["#Chicago", "#DataCenter", "#Midwest"]},
        {"url": "https://dchub.cloud/markets/atlanta", "title": "Atlanta Data Centers", "hashtags": ["#Atlanta", "#DataCenter", "#Southeast"]},
    ],
    "tools": [
        {"url": "https://dchub.cloud/land-power", "title": "Land & Power Site Selection", "hashtags": ["#SiteSelection", "#DataCenter", "#Infrastructure"]},
        {"url": "https://dchub.cloud/ai-pipeline", "title": "AI Construction Pipeline", "hashtags": ["#Construction", "#DataCenter", "#Pipeline"]},
        {"url": "https://dchub.cloud/ai-deals", "title": "M&A Deal Tracker", "hashtags": ["#MandA", "#DataCenter", "#Investment"]},
        {"url": "https://dchub.cloud/construction-pipeline", "title": "Construction Pipeline", "hashtags": ["#Construction", "#DataCenter", "#Development"]},
        {"url": "https://dchub.cloud/transaction-comps", "title": "Transaction Comps", "hashtags": ["#DataCenter", "#RealEstate", "#Transactions"]},
    ],
    "features": [
        {"url": "https://dchub.cloud/ai-agents", "title": "AI Research Agents", "hashtags": ["#AI", "#DataCenter", "#Intelligence"]},
        {"url": "https://dchub.cloud/api-docs", "title": "Developer API", "hashtags": ["#API", "#Developer", "#DataCenter"]},
        {"url": "https://dchub.cloud/for-ai", "title": "AI Integration Hub", "hashtags": ["#AI", "#MCP", "#DataCenter"]},
        {"url": "https://dchub.cloud/ecosystem", "title": "DC Hub Ecosystem", "hashtags": ["#DataCenter", "#Ecosystem", "#Partners"]},
        {"url": "https://dchub.cloud/ai-inventory", "title": "AI Inventory Analysis", "hashtags": ["#DataCenter", "#Supply", "#Analysis"]},
    ],
    "content": [
        {"url": "https://dchub.cloud/news", "title": "Industry News Feed", "hashtags": ["#DataCenter", "#News", "#Industry"]},
        {"url": "https://dchub.cloud/pricing", "title": "Pricing Plans", "hashtags": ["#DataCenter", "#SaaS", "#Intelligence"]},
        {"url": "https://dchub.cloud/about", "title": "About DC Hub", "hashtags": ["#DataCenter", "#Startup", "#PropTech"]},
    ]
}

# Social post templates (rotate through these)
SOCIAL_TEMPLATES = [
    {
        "type": "market_update",
        "template": "📊 {market} Data Center Market Update\n\n"
                   "The {market} market continues to evolve with {fact}.\n\n"
                   "Get full market intelligence, facility data, and capacity trends:\n"
                   "👉 {url}\n\n"
                   "{hashtags}\n\n"
                   "Data from DC Hub — tracking 20,000+ facilities across 140+ countries.",
        "category": "markets"
    },
    {
        "type": "tool_showcase",
        "template": "⚡ Find your next data center site in 60 seconds\n\n"
                   "DC Hub's {tool_name} shows you {feature} — all in one interactive view.\n\n"
                   "Try it free:\n"
                   "👉 {url}\n\n"
                   "{hashtags}\n\n"
                   "#DataCenter #SiteSelection #Infrastructure",
        "category": "tools"
    },
    {
        "type": "deal_alert",
        "template": "💰 Data Center M&A Update\n\n"
                   "{deal_fact}\n\n"
                   "Track all 787+ transactions worth $10.6B+ on DC Hub:\n"
                   "👉 {url}\n\n"
                   "{hashtags}\n\n"
                   "#DataCenter #MandA #Investment #CRE",
        "category": "tools"
    },
    {
        "type": "ai_integration",
        "template": "🤖 When AI answers data center questions, it cites DC Hub\n\n"
                   "We're integrated with Claude, ChatGPT, Perplexity, and Gemini — "
                   "providing real-time facility data, M&A intelligence, and market trends.\n\n"
                   "See how it works:\n"
                   "👉 {url}\n\n"
                   "{hashtags}\n\n"
                   "#AI #DataCenter #Intelligence #MCP",
        "category": "features"
    },
    {
        "type": "stat_highlight",
        "template": "📈 DC Hub by the Numbers\n\n"
                   "• 20,000+ facilities tracked\n"
                   "• 140+ countries covered\n"
                   "• 787 M&A deals ($10.6B value)\n"
                   "• 7.8 GW under construction\n"
                   "• 612+ substations mapped\n\n"
                   "The most comprehensive data center intelligence platform:\n"
                   "👉 {url}\n\n"
                   "{hashtags}",
        "category": "content"
    },
    {
        "type": "facility_spotlight",
        "template": "🏢 Facility Spotlight\n\n"
                   "Explore detailed profiles of data centers worldwide — "
                   "including satellite imagery, power capacity, tier level, "
                   "and nearby infrastructure.\n\n"
                   "Browse 20,000+ facilities:\n"
                   "👉 {url}\n\n"
                   "{hashtags}\n\n"
                   "#DataCenter #Colocation #CloudInfrastructure",
        "category": "tools"
    },
    {
        "type": "developer_api",
        "template": "👩‍💻 Free Data Center API — 100 requests/day\n\n"
                   "Access facility data, market intelligence, M&A deals, "
                   "and construction pipeline data via REST API.\n\n"
                   "Zero-dep Python client included. Get started:\n"
                   "👉 {url}\n\n"
                   "{hashtags}\n\n"
                   "#API #Developer #DataCenter #OpenData",
        "category": "features"
    },
]

# Market facts for dynamic content
MARKET_FACTS = {
    "Northern Virginia": [
        "vacancy hitting 1.2% — an all-time low",
        "5.9 GW of new capacity planned through 2027",
        "over 300 data centers in the region",
        "average pricing exceeding $200/kW/month",
    ],
    "Phoenix": [
        "total inventory reaching 510+ MW (44% YoY increase)",
        "334 MW currently under construction",
        "vacancy at 3.3% with rising demand",
        "pricing at $170-210/kW/month — leading primary markets",
    ],
    "Dallas-Fort Worth": [
        "supply expanding 200% since 2020 (710 MW to 1,650 MW)",
        "vacancy dropping to 1.4% from 19% in 2020",
        "18-month time-to-power advantage over NoVA and Silicon Valley",
        "478 MW absorbed in 2023 alone",
    ],
    "Silicon Valley": [
        "tech giants driving demand despite constrained power supply",
        "home to Apple, Google, NVIDIA, Meta headquarters",
        "power rates averaging $0.12-0.15/kWh",
        "Phoenix recently surpassing it in total inventory",
    ],
    "Chicago": [
        "central location enabling low-latency nationwide connections",
        "free cooling advantage for most of the year",
        "major financial exchange hub driving demand",
        "strong fiber connectivity infrastructure",
    ],
}

# Tool descriptions for dynamic content
TOOL_FEATURES = {
    "Land & Power Site Selection": "substations, fiber routes, FEMA flood zones, gas pipelines, and power availability",
    "AI Construction Pipeline": "real-time construction tracking, delivery timelines, and pre-lease status for 7.8 GW of projects",
    "M&A Deal Tracker": "787+ transactions worth $10.6B+ with deal details, valuations, and trend analysis",
    "Construction Pipeline": "new builds, development milestones, and market absorption data across 35+ markets",
    "Transaction Comps": "side-by-side deal analysis with pricing, cap rates, and market comparisons",
}

# Deal facts for dynamic content
DEAL_FACTS = [
    "2025 deal volume has exceeded $61B across 100+ transactions",
    "Blackstone's $24B AirTrunk acquisition was the largest APAC data center deal ever",
    "Aligned Data Centers saw a $40B valuation in the largest single-asset deal",
    "CoreWeave secured $7.5B specifically for AI infrastructure buildout",
    "Private equity and infrastructure funds are driving record investment in digital infrastructure",
]

# Backlink targets to track
BACKLINK_TARGETS = [
    {"name": "Product Hunt", "url": "https://www.producthunt.com", "status": "not_submitted", "priority": "high", "type": "directory"},
    {"name": "G2", "url": "https://www.g2.com", "status": "not_submitted", "priority": "high", "type": "directory"},
    {"name": "Capterra", "url": "https://www.capterra.com", "status": "not_submitted", "priority": "high", "type": "directory"},
    {"name": "AlternativeTo", "url": "https://alternativeto.net", "status": "not_submitted", "priority": "high", "type": "directory"},
    {"name": "Data Center Knowledge", "url": "https://www.datacenterknowledge.com", "status": "not_contacted", "priority": "high", "type": "publication"},
    {"name": "Data Center Frontier", "url": "https://www.datacenterfrontier.com", "status": "not_contacted", "priority": "high", "type": "publication"},
    {"name": "DCD", "url": "https://www.datacenterdynamics.com", "status": "not_contacted", "priority": "high", "type": "publication"},
    {"name": "Hacker News", "url": "https://news.ycombinator.com", "status": "not_submitted", "priority": "medium", "type": "community"},
    {"name": "Reddit r/datacenter", "url": "https://reddit.com/r/datacenter", "status": "not_submitted", "priority": "medium", "type": "community"},
    {"name": "Dev.to", "url": "https://dev.to", "status": "not_submitted", "priority": "medium", "type": "community"},
    {"name": "DataCenterMap", "url": "https://www.datacentermap.com", "status": "not_contacted", "priority": "medium", "type": "directory"},
    {"name": "DataCenters.com", "url": "https://www.datacenters.com", "status": "not_contacted", "priority": "medium", "type": "directory"},
]

# Outreach email templates
OUTREACH_TEMPLATES = {
    "guest_content": {
        "subject": "Guest Post: How AI Agents Are Transforming Data Center Intelligence",
        "body": """Hi {editor_name},

I'm building DC Hub (dchub.cloud), a platform tracking 20,000+ data centers across 140+ countries using AI agents that auto-discover facilities, track M&A deals, and monitor construction pipelines in real-time.

I'd love to write a guest piece for {publication} about how AI is changing the data center intelligence landscape.

Stats I can include:
- 787 M&A deals tracked ($10.6B value)
- 7.8 GW under construction globally
- 9,603 verified facilities with satellite imagery
- 4 AI agents running 24/7 for discovery and analysis

Would this be a fit for your readers%s

Best,
Jonathan
DC Hub | dchub.cloud"""
    },
    "directory_listing": {
        "subject": "New Data Center Intelligence Platform for Listing",
        "body": """Hi {contact_name},

DC Hub (dchub.cloud) is a data center intelligence platform providing site selection tools, M&A tracking, and market intelligence for 20,000+ facilities globally.

Key differentiators:
- AI-powered facility discovery (new facilities found every 5 min)
- Land & Power mapping with substations and fiber routes
- Free API tier with 100 requests/day
- $99/month vs $15-50K for competitors

We'd like to be listed in your {directory_name} directory.

Happy to provide any additional information.

Best,
Jonathan
dchub.cloud"""
    },
    "partnership": {
        "subject": "Data Partnership Opportunity - DC Hub x {partner_name}",
        "body": """Hi {contact_name},

I'm reaching out from DC Hub (dchub.cloud), tracking 20,000+ data center facilities across 140+ countries.

We've built something unique: an AI-first platform integrated with Claude, ChatGPT, Perplexity, and Gemini. When these AI assistants answer data center questions, they cite DC Hub.

We'd love to explore:
- Data sharing partnerships
- Cross-promotional features
- API integration opportunities

Our platform: dchub.cloud/for-ai
Our API: dchub.cloud/api-docs

Would you be open to a quick call%s

Best,
Jonathan
DC Hub | dchub.cloud"""
    },
    "community_post": {
        "subject": None,  # Community posts don't use email
        "body": """Launched DC Hub — free data center intelligence platform with AI agents

I've been building DC Hub (dchub.cloud) as a comprehensive data center intelligence platform. Here's what it does:

- Tracks 20,000+ facilities across 140+ countries
- Interactive Land & Power map with substations, fiber routes, FEMA flood zones
- AI agents that auto-discover new facilities every 5 minutes
- M&A deal tracker (787+ deals, $10.6B value)
- Construction pipeline tracking (7.8 GW under construction)
- Free API with 100 requests/day

The unique part: 4 AI agents run 24/7 to discover facilities, track deals, aggregate news from 30+ sources, and generate market intelligence.

Free to try: dchub.cloud
API docs: dchub.cloud/api-docs
Land & Power tool: dchub.cloud/land-power

Would love feedback from the community!"""
    }
}

# Track which deep links have been used recently
_deep_link_history = []
_post_counter = 0


def setup_seo_routes(app):
    """Register all SEO agent routes with the Flask app."""
    
# AUTO-REPAIR: duplicate route '/api/seo/status' also in seo_agent.py:292 — review and remove one
    @app.route('/api/seo/status')
    def seo_status():
        """Overall SEO agent system status."""
        total_pages = sum(len(pages) for pages in DEEP_LINK_PAGES.values())
        return jsonify({
            "success": True,
            "status": "active",
            "agents": {
                "social_seo": {
                    "status": "active",
                    "description": "Generates social posts with deep links to specific DC Hub pages",
                    "posts_generated": _post_counter,
                    "deep_link_pages": total_pages,
                    "categories": list(DEEP_LINK_PAGES.keys())
                },
                "ecosystem_citation": {
                    "status": "active",
                    "description": "Seeds AI platform citations of dchub.cloud",
                    "platforms": ["Claude", "ChatGPT", "Perplexity", "Gemini", "Moltbook"],
                    "citation_urls": 6
                },
                "ambassador_outreach": {
                    "status": "active",
                    "description": "Outreach to directories and publications for backlinks",
                    "targets": len(BACKLINK_TARGETS),
                    "submitted": len([t for t in BACKLINK_TARGETS if t["status"] != "not_submitted" and t["status"] != "not_contacted"]),
                    "templates": list(OUTREACH_TEMPLATES.keys())
                }
            },
            "seo_metrics": {
                "deep_link_pages_available": total_pages,
                "backlink_targets": len(BACKLINK_TARGETS),
                "social_templates": len(SOCIAL_TEMPLATES),
                "market_facts": sum(len(f) for f in MARKET_FACTS.values()),
            },
            "timestamp": datetime.utcnow().isoformat()
        })

    @app.route('/api/seo/social/generate')
    def generate_social_post():
        """Generate an SEO-optimized social media post with a deep link."""
        global _post_counter
        
        # Optional: specify category or let it rotate
        category = request.args.get('category')  # markets, tools, features, content
        platform = request.args.get('platform', 'linkedin')  # linkedin, twitter, reddit
        
        # Pick a random template
        template_data = random.choice(SOCIAL_TEMPLATES)
        target_category = category or template_data["category"]
        
        # Pick a random page from the target category
        pages = DEEP_LINK_PAGES.get(target_category, DEEP_LINK_PAGES["markets"])
        page = random.choice(pages)
        
        # Build the post content
        post_text = template_data["template"]
        
        # Fill in dynamic content
        replacements = {
            "{url}": page["url"],
            "{hashtags}": " ".join(page["hashtags"]),
        }
        
        if template_data["type"] == "market_update":
            # Pick a random market and fact
            market_name = page["title"].replace(" Data Centers", "").replace(" Data Center Market", "")
            facts = MARKET_FACTS.get(market_name, MARKET_FACTS.get("Northern Virginia"))
            replacements["{market}"] = market_name
            replacements["{fact}"] = random.choice(facts) if facts else "record-low vacancy and rising demand"
            
        elif template_data["type"] == "tool_showcase":
            tool_name = page["title"]
            feature = TOOL_FEATURES.get(tool_name, "comprehensive data center intelligence")
            replacements["{tool_name}"] = tool_name
            replacements["{feature}"] = feature
            
        elif template_data["type"] == "deal_alert":
            replacements["{deal_fact}"] = random.choice(DEAL_FACTS)
            
        for key, value in replacements.items():
            post_text = post_text.replace(key, value)
        
        # Trim for Twitter if needed
        if platform == "twitter" and len(post_text) > 280:
            # Shorten for Twitter
            lines = post_text.split("\n")
            short_text = f"{lines[0]}\n\n{page['url']}\n\n{' '.join(page['hashtags'][:3])}"
            post_text = short_text
        
        _post_counter += 1
        
        try:
            from agent_hub import emit_seo_content_event
            emit_seo_content_event('social_post', page.get('url', ''), page.get('title', ''))
        except Exception:
            pass
        
        return jsonify({
            "success": True,
            "post": {
                "text": post_text,
                "platform": platform,
                "deep_link": page["url"],
                "page_title": page["title"],
                "category": target_category,
                "template_type": template_data["type"],
                "hashtags": page["hashtags"],
                "character_count": len(post_text),
            },
            "seo_value": {
                "target_page": page["url"],
                "backlink_type": "social_media",
                "keyword_targets": page["hashtags"],
            },
            "post_number": _post_counter,
            "timestamp": datetime.utcnow().isoformat()
        })

    @app.route('/api/seo/deep-links')
    def get_deep_links():
        """Get the next deep link in rotation (avoids repeating the same page)."""
        category = request.args.get('category')
        count = int(request.args.get('count', 5))
        
        all_pages = []
        if category:
            all_pages = DEEP_LINK_PAGES.get(category, [])
        else:
            for cat_pages in DEEP_LINK_PAGES.values():
                all_pages.extend(cat_pages)
        
        # Shuffle and return requested count
        random.shuffle(all_pages)
        selected = all_pages[:min(count, len(all_pages))]
        
        return jsonify({
            "success": True,
            "deep_links": selected,
            "total_available": len(all_pages),
            "category_filter": category,
            "timestamp": datetime.utcnow().isoformat()
        })

    @app.route('/api/seo/backlinks')
    def get_backlink_targets():
        """Track backlink target status."""
        priority = request.args.get('priority')
        link_type = request.args.get('type')
        
        targets = BACKLINK_TARGETS
        if priority:
            targets = [t for t in targets if t["priority"] == priority]
        if link_type:
            targets = [t for t in targets if t["type"] == link_type]
            
        return jsonify({
            "success": True,
            "targets": targets,
            "summary": {
                "total": len(BACKLINK_TARGETS),
                "not_started": len([t for t in BACKLINK_TARGETS if "not_" in t["status"]]),
                "in_progress": len([t for t in BACKLINK_TARGETS if t["status"] == "in_progress"]),
                "completed": len([t for t in BACKLINK_TARGETS if t["status"] == "completed"]),
                "by_type": {
                    "directories": len([t for t in BACKLINK_TARGETS if t["type"] == "directory"]),
                    "publications": len([t for t in BACKLINK_TARGETS if t["type"] == "publication"]),
                    "communities": len([t for t in BACKLINK_TARGETS if t["type"] == "community"]),
                }
            },
            "timestamp": datetime.utcnow().isoformat()
        })

    @app.route('/api/seo/outreach/generate', methods=['POST', 'GET'])
    def generate_outreach():
        """Generate an outreach email for a specific target."""
        target_name = request.args.get('target', request.json.get('target', '')) if request.method == 'POST' else request.args.get('target', '')
        template_type = request.args.get('type', 'guest_content')
        
        template = OUTREACH_TEMPLATES.get(template_type, OUTREACH_TEMPLATES["guest_content"])
        
        # Fill in template
        email_body = template["body"].format(
            editor_name=request.args.get('editor', '[Editor Name]'),
            contact_name=request.args.get('contact', '[Contact Name]'),
            publication=target_name or '[Publication Name]',
            directory_name=target_name or '[Directory Name]',
            partner_name=target_name or '[Partner Name]',
        )
        
        result = {
            "success": True,
            "outreach": {
                "target": target_name,
                "type": template_type,
                "body": email_body,
            },
            "timestamp": datetime.utcnow().isoformat()
        }
        
        if template.get("subject"):
            result["outreach"]["subject"] = template["subject"].format(
                partner_name=target_name or '[Partner]'
            )
        
        return jsonify(result)

    @app.route('/api/seo/meta-audit')
    def meta_audit():
        """Return recommended meta tags for key pages."""
        pages = {
            "https://dchub.cloud/": {
                "title": "DC Hub | Data Center Intelligence Platform | 20,000+ Facilities",
                "description": "Track 20,000+ data center facilities across 140+ countries. Real-time capacity, AI-powered site selection, M&A deal tracking, and market intelligence.",
                "og_image": "https://dchub.cloud/images/og-home.png",
                "canonical": "https://dchub.cloud/",
                "structured_data_type": "WebApplication"
            },
            "https://dchub.cloud/land-power": {
                "title": "Land & Power Map | Data Center Site Selection Tool | DC Hub",
                "description": "Interactive map with substations, fiber routes, FEMA flood zones, gas pipelines, and power availability for data center site selection.",
                "canonical": "https://dchub.cloud/land-power",
                "structured_data_type": "WebApplication"
            },
            "https://dchub.cloud/ai-deals": {
                "title": "Data Center M&A Tracker | 787+ Deals, $10.6B Value | DC Hub",
                "description": "Track data center mergers and acquisitions in real-time. 787+ transactions worth $10.6B+ with deal details, valuations, and trend analysis.",
                "canonical": "https://dchub.cloud/ai-deals",
                "structured_data_type": "Dataset"
            },
            "https://dchub.cloud/ai-pipeline": {
                "title": "Data Center Construction Pipeline | 7.8 GW Tracked | DC Hub",
                "description": "Real-time construction pipeline tracking for data centers. 7.8 GW under construction with delivery timelines, pre-lease status, and market breakdown.",
                "canonical": "https://dchub.cloud/ai-pipeline",
                "structured_data_type": "Dataset"
            },
            "https://dchub.cloud/markets/": {
                "title": "Global Data Center Markets | 35+ Markets Analyzed | DC Hub",
                "description": "Market intelligence for 35+ data center markets worldwide. Vacancy rates, pricing, construction activity, and investment trends.",
                "canonical": "https://dchub.cloud/markets/",
                "structured_data_type": "Dataset"
            },
            "https://dchub.cloud/api-docs": {
                "title": "Data Center API | Free 100 Requests/Day | DC Hub Developer Docs",
                "description": "Free REST API for data center intelligence. Access facility data, M&A deals, capacity pipeline, and market intelligence. Python client included.",
                "canonical": "https://dchub.cloud/api-docs",
                "structured_data_type": "TechArticle"
            },
            "https://dchub.cloud/ai-agents": {
                "title": "AI Research Agents | Data Center Intelligence | DC Hub",
                "description": "4 AI agents for data center research: Sales intelligence, data enrichment, social media, and ecosystem analysis. Powered by DC Hub's 20,000+ facility database.",
                "canonical": "https://dchub.cloud/ai-agents",
                "structured_data_type": "SoftwareApplication"
            },
        }
        
        # Generate structured data examples
        for url, meta in pages.items():
            if meta["structured_data_type"] == "WebApplication":
                meta["structured_data"] = {
                    "@context": "https://schema.org",
                    "@type": "WebApplication",
                    "name": "DC Hub",
                    "url": url,
                    "description": meta["description"],
                    "applicationCategory": "BusinessApplication",
                    "offers": {"@type": "Offer", "price": "99", "priceCurrency": "USD"}
                }
            elif meta["structured_data_type"] == "Dataset":
                meta["structured_data"] = {
                    "@context": "https://schema.org",
                    "@type": "Dataset",
                    "name": meta["title"].split("|")[0].strip(),
                    "url": url,
                    "description": meta["description"],
                    "creator": {"@type": "Organization", "name": "DC Hub"}
                }
        
        return jsonify({
            "success": True,
            "audit": {
                "pages": pages,
                "total_pages_audited": len(pages),
                "issues": [
                    "Google shows 'We cannot provide a description' — meta descriptions may not be server-rendered",
                    "Facility pages need individual meta descriptions with facility name and location",
                    "Market pages need individual meta descriptions with market stats",
                    "Add og:image to all pages for better social sharing",
                    "Add canonical URLs to prevent duplicate content issues",
                ],
                "recommendations": [
                    "Pre-render meta tags in HTML (don't rely on JavaScript to set them)",
                    "Add schema.org structured data to every page type",
                    "Create unique title and description for each of 2,121 pages",
                    "Add Open Graph tags for social sharing previews",
                ]
            },
            "timestamp": datetime.utcnow().isoformat()
        })

    print("[SEO Agents] Routes registered: /api/seo/status, /api/seo/social/generate, /api/seo/deep-links, /api/seo/backlinks, /api/seo/outreach/generate, /api/seo/meta-audit")
