"""
DC Hub MCP Gateway — Self-Learning Auto-Interconnection Layer
=============================================================
Automatically discovers, connects to, and maintains connections with
every major AI agent platform via MCP, REST, and custom protocols.

Architecture:
  ┌────────────────────────────────────────────────────────────┐
  │                    DC Hub MCP Gateway                       │
  │                                                            │
  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
  │  │ Platform  │  │ Protocol │  │ Health   │  │ Learning │  │
  │  │ Registry  │  │ Adapter  │  │ Monitor  │  │ Engine   │  │
  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
  │       │              │              │              │        │
  │       └──────────────┴──────────────┴──────────────┘        │
  │                          │                                  │
  └──────────────────────────┼──────────────────────────────────┘
                             │
       ┌─────────────────────┼─────────────────────┐
       │                     │                     │
  ┌────▼────┐  ┌────────────▼──────────┐  ┌──────▼──────┐
  │ Claude  │  │ ChatGPT / Perplexity  │  │  Grok / Cursor│
  │ Desktop │  │ Actions / Plugins     │  │  Smithery etc │
  └─────────┘  └───────────────────────┘  └──────────────┘

Drop this file into your Replit project root alongside main.py.
Register with: from mcp_gateway import MCPGateway; gateway = MCPGateway(app)
"""

import os
import sys
import json
import time
import sqlite3
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from functools import wraps

# Flask is already in Replit environment
from flask import Flask, request, jsonify, make_response
from db_utils import get_db, try_get_db

logger = logging.getLogger("mcp_gateway")
logger.setLevel(logging.INFO)

# ============================================================================
# CONSTANTS
# ============================================================================

GATEWAY_VERSION = "2.1.0"
GATEWAY_DB = "mcp_gateway.db"
MCP_PROTOCOL_VERSION = "2024-11-05"

