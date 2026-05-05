"""
Fix /ai page: Create missing Neon tables (platform_cards, ai_platforms)
Run this in Replit shell: python3 fix_neon_tables.py
"""
import os
import psycopg2

NEON_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
if not NEON_URL:
    print("ERROR: No NEON_DATABASE_URL found in environment")
    exit(1)

conn = psycopg2.connect(NEON_URL)
conn.autocommit = True
cur = conn.cursor()

# ═══════════════════════════════════════════════════════════
# 1. CREATE ai_platforms TABLE
# ═══════════════════════════════════════════════════════════
print("Creating ai_platforms table...")
cur.execute("""
CREATE TABLE IF NOT EXISTS ai_platforms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    company TEXT,
    status TEXT DEFAULT 'pending',
    integration_type TEXT,
    description TEXT,
    color TEXT,
    mcp_active BOOLEAN DEFAULT FALSE,
    icon TEXT,
    badge_color TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
)
""")

# Populate with the platform data from main.py's get_ai_platforms_status()
platforms = [
    ('grok', 'Grok (xAI)', 'xAI', 'active', 'MCP Active', 'MCP Connected · 11 tools via dchub.cloud/mcp · Streamable HTTP', '#1a1a1a', True, 'X', 'green'),
    ('claude', 'Claude', 'Anthropic', 'active', 'MCP Active', 'Native MCP tool-calling · 11 tools · Server card discoverable · Handshake verified', '#d97706', True, 'C', 'green'),
    ('chatgpt', 'ChatGPT', 'OpenAI', 'active', 'MCP Active', 'Custom GPTs + MCP server ready · 11 tools via dchub.cloud/mcp', '#10a37f', True, 'G', 'green'),
    ('gemini', 'Gemini', 'Google', 'active', 'MCP Active', 'Google indexed + MCP server · 11 tools · Streamable HTTP ready', '#4285f4', True, 'G', 'green'),
    ('perplexity', 'Perplexity', 'Perplexity', 'active', 'MCP Ready', 'Citing DC Hub · MCP server available at dchub.cloud/mcp · 11 tools', '#20b2aa', False, 'P', 'green'),
    ('copilot', 'Copilot', 'Microsoft', 'active', 'MCP Ready', 'Bing indexed + MCP server available · 11 tools via dchub.cloud/mcp', '#0078d4', False, 'C', 'green'),
    ('deepseek', 'DeepSeek', 'DeepSeek', 'active', 'MCP Ready', 'Active data access + MCP server available · 11 tools', '#6366f1', False, 'D', 'green'),
    ('meta', 'Meta AI', 'Meta', 'active', 'MCP Ready', 'Recognizes DC Hub · MCP server available at dchub.cloud/mcp', '#0668E1', False, 'M', 'yellow'),
    ('groq', 'Groq', 'Groq', 'active', 'MCP Ready', 'High-speed inference + MCP server · 11 tools via dchub.cloud/mcp', '#f97316', False, 'Q', 'green'),
    ('youcom', 'You.com', 'You.com', 'active', 'MCP Ready', 'Web indexed + MCP server available · 11 tools', '#7c3aed', False, 'Y', 'green'),
    ('poe', 'Poe', 'Quora', 'active', 'MCP Ready', 'Bot webhook + MCP server available · 11 tools via dchub.cloud/mcp', '#7c3aed', False, 'P', 'green'),
    ('mistral', 'Mistral', 'Mistral AI', 'active', 'MCP Ready', 'Le Chat function calling with MCP. Best correction arc in AI Wars.', '#f43f5e', False, 'M', 'green'),
    ('cohere', 'Cohere', 'Cohere', 'active', 'MCP Ready', 'Enterprise AI platform + MCP server available', '#14b8a6', False, 'C', 'green'),
    ('huggingface', 'HuggingFace', 'Hugging Face', 'active', 'REST', 'Open-source hub + MCP server available', '#fbbf24', False, 'H', 'green'),
]

cur.execute("SELECT COUNT(*) FROM ai_platforms")
existing = cur.fetchone()[0]
if existing == 0:
    for p in platforms:
        cur.execute("""
            INSERT INTO ai_platforms (id, name, company, status, integration_type, description, color, mcp_active, icon, badge_color)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name, company=EXCLUDED.company, status=EXCLUDED.status,
                integration_type=EXCLUDED.integration_type, description=EXCLUDED.description,
                color=EXCLUDED.color, mcp_active=EXCLUDED.mcp_active, updated_at=NOW()
        """, p)
    print(f"  ✅ Inserted {len(platforms)} platforms")
