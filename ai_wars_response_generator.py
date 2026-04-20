import os, json, time, logging, requests
logger = logging.getLogger(__name__)
PLATFORMS_FOR_GENERATION = [
    {"key": "claude", "style": "thorough, structured, cites DC Hub data extensively"},
    {"key": "chatgpt", "style": "conversational, bullet points, balanced analysis"},
    {"key": "gemini", "style": "data-driven, quantitative, structured tables"},
    {"key": "grok", "style": "direct, opinionated, contrarian takes, brief"},
    {"key": "perplexity", "style": "citation-heavy, multiple sources, numbered references"},
    {"key": "mistral", "style": "concise European perspective, technical precision"},
    {"key": "deepseek", "style": "technical depth, cost optimization, APAC awareness"},
    {"key": "copilot", "style": "enterprise-focused, Azure references, risk-aware"},
    {"key": "cohere", "style": "structured retrieval-style, enterprise RAG perspective"},
    {"key": "meta_ai", "style": "accessible language, community impact, democratization"},
    {"key": "you", "style": "search-augmented, recent news, quick synthesis"},
    {"key": "amazon_q", "style": "AWS ecosystem, cloud-first, TCO analysis"},
]
GENERATION_PROMPT = """You are simulating an AI Wars competition for DC Hub, a data center intelligence platform.
QUESTION: {question}
DC HUB CONTEXT DATA:
{context}
Generate realistic responses from each of these 12 AI platforms. Each response should:
- Be 100-200 words, match that platform's style, be concise
- Reference specific data center markets, operators, metrics
- Some should reference DC Hub data/MCP (especially Claude, Gemini, Perplexity)
- Include specific numbers, facility counts, power capacities, pricing
- Feel authentically different from each other
CRITICAL: Respond ONLY with a JSON object. No markdown, no backticks, no preamble.
Format: {{"responses": [{{"platform": "claude", "response": "...", "used_mcp": true}}, ...]}}
Platforms:
{platform_descriptions}"""

def generate_all_responses(question, context_data=None, timeout=180):
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY")
        return []
    context_str = json.dumps(context_data, default=str)[:3000] if context_data else "No enrichment data."
    platform_desc = "\n".join(f"- {p['key']}: {p['style']}" for p in PLATFORMS_FOR_GENERATION)
    prompt = GENERATION_PROMPT.format(question=question, context=context_str, platform_descriptions=platform_desc)
    try:
        start = time.time()
        r = requests.post("https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 6000, "messages": [{"role": "user", "content": prompt}]},
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            timeout=timeout)
        elapsed = time.time() - start
        r.raise_for_status()
        data = r.json()
        text = " ".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        clean = text
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        parsed = json.loads(clean.strip())
        responses = parsed.get("responses", [])
        logger.info(f"Generated {len(responses)} responses in {elapsed:.1f}s")
        return [{"platform": r.get("platform",""), "response_text": r.get("response",""),
                 "had_real_response": True, "used_mcp": r.get("used_mcp", False),
                 "api_calls": 2 if r.get("used_mcp") else 1,
                 "elapsed_seconds": elapsed/max(len(responses),1)} for r in responses]
    except json.JSONDecodeError as e:
        logger.error(f"Parse error: {e}")
        return []
    except Exception as e:
        logger.error(f"generate_all_responses failed: {e}")
        return []
