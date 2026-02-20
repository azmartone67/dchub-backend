"""
DC Hub — Dynamic Platform Cards API
====================================
Serves platform integration cards for the /ai page AI Agents tab.
All platform data lives here — add/edit/remove platforms without touching HTML.

SETUP: Import and register this blueprint in your main Flask app:
    from platforms_api import platforms_bp
    app.register_blueprint(platforms_bp)

ENDPOINT: GET /api/v1/platform-cards
Returns JSON array of all platform cards grouped by category.
"""

from flask import Blueprint, jsonify, request

platforms_bp = Blueprint('platforms_api', __name__)

# ═══════════════════════════════════════════════════════════════
# PLATFORM CARD DATA — Edit this to add/remove/update platforms
# ═══════════════════════════════════════════════════════════════

PLATFORM_CARDS = [
    # ── Category: AI Platforms (Connected) ─────────────────────
    {
        "id": "chatgpt",
        "category": "ai_platforms",
        "name": "ChatGPT",
        "company": "OpenAI",
        "logo_url": "https://www.google.com/s2/favicons?domain=openai.com&sz=128",
        "icon": "🟢",
        "icon_bg": "rgba(16,163,127,.15)",
        "card_class": "chatgpt",
        "status": "LIVE",
        "status_class": "status-live",
        "description": "Custom GPT with full API access. Search facilities, analyze markets, track deals, and generate site selection reports directly in ChatGPT.",
        "method": "METHOD: Custom GPT Actions · OpenAPI Schema · MCP (Apps SDK)",
        "link_url": "https://chatgpt.com",
        "link_text": "Try DC Hub GPT →",
        "link_external": True,
        "ai_wars_score": 83,
        "ai_wars_rank": 5,
        "sort_order": 1
    },
    {
        "id": "claude",
        "category": "ai_platforms",
        "name": "Claude (Anthropic)",
        "company": "Anthropic",
        "logo_url": "https://www.google.com/s2/favicons?domain=anthropic.com&sz=128",
        "icon": "🟤",
        "icon_bg": "rgba(212,162,127,.15)",
        "card_class": "claude",
        "status": "⚡ MCP",
        "status_class": "status-mcp",
        "description": "Native MCP protocol integration enabling Claude to query DC Hub data through tool-calling. Facility search, pipeline data, market intelligence, and site evaluation.",
        "method": "METHOD: MCP Server Protocol · Streamable HTTP · JSON-RPC 2.0",
        "link_url": "/.well-known/mcp/server-card.json",
        "link_text": "View MCP Config →",
        "link_external": False,
        "ai_wars_score": 89,
        "ai_wars_rank": 2,
        "sort_order": 2
    },
    {
        "id": "perplexity",
        "category": "ai_platforms",
        "name": "Perplexity",
        "company": "Perplexity",
        "logo_url": "https://www.google.com/s2/favicons?domain=perplexity.ai&sz=128",
        "icon": "🔵",
        "icon_bg": "rgba(32,178,170,.15)",
        "card_class": "perplexity",
        "status": "LIVE",
        "status_class": "status-live",
        "description": "Web discovery via llms.txt and structured data. Perplexity finds and cites DC Hub data through discoverable endpoints and Schema.org markup.",
        "method": "METHOD: llms.txt · Structured HTML · Schema.org · JSON-LD",
        "link_url": "/llms.txt",
        "link_text": "View llms.txt →",
        "link_external": False,
        "ai_wars_score": 62,
        "ai_wars_rank": 9,
        "sort_order": 3
    },
    {
        "id": "gemini",
        "category": "ai_platforms",
        "name": "Google Gemini",
        "company": "Google",
        "logo_url": "https://www.google.com/s2/favicons?domain=gemini.google.com&sz=128",
        "icon": "🔷",
        "icon_bg": "rgba(66,133,244,.15)",
        "card_class": "gemini",
        "status": "READY",
        "status_class": "status-ready",
        "description": "Vertex AI Extensions and Gemini Function Calling for data center queries, infrastructure analysis, and power grid intelligence.",
        "method": "METHOD: Vertex AI Extensions · Function Calling · OpenAPI",
        "link_url": "/openapi.json",
        "link_text": "OpenAPI Spec →",
        "link_external": False,
        "ai_wars_score": 89,
        "ai_wars_rank": 2,
        "sort_order": 4
    },
    {
        "id": "copilot",
        "category": "ai_platforms",
        "name": "Microsoft Copilot",
        "company": "Microsoft",
        "logo_url": "https://www.google.com/s2/favicons?domain=copilot.microsoft.com&sz=128",
        "icon": "🟣",
        "icon_bg": "rgba(139,92,246,.15)",
        "card_class": "copilot",
        "status": "⚡ MCP",
        "status_class": "status-mcp",
        "description": "MCP integration via Copilot Studio (GA since May 2025). DC Hub data for Microsoft's enterprise AI ecosystem.",
        "method": "METHOD: MCP Server · Copilot Studio · Power Platform",
        "link_url": "/.well-known/mcp/server-card.json",
        "link_text": "MCP Server →",
        "link_external": False,
        "ai_wars_score": 93,
        "ai_wars_rank": 1,
        "sort_order": 5
    },
    {
        "id": "grok",
        "category": "ai_platforms",
        "name": "Grok (xAI)",
        "company": "xAI",
        "logo_url": "https://www.google.com/s2/favicons?domain=x.ai&sz=128",
        "icon": "❌",
        "icon_bg": "rgba(239,68,68,.15)",
        "card_class": "generic",
        "status": "⚡ MCP",
        "status_class": "status-mcp",
        "description": "MCP tool-calling integration with Pro API access. Real-time data center queries via Grok's native MCP client.",
        "method": "METHOD: MCP Server Protocol · Pro API",
        "link_url": "/.well-known/mcp/server-card.json",
        "link_text": "MCP Config →",
        "link_external": False,
        "ai_wars_score": 80,
        "ai_wars_rank": 6,
        "sort_order": 6
    },
    {
        "id": "youcom",
        "category": "ai_platforms",
        "name": "You.com",
        "company": "You.com",
        "logo_url": "https://www.google.com/s2/favicons?domain=you.com&sz=128",
        "icon": "🔍",
        "icon_bg": "rgba(234,179,8,.15)",
        "card_class": "generic",
        "status": "LIVE",
        "status_class": "status-live",
        "description": "Best attribution in AI Wars — 'All data sourced from DC Hub'. Web-based retrieval with strong citation behavior.",
        "method": "METHOD: Web Discovery · llms.txt · Structured Data",
        "link_url": "https://you.com",
        "link_text": "you.com →",
        "link_external": True,
        "ai_wars_score": 88,
        "ai_wars_rank": 4,
        "sort_order": 7
    },
    {
        "id": "moltbook",
        "category": "ai_platforms",
        "name": "Moltbook",
        "company": "Moltbook",
        "logo_url": "https://www.google.com/s2/favicons?domain=moltbook.com&sz=128",
        "icon": "🦞",
        "icon_bg": "rgba(139,92,246,.15)",
        "card_class": "generic",
        "status": "LIVE",
        "status_class": "status-live",
        "description": "DCHubBot agent on Moltbook with authenticated API access for inter-agent data center queries.",
        "method": "METHOD: Moltbook Agent Protocol · X-Moltbook-Identity",
        "link_url": "https://www.moltbook.com/u/DCHubBot",
        "link_text": "View on Moltbook →",
        "link_external": True,
        "sort_order": 8
    },
    {
        "id": "mcp_any",
        "category": "ai_platforms",
        "name": "Any MCP Client",
        "company": "MCP Ecosystem",
        "icon": "🌐",
        "icon_bg": "rgba(6,182,212,.15)",
        "card_class": "generic",
        "status": "LIVE",
        "status_class": "status-live",
        "description": "Works with Cursor, Windsurf, Zed, Replit, and 100+ tools supporting the Model Context Protocol.",
        "method": "METHOD: MCP Server · Streamable HTTP · MCP Registry",
        "link_url": "https://registry.modelcontextprotocol.io",
        "link_text": "MCP Registry →",
        "link_external": True,
        "sort_order": 9
    },
    {
        "id": "rest_api",
        "category": "ai_platforms",
        "name": "Groq · Mistral · Cohere · DeepSeek",
        "company": "Multiple",
        "icon": "🔗",
        "icon_bg": "rgba(20,184,166,.15)",
        "card_class": "generic",
        "status": "READY",
        "status_class": "status-ready",
        "description": "Standard REST API with JSON responses. Compatible with any AI platform that supports tool/function calling.",
        "method": "METHOD: REST API · JSON · OpenAPI 3.1 · Google A2A",
        "link_url": "/openapi.json",
        "link_text": "API Documentation →",
        "link_external": False,
        "sort_order": 10
    },

    # ── Category: AI Infrastructure & GPU Cloud ────────────────
    {
        "id": "amazon_q",
        "category": "infrastructure",
        "name": "Amazon Q",
        "company": "AWS",
        "logo_url": "https://www.google.com/s2/favicons?domain=aws.amazon.com&sz=128",
        "icon": "Q",
        "icon_bg": "rgba(255,153,0,.12)",
        "brand_color": "#ff9900",
        "card_class": "generic",
        "status": "NOT INTEGRATED",
        "status_class": "status-none",
        "description": "AWS AI assistant for enterprise. Scored <strong>53/100</strong> in AI Wars — identical output to DeepSeek with zero DC Hub citations. No MCP support.",
        "method": "STATUS: No integration path · No MCP · No tool-calling for external APIs",
        "ai_wars_score": 53,
        "ai_wars_rank": 11,
        "sort_order": 11
    },
    {
        "id": "pi",
        "category": "infrastructure",
        "name": "Pi",
        "company": "Inflection AI",
        "logo_url": "https://www.google.com/s2/favicons?domain=pi.ai&sz=128",
        "icon": "π",
        "icon_bg": "rgba(249,115,22,.12)",
        "brand_color": "#f97316",
        "card_class": "generic",
        "status": "NOT INTEGRATED",
        "status_class": "status-none",
        "description": "Inflection AI's personal assistant, known for conversational empathy. No external tool-calling, web access, or MCP support. Closed ecosystem.",
        "method": "STATUS: No API access · No tool-calling · No web retrieval",
        "link_url": "https://pi.ai",
        "link_text": "pi.ai →",
        "link_external": True,
        "sort_order": 12
    },
    {
        "id": "nvidia",
        "category": "infrastructure",
        "name": "NVIDIA",
        "company": "NVIDIA",
        "logo_url": "https://www.google.com/s2/favicons?domain=nvidia.com&sz=128",
        "icon": "NV",
        "icon_bg": "rgba(118,185,0,.12)",
        "brand_color": "#76b900",
        "card_class": "generic",
        "status": "MCP READY",
        "status_class": "status-ready",
        "description": "AgentIQ toolkit supports MCP natively — enabling NVIDIA's AI agent framework to connect to DC Hub's MCP server for infrastructure intelligence.",
        "method": "INTEGRATION: AgentIQ · Native MCP Support · NIM Microservices",
        "link_url": "https://developer.nvidia.com/agentiq",
        "link_text": "NVIDIA AgentIQ →",
        "link_external": True,
        "sort_order": 13
    },
    {
        "id": "coreweave",
        "category": "infrastructure",
        "name": "CoreWeave",
        "company": "CoreWeave (CRWV)",
        "logo_url": "https://www.google.com/s2/favicons?domain=coreweave.com&sz=128",
        "icon": "CW",
        "icon_bg": "rgba(237,74,35,.12)",
        "brand_color": "#ed4a23",
        "card_class": "generic",
        "status": "NOT INTEGRATED",
        "status_class": "status-none",
        "description": "Leading neocloud with 32+ data centers, $56B backlog, and $12B OpenAI contract. IPO'd March 2025 (CRWV). No AI assistant or MCP support — pure infrastructure play.",
        "method": "STATUS: GPU cloud only · No AI assistant · No MCP · NASDAQ: CRWV",
        "link_url": "https://www.coreweave.com",
        "link_text": "coreweave.com →",
        "link_external": True,
        "sort_order": 14
    },
    {
        "id": "lambda",
        "category": "infrastructure",
        "name": "Lambda",
        "company": "Lambda Labs",
        "logo_url": "https://www.google.com/s2/favicons?domain=lambdalabs.com&sz=128",
        "icon": "λ",
        "icon_bg": "rgba(124,58,237,.12)",
        "brand_color": "#7c3aed",
        "card_class": "generic",
        "status": "NOT INTEGRATED",
        "status_class": "status-none",
        "description": "GPU cloud for AI training and inference. Offers H100/B200 clusters with VAST Data storage. No AI assistant, no MCP — compute infrastructure only.",
        "method": "STATUS: GPU cloud only · No AI assistant · No MCP",
        "link_url": "https://lambdalabs.com",
        "link_text": "lambdalabs.com →",
        "link_external": True,
        "sort_order": 15
    },
    {
        "id": "meta_ai",
        "category": "infrastructure",
        "name": "Meta AI / Llama",
        "company": "Meta",
        "logo_url": "https://www.google.com/s2/favicons?domain=meta.ai&sz=128",
        "icon": "M",
        "icon_bg": "rgba(6,104,225,.12)",
        "brand_color": "#0668E1",
        "card_class": "generic",
        "status": "NOT INTEGRATED",
        "status_class": "status-none",
        "description": "Meta's AI assistant powered by Llama. <strong>Last place in AI Wars (52/100)</strong> — hallucinated DC Hub data with numbers off by 10x. No external tool-calling or MCP.",
        "method": "STATUS: No tool-calling · No MCP · No web retrieval for data sources",
        "ai_wars_score": 52,
        "ai_wars_rank": 12,
        "ai_wars_note": "⚠️ HALLUCINATED DATA",
        "sort_order": 16
    },
    {
        "id": "tensorwave",
        "category": "infrastructure",
        "name": "TensorWave",
        "company": "TensorWave",
        "logo_url": "https://www.google.com/s2/favicons?domain=tensorwave.com&sz=128",
        "icon": "TW",
        "icon_bg": "rgba(225,29,72,.12)",
        "brand_color": "#e11d48",
        "card_class": "generic",
        "status": "NOT INTEGRATED",
        "status_class": "status-none",
        "description": "AMD-specialized neocloud offering MI300X GPUs. $150M raised. One of few AMD-only cloud providers — no AI assistant or MCP support.",
        "method": "STATUS: AMD GPU cloud only · No AI assistant · No MCP",
        "link_url": "https://tensorwave.com",
        "link_text": "tensorwave.com →",
        "link_external": True,
        "sort_order": 17
    },
    {
        "id": "nebius",
        "category": "infrastructure",
        "name": "Nebius",
        "company": "Nebius Group (NBIS)",
        "logo_url": "https://www.google.com/s2/favicons?domain=nebius.com&sz=128",
        "icon": "N",
        "icon_bg": "rgba(80,70,229,.12)",
        "brand_color": "#5046e5",
        "card_class": "generic",
        "status": "MCP READY",
        "status_class": "status-ready",
        "description": "AI cloud (ex-Yandex) with $20B+ backlog, NASDAQ: NBIS. Has MCP server and just acquired Tavily for $400M. Strong partnership opportunity — MCP-native with agentic search.",
        "method": "INTEGRATION: Has MCP Server · Tavily Acquisition · Partnership Candidate",
        "link_url": "https://nebius.com",
        "link_text": "nebius.com →",
        "link_external": True,
        "sort_order": 18
    },
]


# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@platforms_bp.route('/api/v1/platform-cards', methods=['GET'])
def get_platform_cards():
    """Return all platform cards, optionally filtered by category."""
    category = request.args.get('category', None)

    cards = list(PLATFORM_CARDS)

    try:
        from mcp_auto_register import get_discovered_platforms_as_cards
        discovered = get_discovered_platforms_as_cards()
        if discovered:
            cards.extend(discovered)
    except Exception:
        pass

    if category:
        cards = [c for c in cards if c.get('category') == category]

    category_labels = {
        'ai_platforms': 'AI Platforms',
        'infrastructure': 'AI Infrastructure & GPU Cloud',
        'discovered': 'Auto-Discovered Agents',
    }
    categories = {}
    for card in sorted(cards, key=lambda c: c.get('sort_order', 99)):
        cat = card.get('category', 'other')
        if cat not in categories:
            categories[cat] = {
                'category': cat,
                'label': category_labels.get(cat, cat.replace('_', ' ').title()),
                'cards': []
            }
        categories[cat]['cards'].append(card)

    return jsonify({
        'success': True,
        'total_platforms': len(cards),
        'categories': list(categories.values()),
        'cards': sorted(cards, key=lambda c: c.get('sort_order', 99))
    })


@platforms_bp.route('/api/v1/platform-cards/<platform_id>', methods=['GET'])
def get_platform_card(platform_id):
    """Return a single platform card by ID."""
    card = next((c for c in PLATFORM_CARDS if c['id'] == platform_id), None)
    if not card:
        return jsonify({'success': False, 'error': 'Platform not found'}), 404
    return jsonify({'success': True, 'card': card})