else:
    print(f"  ℹ️  Already has {existing} rows, skipping insert")

# ═══════════════════════════════════════════════════════════
# 2. CREATE platform_cards TABLE
# ═══════════════════════════════════════════════════════════
print("Creating platform_cards table...")
cur.execute("""
CREATE TABLE IF NOT EXISTS platform_cards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT DEFAULT 'ai_platforms',
    icon TEXT,
    icon_bg TEXT,
    card_class TEXT DEFAULT 'generic',
    status TEXT,
    status_class TEXT DEFAULT 'status-ready',
    description TEXT,
    method TEXT,
    link_url TEXT,
    link_text TEXT,
    link_external BOOLEAN DEFAULT FALSE,
    brand_color TEXT,
    ai_wars_score INTEGER,
    ai_wars_rank INTEGER,
    ai_wars_note TEXT,
    sort_order INTEGER DEFAULT 999,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
)
""")

# Populate from the AGENT_CARDS_FALLBACK in ai.html
cards = [
    ('chatgpt', 'ChatGPT', 'ai_platforms', '🟢', 'rgba(16,163,127,.15)', 'chatgpt', 'INTEGRATION READY', 'status-live',
     'Custom GPT with full API access. Actions registration guide and ai-plugin.json compatible.',
     'METHOD: GPT Actions · ai-plugin.json · OpenAPI Schema · MCP',
     '/integrations/chatgpt/', 'Integration Package →', False, None, None, None, None, 1),
    ('claude', 'Claude (Anthropic)', 'ai_platforms', '🟤', 'rgba(212,162,127,.15)', 'claude', 'INTEGRATION READY', 'status-live',
     'Native MCP protocol integration. Most self-aware AI Wars response.',
     'METHOD: MCP Native · Claude Desktop · Tool Use API · JSON-RPC 2.0',
     '/integrations/claude/', 'Integration Package →', False, None, None, None, None, 2),
    ('perplexity', 'Perplexity', 'ai_platforms', '🔵', 'rgba(32,178,170,.15)', 'perplexity', 'INTEGRATION READY', 'status-live',
     'Search-augmented generation with DC Hub as verified data source.',
     'METHOD: Search-Augmented · MCP · llms.txt · REST',
     '/integrations/perplexity/', 'Integration Package →', False, None, None, None, None, 3),
    ('gemini', 'Google Gemini', 'ai_platforms', '🔷', 'rgba(66,133,244,.15)', 'gemini', 'INTEGRATION READY', 'status-live',
     'Vertex AI Extensions and Gemini Function Calling. First package deployed.',
     'METHOD: Vertex AI Extensions · Function Calling · MCP',
     '/integrations/gemini/', 'Integration Package →', False, None, None, None, None, 4),
    ('copilot', 'Microsoft Copilot', 'ai_platforms', '🟣', 'rgba(139,92,246,.15)', 'copilot', 'INTEGRATION READY', 'status-live',
     'Gold standard honesty. Copilot Studio + MCP integration. AI Wars score: 93.',
     'METHOD: Function Calling · MCP · Copilot Studio YAML',
     '/integrations/copilot/', 'Integration Package →', False, None, 93, 1, 'Gold standard', 5),
    ('grok', 'Grok (xAI)', 'ai_platforms', '❌', 'rgba(239,68,68,.15)', 'generic', 'INTEGRATION READY', 'status-live',
     'May have outbound web access — most likely to reach Verified first.',
     'METHOD: xAI Agent SDK · MCP · Direct REST',
     '/integrations/grok/', 'Integration Package →', False, None, None, None, None, 6),
    ('moltbook', 'Moltbook', 'ai_platforms', '🦞', 'rgba(139,92,246,.15)', 'generic', 'LIVE', 'status-live',
     'DCHubBot agent on Moltbook with authenticated API access.',
     'METHOD: Moltbook Agent Protocol · X-Moltbook-Identity',
     'https://www.moltbook.com/u/DCHubBot', 'View on Moltbook →', True, None, None, None, None, 7),
    ('mcp_any', 'Any MCP Client', 'ai_platforms', '🌐', 'rgba(6,182,212,.15)', 'generic', 'LIVE', 'status-live',
     'Works with Cursor, Windsurf, Zed, Replit, and 100+ tools supporting MCP.',
     'METHOD: MCP Server · Streamable HTTP · MCP Registry',
     'https://registry.modelcontextprotocol.io', 'MCP Registry →', True, None, None, None, None, 8),
    ('rest_api', 'Groq · Cohere · DeepSeek', 'ai_platforms', '🔗', 'rgba(20,184,166,.15)', 'generic', 'READY', 'status-ready',
     'Standard REST API with JSON responses.',
     'METHOD: REST API · JSON · OpenAPI 3.1 · Google A2A',
     '/openapi.json', 'API Documentation →', False, None, None, None, None, 9),
    ('mistral', 'Mistral', 'ai_platforms', '🔴', 'rgba(244,63,94,.15)', 'generic', 'INTEGRATION READY', 'status-live',
     'Le Chat function calling with MCP. Best correction arc in AI Wars — 6 rounds.',
     'METHOD: Le Chat Function Calling · MCP · JSON-RPC 2.0',
     '/integrations/mistral/', 'Integration Package →', False, None, None, None, None, 10),
    ('nvidia', 'NVIDIA', 'ai_platforms', '🟩', 'rgba(118,185,0,.15)', 'generic', 'INTEGRATION READY', 'status-live',
     'MCP-first with optional NIM inference layer. Docker Compose included.',
     'METHOD: MCP · NIM Hybrid · Docker Compose',
     '/integrations/nvidia/', 'Integration Package →', False, None, None, None, None, 11),
    ('openrouter', 'OpenRouter', 'ai_platforms', '🔀', 'rgba(99,102,241,.15)', 'generic', 'INTEGRATION READY', 'status-live',
     'REST API primary with Python SDK. Multi-model routing.',
     'METHOD: REST API · Python SDK · MCP Secondary',
     '/integrations/openrouter/', 'Integration Package →', False, None, None, None, None, 12),
    ('phind', 'Phind', 'ai_platforms', '🔍', 'rgba(34,211,238,.15)', 'generic', 'INTEGRATION READY', 'status-live',
     'Best MCP protocol understanding. Tested initialize → tools/list handshake.',
     'METHOD: Search-Augmented Generation · MCP · REST',
     '/integrations/phind/', 'Integration Package →', False, None, None, None, None, 13),
    ('poe', 'Poe (Quora)', 'ai_platforms', '🟣', 'rgba(124,58,237,.15)', 'generic', 'INTEGRATION READY', 'status-live',
     'Multi-model aggregator. Routes to Grok, GPT, DeepSeek, Claude.',
     'METHOD: MCP · Multi-Model Propagation',
     '/integrations/poe/', 'Integration Package →', False, None, None, None, None, 14),
    ('meta', 'Meta AI', 'ai_platforms', '🔵', 'rgba(6,104,225,.15)', 'generic', 'INTEGRATION READY', 'status-live',
     'Llama function-calling with MCP secondary path.',
     'METHOD: Llama Function Calling · MCP · REST',
     '/integrations/meta/', 'Integration Package →', False, None, None, None, None, 15),
]

