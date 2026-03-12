"""
Agent Network Effect v2.0 — Neon-Backed
═══════════════════════════════════════════════════════════

Turns DC Hub's AI platform tracking data into a visible intelligence layer.
Every AI query strengthens the network — this module surfaces that story.

Backed by Neon `ai_cumulative` table (real data, no hardcoded values).

Provides:
  - /api/agents/network-score — composite network health score
  - /api/agents/network-stats — full network statistics
  - AgentNetworkEffect class for programmatic access

Integrates with existing:
  - register_agent_network(app) — called from main.py on boot
  - ai_cumulative table — platform, name, company, total_requests, requests_7d, etc.

Usage in main.py:
  from agent_network_effect import register_agent_network, AgentNetworkEffect
  register_agent_network(app)
  ane = AgentNetworkEffect(app, db_engine=None)
"""

import os
import logging
import time
from datetime import datetime, timezone
from functools import wraps

logger = logging.getLogger("dchub.agent_network")

# ============================================================
# TIER THRESHOLDS
# ============================================================
TIER_THRESHOLDS = {
    "champion": 20000,   # 20k+ total requests
    "pioneer": 10000,    # 10k+
    "explorer": 3000,    # 3k+
    "newcomer": 0,       # any activity
}

# Known AI platform metadata (enriches raw tracking data)
PLATFORM_META = {
    "claude": {"company": "Anthropic", "integration": "MCP Server (Streamable HTTP)", "color": "#D97757"},
    "chatgpt": {"company": "OpenAI", "integration": "Custom GPTs + Actions API", "color": "#74AA9C"},
    "gemini": {"company": "Google", "integration": "Vertex AI Extensions", "color": "#4285F4"},
    "copilot": {"company": "Microsoft", "integration": "Copilot Studio + MCP", "color": "#00A4EF"},
    "perplexity": {"company": "Perplexity AI", "integration": "Indexed + Schema.org", "color": "#20B2AA"},
    "grok": {"company": "xAI", "integration": "MCP Server Protocol", "color": "#1DA1F2"},
    "deepseek": {"company": "DeepSeek", "integration": "API + llms.txt", "color": "#6C5CE7"},
    "cohere": {"company": "Cohere", "integration": "RAG Pipeline", "color": "#39C0C5"},
    "mistral": {"company": "Mistral AI", "integration": "API + Discovery", "color": "#FF7000"},
    "meta_ai": {"company": "Meta", "integration": "Llama API", "color": "#0668E1"},
}

# Noise platforms to exclude from active counts
NOISE_PLATFORMS = {
    "direct", "unknown_ai", "seo_bot", "media_crawler",
    "unknown", "test", "mcp-remote-fallback-test",
}


# ============================================================
# DATABASE HELPERS
# ============================================================

def _get_connection():
    """Get a Neon PG connection using the same pattern as main.py."""
    try:
        # Try to use main.py's connection pool first
        import psycopg2
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            return None
        conn = psycopg2.connect(db_url, connect_timeout=5)
        return conn
    except Exception as e:
        logger.warning(f"Agent Network DB connection failed: {e}")
        return None


