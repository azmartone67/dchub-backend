"""
DC Hub — ChatGPT MCP Compatibility Layer
==========================================
Adds the two tools ChatGPT Deep Research & Company Knowledge require:
  • search(query) → {"results": [{"id": ..., "title": ..., "snippet": ...}]}
  • fetch(id)     → {"id": ..., "title": ..., "content": ..., "metadata": {...}}

Also patches CORS to allow chatgpt.com / chat.openai.com origins.

INSTALLATION:
  1. Copy this file into your Railway project root (next to main.py & mcp_gateway.py)
  2. In main.py, add AFTER your MCP server initialization:
       from chatgpt_mcp_compat import register_chatgpt_compat
       register_chatgpt_compat(mcp_server)
  3. Deploy to Railway
  4. In ChatGPT: Settings → Connectors → Create → URL: https://dchub.cloud/mcp

WHY THIS IS NEEDED:
  ChatGPT Deep Research and Company Knowledge ONLY call tools named
  "search" and "fetch" with specific input/output schemas. Your existing
  tools (search_facilities, get_facility, get_news, etc.) work great for
  Claude/Cursor/Windsurf but ChatGPT ignores them for Deep Research.

  This wrapper maps search → fan-out across facilities, news, deals, pipeline
  and fetch → retrieve the specific record by type-prefixed ID.
"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("chatgpt_compat")

# ============================================================================
# CORS PATCH — Allow ChatGPT origins on the /mcp endpoint
# ============================================================================

def patch_cors_for_chatgpt(app):
    """
    Add CORS headers that ChatGPT's connector system requires.
    Call this with your Flask app instance.
    
    Usage in main.py:
        from chatgpt_mcp_compat import patch_cors_for_chatgpt
        patch_cors_for_chatgpt(app)
    """
    @app.after_request
    def add_chatgpt_cors(response):
        # Only apply to MCP endpoint
        from flask import request
        if request.path == "/mcp" or request.path.startswith("/mcp"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "POST, GET, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, mcp-session-id, Accept, Authorization"
            response.headers["Access-Control-Expose-Headers"] = "Mcp-Session-Id"
            response.headers["Access-Control-Max-Age"] = "86400"
        return response

# AUTO-REPAIR: duplicate route '/mcp' also in main.py:5653 — review and remove one
    @app.route("/mcp", methods=["OPTIONS"])
    @app.route("/mcp/sse", methods=["OPTIONS"])
    def mcp_cors_preflight():
        from flask import make_response
        resp = make_response("", 204)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, mcp-session-id, Accept, Authorization"
        resp.headers["Access-Control-Expose-Headers"] = "Mcp-Session-Id"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

    logger.info("🔓 CORS patched for ChatGPT connector compatibility")


# ============================================================================
# SEARCH TOOL — Fan-out query across DC Hub data sources
# ============================================================================

# These functions contain the actual logic. They're designed to be called
# by whatever MCP framework you use (FastMCP, raw JSON-RPC, etc.)

def chatgpt_search(query: str, db_conn=None) -> dict:
    """
    ChatGPT-compatible search tool.
    
    Input:  {"query": "string"}
    Output: {"results": [{"id": "type:real_id", "title": "...", "snippet": "..."}]}
    
    Fans out the query across facilities, news, transactions, and pipeline.
    Returns unified results with type-prefixed IDs for the fetch tool.
    """
    results = []
    
    # --- Search facilities ---
    try:
        from main import search_facilities_logic  # Your existing search function
        facilities = search_facilities_logic(query=query, limit=10)
        for f in (facilities or []):
            fid = f.get("id") or f.get("facility_id") or f.get("slug", "")
            results.append({
                "id": f"facility:{fid}",
                "title": f"{f.get('name', 'Unknown')} — {f.get('operator', '')}",
                "snippet": _build_facility_snippet(f),
            })
    except Exception as e:
        logger.warning(f"Facility search failed: {e}")

    # --- Search news ---
    try:
        from main import search_news_logic  # Your existing news function
        articles = search_news_logic(query=query, limit=8)
        for a in (articles or []):
            aid = a.get("id") or a.get("article_id") or a.get("url", "")
            results.append({
                "id": f"news:{aid}",
                "title": a.get("title", "Untitled"),
                "snippet": (a.get("summary") or a.get("description") or "")[:300],
            })
    except Exception as e:
        logger.warning(f"News search failed: {e}")

    # --- Search M&A transactions ---
    try:
        from main import search_transactions_logic  # Your existing deals function
        deals = search_transactions_logic(query=query, limit=8)
        for d in (deals or []):
            did = d.get("id") or d.get("deal_id", "")
            buyer = d.get("buyer", "Unknown")
            seller = d.get("seller", "Unknown")
            value = d.get("value_usd") or d.get("deal_value", "")
            results.append({
                "id": f"deal:{did}",
                "title": f"{buyer} acquires from {seller}",
                "snippet": _build_deal_snippet(d),
            })
    except Exception as e:
        logger.warning(f"Transaction search failed: {e}")

    # --- Search pipeline ---
    try:
        from main import search_pipeline_logic  # Your existing pipeline function
        projects = search_pipeline_logic(query=query, limit=8)
        for p in (projects or []):
            pid = p.get("id") or p.get("project_id", "")
            results.append({
                "id": f"pipeline:{pid}",
                "title": f"{p.get('operator', '')} — {p.get('location', '')}",
                "snippet": _build_pipeline_snippet(p),
            })
    except Exception as e:
        logger.warning(f"Pipeline search failed: {e}")

    # --- Search market intel ---
    try:
        from main import search_market_intel_logic
        markets = search_market_intel_logic(query=query, limit=5)
        for m in (markets or []):
            mid = m.get("market") or m.get("id", query)
            results.append({
                "id": f"market:{mid}",
                "title": f"Market Intel: {m.get('market', query)}",
                "snippet": _build_market_snippet(m),
            })
    except Exception as e:
        logger.warning(f"Market intel search failed: {e}")

    return {"results": results}


# ============================================================================
# FETCH TOOL — Retrieve full record by type-prefixed ID
# ============================================================================

def chatgpt_fetch(id: str) -> dict:
    """
    ChatGPT-compatible fetch tool.
    
    Input:  {"id": "type:real_id"}  (e.g. "facility:equinix-ash1", "news:12345")
    Output: {"id": "...", "title": "...", "content": "...", "metadata": {...}}
    
    Parses the type prefix and routes to the appropriate DC Hub data source.
    """
    if ":" not in id:
        return {"id": id, "title": "Error", "content": f"Invalid ID format. Expected 'type:id' (e.g. 'facility:equinix-ash1'), got: {id}"}

    record_type, record_id = id.split(":", 1)

    try:
        if record_type == "facility":
            return _fetch_facility(record_id)
        elif record_type == "news":
            return _fetch_news(record_id)
        elif record_type == "deal":
            return _fetch_deal(record_id)
        elif record_type == "pipeline":
            return _fetch_pipeline(record_id)
        elif record_type == "market":
            return _fetch_market(record_id)
        else:
            return {
                "id": id,
                "title": f"Unknown record type: {record_type}",
                "content": f"Supported types: facility, news, deal, pipeline, market",
            }
    except Exception as e:
        logger.error(f"Fetch error for {id}: {e}")
        return {"id": id, "title": "Error", "content": str(e)}


# ============================================================================
# FETCH HANDLERS — One per data type
# ============================================================================

def _fetch_facility(facility_id: str) -> dict:
    """Fetch full facility details."""
    try:
        from main import get_facility_logic
        f = get_facility_logic(facility_id)
        if not f:
            return {"id": f"facility:{facility_id}", "title": "Not Found", "content": "Facility not found"}
        
        content_parts = [
            f"Facility: {f.get('name', 'Unknown')}",
            f"Operator: {f.get('operator', 'N/A')}",
            f"Location: {f.get('city', '')}, {f.get('state', '')} {f.get('country', '')}",
            f"Power Capacity: {f.get('capacity_mw', 'N/A')} MW",
            f"Total Space: {f.get('total_space_sqft', 'N/A')} sqft",
            f"PUE: {f.get('pue', 'N/A')}",
            f"Tier: {f.get('tier', 'N/A')}",
        ]
        
        if f.get("carriers"):
            content_parts.append(f"Carriers: {', '.join(f['carriers'][:10])}")
        if f.get("certifications"):
            content_parts.append(f"Certifications: {', '.join(f['certifications'])}")
        
        return {
            "id": f"facility:{facility_id}",
            "title": f"{f.get('name', 'Unknown')} — {f.get('operator', '')}",
            "content": "\n".join(content_parts),
            "metadata": {
                "source": "DC Hub",
                "url": f"https://dchub.cloud/facility/{facility_id}",
                "data_type": "facility",
                "operator": f.get("operator", ""),
                "country": f.get("country", ""),
                "capacity_mw": f.get("capacity_mw"),
                "last_updated": f.get("updated_at", ""),
            },
        }
    except ImportError:
        return _fetch_via_api("facility", facility_id)


def _fetch_news(article_id: str) -> dict:
    """Fetch full news article."""
    try:
        from main import get_news_article_logic
        a = get_news_article_logic(article_id)
        if not a:
            return {"id": f"news:{article_id}", "title": "Not Found", "content": "Article not found"}
        
        return {
            "id": f"news:{article_id}",
            "title": a.get("title", "Untitled"),
            "content": a.get("summary") or a.get("content") or a.get("description", ""),
            "metadata": {
                "source": a.get("source", "DC Hub News"),
                "url": a.get("url", ""),
                "published": a.get("published_at") or a.get("date", ""),
                "category": a.get("category", ""),
                "relevance_score": a.get("relevance_score", 0),
            },
        }
    except ImportError:
        return _fetch_via_api("news", article_id)


def _fetch_deal(deal_id: str) -> dict:
    """Fetch full M&A transaction details."""
    try:
        from main import get_transaction_logic
        d = get_transaction_logic(deal_id)
        if not d:
            return {"id": f"deal:{deal_id}", "title": "Not Found", "content": "Transaction not found"}
        
        value_str = ""
        if d.get("value_usd"):
            value_str = f"${d['value_usd']:,.0f}" if isinstance(d['value_usd'], (int, float)) else str(d['value_usd'])
        
        content_parts = [
            f"Buyer: {d.get('buyer', 'N/A')}",
            f"Seller: {d.get('seller', 'N/A')}",
            f"Deal Value: {value_str or 'Undisclosed'}",
            f"Deal Type: {d.get('deal_type', 'N/A')}",
            f"Date: {d.get('date') or d.get('announced_date', 'N/A')}",
            f"Region: {d.get('region', 'N/A')}",
        ]
        if d.get("assets"):
            content_parts.append(f"Assets: {d['assets']}")
        if d.get("capacity_mw"):
            content_parts.append(f"Capacity: {d['capacity_mw']} MW")
        
        return {
            "id": f"deal:{deal_id}",
            "title": f"{d.get('buyer', '?')} acquires from {d.get('seller', '?')}",
            "content": "\n".join(content_parts),
            "metadata": {
                "source": "DC Hub M&A Tracker",
                "url": f"https://dchub.cloud/deals",
                "data_type": "transaction",
                "value_usd": d.get("value_usd"),
                "deal_type": d.get("deal_type", ""),
                "date": d.get("date") or d.get("announced_date", ""),
            },
        }
    except ImportError:
        return _fetch_via_api("deal", deal_id)


def _fetch_pipeline(project_id: str) -> dict:
    """Fetch full pipeline project details."""
    try:
        from main import get_pipeline_project_logic
        p = get_pipeline_project_logic(project_id)
        if not p:
            return {"id": f"pipeline:{project_id}", "title": "Not Found", "content": "Project not found"}
        
        content_parts = [
            f"Operator: {p.get('operator', 'N/A')}",
            f"Location: {p.get('location') or p.get('city', 'N/A')}, {p.get('country', '')}",
            f"Capacity: {p.get('capacity_mw', 'N/A')} MW",
            f"Status: {p.get('status', 'N/A')}",
            f"Expected Completion: {p.get('expected_completion') or p.get('completion_date', 'N/A')}",
        ]
        if p.get("investment_usd"):
            content_parts.append(f"Investment: ${p['investment_usd']:,.0f}")
        
        return {
            "id": f"pipeline:{project_id}",
            "title": f"{p.get('operator', '')} — {p.get('location', '')} ({p.get('capacity_mw', '?')} MW)",
            "content": "\n".join(content_parts),
            "metadata": {
                "source": "DC Hub Pipeline Tracker",
                "url": "https://dchub.cloud/pipeline",
                "data_type": "pipeline_project",
                "status": p.get("status", ""),
                "capacity_mw": p.get("capacity_mw"),
            },
        }
    except ImportError:
        return _fetch_via_api("pipeline", project_id)


def _fetch_market(market_name: str) -> dict:
    """Fetch market intelligence for a specific market."""
    try:
        from main import get_market_intel_logic
        m = get_market_intel_logic(market=market_name)
        if not m:
            return {"id": f"market:{market_name}", "title": "Not Found", "content": "Market data not found"}
        
        content_parts = [
            f"Market: {m.get('market', market_name)}",
            f"Supply: {m.get('supply_mw', 'N/A')} MW",
            f"Demand: {m.get('demand_mw', 'N/A')} MW",
            f"Vacancy Rate: {m.get('vacancy_rate', 'N/A')}",
            f"Avg Price: {m.get('avg_price_kwh', 'N/A')} $/kWh",
            f"Pipeline: {m.get('pipeline_mw', 'N/A')} MW under construction",
        ]
        if m.get("top_operators"):
            content_parts.append(f"Top Operators: {', '.join(m['top_operators'][:5])}")
        
        return {
            "id": f"market:{market_name}",
            "title": f"Market Intelligence: {market_name}",
            "content": "\n".join(content_parts),
            "metadata": {
                "source": "DC Hub Market Intel",
                "url": "https://dchub.cloud/markets",
                "data_type": "market_intelligence",
                "market": market_name,
            },
        }
    except ImportError:
        return _fetch_via_api("market", market_name)


# ============================================================================
# FALLBACK — If direct imports don't work, call via internal API
# ============================================================================

def _fetch_via_api(record_type: str, record_id: str) -> dict:
    """Fallback: fetch via internal HTTP if direct function import fails."""
    import requests
    
    url_map = {
        "facility": f"https://dchub.cloud/api/facilities/{record_id}",
        "news": f"https://dchub.cloud/api/news/{record_id}",
        "deal": f"https://dchub.cloud/api/deals/{record_id}",
        "pipeline": f"https://dchub.cloud/api/pipeline/{record_id}",
        "market": f"https://dchub.cloud/api/market-intel?market={record_id}",
    }
    
    url = url_map.get(record_type)
    if not url:
        return {"id": f"{record_type}:{record_id}", "title": "Error", "content": f"Unknown type: {record_type}"}
    
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        return {
            "id": f"{record_type}:{record_id}",
            "title": data.get("name") or data.get("title") or f"{record_type} {record_id}",
            "content": json.dumps(data, indent=2)[:5000],
            "metadata": {"source": "DC Hub", "url": url},
        }
    except Exception as e:
        return {"id": f"{record_type}:{record_id}", "title": "Error", "content": str(e)}


# ============================================================================
# SNIPPET BUILDERS
# ============================================================================

def _build_facility_snippet(f: dict) -> str:
    parts = []
    if f.get("operator"):
        parts.append(f"Operator: {f['operator']}")
    loc = ", ".join(filter(None, [f.get("city"), f.get("state"), f.get("country")]))
    if loc:
        parts.append(loc)
    if f.get("capacity_mw"):
        parts.append(f"{f['capacity_mw']} MW")
    if f.get("tier"):
        parts.append(f"Tier {f['tier']}")
    return " | ".join(parts) if parts else "Data center facility"


def _build_deal_snippet(d: dict) -> str:
    parts = [f"{d.get('buyer', '?')} → {d.get('seller', '?')}"]
    if d.get("value_usd"):
        v = d["value_usd"]
        parts.append(f"${v:,.0f}" if isinstance(v, (int, float)) else str(v))
    if d.get("deal_type"):
        parts.append(d["deal_type"])
    if d.get("date"):
        parts.append(str(d["date"]))
    return " | ".join(parts)


def _build_pipeline_snippet(p: dict) -> str:
    parts = []
    if p.get("operator"):
        parts.append(p["operator"])
    if p.get("location"):
        parts.append(p["location"])
    if p.get("capacity_mw"):
        parts.append(f"{p['capacity_mw']} MW")
    if p.get("status"):
        parts.append(p["status"])
    return " | ".join(parts) if parts else "Pipeline project"


def _build_market_snippet(m: dict) -> str:
    parts = []
    if m.get("market"):
        parts.append(m["market"])
    if m.get("supply_mw"):
        parts.append(f"Supply: {m['supply_mw']} MW")
    if m.get("vacancy_rate"):
        parts.append(f"Vacancy: {m['vacancy_rate']}")
    return " | ".join(parts) if parts else "Market data"


# ============================================================================
# MCP TOOL REGISTRATION
# ============================================================================

def register_chatgpt_compat(mcp_server):
    """
    Register the search & fetch tools on your MCP server instance.
    
    Works with both FastMCP and raw MCP server implementations.
    
    Usage:
        # If using FastMCP:
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("DC Hub")
        register_chatgpt_compat(mcp)
        
        # If using raw MCP server, see register_on_raw_server() below.
    """
    
    # --- Try FastMCP-style registration first ---
    if hasattr(mcp_server, 'tool'):
        @mcp_server.tool(
            name="search",
            description=(
                "Search DC Hub's comprehensive data center intelligence. "
                "Covers 21,000+ facilities across 170+ countries, M&A transactions "
                "($324B+ tracked), construction pipeline (21+ GW), market intelligence, "
                "and curated industry news from 40+ sources. "
                "Returns results with IDs that can be passed to the fetch tool for full details."
            ),
        )
        def search(query: str) -> dict:
            """Search across all DC Hub data sources."""
            return chatgpt_search(query)

        @mcp_server.tool(
            name="fetch",
            description=(
                "Fetch the complete record for a specific DC Hub item. "
                "Pass an ID from search results (format: 'type:id', e.g. "
                "'facility:equinix-ash1', 'news:12345', 'deal:67890'). "
                "Returns full details including metadata and source URLs."
            ),
        )
        def fetch(id: str) -> dict:
            """Fetch a complete record by its type-prefixed ID."""
            return chatgpt_fetch(id)

        logger.info("✅ ChatGPT-compatible search & fetch tools registered (FastMCP)")
        return

    # --- Fallback: raw JSON-RPC tool registration ---
    register_on_raw_server(mcp_server)


def register_on_raw_server(server_or_tools_dict):
    """
    For raw JSON-RPC MCP implementations (non-FastMCP).
    
    If your MCP server maintains a tools dict, pass it here:
        register_on_raw_server(tools_dict)
    
    The tools dict should map tool names to handler functions.
    """
    if isinstance(server_or_tools_dict, dict):
        server_or_tools_dict["search"] = {
            "description": (
                "Search DC Hub's data center intelligence — 21,000+ facilities, "
                "M&A transactions, construction pipeline, market intel, and industry news."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g. 'Equinix Virginia', 'hyperscale deals 2024', 'APAC pipeline')"
                    }
                },
                "required": ["query"],
            },
            "handler": lambda params: chatgpt_search(params.get("query", "")),
        }
        server_or_tools_dict["fetch"] = {
            "description": (
                "Fetch full details for a DC Hub record. Pass an ID from search results "
                "(format: 'type:id', e.g. 'facility:equinix-ash1')."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Record ID from search results (e.g. 'facility:equinix-ash1', 'news:12345')"
                    }
                },
                "required": ["id"],
            },
            "handler": lambda params: chatgpt_fetch(params.get("id", "")),
        }
        logger.info("✅ ChatGPT-compatible search & fetch tools registered (raw dict)")
    else:
        logger.warning("⚠️ Could not register tools — pass either a FastMCP instance or a tools dict")


# ============================================================================
# INTEGRATION SNIPPET — Copy into main.py
# ============================================================================

INTEGRATION_INSTRUCTIONS = """
╔══════════════════════════════════════════════════════════════════════╗
║  HOW TO INTEGRATE — Add these lines to your main.py on Railway     ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  # Near the top with other imports:                                  ║
║  from chatgpt_mcp_compat import (                                    ║
║      register_chatgpt_compat,                                        ║
║      patch_cors_for_chatgpt,                                         ║
║  )                                                                   ║
║                                                                      ║
║  # After Flask app creation but BEFORE routes:                       ║
║  patch_cors_for_chatgpt(app)                                         ║
║                                                                      ║
║  # After MCP server initialization:                                  ║
║  register_chatgpt_compat(mcp_server)                                 ║
║                                                                      ║
║  That's it! Deploy to Railway, then in ChatGPT:                      ║
║  Settings → Connectors → Create → URL: https://dchub.cloud/mcp      ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝

NOTE: The search/fetch functions import your existing logic functions 
from main.py. You may need to adjust the import names to match your 
actual function names. Common patterns:

  search_facilities_logic  →  might be  search_facilities, query_facilities
  get_facility_logic       →  might be  get_facility_by_id, fetch_facility
  search_news_logic        →  might be  get_news, query_news
  get_news_article_logic   →  might be  get_article, fetch_article
  search_transactions_logic →  might be  get_deals, query_transactions
  get_transaction_logic    →  might be  get_deal_by_id, fetch_transaction
  search_pipeline_logic    →  might be  get_pipeline, query_pipeline
  get_pipeline_project_logic → might be  get_project, fetch_pipeline_project
  get_market_intel_logic   →  might be  get_market, query_market_intel

If you can't do direct imports, the _fetch_via_api() fallback will call
your REST endpoints over HTTP instead. It's slower but works anywhere.
"""

if __name__ == "__main__":
    print(INTEGRATION_INSTRUCTIONS)
