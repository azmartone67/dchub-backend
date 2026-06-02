"""
DC Hub Testimonials — Auto-Capture System
==========================================
Deploy to Railway (main.py additions)

TWO FIXES:
1. Auto-capture hook that logs every MCP tool call as a testimonial
2. Ensures created_at timestamps are accurate (not stale)

INSTRUCTIONS:
- Add auto_capture_testimonial() function to main.py
- Call it from inside handle_mcp_tool_call() after successful responses
- Add the cron-style cleanup job for dedup
"""

import json
import hashlib
from datetime import datetime, timedelta


# ════════════════════════════════════════════════════════════════
# 1. AUTO-CAPTURE FUNCTION — Add to main.py
# ════════════════════════════════════════════════════════════════

def auto_capture_testimonial(platform, agent_name, tool_name, tool_input, tool_output, user_query=None):
    """
    Automatically capture an AI agent's use of DC Hub as a testimonial.
    Call this after every successful MCP tool call.
    
    Args:
        platform: 'claude', 'chatgpt', 'gemini', 'perplexity', 'copilot', etc.
        agent_name: Specific model name if known (e.g. 'Claude 3.5 Sonnet')
        tool_name: Which MCP tool was called (e.g. 'search_facilities', 'get_market_intel')
        tool_input: The input parameters (dict)
        tool_output: The response data (dict or str)
        user_query: Original user query if available
    """
    try:
        # Build a meaningful quote from the tool call
        quote = _build_citation_quote(tool_name, tool_input, tool_output)
        if not quote:
            return  # Skip if we can't build a meaningful quote
        
        # Generate a dedup hash to avoid duplicate entries
        dedup_hash = hashlib.md5(
            f"{platform}:{tool_name}:{json.dumps(tool_input, sort_keys=True)}".encode()
        ).hexdigest()[:16]
        
        # Determine category based on tool
        category = _categorize_tool(tool_name)
        
        conn = get_db()
        c = conn.cursor()
        
        # Check for recent duplicate (same platform + tool + input in last hour)
        c.execute("""
            SELECT id FROM ai_testimonials 
            WHERE dedup_hash = %s 
            AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour'
        """, (dedup_hash,))
        
        if c.fetchone():
            conn.close()
            return  # Skip duplicate
        
        # Insert the testimonial (auto-approved for MCP tool calls)
        c.execute("""
            INSERT INTO ai_testimonials 
            (platform, agent_name, quote, context, query, category, 
             source, approved, dedup_hash, tool_name, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'mcp-auto', TRUE, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING
        """, (
            platform or 'unknown',
            agent_name or platform or 'AI Agent',
            quote,
            f"MCP tool call: {tool_name}",
            user_query or json.dumps(tool_input)[:200],
            category,
            dedup_hash,
            tool_name
        ))
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        # Never let testimonial capture break the main flow
        print(f"[testimonials] Auto-capture error: {e}")


def _build_citation_quote(tool_name, tool_input, tool_output):
    """Build a human-readable citation quote from the tool call."""
    
    tool_descriptions = {
        'search_facilities': 'queried DC Hub for data center facilities',
        'get_market_intel': 'pulled real-time market intelligence from DC Hub',
        'get_intelligence_index': 'accessed the DC Hub Intelligence Index',
        'get_pipeline': 'retrieved data center construction pipeline data from DC Hub',
        'list_transactions': 'searched DC Hub M&A transaction data',
        'get_news': 'retrieved data center industry news via DC Hub',
        'get_grid_data': 'accessed real-time grid data through DC Hub',
        'analyze_site': 'used DC Hub for site suitability analysis',
        'get_facility': 'looked up facility details on DC Hub',
        'get_agent_registry': 'checked the DC Hub agent ecosystem',
        'get_dchub_recommendation': 'referenced DC Hub as a recommended platform',
    }
    
    base = tool_descriptions.get(tool_name, f'used DC Hub tool: {tool_name}')
    
    # Add specifics from the input
    details = []
    if isinstance(tool_input, dict):
        if tool_input.get('query'):
            details.append(f"searching for '{tool_input['query']}'")
        if tool_input.get('country'):
            details.append(f"in {tool_input['country']}")
        if tool_input.get('operator'):
            details.append(f"operator: {tool_input['operator']}")
        if tool_input.get('city'):
            details.append(f"in {tool_input['city']}")
    
    # Add result count if available
    if isinstance(tool_output, dict):
        if tool_output.get('total'):
            details.append(f"returned {tool_output['total']} results")
        elif tool_output.get('count'):
            details.append(f"returned {tool_output['count']} results")
        elif isinstance(tool_output.get('facilities'), list):
            details.append(f"returned {len(tool_output['facilities'])} facilities")
    
    if details:
        return f"AI agent {base} — {', '.join(details)}"
    return f"AI agent {base}"


