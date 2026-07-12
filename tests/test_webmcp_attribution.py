"""WebMCP attribution classifier tests (2026-07-11, webmcp-lane).

Never imports main; never opens a DB. Covers the Task-1 attribution
contract in ai_tracking:
  1. is_webmcp_request — src=webmcp param OR X-DC-Source: webmcp header,
     case-insensitive, and NOTHING else
  2. path_marks_webmcp — worker-forwarded path strings (no headers there)
  3. additivity guard — detect_platform is untouched: browser UA is still
     'direct', named-agent UAs still map to their platforms (webmcp
     classification is marker-based, never a UA reclassification)
  4. the /ai dashboard metadata exists (AI_PLATFORMS['webmcp'])
  5. MCP_PLATFORM_MAP maps the 'webmcp' substring (read from main.py's
     source text — main must never be imported in unit tests)
"""
import os
import re

import flask

import ai_tracking as at

_app = flask.Flask(__name__)


def _req(path="/api/v1/search", headers=None):
    return _app.test_request_context(path, headers=headers or {})


# ── 1 · is_webmcp_request ─────────────────────────────────────────────

def test_marker_param():
    with _req("/api/v1/search?q=x&src=webmcp"):
        assert at.is_webmcp_request(flask.request) is True


def test_marker_header():
    with _req(headers={"X-DC-Source": "webmcp"}):
        assert at.is_webmcp_request(flask.request) is True


def test_marker_case_insensitive():
    with _req("/api/v1/search?src=WebMCP"):
        assert at.is_webmcp_request(flask.request) is True
    with _req(headers={"X-DC-Source": " WEBMCP "}):
        assert at.is_webmcp_request(flask.request) is True


def test_no_marker_no_match():
    with _req("/api/v1/search?q=webmcp"):        # q param is NOT the marker
        assert at.is_webmcp_request(flask.request) is False
    with _req(headers={"X-DC-Source": "map-ui"}):
        assert at.is_webmcp_request(flask.request) is False
    with _req():
        assert at.is_webmcp_request(flask.request) is False


# ── 2 · worker-forwarded path marker ──────────────────────────────────

def test_path_marker():
    assert at.path_marks_webmcp("/api/v1/deals?limit=2&src=webmcp") is True
    assert at.path_marks_webmcp("/api/v1/deals?limit=2") is False
    assert at.path_marks_webmcp("") is False
    assert at.path_marks_webmcp(None) is False


# ── 3 · additivity: UA detection is untouched ─────────────────────────

def test_browser_ua_still_direct():
    chrome = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
    assert at.detect_platform(chrome) == "direct"


def test_named_agents_unchanged():
    assert at.detect_platform("ChatGPT-User/1.0") == "chatgpt"
    assert at.detect_platform("Meta-ExternalAgent/1.1") == "meta"
    assert at.detect_platform("PerplexityBot/1.0") == "perplexity"


def test_webmcp_constant():
    assert at.WEBMCP_PLATFORM == "webmcp"


# ── 4 · /ai dashboard metadata ────────────────────────────────────────

def test_ai_platforms_entry():
    entry = at.AI_PLATFORMS.get("webmcp")
    assert entry is not None
    assert entry["name"] == "WebMCP"
    assert entry["color"].startswith("#")
    assert entry["company"]


# ── 5 · MCP_PLATFORM_MAP (source-text check; never import main) ───────

def test_mcp_platform_map_has_webmcp():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"MCP_PLATFORM_MAP\s*=\s*\{(.*?)\n\}", src, re.S)
    assert m, "MCP_PLATFORM_MAP literal not found in main.py"
    assert "'webmcp': 'WebMCP'" in m.group(1)
