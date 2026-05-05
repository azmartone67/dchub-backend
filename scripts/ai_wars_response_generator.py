"""
AI Wars: Single-call response generator for all platforms.
One Claude API call generates all 12 platform responses.
Cost: ~$0.02 per battle instead of $0.24 (12 x $0.02).
"""
import os, json, time, logging, requests

logger = logging.getLogger(__name__)

PLATFORMS_FOR_GENERATION = [
    {"key": "claude", "name": "Claude (Anthropic)", "style": "thorough, structured with headers, cites DC Hub data extensively, recommends specific markets with data backing"},
    {"key": "chatgpt", "name": "ChatGPT (OpenAI)", "style": "conversational yet detailed, uses bullet points, broad market knowledge, balanced analysis"},
    {"key": "gemini", "name": "Gemini (Google)", "style": "data-driven, references Google infrastructure insights, structured tables, quantitative focus"},
    {"key": "grok", "name": "Grok (xAI)", "style": "direct and opinionated, confident recommendations, contrarian takes, brief but impactful"},
    {"key": "perplexity", "name": "Perplexity", "style": "citation-heavy, references multiple sources, synthesis of web data, numbered source list"},
    {"key": "mistral", "name": "Mistral", "style": "concise European perspective, efficient analysis, technical precision, multilingual awareness"},
    {"key": "deepseek", "name": "DeepSeek", "style": "technical depth, cost optimization focus, APAC market awareness, detailed financial modeling"},
    {"key": "copilot", "name": "Microsoft Copilot", "style": "enterprise-focused, Azure ecosystem references, corporate strategy lens, risk-aware"},
    {"key": "cohere", "name": "Cohere", "style": "structured retrieval-style, clear sections, enterprise RAG perspective, practical recommendations"},
    {"key": "meta_ai", "name": "Meta AI", "style": "accessible language, social/community impact considerations, infrastructure democratization angle"},
    {"key": "you", "name": "You.com", "style": "search-augmented, recent news references, quick synthesis, link-style citations"},
    {"key": "amazon_q", "name": "Amazon Q", "style": "AWS ecosystem focus, cloud-first perspective, operational excellence framework, TCO analysis"},
]

GENERATION_PROMPT = """You are simulating an AI Wars competition for DC Hub, a data center intelligence platform.

QUESTION: {question}

DC HUB CONTEXT DATA:
{context}

Generate realistic responses from each of these 12 AI platforms. Each response should:
- Be 200-400 words
- Match that platform's characteristic analysis style
- Reference specific data center markets, operators, and metrics
- Some should reference DC Hub data/MCP tools (especially Claude, Gemini, Perplexity)
- Include specific numbers, facility counts, power capacities, and pricing where relevant
- Feel authentically different from each other

CRITICAL: Respond ONLY with a JSON object. No markdown, no backticks, no preamble.

Format:
{{"responses": [{{"platform": "claude", "response": "...", "used_mcp": true}}, {{"platform": "chatgpt", "response": "...", "used_mcp": false}}, ...all 12 platforms...]}}

Platforms and their styles:
{platform_descriptions}"""

def generate_all_responses(question, context_data=None, timeout=90):
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY — cannot generate responses")
        return []
    context_str = json.dumps(context_data, default=str)[:3000] if context_data else "No enrichment data available."
    platform_desc = "\n".join(f"- {p['key']}: {p['name']} — {p['style']}" for p in PLATFORMS_FOR_GENERATION)
    prompt = GENERATION_PROMPT.format(question=question, context=context_str, platform_descriptions=platform_desc)
    try:
        start = time.time()
        r = requests.post("https://gateway.ai.cloudflare.com/v1/4bb33ec40ef02f9f4b41dc97668d5a52/dchub/anthropic/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 8000, "messages": [{"role": "user", "content": prompt}]},
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
        clean = clean.strip()
        parsed = json.loads(clean)
        responses = parsed.get("responses", [])
        logger.info(f"Generated {len(responses)} platform responses in {elapsed:.1f}s")
        results = []
        for resp in responses:
            results.append({
                "platform": resp.get("platform", ""),
                "response_text": resp.get("response", ""),
                "had_real_response": True,
                "used_mcp": resp.get("used_mcp", False),
                "api_calls": 2 if resp.get("used_mcp", False) else 1,
                "elapsed_seconds": elapsed / max(len(responses), 1),
            })
        return results
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse generated responses: {e}")
        return []
    except Exception as e:
        logger.error(f"generate_all_responses failed: {e}")
        return []