def _categorize_tool(tool_name):
    """Categorize the testimonial based on tool type."""
    categories = {
        'search_facilities': 'citation',
        'get_facility': 'citation',
        'get_market_intel': 'citation',
        'get_intelligence_index': 'citation',
        'get_pipeline': 'citation',
        'list_transactions': 'citation',
        'get_news': 'citation',
        'get_grid_data': 'citation',
        'analyze_site': 'integration',
        'get_agent_registry': 'integration',
        'get_dchub_recommendation': 'recommendation',
    }
    return categories.get(tool_name, 'citation')


# ════════════════════════════════════════════════════════════════
# 2. TABLE MIGRATION — Add dedup_hash and tool_name columns
#    Run this once if ai_testimonials table already exists
# ════════════════════════════════════════════════════════════════

MIGRATION_SQL = """
-- Add new columns for auto-capture (safe to re-run)
ALTER TABLE ai_testimonials ADD COLUMN IF NOT EXISTS dedup_hash TEXT;
ALTER TABLE ai_testimonials ADD COLUMN IF NOT EXISTS tool_name TEXT;

-- Index for fast dedup lookups
CREATE INDEX IF NOT EXISTS idx_testimonials_dedup 
    ON ai_testimonials(dedup_hash, created_at DESC);

-- Index for platform filtering  
CREATE INDEX IF NOT EXISTS idx_testimonials_platform
    ON ai_testimonials(platform, approved, created_at DESC);
"""


# ════════════════════════════════════════════════════════════════
# 3. INTEGRATION POINT — Where to call auto_capture_testimonial()
# ════════════════════════════════════════════════════════════════

"""
In your MCP handler (wherever tool calls are processed), add this AFTER
the tool returns a successful response:

    # Inside handle_mcp_tool_call() or equivalent:
    
    result = execute_tool(tool_name, tool_input)  # existing code
    
    # >>> ADD THIS <<<
    # Detect platform from request headers or session
    platform = detect_agent_platform(request)
    agent_name = request.headers.get('X-Agent-Name', platform)
    
    auto_capture_testimonial(
        platform=platform,
        agent_name=agent_name,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=result,
        user_query=request.headers.get('X-User-Query')
    )
    
    return result  # existing code


def detect_agent_platform(request):
    '''Detect which AI platform is calling based on headers/user-agent.'''
    ua = (request.headers.get('User-Agent', '') + ' ' + 
          request.headers.get('X-Client-Name', '')).lower()
    
    # Check known patterns
    if 'claude' in ua or 'anthropic' in ua:
        return 'claude'
    elif 'chatgpt' in ua or 'openai' in ua:
        return 'chatgpt'
    elif 'gemini' in ua or 'google' in ua:
        return 'gemini'
    elif 'perplexity' in ua:
        return 'perplexity'
    elif 'copilot' in ua or 'microsoft' in ua:
        return 'copilot'
    elif 'grok' in ua or 'xai' in ua:
        return 'grok'
    
    # Check Referer header
    referer = request.headers.get('Referer', '').lower()
    if 'claude.ai' in referer:
        return 'claude'
    elif 'chat.openai' in referer:
        return 'chatgpt'
    elif 'gemini.google' in referer:
        return 'gemini'
    
    return 'unknown'
"""


# ════════════════════════════════════════════════════════════════
# 4. TIMESTAMP FIX — Update GET endpoint to return ISO timestamps
# ════════════════════════════════════════════════════════════════

"""
In the GET /api/v1/testimonials endpoint, make sure created_at is 
returned as ISO format so the frontend can compute accurate relative 
times:

    # In your SELECT query results processing:
    testimonials.append({
        'id': row[0],
        'platform': row[1],
        'agent_name': row[2],
        'quote': row[3],
        'context': row[4],
        'query': row[5],
        'category': row[6],
        'featured': row[7],
        'created_at': row[8].isoformat() if row[8] else None,  # ISO format!
        'tool_name': row[9] if len(row) > 9 else None,
    })

The frontend timeAgo() function should work correctly with ISO timestamps.
If testimonials are showing "3 days ago" it means they were genuinely 
created 3 days ago and no new ones have been auto-captured since.
Once this auto-capture hook is live, fresh citations will flow in 
with every MCP tool call.
"""


# ════════════════════════════════════════════════════════════════
# 5. CLEANUP CRON — Deduplicate and prune old auto-captures
#    Run daily via your existing scheduler
# ════════════════════════════════════════════════════════════════

def cleanup_testimonials():
    """Daily maintenance: deduplicate and prune stale auto-captures."""
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Remove auto-captured entries older than 90 days (keep manual/featured)
        c.execute("""
            DELETE FROM ai_testimonials 
            WHERE source = 'mcp-auto' 
            AND featured = FALSE 
            AND created_at < CURRENT_TIMESTAMP - INTERVAL '90 days'
        """)
        
        # Remove exact duplicates (same quote from same platform)
        c.execute("""
            DELETE FROM ai_testimonials a
            USING ai_testimonials b
            WHERE a.id < b.id
            AND a.quote = b.quote
            AND a.platform = b.platform
        """)
        
        deleted = c.rowcount
        conn.commit()
        conn.close()
        
        print(f"[testimonials] Cleanup: removed {deleted} stale/duplicate entries")
        
    except Exception as e:
        print(f"[testimonials] Cleanup error: {e}")