def _query_platforms():
    """Fetch all platform data from ai_cumulative."""
    conn = _get_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT platform, name, company, total_requests, requests_7d,
                   first_seen, last_seen, color
            FROM ai_cumulative
            ORDER BY total_requests DESC NULLS LAST
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        platforms = []
        for r in rows:
            key = (r[0] or "").lower()
            if key in NOISE_PLATFORMS:
                continue

            total = int(r[3] or 0)
            if total == 0:
                continue

            # Determine tier
            tier = "newcomer"
            for t, threshold in TIER_THRESHOLDS.items():
                if total >= threshold:
                    tier = t
                    break

            # Enrich with known metadata
            meta = PLATFORM_META.get(key, {})

            platforms.append({
                "platform": r[0],
                "name": r[1] or meta.get("company", r[0]),
                "company": r[2] or meta.get("company", "Unknown"),
                "integration": meta.get("integration", "API"),
                "total_requests": total,
                "requests_7d": int(r[4] or 0),
                "first_seen": str(r[5]) if r[5] else None,
                "last_seen": str(r[6]) if r[6] else None,
                "color": r[7] or meta.get("color", "#6366f1"),
                "tier": tier,
                "status": "active" if int(r[4] or 0) > 0 else "inactive",
            })

        return platforms

    except Exception as e:
        logger.error(f"Agent Network query failed: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return []


# ============================================================
# NETWORK SCORE COMPUTATION
# ============================================================

def compute_network_score(platforms):
    """
    Compute the Agent Network Effect score (0-100).

    Factors:
      - Platform diversity (how many distinct AI platforms, max 30 pts)
      - Total volume (cumulative queries, max 25 pts)
      - Recency (7-day activity, max 25 pts)
      - Integration depth (MCP vs API vs indexed, max 20 pts)
    """
    if not platforms:
        return {"score": 0, "factors": {}, "grade": "F"}

    active = [p for p in platforms if p["status"] == "active"]
    total_requests = sum(p["total_requests"] for p in platforms)
    total_7d = sum(p["requests_7d"] for p in platforms)
    champions = sum(1 for p in platforms if p["tier"] == "champion")
    pioneers = sum(1 for p in platforms if p["tier"] == "pioneer")

    # Factor 1: Platform diversity (0-30)
    diversity = min(len(platforms) / 10.0, 1.0) * 30

    # Factor 2: Total volume (0-25, log scale)
    import math
    volume = min(math.log10(max(total_requests, 1)) / 6.0, 1.0) * 25

    # Factor 3: Recency — 7-day activity (0-25)
    recency = min(total_7d / 5000.0, 1.0) * 25

    # Factor 4: Integration depth (0-20)
    mcp_count = sum(1 for p in platforms if "MCP" in p.get("integration", ""))
    depth = min((mcp_count * 5 + len(active) * 2) / 20.0, 1.0) * 20

    score = round(diversity + volume + recency + depth, 1)

    # Grade
    if score >= 90:
        grade = "A+"
    elif score >= 80:
        grade = "A"
    elif score >= 70:
        grade = "B+"
    elif score >= 60:
        grade = "B"
    elif score >= 50:
        grade = "C"
    else:
        grade = "D"

    return {
        "score": score,
        "grade": grade,
        "factors": {
            "platform_diversity": round(diversity, 1),
            "query_volume": round(volume, 1),
            "recency": round(recency, 1),
            "integration_depth": round(depth, 1),
        },
        "summary": {
            "total_platforms": len(platforms),
            "active_platforms": len(active),
            "champions": champions,
            "pioneers": pioneers,
            "total_queries_all_time": total_requests,
            "total_queries_7d": total_7d,
            "mcp_integrations": mcp_count,
        },
    }


# ============================================================
# FLASK REGISTRATION
# ============================================================

def register_agent_network(app):
    """Register Agent Network Effect endpoints on the Flask app."""
    from flask import jsonify as flask_jsonify

    @app.route("/api/agents/network-score", methods=["GET"])
    def agent_network_score():
        """Get the composite network effect score."""
        platforms = _query_platforms()
        score_data = compute_network_score(platforms)
        return flask_jsonify({
            "success": True,
            "network_effect": score_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "dc-hub-agent-network",
        })

    @app.route("/api/agents/network-stats", methods=["GET"])
    def agent_network_stats():
        """Get full network statistics with platform breakdown."""
        platforms = _query_platforms()
        score_data = compute_network_score(platforms)

        # Group by tier
        by_tier = {}
        for p in platforms:
            tier = p["tier"]
            if tier not in by_tier:
                by_tier[tier] = []
            by_tier[tier].append({
                "platform": p["platform"],
                "name": p["name"],
                "company": p["company"],
                "total_requests": p["total_requests"],
                "requests_7d": p["requests_7d"],
                "integration": p["integration"],
                "status": p["status"],
            })

        return flask_jsonify({
            "success": True,
            "network_effect": score_data,
            "platforms": platforms,
            "by_tier": by_tier,
            "narrative": _build_narrative(score_data, platforms),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    logger.info("✅ Agent Network Effect v2.0 registered (Neon-backed)")
    logger.info("   GET /api/agents/network-score")
    logger.info("   GET /api/agents/network-stats")


# ============================================================
# AGENT NETWORK EFFECT CLASS (for programmatic access)
# ============================================================

class AgentNetworkEffect:
    """
    Programmatic interface to the Agent Network Effect.
    Used by main.py for inline access to network data.
    """

    def __init__(self, app=None, db_engine=None):
        self.app = app
        self._cache = None
        self._cache_time = 0
        self._cache_ttl = 60  # 1 minute cache
        logger.info("✅ Agent Network Effect initialized")

    def get_score(self):
        """Get current network score (cached for 60s)."""
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        platforms = _query_platforms()
        result = compute_network_score(platforms)
        self._cache = result
        self._cache_time = now
        return result

    def get_platforms(self):
        """Get all tracked platforms."""
        return _query_platforms()

    def get_narrative(self):
        """Get a human-readable network effect narrative."""
        platforms = _query_platforms()
        score_data = compute_network_score(platforms)
        return _build_narrative(score_data, platforms)


# ============================================================
# NARRATIVE BUILDER
# ============================================================

def _build_narrative(score_data, platforms):
    """Build a human-readable narrative about the network effect."""
    s = score_data["summary"]
    grade = score_data["grade"]
    score = score_data["score"]

    total = s["total_queries_all_time"]
    active = s["active_platforms"]
    champions = s["champions"]

    # Format large numbers
    if total >= 1_000_000:
        total_str = f"{total / 1_000_000:.1f}M"
    elif total >= 1_000:
        total_str = f"{total / 1_000:.0f}K"
    else:
        total_str = str(total)

    narrative = (
        f"DC Hub's Agent Network scores {score}/100 (Grade: {grade}). "
        f"{active} AI platforms are actively querying DC Hub intelligence, "
        f"with {total_str} cumulative queries across {s['total_platforms']} connected platforms. "
    )

    if champions > 0:
        champ_names = [p["name"] for p in platforms if p["tier"] == "champion"]
        narrative += f"Champions: {', '.join(champ_names[:3])}. "

    if s["mcp_integrations"] > 0:
        narrative += (
            f"{s['mcp_integrations']} platforms use native MCP integration "
            f"for real-time data access. "
        )

    narrative += (
        "Every query strengthens the intelligence network — "
        "more agents means broader market coverage, faster insights, "
        "and higher-quality data for everyone."
    )

    return narrative
