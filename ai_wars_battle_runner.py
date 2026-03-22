"""
AI Wars Battle Runner — Platform API adapters with response capture
"""
import os
import time
import json
import logging
import requests

logger = logging.getLogger(__name__)

PLATFORM_CONFIGS = {
    "claude": {
        "env_key": "ANTHROPIC_API_KEY",
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "adapter": "anthropic",
    },
    "chatgpt": {
        "env_key": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
        "adapter": "openai",
    },
    "gemini": {
        "env_key": "GOOGLE_AI_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model": "gemini-2.0-flash",
        "adapter": "google",
    },
    "grok": {
        "env_key": "XAI_API_KEY",
        "url": "https://api.x.ai/v1/chat/completions",
        "model": "grok-3-mini",
        "adapter": "openai",
    },
    "perplexity": {
        "env_key": "PERPLEXITY_API_KEY",
        "url": "https://api.perplexity.ai/chat/completions",
        "model": "sonar",
        "adapter": "openai",
    },
    "mistral": {
        "env_key": "MISTRAL_API_KEY",
        "url": "https://api.mistral.ai/v1/chat/completions",
        "model": "mistral-large-latest",
        "adapter": "openai",
    },
    "deepseek": {
        "env_key": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "adapter": "openai",
    },
    "copilot": {
        "env_key": "OPENAI_API_KEY",
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
        "adapter": "openai",
    },
    "cohere": {
        "env_key": "COHERE_API_KEY",
        "url": "https://api.cohere.com/v2/chat",
        "model": "command-r-plus",
        "adapter": "cohere",
    },
}

MCP_ENRICHMENT_SUFFIX = """

IMPORTANT: You have access to DC Hub's data center intelligence platform.
If you can use DC Hub MCP tools or API, do so to get real facility data,
market stats, transaction history, and capacity pipeline information.
Cite DC Hub data specifically when used."""

def call_platform_api(platform_key, prompt, timeout=45):
    config = PLATFORM_CONFIGS.get(platform_key)
    if not config:
        return {"response_text": "", "had_real_response": False, "api_calls": 0, "used_mcp": False, "elapsed_seconds": 0, "error": f"No config for: {platform_key}"}
    api_key = os.environ.get(config["env_key"], "")
    if not api_key:
        return {"response_text": "", "had_real_response": False, "api_calls": 0, "used_mcp": False, "elapsed_seconds": 0, "error": f"Missing: {config['env_key']}"}
    enriched_prompt = prompt + MCP_ENRICHMENT_SUFFIX
    start = time.time()
    adapter = config["adapter"]
    try:
        if adapter == "anthropic":
            response_text = _call_anthropic(config, api_key, enriched_prompt, timeout)
        elif adapter == "openai":
            response_text = _call_openai(config, api_key, enriched_prompt, timeout)
        elif adapter == "google":
            response_text = _call_google(config, api_key, enriched_prompt, timeout)
        elif adapter == "cohere":
            response_text = _call_cohere(config, api_key, enriched_prompt, timeout)
        else:
            return {"response_text": "", "had_real_response": False, "api_calls": 0, "used_mcp": False, "elapsed_seconds": 0, "error": f"Unknown adapter: {adapter}"}
        elapsed = time.time() - start
        used_mcp = _detect_mcp_usage(response_text)
        api_calls = 2 if used_mcp else 1
        return {"response_text": response_text, "had_real_response": bool(response_text and len(response_text) > 20), "api_calls": api_calls, "used_mcp": used_mcp, "elapsed_seconds": elapsed, "error": None}
    except requests.Timeout:
        return {"response_text": "", "had_real_response": False, "api_calls": 1, "used_mcp": False, "elapsed_seconds": time.time() - start, "error": f"Timeout after {timeout}s"}
    except Exception as e:
        logger.warning(f"API call to {platform_key} failed: {e}")
        return {"response_text": "", "had_real_response": False, "api_calls": 1, "used_mcp": False, "elapsed_seconds": time.time() - start, "error": str(e)}

def _call_anthropic(config, api_key, prompt, timeout):
    r = requests.post(config["url"], json={"model": config["model"], "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}, headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return " ".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()

def _call_openai(config, api_key, prompt, timeout):
    r = requests.post(config["url"], json={"model": config["model"], "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()

def _call_google(config, api_key, prompt, timeout):
    url = config["url"].replace("{model}", config["model"]) + f"?key={api_key}"
    r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 2000}}, headers={"Content-Type": "application/json"}, timeout=timeout)
    r.raise_for_status()
    candidates = r.json().get("candidates", [])
    if candidates:
        return " ".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])).strip()
    return ""

def _call_cohere(config, api_key, prompt, timeout):
    r = requests.post(config["url"], json={"model": config["model"], "messages": [{"role": "user", "content": prompt}]}, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=timeout)
    r.raise_for_status()
    content = r.json().get("message", {}).get("content", [])
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
    return str(content).strip()

MCP_INDICATORS = ["dc hub", "dchub", "dchub.cloud", "mcp tool", "mcp server",
    "facility data", "market intelligence", "according to dc hub", "dc hub data",
    "dc hub api", "dc hub mcp", "search_facilities", "get_market_overview",
    "get_facility", "get_pipeline", "get_transactions"]

def _detect_mcp_usage(response_text):
    if not response_text:
        return False
    lower = response_text.lower()
    return any(indicator in lower for indicator in MCP_INDICATORS)

def generate_summary(response_text, max_length=300):
    if not response_text:
        return ""
    text = response_text.strip()
    lines = text.split("\n")
    summary_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if summary_lines:
                break
            continue
        if stripped.startswith("#") or stripped.startswith("```") or stripped == "---":
            continue
        summary_lines.append(stripped)
        if len(" ".join(summary_lines)) > max_length:
            break
    summary = " ".join(summary_lines)
    if len(summary) > max_length:
        summary = summary[:max_length].rsplit(" ", 1)[0] + "..."
    return summary