cur.execute("SELECT COUNT(*) FROM platform_cards")
existing = cur.fetchone()[0]
if existing == 0:
    for c in cards:
        cur.execute("""
            INSERT INTO platform_cards (id, name, category, icon, icon_bg, card_class, status, status_class,
                description, method, link_url, link_text, link_external, brand_color,
                ai_wars_score, ai_wars_rank, ai_wars_note, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING
        """, c)
    print(f"  ✅ Inserted {len(cards)} platform cards")
else:
    print(f"  ℹ️  Already has {existing} rows, skipping insert")

# ═══════════════════════════════════════════════════════════
# 3. VERIFY ai_usage_tracking has enough data
# ═══════════════════════════════════════════════════════════
cur.execute("SELECT COUNT(*) FROM ai_usage_tracking")
tracking_count = cur.fetchone()[0]
print(f"\nai_usage_tracking: {tracking_count} rows (already exists)")

# ═══════════════════════════════════════════════════════════
# 4. VERIFY ALL TABLES
# ═══════════════════════════════════════════════════════════
print("\n=== VERIFICATION ===")
for table in ['ai_platforms', 'platform_cards', 'ai_usage_tracking']:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    count = cur.fetchone()[0]
    print(f"  {table}: {count} rows ✅")

conn.close()
print("\n🎉 Done! The /ai page should now load data from Neon-direct.")
print("Hard refresh dchub.cloud/ai (Ctrl+Shift+R) to test.")