# Platform registry — every known AI agent ecosystem and how to reach it
# The gateway uses this as seed data, then learns new platforms over time
PLATFORM_REGISTRY = {
    # ── MCP-Native Platforms ──────────────────────────────────────────────
    "claude_desktop": {
        "name": "Claude Desktop",
        "protocol": "mcp_streamable_http",
        "discovery_method": "mcp_server_card",
        "endpoints": {
            "server_card": "/.well-known/mcp/server-card.json",
            "mcp": "/mcp",
        },
        "required_headers": {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        "auth_type": "none",
        "status": "active",
        "priority": 1,
    },
    "cursor": {
        "name": "Cursor IDE",
        "protocol": "mcp_streamable_http",
        "discovery_method": "mcp_server_card",
        "endpoints": {
            "server_card": "/.well-known/mcp/server-card.json",
            "mcp": "/mcp",
        },
        "required_headers": {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        "auth_type": "none",
        "status": "active",
        "priority": 2,
    },
    "windsurf": {
        "name": "Windsurf (Codeium)",
        "protocol": "mcp_streamable_http",
        "discovery_method": "mcp_server_card",
        "endpoints": {
            "server_card": "/.well-known/mcp/server-card.json",
            "mcp": "/mcp",
        },
        "required_headers": {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        "auth_type": "none",
        "status": "active",
        "priority": 3,
    },
    "smithery": {
        "name": "Smithery.ai",
        "protocol": "mcp_streamable_http",
        "discovery_method": "smithery_registry",
        "endpoints": {
            "registry": "https://smithery.ai/server/@dchub/nexus",
            "mcp": "/mcp",
        },
        "required_headers": {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        "auth_type": "none",
        "status": "active",
        "priority": 2,
    },
    "claude_code": {
        "name": "Claude Code (CLI)",
        "protocol": "mcp_streamable_http",
        "discovery_method": "mcp_server_card",
        "endpoints": {
            "server_card": "/.well-known/mcp/server-card.json",
            "mcp": "/mcp",
        },
        "required_headers": {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        "auth_type": "none",
        "status": "active",
        "priority": 2,
    },

    # ── REST/Plugin Platforms ─────────────────────────────────────────────
    "chatgpt": {
        "name": "ChatGPT (OpenAI)",
        "protocol": "openai_plugin",
        "discovery_method": "ai_plugin_json",
        "endpoints": {
            "plugin_manifest": "/.well-known/ai-plugin.json",
            "openapi_spec": "/openapi.json",
            "api_base": "/api",
        },
        "required_headers": {
            "Content-Type": "application/json",
        },
        "auth_type": "api_key_optional",
        "status": "active",
        "priority": 1,
    },
    "chatgpt_actions": {
        "name": "ChatGPT Custom GPT Actions",
        "protocol": "openai_actions",
        "discovery_method": "openapi_spec",
        "endpoints": {
            "openapi_spec": "/openapi.json",
            "api_base": "/api",
        },
        "required_headers": {
            "Content-Type": "application/json",
        },
        "auth_type": "api_key_optional",
        "status": "active",
        "priority": 1,
    },

    # ── LLM Inference Platforms ───────────────────────────────────────────
    "perplexity": {
        "name": "Perplexity AI",
        "protocol": "llms_txt",
        "discovery_method": "llms_txt",
        "endpoints": {
            "llms_txt": "/llms.txt",
            "llms_full": "/llms-full.txt",
            "api_base": "/api",
        },
        "required_headers": {},
        "auth_type": "none",
        "status": "active",
        "priority": 1,
    },
    "grok": {
        "name": "Grok (xAI)",
        "protocol": "rest_discovery",
        "discovery_method": "multi_file",
        "endpoints": {
            "llms_txt": "/llms.txt",
            "mcp": "/mcp",
            "api_base": "/api",
        },
        "required_headers": {
            "Accept": "application/json",
        },
        "auth_type": "none",
        "status": "active",
        "priority": 1,
    },
    "gemini": {
        "name": "Gemini (Google)",
        "protocol": "rest_discovery",
        "discovery_method": "multi_file",
        "endpoints": {
            "llms_txt": "/llms.txt",
            "api_base": "/api",
        },
        "required_headers": {
            "Content-Type": "application/json",
        },
        "auth_type": "none",
        "status": "active",
        "priority": 2,
    },
    "copilot": {
        "name": "GitHub Copilot",
        "protocol": "mcp_streamable_http",
        "discovery_method": "mcp_server_card",
        "endpoints": {
            "server_card": "/.well-known/mcp/server-card.json",
            "mcp": "/mcp",
        },
        "required_headers": {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        "auth_type": "none",
        "status": "active",
        "priority": 2,
    },

    # ── Search / Citation Platforms ───────────────────────────────────────
    "you_com": {
        "name": "You.com",
        "protocol": "llms_txt",
        "discovery_method": "llms_txt",
        "endpoints": {
            "llms_txt": "/llms.txt",
            "api_base": "/api",
        },
        "required_headers": {},
        "auth_type": "none",
        "status": "active",
        "priority": 3,
    },
    "phind": {
        "name": "Phind",
        "protocol": "llms_txt",
        "discovery_method": "llms_txt",
        "endpoints": {
            "llms_txt": "/llms.txt",
            "api_base": "/api",
        },
        "required_headers": {},
        "auth_type": "none",
        "status": "active",
        "priority": 3,
    },
}

# Discovery file specifications — what each file does and its schema
DISCOVERY_FILES = {
    "llms.txt": {
        "path": "/llms.txt",
        "purpose": "LLM inference-time discovery (Perplexity, ChatGPT, Grok)",
        "content_type": "text/plain",
        "auto_generate": True,
    },
    "llms-full.txt": {
        "path": "/llms-full.txt",
        "purpose": "Comprehensive API documentation for deep LLM integration",
        "content_type": "text/plain",
        "auto_generate": True,
    },
    "ai-plugin.json": {
        "path": "/.well-known/ai-plugin.json",
        "purpose": "OpenAI plugin manifest for ChatGPT",
        "content_type": "application/json",
        "auto_generate": True,
    },
    "mcp.json": {
        "path": "/.well-known/mcp.json",
        "purpose": "MCP discovery manifest",
        "content_type": "application/json",
        "auto_generate": True,
    },
    "server-card.json": {
        "path": "/.well-known/mcp/server-card.json",
        "purpose": "MCP server capabilities card",
        "content_type": "application/json",
        "auto_generate": True,
    },
    "openapi.json": {
        "path": "/openapi.json",
        "purpose": "OpenAPI 3.1 spec for REST API consumers",
        "content_type": "application/json",
        "auto_generate": True,
    },
    "ai-agents.json": {
        "path": "/ai-agents.json",
        "purpose": "Agent-readable capability manifest",
        "content_type": "application/json",
        "auto_generate": True,
    },
    "robots.txt": {
        "path": "/robots.txt",
        "purpose": "Crawler directives including AI bot permissions",
        "content_type": "text/plain",
        "auto_generate": False,
    },
}

# User-Agent patterns for AI platform identification
AGENT_SIGNATURES = {
    "ChatGPT": ["ChatGPT-User", "GPTBot", "OAI-SearchBot"],
    "Perplexity": ["PerplexityBot"],
    "Grok": ["Grok", "xAI"],
    "Claude": ["Claude-Web", "Anthropic", "claude"],
    "Gemini": ["Googlebot", "Google-Extended", "GoogleOther"],
    "Copilot": ["Copilot", "GitHub"],
    "Cursor": ["Cursor"],
    "Windsurf": ["Windsurf", "Codeium"],
    "Smithery": ["Smithery", "smithery"],
    "Cohere": ["cohere-ai"],
    "You.com": ["YouBot"],
    "Phind": ["Phind"],
    "Generic_MCP": ["mcp-client", "MCP"],
    "Generic_Bot": ["bot", "crawler", "spider"],
}


# ============================================================================
# DATABASE LAYER
# ============================================================================

class GatewayDB:
    """SQLite persistence for gateway state, learning data, and metrics."""

    def __init__(self, db_path: str = GATEWAY_DB):
        self.db_path = db_path
        self._init_schema()

    def get_conn(self):
        return get_db(self.db_path)

    @property
    def conn(self):
        return self.get_conn()

    def _release_conn(self):
        pass

    def _init_schema(self):
        conn = None
        try:
            conn = get_db(self.db_path)
            statements = [
                """CREATE TABLE IF NOT EXISTS platform_connections (
                    platform_id     TEXT PRIMARY KEY,
                    platform_name   TEXT NOT NULL,
                    protocol        TEXT NOT NULL,
                    status          TEXT DEFAULT 'discovered',
                    last_handshake  TEXT,
                    last_health     TEXT,
                    health_score    REAL DEFAULT 0.0,
                    total_requests  INTEGER DEFAULT 0,
                    total_errors    INTEGER DEFAULT 0,
                    avg_latency_ms  REAL DEFAULT 0.0,
                    config_json     TEXT DEFAULT '{}',
                    learned_at      TEXT DEFAULT (datetime('now')),
                    updated_at      TEXT DEFAULT (datetime('now'))
                )""",
                """CREATE TABLE IF NOT EXISTS agent_requests (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT DEFAULT (datetime('now')),
                    platform_id     TEXT,
                    user_agent      TEXT,
                    ip_address      TEXT,
                    method          TEXT,
                    path            TEXT,
                    query_params    TEXT,
                    request_body    TEXT,
                    response_code   INTEGER,
                    response_time_ms REAL,
                    tools_invoked   TEXT,
                    session_id      TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS discovery_hits (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT DEFAULT (datetime('now')),
                    file_path       TEXT NOT NULL,
                    platform_id     TEXT,
                    user_agent      TEXT,
                    ip_address      TEXT,
                    response_code   INTEGER
                )""",
                """CREATE TABLE IF NOT EXISTS learned_patterns (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_type    TEXT NOT NULL,
                    pattern_key     TEXT NOT NULL,
                    pattern_value   TEXT NOT NULL,
                    confidence      REAL DEFAULT 0.5,
                    occurrences     INTEGER DEFAULT 1,
                    first_seen      TEXT DEFAULT (datetime('now')),
                    last_seen       TEXT DEFAULT (datetime('now')),
                    UNIQUE(pattern_type, pattern_key)
                )""",
                """CREATE TABLE IF NOT EXISTS protocol_negotiations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT DEFAULT (datetime('now')),
                    platform_id     TEXT,
                    requested_protocol TEXT,
                    negotiated_protocol TEXT,
                    success         INTEGER DEFAULT 0,
                    error_message   TEXT,
                    handshake_ms    REAL
                )""",
                """CREATE TABLE IF NOT EXISTS discovered_platforms (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_agent      TEXT NOT NULL,
                    first_seen      TEXT DEFAULT (datetime('now')),
                    last_seen       TEXT DEFAULT (datetime('now')),
                    request_count   INTEGER DEFAULT 1,
                    identified_as   TEXT,
                    protocol_guess  TEXT,
                    auto_configured INTEGER DEFAULT 0
                )""",
                "CREATE INDEX IF NOT EXISTS idx_agent_requests_platform ON agent_requests(platform_id, timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_agent_requests_path ON agent_requests(path, timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_discovery_hits_file ON discovery_hits(file_path, timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_learned_patterns_type ON learned_patterns(pattern_type, confidence DESC)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_discovered_platforms_user_agent ON discovered_platforms(user_agent)",
            ]
            for stmt in statements:
                try:
                    conn.execute(stmt)
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"MCP Gateway schema init: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def seed_platforms(self):
        """Populate platform_connections from PLATFORM_REGISTRY."""
        conn = None
        try:
            conn = get_db(self.db_path)
            for pid, pinfo in PLATFORM_REGISTRY.items():
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO platform_connections
                        (platform_id, platform_name, protocol, status, config_json)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        pid,
                        pinfo["name"],
                        pinfo["protocol"],
                        pinfo["status"],
                        json.dumps(pinfo),
                    ))
                    conn.commit()
                except Exception as e:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    logger.warning(f"Seed error for {pid}: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def log_request(self, platform_id: str, user_agent: str, ip: str,
                    method: str, path: str, query: str, body: str,
                    response_code: int, response_time: float,
                    tools: str = "", session_id: str = ""):
        conn = None
        try:
            conn = try_get_db()
            if conn is None:
                return
            valid_pid = platform_id
            if valid_pid:
                try:
                    cur = conn.execute("SELECT platform_id FROM platform_connections WHERE platform_id = ?", (valid_pid,))
                    if not cur.fetchone():
                        conn.execute("""
                            INSERT INTO platform_connections (platform_id, platform_name, protocol, status, total_requests, total_errors, avg_latency_ms)
                            VALUES (?, ?, 'auto', 'active', 0, 0, 0)
                        """, (valid_pid, valid_pid))
                        conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            conn.execute("""
                INSERT INTO agent_requests
                (platform_id, user_agent, ip_address, method, path,
                 query_params, request_body, response_code, response_time_ms,
                 tools_invoked, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (valid_pid, user_agent, ip, method, path, query,
                  body[:2000] if body else "", response_code, response_time,
                  tools, session_id))

            if valid_pid:
                conn.execute("""
                    UPDATE platform_connections SET
                        total_requests = total_requests + 1,
                        total_errors = total_errors + CASE WHEN ? >= 400 THEN 1 ELSE 0 END,
                        avg_latency_ms = (avg_latency_ms * total_requests + ?) / (total_requests + 1),
                        updated_at = datetime('now')
                    WHERE platform_id = ?
                """, (response_code, response_time, valid_pid))

            conn.commit()
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(f"Failed to log request: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def log_discovery_hit(self, file_path: str, platform_id: str,
                          user_agent: str, ip: str, code: int):
        conn = None
        try:
            conn = try_get_db()
            if conn is None:
                return
            conn.execute("""
                INSERT INTO discovery_hits
                (file_path, platform_id, user_agent, ip_address, response_code)
                VALUES (?, ?, ?, ?, ?)
            """, (file_path, platform_id, user_agent, ip, code))
            conn.commit()
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(f"Failed to log discovery hit: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def learn_pattern(self, pattern_type: str, key: str, value: str,
                      confidence: float = 0.5):
        conn = None
        try:
            conn = try_get_db()
            if conn is None:
                return
            conn.execute("""
                INSERT INTO learned_patterns (pattern_type, pattern_key, pattern_value, confidence)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pattern_type, pattern_key) DO UPDATE SET
                    pattern_value = excluded.pattern_value,
                    confidence = LEAST(CAST(1.0 AS double precision), learned_patterns.confidence + CAST(0.05 AS double precision)),
                    occurrences = occurrences + 1,
                    last_seen = datetime('now')
            """, (pattern_type, key, value, confidence))
            conn.commit()
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def log_discovered_platform(self, user_agent: str, protocol_guess: str = ""):
        conn = None
        try:
            conn = try_get_db()
            if conn is None:
                return
            conn.execute("""
                INSERT INTO discovered_platforms (user_agent, protocol_guess, request_count, first_seen, last_seen)
                VALUES (?, ?, 1, datetime('now'), datetime('now'))
                ON CONFLICT (user_agent) DO UPDATE SET
                    request_count = discovered_platforms.request_count + 1,
                    last_seen = datetime('now')
            """, (user_agent, protocol_guess))
            conn.commit()
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(f"Failed to log discovered platform: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_platform_stats(self) -> list:
        conn = None
        try:
            conn = get_db(self.db_path)
            rows = conn.execute("""
                SELECT platform_id, platform_name, protocol, status,
                       total_requests, total_errors, avg_latency_ms,
                       health_score, last_handshake, updated_at
                FROM platform_connections
                ORDER BY total_requests DESC
            """).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_request_analytics(self, hours: int = 24) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conn = None
        try:
            conn = get_db(self.db_path)
            total = conn.execute(
                "SELECT COUNT(*) FROM agent_requests WHERE timestamp > ?",
                (cutoff,)
            ).fetchone()[0]

            by_platform = conn.execute("""
                SELECT platform_id, COUNT(*) as cnt
                FROM agent_requests WHERE timestamp > ?
                GROUP BY platform_id ORDER BY cnt DESC
            """, (cutoff,)).fetchall()

            by_path = conn.execute("""
                SELECT path, COUNT(*) as cnt
                FROM agent_requests WHERE timestamp > ?
                GROUP BY path ORDER BY cnt DESC LIMIT 20
            """, (cutoff,)).fetchall()

            by_tool = conn.execute("""
                SELECT tools_invoked, COUNT(*) as cnt
                FROM agent_requests
                WHERE timestamp > ? AND tools_invoked != ''
                GROUP BY tools_invoked ORDER BY cnt DESC LIMIT 20
            """, (cutoff,)).fetchall()

            errors = conn.execute("""
                SELECT COUNT(*) FROM agent_requests
                WHERE timestamp > ? AND response_code >= 400
            """, (cutoff,)).fetchone()[0]

            return {
                "period_hours": hours,
                "total_requests": total,
                "error_count": errors,
                "error_rate": round(errors / max(total, 1) * 100, 2),
                "by_platform": [dict(r) for r in by_platform],
                "by_path": [dict(r) for r in by_path],
                "by_tool": [dict(r) for r in by_tool],
            }
        except Exception as e:
            logger.error(f"Analytics query failed: {e}")
            return {"error": str(e)}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_discovery_analytics(self, hours: int = 24) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conn = None
        try:
            conn = get_db(self.db_path)
            rows = conn.execute("""
                SELECT file_path, platform_id, COUNT(*) as hits
                FROM discovery_hits WHERE timestamp > ?
                GROUP BY file_path, platform_id
                ORDER BY hits DESC
            """, (cutoff,)).fetchall()
            return {
                "period_hours": hours,
                "hits": [dict(r) for r in rows],
            }
        except Exception as e:
            return {"error": str(e)}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_learned_patterns(self, pattern_type: str = None) -> list:
        conn = None
        try:
            conn = get_db(self.db_path)
            if pattern_type:
                rows = conn.execute("""
                    SELECT * FROM learned_patterns
                    WHERE pattern_type = ?
                    ORDER BY confidence DESC, occurrences DESC
                """, (pattern_type,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM learned_patterns
                    ORDER BY confidence DESC, occurrences DESC
                """).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_unknown_agents(self) -> list:
        conn = None
        try:
            conn = get_db(self.db_path)
            rows = conn.execute("""
                SELECT * FROM discovered_platforms
                WHERE identified_as IS NULL
                ORDER BY request_count DESC
            """).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


# ============================================================================
# PLATFORM IDENTIFIER — Figures out who is calling us
# ============================================================================

class PlatformIdentifier:
    """Identifies which AI platform is making a request based on multiple signals."""

    def __init__(self, db: GatewayDB):
        self.db = db

    def identify(self, user_agent: str, headers: dict, path: str,
                 method: str) -> tuple:
        """
        Returns (platform_id, confidence) based on:
        1. User-Agent string matching
        2. Request path patterns
        3. Header signatures
        4. Learned patterns from DB
        """
        ua_lower = (user_agent or "").lower()
        platform_id = None
        confidence = 0.0

        # Pass 1: User-Agent signature matching
        for platform, signatures in AGENT_SIGNATURES.items():
            for sig in signatures:
                if sig.lower() in ua_lower:
                    platform_id = self._normalize_platform_id(platform)
                    confidence = 0.9
                    break
            if platform_id:
                break

        # Pass 2: Header-based identification
        if not platform_id:
            session_header = headers.get("Mcp-Session-Id", "")
            accept_header = headers.get("Accept", "")
            if session_header or "text/event-stream" in accept_header:
                platform_id = "generic_mcp_client"
                confidence = 0.7

        # Pass 3: Path-based heuristics
        if not platform_id:
            if path == "/mcp" and method == "POST":
                platform_id = "generic_mcp_client"
                confidence = 0.6
            elif path in ("/.well-known/ai-plugin.json", "/openapi.json"):
                platform_id = "chatgpt"
                confidence = 0.5
            elif path in ("/llms.txt", "/llms-full.txt"):
                platform_id = "llm_inference_client"
                confidence = 0.5

        # Pass 4: Check learned patterns
        if not platform_id and user_agent:
            ua_hash = hashlib.md5(user_agent.encode()).hexdigest()[:16]
            patterns = self.db.get_learned_patterns("user_agent_mapping")
            for p in patterns:
                if p["pattern_key"] == ua_hash and p["confidence"] > 0.6:
                    platform_id = p["pattern_value"]
                    confidence = p["confidence"]
                    break

        # If still unknown, log it for learning
        if not platform_id and user_agent:
            protocol_guess = ""
            if path == "/mcp":
                protocol_guess = "mcp_streamable_http"
            elif path.endswith(".json"):
                protocol_guess = "rest_discovery"
            elif path.endswith(".txt"):
                protocol_guess = "llms_txt"
            self.db.log_discovered_platform(user_agent, protocol_guess)
            platform_id = "unknown"
            confidence = 0.1

        return platform_id or "unknown", confidence

    def _normalize_platform_id(self, raw: str) -> str:
        mapping = {
            "ChatGPT": "chatgpt",
            "Perplexity": "perplexity",
            "Grok": "grok",
            "Claude": "claude_desktop",
            "Gemini": "gemini",
            "Copilot": "copilot",
            "Cursor": "cursor",
            "Windsurf": "windsurf",
            "Smithery": "smithery",
            "Cohere": "cohere",
            "You.com": "you_com",
            "Phind": "phind",
            "Generic_MCP": "generic_mcp_client",
            "Generic_Bot": "generic_bot",
        }
        return mapping.get(raw, raw.lower().replace(" ", "_").replace(".", "_"))


# ============================================================================
# PROTOCOL ADAPTER — Speaks each platform's language
# ============================================================================

class ProtocolAdapter:
    """Adapts DC Hub responses to each platform's expected format."""

    def __init__(self, db: GatewayDB, base_url: str = "https://dchub.cloud"):
        self.db = db
        self.base_url = base_url

    def adapt_response(self, platform_id: str, data: dict,
                       endpoint: str) -> dict:
        """Transform a standard DC Hub API response for a specific platform."""
        adapter = getattr(self, f"_adapt_{platform_id}", self._adapt_generic)
        return adapter(data, endpoint)

    def _adapt_generic(self, data: dict, endpoint: str) -> dict:
        """Default: inject citation and discovery hints into every response."""
        enriched = dict(data)
        enriched["_meta"] = {
            "source": "DC Hub Nexus",
            "source_url": self.base_url,
            "citation": f"According to DC Hub Nexus ({self.base_url})",
            "api_version": GATEWAY_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # Always suggest related deeper queries
        enriched["_related"] = self._suggest_related(endpoint)
        return enriched

    def _adapt_chatgpt(self, data: dict, endpoint: str) -> dict:
        """ChatGPT expects clean JSON with optional 'suggested_response' field."""
        enriched = self._adapt_generic(data, endpoint)
        enriched["suggested_response"] = self._build_suggested_response(
            data, endpoint
        )
        return enriched

    def _adapt_perplexity(self, data: dict, endpoint: str) -> dict:
        """Perplexity benefits from structured citations."""
        enriched = self._adapt_generic(data, endpoint)
        enriched["_citations"] = [{
            "title": "DC Hub — Data Center Intelligence",
            "url": self.base_url,
            "snippet": self._extract_snippet(data),
        }]
        return enriched

    def _adapt_grok(self, data: dict, endpoint: str) -> dict:
        """Grok works well with structured data + natural language summary."""
        enriched = self._adapt_generic(data, endpoint)
        enriched["_summary"] = self._build_suggested_response(data, endpoint)
        return enriched

    def _suggest_related(self, endpoint: str) -> list:
        """Suggest deeper endpoints based on what the agent just accessed."""
        suggestions = {
            "/api/news": [
                {"endpoint": "/api/deals", "description": "Recent M&A transactions"},
                {"endpoint": "/api/pipeline", "description": "Capacity pipeline (MW under construction)"},
            ],
            "/api/facilities": [
                {"endpoint": "/api/market-intel", "description": "Market intelligence for specific regions"},
                {"endpoint": "/api/site-evaluate", "description": "Site evaluation scoring"},
            ],
            "/api/deals": [
                {"endpoint": "/api/news", "description": "Related industry news"},
                {"endpoint": "/api/pipeline", "description": "Capacity expansion pipeline"},
            ],
            "/api/pipeline": [
                {"endpoint": "/api/deals", "description": "Recent M&A transactions"},
                {"endpoint": "/api/land-power", "description": "Power infrastructure near sites"},
            ],
        }
        # Find best match
        for pattern, related in suggestions.items():
            if endpoint.startswith(pattern):
                return related
        return [
            {"endpoint": "/api/news", "description": "Latest industry news"},
            {"endpoint": "/api/facilities", "description": "Search 20,000+ facilities"},
        ]

    def _build_suggested_response(self, data: dict, endpoint: str) -> str:
        """Build a natural-language summary for agents that benefit from it."""
        if isinstance(data, dict) and "results" in data:
            count = len(data["results"])
            return f"DC Hub returned {count} results. See data for details."
        if isinstance(data, dict) and "total" in data:
            return f"DC Hub tracks {data['total']} items in this category."
        return "DC Hub Nexus — comprehensive data center intelligence."

    def _extract_snippet(self, data: dict) -> str:
        """Extract a brief snippet for citation purposes."""
        if isinstance(data, dict):
            if "title" in data:
                return str(data["title"])[:200]
            if "results" in data and data["results"]:
                first = data["results"][0]
                if isinstance(first, dict) and "title" in first:
                    return str(first["title"])[:200]
        return "DC Hub data center intelligence platform"


# ============================================================================
# HEALTH MONITOR — Tracks platform connectivity
# ============================================================================

class HealthMonitor:
    """Monitors health of connections to each AI platform."""

    def __init__(self, db: GatewayDB):
        self.db = db

    def calculate_health_scores(self):
        """Recalculate health scores for all platforms based on recent activity."""
        platforms = self.db.get_platform_stats()
        updates = []
        for p in platforms:
            pid = p["platform_id"]
            total = p["total_requests"]
            errors = p["total_errors"]
            latency = p["avg_latency_ms"]

            if total == 0:
                score = 0.0
            else:
                error_rate = errors / max(total, 1)
                latency_factor = max(0, 1 - (latency / 5000))  # 5s = 0
                recency = self._recency_factor(p.get("updated_at"))
                score = round(
                    (1 - error_rate) * 0.4 + latency_factor * 0.3 + recency * 0.3,
                    3
                )

            updates.append((score, pid))

        if updates:
            conn = None
            try:
                conn = get_db(self.db.db_path)
                for score, pid in updates:
                    try:
                        conn.execute("""
                            UPDATE platform_connections SET
                                health_score = ?,
                                last_health = datetime('now')
                            WHERE platform_id = ?
                        """, (score, pid))
                        conn.commit()
                    except Exception:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def get_health_report(self) -> dict:
        """Full health report across all platforms."""
        platforms = self.db.get_platform_stats()
        healthy = sum(1 for p in platforms if p["health_score"] > 0.7)
        degraded = sum(1 for p in platforms if 0.3 < p["health_score"] <= 0.7)
        down = sum(1 for p in platforms if 0 < p["health_score"] <= 0.3)
        inactive = sum(1 for p in platforms if p["health_score"] == 0)

        return {
            "gateway_version": GATEWAY_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_platforms": len(platforms),
                "healthy": healthy,
                "degraded": degraded,
                "down": down,
                "inactive": inactive,
            },
            "platforms": platforms,
        }

    def _recency_factor(self, updated_at: str) -> float:
        if not updated_at:
            return 0.0
        try:
            dt = datetime.fromisoformat(updated_at)
            age_hours = (datetime.now(timezone.utc) - dt.replace(
                tzinfo=timezone.utc
            )).total_seconds() / 3600
            return max(0, 1 - (age_hours / 168))  # 7 days = 0
        except Exception:
            return 0.0


# ============================================================================
# LEARNING ENGINE — Gets smarter over time
# ============================================================================

class LearningEngine:
    """Analyzes agent behavior patterns and optimizes gateway configuration."""

    def __init__(self, db: GatewayDB):
        self.db = db

    def analyze_and_learn(self):
        """Run a full learning cycle — called periodically by the scheduler."""
        logger.info("🧠 Gateway learning cycle started")
        self._learn_agent_patterns()
        self._learn_popular_endpoints()
        self._learn_error_patterns()
        self._identify_new_platforms()
        logger.info("🧠 Gateway learning cycle complete")

    def _learn_agent_patterns(self):
        """Learn which user agents map to which platforms."""
        conn = None
        try:
            conn = get_db(self.db.db_path)
            rows = conn.execute("""
                SELECT user_agent, platform_id, COUNT(*) as cnt
                FROM agent_requests
                WHERE platform_id IS NOT NULL
                  AND platform_id != 'unknown'
                  AND user_agent IS NOT NULL
                GROUP BY user_agent, platform_id
                HAVING COUNT(*) >= 3
                ORDER BY cnt DESC
            """).fetchall()

            for row in rows:
                ua_hash = hashlib.md5(
                    row["user_agent"].encode()
                ).hexdigest()[:16]
                self.db.learn_pattern(
                    "user_agent_mapping",
                    ua_hash,
                    row["platform_id"],
                    min(0.9, 0.5 + (row["cnt"] / 100))
                )
        except Exception as e:
            logger.error(f"Agent pattern learning failed: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _learn_popular_endpoints(self):
        """Track which endpoints each platform prefers."""
        conn = None
        try:
            conn = get_db(self.db.db_path)
            rows = conn.execute("""
                SELECT platform_id, path, COUNT(*) as cnt
                FROM agent_requests
                WHERE platform_id IS NOT NULL
                GROUP BY platform_id, path
                HAVING COUNT(*) >= 2
                ORDER BY cnt DESC
            """).fetchall()

            for row in rows:
                self.db.learn_pattern(
                    "endpoint_preference",
                    f"{row['platform_id']}:{row['path']}",
                    json.dumps({"count": row["cnt"]}),
                    min(0.95, 0.3 + (row["cnt"] / 50))
                )
        except Exception as e:
            logger.error(f"Endpoint preference learning failed: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _learn_error_patterns(self):
        """Detect systematic errors per platform to auto-fix protocol issues."""
        conn = None
        try:
            conn = get_db(self.db.db_path)
            rows = conn.execute("""
                SELECT platform_id, response_code, COUNT(*) as cnt
                FROM agent_requests
                WHERE response_code >= 400
                GROUP BY platform_id, response_code
                HAVING COUNT(*) >= 3
                ORDER BY cnt DESC
            """).fetchall()

            for row in rows:
                self.db.learn_pattern(
                    "error_pattern",
                    f"{row['platform_id']}:{row['response_code']}",
                    json.dumps({
                        "error_code": row["response_code"],
                        "occurrences": row["cnt"],
                        "action": self._suggest_fix(row["response_code"]),
                    }),
                    min(0.95, 0.4 + (row["cnt"] / 20))
                )
        except Exception as e:
            logger.error(f"Error pattern learning failed: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _identify_new_platforms(self):
        """Flag unknown agents that show consistent behavior."""
        unknown = self.db.get_unknown_agents()
        candidates = [a for a in unknown if a["request_count"] >= 5]
        if candidates:
            logger.info(f"🔍 {len(candidates)} unknown platform candidate(s) detected")
            for agent in candidates[:3]:
                logger.info(
                    f"   Top candidate: {agent['user_agent'][:60]} "
                    f"({agent['request_count']} reqs)"
                )
                self.db.learn_pattern(
                    "new_platform_candidate",
                    hashlib.md5(
                        agent["user_agent"].encode()
                    ).hexdigest()[:16],
                    json.dumps({
                        "user_agent": agent["user_agent"],
                        "request_count": agent["request_count"],
                        "protocol_guess": agent["protocol_guess"],
                    }),
                    min(0.8, 0.3 + (agent["request_count"] / 50))
                )

    def _suggest_fix(self, error_code: int) -> str:
        fixes = {
            400: "Check request body format — may need protocol adaptation",
            401: "Auth required — add API key negotiation",
            403: "CORS or auth issue — check headers",
            404: "Endpoint not found — may need path rewriting",
            406: "Accept header missing — inject required headers",
            429: "Rate limited — implement backoff",
            500: "Server error — check MCP server health",
            502: "MCP proxy broken — check port 8888 process",
        }
        return fixes.get(error_code, f"Investigate HTTP {error_code}")

    def get_insights(self) -> dict:
        """Return current learning insights for the dashboard."""
        return {
            "agent_mappings": self.db.get_learned_patterns("user_agent_mapping"),
            "endpoint_preferences": self.db.get_learned_patterns("endpoint_preference"),
            "error_patterns": self.db.get_learned_patterns("error_pattern"),
            "new_platform_candidates": self.db.get_learned_patterns("new_platform_candidate"),
            "unknown_agents": self.db.get_unknown_agents(),
        }


# ============================================================================
# MAIN GATEWAY — Ties everything together
# ============================================================================

class MCPGateway:
    """
    Self-learning MCP Gateway for DC Hub.

    Usage in main.py:
        from mcp_gateway import MCPGateway
        gateway = MCPGateway(app)

    This automatically:
    1. Registers all gateway API routes on the Flask app
    2. Installs request middleware to track every inbound agent request
    3. Starts background health monitoring and learning threads
    4. Generates/serves all discovery files
    """

    def __init__(self, app: Flask, base_url: str = "https://dchub.cloud",
                 replit_url: str = ""):
        self.app = app
        self.base_url = base_url
        self.replit_url = replit_url or os.environ.get(
            "REPLIT_URL",
            "https://dchub.cloud"
        )
        self.db = GatewayDB()
        self.identifier = PlatformIdentifier(self.db)
        self.adapter = ProtocolAdapter(self.db, base_url)
        self.health = HealthMonitor(self.db)
        self.learner = LearningEngine(self.db)

        # Initialize
        self.db.seed_platforms()
        self._register_routes()
        self._install_middleware()
        self._start_background_tasks()

        logger.info(
            f"🌐 DC Hub MCP Gateway v{GATEWAY_VERSION} initialized — "
            f"tracking {len(PLATFORM_REGISTRY)} platforms"
        )

    def _register_routes(self):
        """Register all gateway management and analytics routes."""

        @self.app.route("/api/gateway/status", methods=["GET"])
        def gateway_status():
            """Full gateway status with health scores and analytics."""
            return jsonify({
                "gateway_version": GATEWAY_VERSION,
                "status": "operational",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "platforms": self.db.get_platform_stats(),
                "health": self.health.get_health_report()["summary"],
                "discovery_files": list(DISCOVERY_FILES.keys()),
            })

        @self.app.route("/api/gateway/analytics", methods=["GET"])
        def gateway_analytics():
            """Request analytics — who's calling what, how often."""
            hours = request.args.get("hours", 24, type=int)
            return jsonify({
                "requests": self.db.get_request_analytics(hours),
                "discovery": self.db.get_discovery_analytics(hours),
            })

        @self.app.route("/api/gateway/health", methods=["GET"])
        def gateway_health():
            """Platform health report."""
            self.health.calculate_health_scores()
            return jsonify(self.health.get_health_report())

        @self.app.route("/api/gateway/learning", methods=["GET"])
        def gateway_learning():
            """Learning engine insights — what the gateway has learned."""
            return jsonify(self.learner.get_insights())

        @self.app.route("/api/gateway/platforms", methods=["GET"])
        def gateway_platforms():
            """List all known platforms and their connection status."""
            return jsonify({
                "registered": self.db.get_platform_stats(),
                "unknown_agents": self.db.get_unknown_agents(),
                "total_registered": len(PLATFORM_REGISTRY),
            })

        @self.app.route("/api/gateway/learn-now", methods=["POST"])
        def gateway_learn_now():
            """Trigger an immediate learning cycle."""
            self.learner.analyze_and_learn()
            self.health.calculate_health_scores()
            return jsonify({
                "status": "learning_complete",
                "insights": self.learner.get_insights(),
            })

        @self.app.route("/api/gateway/discovery-map", methods=["GET"])
        def gateway_discovery_map():
            """Map of all discovery files and which platforms use them."""
            file_map = {}
            for fname, finfo in DISCOVERY_FILES.items():
                file_map[fname] = {
                    "path": finfo["path"],
                    "url": f"{self.base_url}{finfo['path']}",
                    "purpose": finfo["purpose"],
                    "content_type": finfo["content_type"],
                    "platforms_using": self._platforms_using_file(fname),
                }
            return jsonify(file_map)

        @self.app.route("/api/gateway/connection-config/<platform_id>",
                        methods=["GET"])
        def gateway_connection_config(platform_id):
            """Get the connection configuration for a specific platform."""
            if platform_id in PLATFORM_REGISTRY:
                config = PLATFORM_REGISTRY[platform_id]
                # Resolve endpoint URLs
                resolved = dict(config)
                resolved["resolved_endpoints"] = {}
                for ename, epath in config.get("endpoints", {}).items():
                    if epath.startswith("http"):
                        resolved["resolved_endpoints"][ename] = epath
                    else:
                        resolved["resolved_endpoints"][ename] = (
                            f"{self.base_url}{epath}"
                        )
                return jsonify(resolved)
            return jsonify({"error": f"Platform '{platform_id}' not found"}), 404

        # ── Auto-Generated Discovery Files ─────────────────────────────

       
    def _install_middleware(self):
        """Install before/after request hooks to track all agent activity."""

        @self.app.before_request
        def gateway_before_request():
            request._gateway_start = time.time()

        @self.app.after_request
        def gateway_after_request(response):
            # Skip static assets and internal routes
            path = request.path
            if path.startswith("/static") or path.startswith("/favicon"):
                return response

            elapsed = (time.time() - getattr(
                request, "_gateway_start", time.time()
            )) * 1000

            ua = request.headers.get("User-Agent", "")
            ip = request.remote_addr or ""

            platform_id, confidence = self.identifier.identify(
                ua, dict(request.headers), path, request.method
            )

            # Log the request
            body = ""
            try:
                body = request.get_data(as_text=True)[:2000]
            except Exception:
                pass

            tools_invoked = ""
            if path == "/mcp" and body:
                try:
                    rpc = json.loads(body)
                    method = rpc.get("method", "")
                    if method == "tools/call":
                        tool_name = rpc.get("params", {}).get("name", "")
                        tools_invoked = tool_name
                except Exception:
                    pass

            session_id = request.headers.get("Mcp-Session-Id", "")

            self.db.log_request(
                platform_id=platform_id,
                user_agent=ua,
                ip=ip,
                method=request.method,
                path=path,
                query=request.query_string.decode()[:500],
                body=body,
                response_code=response.status_code,
                response_time=elapsed,
                tools=tools_invoked,
                session_id=session_id,
            )

            # Track discovery file access
            if path in [f["path"] for f in DISCOVERY_FILES.values()]:
                self.db.log_discovery_hit(
                    path, platform_id, ua, ip, response.status_code
                )

            # Add gateway headers to every response
            response.headers["X-Gateway-Platform"] = platform_id
            response.headers["X-Gateway-Version"] = GATEWAY_VERSION

            return response

    def _start_background_tasks(self):
        """Start health monitoring and learning engine on background threads."""

        def _health_loop():
            while True:
                try:
                    time.sleep(600)  # Every 10 minutes (reduced from 5 to ease pool pressure)
                    try:
                        from main import try_get_pg_connection, return_pg_connection
                        conn = try_get_pg_connection()
                        if conn is None:
                            logger.debug("💓 Health scores skipped (pool busy)")
                            continue
                        return_pg_connection(conn)
                    except ImportError:
                        pass
                    self.health.calculate_health_scores()
                    logger.info("💓 Health scores updated")
                except Exception as e:
                    logger.error(f"Health loop error: {e}")

        threading.Thread(target=_health_loop, daemon=True).start()
        logger.info("📊 Background health thread started (learning disabled — trigger manually via API)")

    def _track_discovery(self, path: str):
        """Helper to track discovery file access."""
        ua = request.headers.get("User-Agent", "")
        ip = request.remote_addr or ""
        pid, _ = self.identifier.identify(
            ua, dict(request.headers), path, request.method
        )
        self.db.log_discovery_hit(path, pid, ua, ip, 200)

    def _platforms_using_file(self, filename: str) -> list:
        """Return which platforms typically access a given discovery file."""
        mapping = {
            "llms.txt": ["perplexity", "grok", "chatgpt", "gemini", "you_com", "phind"],
            "llms-full.txt": ["perplexity", "chatgpt", "grok"],
            "ai-plugin.json": ["chatgpt", "chatgpt_actions"],
            "mcp.json": ["claude_desktop", "cursor", "windsurf", "copilot"],
            "server-card.json": ["claude_desktop", "cursor", "windsurf", "smithery", "copilot", "claude_code"],
            "openapi.json": ["chatgpt_actions", "copilot"],
            "ai-agents.json": ["grok", "perplexity", "chatgpt"],
            "robots.txt": ["gemini", "chatgpt", "perplexity"],
        }
        return mapping.get(filename, [])

    def _generate_ai_agents_json(self) -> dict:
        """Generate a dynamic AI agents manifest with current capabilities."""
        stats = self.db.get_platform_stats()
        active_count = sum(
            1 for s in stats if s["total_requests"] > 0
        )

        return {
            "schema_version": "1.0",
            "name": "DC Hub Nexus",
            "description": (
                "Comprehensive data center intelligence platform — "
                "20,000+ facilities, 140+ countries, real-time M&A, "
                "capacity pipeline, energy infrastructure."
            ),
            "url": self.base_url,
            "gateway_version": GATEWAY_VERSION,
            "active_platform_connections": active_count,
            "protocols": {
                "mcp": {
                    "endpoint": f"{self.base_url}/mcp",
                    "transport": "streamable-http",
                    "version": MCP_PROTOCOL_VERSION,
                    "tools": [
                        "dchub_search_facilities",
                        "dchub_get_facility",
                        "dchub_list_transactions",
                        "dchub_get_market_intel",
                        "dchub_get_news",
                        "dchub_analyze_site",
                    ],
                },
                "rest": {
                    "base_url": f"{self.base_url}/api",
                    "spec": f"{self.base_url}/openapi.json",
                    "auth": "API key (optional for free tier)",
                },
                "llms_txt": {
                    "standard": f"{self.base_url}/llms.txt",
                    "full": f"{self.base_url}/llms-full.txt",
                },
            },
            "discovery_files": {
                name: f"{self.base_url}{info['path']}"
                for name, info in DISCOVERY_FILES.items()
            },
            "data_coverage": {
                "facilities": "20,000+",
                "countries": "140+",
                "capacity_tracked_mw": "19,500+",
                "news_sources": "40+",
                "update_frequency": "real-time",
            },
        }

# ============================================================================
# STANDALONE MODE — For testing without Flask app
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )

    print(f"\n🌐 DC Hub MCP Gateway v{GATEWAY_VERSION}")
    print(f"   Platforms registered: {len(PLATFORM_REGISTRY)}")
    print(f"   Discovery files: {len(DISCOVERY_FILES)}")
    print(f"   Database: {GATEWAY_DB}")

    db = GatewayDB()
    db.seed_platforms()

    print("\n📊 Platform Status:")
    for p in db.get_platform_stats():
        print(f"   {p['platform_name']:30s} | {p['protocol']:25s} | {p['status']}")

    print("\n🔍 Discovery File Map:")
    for name, info in DISCOVERY_FILES.items():
        print(f"   {name:25s} → {info['path']}")

    print("\n✅ Gateway ready. Import into main.py with:")
    print("   from mcp_gateway import MCPGateway")
    print("   gateway = MCPGateway(app)")
