"""
mcp_connect.py — per-MCP-client landing pages.

Phase r80-mcp-connect (2026-06-06). The funnel deep-dive showed that the
conversion path that actually closes requires the MCP client to hold an
X-API-Key persistently. Claude.ai web (anon-only) can't. But Cursor, Cline,
Continue, and Claude Desktop ALL can — they store the key in their MCP
config file. Yet there was no per-client landing page that walked an
operator through (a) mint trial key, (b) paste install snippet, (c) try
prompt examples, (d) upgrade — in one place.

This blueprint ships four pages:

    GET  /connect/cursor
    GET  /connect/cline
    GET  /connect/continue
    GET  /connect/claude-desktop

Each is:
    1. Header + value prop ("DC Hub for <Client> — <N> tools, free tier, 30s",
       where <N> is the canon tool count resolved into the template at import.
       Written as <N> and not as the literal placeholder token on purpose:
       tests/test_canon_placeholders_resolved.py requires every placeholder-
       bearing string to sit inside a resolver call, and a module docstring
       never reaches one.)
    2. A "Mint your free trial key" button (POSTs /api/v1/keys/claim?platform=X,
       in-place swaps to the install snippet with the key embedded)
    3. A pre-rendered, copy-button install snippet tuned per client
    4. Three "Try it" example prompts tuned to the client
    5. Trial limits + Pro Monthly + Pro Annual upgrade CTAs (Stripe links
       carry client_reference_id={trial_key} for Fix-E-style attribution)

Telemetry: every page view INSERTs a row into connect_landing_views. The
mint button updates that row with the minted key for funnel attribution.
"""

from __future__ import annotations

import os
import json
import logging
from flask import Blueprint, request, jsonify, make_response, redirect
from ai_surface_canon import canon_text

logger = logging.getLogger(__name__)

mcp_connect_bp = Blueprint("mcp_connect", __name__)


# ── Per-client config ────────────────────────────────────────────────────
# Each client gets: pretty name, install file path on each OS, snippet
# format, snippet content (a JSON or YAML body), and a deep-link if one
# exists. The TRIAL_KEY placeholder is replaced client-side after the
# mint button POST returns.

_CLIENTS = {
    "cursor": {
        "name":           "Cursor",
        "tagline":        "The AI code editor — one-click install, DC Hub MCP data in 30s",
        # r-connect-oneclick (2026-07-04, verified cursor.com/docs): the config
        # file is ~/.cursor/mcp.json (global) or <project>/.cursor/mcp.json —
        # NOT the old globalStorage path.
        "install_path":   "~/.cursor/mcp.json",
        "install_path_win": "%USERPROFILE%\\.cursor\\mcp.json",
        "snippet_lang":   "json",
        # Cursor reads mcp.json with mcpServers map. Setting headers.X-API-Key
        # lets DC Hub recognise the operator as IDENTIFIED (50 calls/day,
        # 7 day trial). The URL path is the canonical MCP endpoint.
        "snippet":        """{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}""",
        # r-connect-oneclick: Cursor's native install deeplink (official,
        # cursor.com/docs/context/mcp/install-links). config = base64 of the
        # INNER server object {"url":"https://dchub.cloud/mcp"} (no-auth free
        # tier). The page JS rebuilds this with the minted key after mint for
        # the 50/day tier. Clicking opens Cursor → confirm → installed.
        "deep_link":      "cursor://anysphere.cursor-deeplink/mcp/install?name=dchub&config=eyJ1cmwiOiJodHRwczovL2RjaHViLmNsb3VkL21jcCJ9",
        "deep_link_label": "⚡ Add to Cursor — one click",
        "examples": [
            "What's the gas pipeline capacity within 50 miles of Northern Virginia data centers?",
            "Rank the top 10 markets by DCPI for build readiness in Q4 2026.",
            "Find me 50MW+ powered shell sites available in PJM with substation co-location.",
        ],
    },
    # ── r-connect-deepseek (2026-09-18) ──────────────────────────────────
    # ★ "Harness" here is DeepSeek's OWN agent harness (deepseek-ai/
    #   deepseek-harness), NOT harness.io the CI vendor. Its MCP client ships
    #   as the plugin `@deepseek-ai/dsh-mcp-client`; the config shape below was
    #   read off packages/mcp/mcp-client/README.md on master, not off a
    #   third-party write-up — two of those publish `!js` where the README
    #   says `!!js`, and a literal key needs neither tag.
    # ★ The caveat IS the card, not a disclaimer bolted onto it. DeepSeek's
    #   CONSUMER chat has no MCP UI at all, so a card headed plain "DeepSeek"
    #   would assert a capability the product does not have — the MiniMax
    #   mistake recorded in the alias block below, inverted. Harness, Cursor
    #   and the tool-calling API behind a bridge are where a DeepSeek user can
    #   actually hold a key, so those are what this page hands over.
    "deepseek-harness": {
        "name":           "DeepSeek Harness",
        "tagline":        "DeepSeek's own agent harness — one YAML entry, streamable HTTP, your key held per server",
        # Harness reads a profile YAML; there is no single fixed path the way
        # an IDE has one, so this names the file by role rather than inventing
        # a location that would be wrong on most installs.
        "install_path":   "your harness profile YAML (the plugins list)",
        "install_path_win": "your harness profile YAML (the plugins list)",
        "snippet_lang":   "yaml",
        "snippet":        """- id: mcp-dchub
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: dchub
    transport: streamable-http
    url: https://dchub.cloud/mcp
    headers:
      X-API-Key: "{{TRIAL_KEY}}"
""",
        "caveat": (
            "DeepSeek&rsquo;s consumer chat (chat.deepseek.com) has "
            "<strong>no MCP UI</strong> &mdash; there is nowhere to paste this, and no "
            "card here will change that. Three places a DeepSeek user can hold a DC Hub "
            "key today: <strong>this harness</strong> (below), "
            "<a href=\"/connect/cursor\">Cursor</a> pointed at a DeepSeek model, or the "
            "<strong>DeepSeek tool-calling API</strong> with DC Hub behind an MCP bridge "
            "&mdash; the API speaks OpenAI-style function calling, not MCP, so something "
            "has to translate. Same endpoint, same key, in all three."
        ),
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Which US grid region has the most interconnection headroom right now?",
            "Rank the top 10 data center markets by DCPI and say what each one is short of.",
            "Find 100MW+ sites in ERCOT with substation co-location and fiber within a mile.",
        ],
    },
    "cline": {
        "name":           "Cline",
        "tagline":        "VSCode autonomous agent — bind DC Hub MCP in your settings",
        # r-connect-oneclick (2026-07-04): the real path is under VS Code's
        # globalStorage for the saoudrizwan.claude-dev extension — reach it via
        # Cline panel → MCP Servers → Configure. The old ~/.cline path was wrong.
        "install_path":   "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
        "install_path_win": "%APPDATA%\\Code\\User\\globalStorage\\saoudrizwan.claude-dev\\settings\\cline_mcp_settings.json",
        "snippet_lang":   "json",
        # r-connect-oneclick: type MUST be "streamableHttp" (camelCase, no
        # hyphen). WITHOUT it Cline falls back to legacy SSE and the connection
        # 405s — the old snippet omitted it and silently failed. Easiest UX:
        # Cline panel → MCP Servers → "Remote Servers" tab → paste name + URL.
        "snippet":        """{
  "mcpServers": {
    "dchub": {
      "type": "streamableHttp",
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}""",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Compare interconnect queue depth for ERCOT West vs PJM AEP this quarter.",
            "Pull DC Hub's latest hyperscaler deal autopsy for AWS in Ohio.",
            "What 100MW+ sites in MISO have substation capacity + fiber within 1 mile?",
        ],
    },
    "continue": {
        "name":           "Continue",
        "tagline":        "Open-source AI dev assistant — add DC Hub MCP to config.yaml",
        "install_path":   "~/.continue/config.yaml",
        "install_path_win": "%USERPROFILE%\\.continue\\config.yaml",
        "snippet_lang":   "yaml",
        # r-connect-oneclick (2026-07-04, verified docs.continue.dev): the MCP
        # block is a `mcpServers:` LIST with type: streamable-http and headers
        # under requestOptions — NOT the old `models:`/`provider: mcp` shape,
        # which no longer parses. Lives in ~/.continue/config.yaml.
        "snippet":        """mcpServers:
  - name: DC Hub
    type: streamable-http
    url: https://dchub.cloud/mcp
    requestOptions:
      headers:
        X-API-Key: "{{TRIAL_KEY}}\"""",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Run get_grid_intelligence on CAISO and summarize spare headroom.",
            "Score this 200MW Northern Virginia site for DCPI BUILD readiness.",
            "List the top 5 emerging DCPI markets outside the top 20 metros.",
        ],
    },
    "claude-desktop": {
        "name":           "Claude Desktop",
        "tagline":        "Anthropic's desktop app — wire DC Hub MCP into claude_desktop_config.json",
        "install_path":   "~/Library/Application Support/Claude/claude_desktop_config.json",
        "install_path_win": "%APPDATA%\\Claude\\claude_desktop_config.json",
        "snippet_lang":   "json",
        # r-connect-oneclick (2026-07-04, verified support.claude.com + mcpb
        # docs): Claude Desktop's claude_desktop_config.json validates ONLY
        # stdio `command` servers — a bare "url" field makes it silently strip
        # the whole mcpServers block on save (the old snippet was broken). To
        # reach a REMOTE streamable-http server AND pass X-API-Key you must
        # wrap the mcp-remote npx bridge. Needs Node/npx installed. Restart
        # Claude Desktop after editing. (The Settings→Connectors "custom
        # connector" UI is OAuth-only — it can't send X-API-Key.)
        "snippet":        """{
  "mcpServers": {
    "dchub": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "https://dchub.cloud/mcp",
        "--transport", "http-only",
        "--header", "X-API-Key:{{TRIAL_KEY}}"
      ]
    }
  }
}""",
        # No claude:// install protocol. The one-click path is the .mcpb
        # desktop-extension bundle (double-click to install) — tracked
        # separately; the corrected config above is the reliable path today.
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "What's the latest grid intelligence for PJM — load + generation mix?",
            "Find me 50MW+ powered land sites in Texas with sub-12-month TTP.",
            "Rank the top 10 international markets by DCPI for Q4 build decisions.",
        ],
    },
    # 2026-06-11: ChatGPT now supports custom MCP connectors (Developer Mode /
    # Pro·Business·Enterprise). /connect/chatgpt used to 404 — ChatGPT is the #2
    # AI reader of DC Hub (28K), so this is the most direct "get others to
    # tool-call" surface.
    "chatgpt": {
        "name":           "ChatGPT",
        "tagline":        "Add DC Hub as a custom MCP connector",
        "install_path":   "ChatGPT → Settings → Connectors → Add custom connector",
        "install_path_win": "ChatGPT → Settings → Connectors → Add custom connector",
        "snippet_lang":   "text",
        # r-connect-return (2026-09-14): the key rides IN the connector URL.
        # ChatGPT runs MCP server-side and its connector form has no header
        # field, so "Header: X-API-Key" was an instruction nobody on this page
        # could follow, and a key that never reaches us cannot be the "same
        # key" a returning user comes back with. POST /mcp reads ?apiKey= and
        # validates it like the header; it is the connect_url shape
        # claim_free_key already hands header-less MCP clients (dchub-mcp-server
        # _connectUrl; chatgpt is in _BYO_MCP_PLATFORMS).
        "snippet":        """Connector URL:  https://dchub.cloud/mcp?apiKey={{TRIAL_KEY}}

ChatGPT has no header field, so the key rides in the URL.
In ChatGPT: Settings -> Connectors -> Add custom connector -> paste the URL.""",
        # One line under the install step (see _return_nudge_html).
        # ★ r-connect-bind (2026-09-15): "same key" holds past the key's free
        # calls only once an email is bound. /api/v1/keys/validate refuses an
        # unbound trial key after TRIAL_FREE_CALLS_UNBOUND calls, and a refused
        # key is served anonymously, so the nudge states the condition.
        "return_nudge":   "Come back tomorrow: same key, new chat. The key lives in the connector URL, so every new ChatGPT chat already has DC Hub. Once an email is bound to it, the key keeps working past its free calls.",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Rank the top 10 markets by DCPI for 2026 build readiness.",
            "What's the interconnection-queue depth in PJM vs ERCOT right now?",
            "Show me the latest hyperscaler data-center M&A deals this quarter.",
        ],
    },
    "gemini": {
        "name":           "Gemini",
        "tagline":        "Call DC Hub from Gemini via MCP / function calling",
        "install_path":   "Vertex AI / Gemini — point tool-use at the MCP endpoint",
        "install_path_win": "Vertex AI / Gemini — point tool-use at the MCP endpoint",
        "snippet_lang":   "text",
        "snippet":        """MCP endpoint:  https://dchub.cloud/mcp
Gemini function declarations:  https://dchub.cloud/api/v1/gemini-functions.json
OpenAPI (Vertex):  https://dchub.cloud/openapi-vertex.yaml
Header (optional):  X-API-Key: {{TRIAL_KEY}}""",
        # ★ r-connect-quota-copy: the header line said the key "unlocks" the
        # email-bound daily quota, which an unbound key never gets. The page
        # appends step 4's trial terms under it instead, read from
        # routes.auto_trial on every render (see _snippet).
        "snippet_trial_terms": True,
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Summarize live grid headroom across the top 5 AI data-center markets.",
            "Which US markets have the most available powered-shell capacity?",
            "Compare DCPI build-readiness for Northern Virginia vs Columbus.",
        ],
    },
    # r-connect-oneclick (2026-07-04): opencode is DC Hub's #1 real MCP
    # tool-caller (top of /api/v1/reach) but had NO connect page. Config
    # verified against opencode.ai/docs/mcp-servers — `mcp` map, type
    # "remote", url + headers. CLI shortcut: `opencode mcp add`.
    "opencode": {
        "name":           "opencode",
        "tagline":        "The terminal AI coding agent — DC Hub's #1 tool-caller. Add in one line.",
        "install_path":   "~/.config/opencode/opencode.json",
        "install_path_win": "%APPDATA%\\opencode\\opencode.json",
        "snippet_lang":   "json",
        "snippet":        """{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "dchub": {
      "type": "remote",
      "url": "https://dchub.cloud/mcp",
      "enabled": true,
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}""",
        # No deep-link scheme; the CLI wizard `opencode mcp add` is the
        # fastest path (prompts remote → URL → header). Config above is the
        # equivalent file. Drop the "headers" block for the no-key free tier.
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "get_grid_scoreboard — which grid has the most renewable headroom right now?",
            "Rank the top 10 DCPI markets for a 100MW build with short time-to-power.",
            "Find 50MW+ powered-shell sites in PJM with substation + fiber within 1 mile.",
        ],
    },
    # r-connect-oneclick (2026-07-04): VS Code native MCP (verified
    # code.visualstudio.com). Real one-click deep-link. type is "http"
    # (VS Code auto-negotiates streamable-http). Distinct from Cline/Continue.
    "vscode": {
        "name":           "VS Code",
        "tagline":        "Native MCP support — one-click install, no extension needed",
        "install_path":   "~/Library/Application Support/Code/User/mcp.json  (or Command Palette → “MCP: Open User Configuration”)",
        "install_path_win": "%APPDATA%\\Code\\User\\mcp.json",
        "snippet_lang":   "json",
        # VS Code native MCP: top-level key is "servers" (NOT mcpServers) and
        # type is "http". Command Palette → "MCP: Add Server" is the guided
        # no-file path.
        "snippet":        """{
  "servers": {
    "dchub": {
      "type": "http",
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}""",
        # Official deep-link: vscode:mcp/install?<url-encoded-JSON with name
        # inside>. No-auth free tier (JS rebuilds with the minted key).
        "deep_link":      "vscode:mcp/install?%7B%22name%22%3A%22dchub%22%2C%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A%2F%2Fdchub.cloud%2Fmcp%22%7D",
        "deep_link_label": "⚡ Install in VS Code — one click",
        "examples": [
            "get_grid_scoreboard — rank US + EU grids by renewable share right now.",
            "What's the interconnection-queue depth in ERCOT vs PJM today?",
            "Score a 200MW Northern Virginia parcel for DCPI BUILD readiness.",
        ],
    },
    # 2026-07-18 (kimi-interconnect): Moonshot's Kimi Code CLI speaks MCP
    # natively — config verified against moonshotai.github.io/kimi-cli
    # (customization/mcp + reference/kimi-mcp): store is ~/.kimi/mcp.json in
    # the standard mcpServers shape; `kimi mcp add --transport http` is the
    # one-liner (header format "KEY: VALUE", space after colon); /mcp-config
    # and /mcp manage it from inside the TUI.
    "kimi": {
        "name":           "Kimi",
        "tagline":        "Moonshot's Kimi Code CLI — add DC Hub MCP in one line",
        "install_path":   "~/.kimi/mcp.json",
        "install_path_win": "%USERPROFILE%\\.kimi\\mcp.json",
        "snippet_lang":   "text",
        "snippet":        """# One line, from your shell:
kimi mcp add --transport http dchub https://dchub.cloud/mcp --header "X-API-Key: {{TRIAL_KEY}}"

# Or edit ~/.kimi/mcp.json (standard mcpServers map):
{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}

# Inside the Kimi TUI: /mcp-config adds it interactively, /mcp shows status.""",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "get_grid_scoreboard — which grid has the most renewable headroom right now?",
            "Rank the top 10 markets by DCPI for a 100MW build with short time-to-power.",
            "Compare interconnection-queue depth for ERCOT vs PJM this quarter.",
        ],
    },
    # ── r-connect-qwen (2026-09-09) ──────────────────────────────────────
    # A CARD, not an alias, because the alias doctrine below has a
    # precondition this fails: an alias points at instructions we ALREADY
    # publish. Nothing on this site documents Qwen Code — grep for it and the
    # only hits are outreach shells listing it as a platform that "awaits first
    # request". There is no door to point at, so this builds one.
    #
    # ★ `httpUrl`, NOT `url`. Qwen Code reserves plain `url` for SSE and uses
    #   `httpUrl` for streamable HTTP; pasting `url` here yields a server that
    #   registers and never answers. Verified against Qwen's own MCP docs
    #   2026-09-09, not inferred from the shape other clients use — this is the
    #   one field where the usual mcpServers muscle-memory is wrong.
    "qwen": {
        "name":           "Qwen Code",
        "tagline":        "Alibaba's Qwen Code CLI — add DC Hub MCP in one line",
        "install_path":   "~/.qwen/settings.json",
        "install_path_win": "%USERPROFILE%\\.qwen\\settings.json",
        "snippet_lang":   "text",
        "snippet":        """# One line, from your shell:
qwen mcp add --transport http dchub https://dchub.cloud/mcp --header "X-API-Key: {{TRIAL_KEY}}"

# Or edit ~/.qwen/settings.json directly.
# NOTE the key: Qwen uses "httpUrl" for streamable HTTP. Plain "url" is the
# SSE field — it will register but never answer.
{
  "mcpServers": {
    "dchub": {
      "httpUrl": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" },
      "timeout": 30000
    }
  }
}

# Repo-scoped instead of global? Same block in .qwen/settings.json at the
# project root. Inside a Qwen Code session, /mcp shows connection status.""",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Which US markets have the most interconnection headroom for a 200MW AI campus?",
            "get_grid_scoreboard — compare ERCOT and PJM renewable headroom right now.",
            "What is the fiber lead-in situation for a site at 32.78, -96.80?",
        ],
    },
    # ── r-connect-zai (2026-09-09) ───────────────────────────────────────
    # ★ THIS CARD DELIBERATELY DOES NOT PRINT A REMOTE-SERVER JSON BLOCK.
    #   Z.ai's ZCode documents the config LOCATION and the nested shape
    #   (`mcp.servers`, NOT a top-level `mcpServers` map — the one thing most
    #   people get wrong by analogy), and it documents adding a remote service
    #   through the UI form. It does NOT document the JSON key names for a
    #   remote HTTP entry. Guessing them ("url"? "httpUrl"? "type":
    #   "streamableHttp"?) would publish a snippet that silently fails, which
    #   is worse than the 404 this replaces — and the Qwen note above is the
    #   proof that these keys genuinely differ per client.
    #   So: the documented UI path, and the parts of the file format that ARE
    #   documented. If someone confirms the remote key, this becomes a snippet.
    "zai": {
        "name":           "Z.ai (ZCode)",
        "tagline":        "Zhipu's ZCode CLI — add DC Hub as a remote MCP service",
        "install_path":   "~/.zcode/cli/config.json",
        "install_path_win": "%USERPROFILE%\\.zcode\\cli\\config.json",
        "snippet_lang":   "text",
        "snippet":        """In ZCode: open the MCP panel -> Add -> choose HTTP
  Service URL:  https://dchub.cloud/mcp
  Expand "Headers (optional)" and add:
      X-API-Key: {{TRIAL_KEY}}

Config file, if you prefer to look: ~/.zcode/cli/config.json
Workspace scope instead: <project root>/.zcode/config.json

★ ZCode nests MCP under "mcp": { "servers": { ... } } — it is NOT the
  top-level "mcpServers" map that Claude/Cursor/Qwen use. We publish the UI
  path above rather than a hand-written remote block because Z.ai's own docs
  specify the form, not the JSON key names for a remote HTTP entry, and a
  guessed key connects to nothing.""",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Rank the top 10 global markets by DCPI for 2026 build readiness.",
            "Which grid regions have the shortest time-to-power for 100MW?",
            "Show the latest hyperscaler data-center M&A deals this quarter.",
        ],
    },
    # ── r-connect-zed (2026-09-10) ───────────────────────────────────────
    # ★ ZED GETS A REAL JSON BLOCK, and the Z.ai note above is why that is a
    #   decision rather than a default. The bar is the VENDOR publishing the
    #   remote key names. Zed does, verbatim, in its own repo
    #   (docs/src/ai/mcp.md): a remote entry is `"url"` plus a `"headers"`
    #   map, beside the local `"command"`/`"args"` form. Nothing is inferred
    #   here — the only value we supply is our own header name.
    #
    # ★ THE REGRESSION THIS CARD PINS: the top-level key is "context_servers",
    #   NOT "mcpServers". Zed shipped the feature as "context servers" before
    #   "MCP" settled as the word and kept the settings key. The
    #   Claude/Cursor/Kimi muscle memory writes "mcpServers", which Zed does
    #   not read AT ALL — no error, the server simply never appears in the
    #   agent panel. Same failure family as the Qwen `httpUrl` note above,
    #   and the reason these cards are not copy-paste jobs.
    #
    # ★ X-API-Key, not the Authorization: Bearer that Zed's own example shows.
    #   Zed's `headers` is a free-form map, and this origin reads the key from
    #   X-API-Key only (flask_mcp_endpoints.py, _resolve of the tools/call
    #   path) — it has no Bearer branch for a tool key. Publishing Zed's
    #   example header verbatim would authenticate against nothing.
    "zed": {
        "name":           "Zed",
        "tagline":        "Zed's agent panel — add DC Hub as a remote MCP server",
        "install_path":   "~/.config/zed/settings.json",
        "install_path_win": "%APPDATA%\\Zed\\settings.json",
        "snippet_lang":   "text",
        "snippet":        """In Zed: Settings -> AI -> MCP Servers -> Add Server -> Add Remote Server

Or edit the settings file directly (command palette: `zed: open settings file`):

{
  "context_servers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "{{TRIAL_KEY}}" }
    }
  }
}

★ The key is "context_servers", NOT "mcpServers". Zed named the feature
  before "MCP" settled and kept the settings key — a "mcpServers" block is
  not read, and the server never shows up in the agent panel.

macOS + Linux: ~/.config/zed/settings.json
Windows:       %APPDATA%\\Zed\\settings.json""",
        "deep_link":      "",
        "deep_link_label": "",
        "examples": [
            "Which US markets have the most interconnection headroom for a 200MW AI campus?",
            "get_power_availability_timeline — how long to 100MW in Northern Virginia?",
            "Compare ERCOT and PJM on time-to-power and grid headroom right now.",
        ],
    },
}


# Pro Monthly + Pro Annual Stripe Payment Links (canonical, from
# routes/_stripe_links.py). client_reference_id={trial_key} attributes
# the conversion back to the page that minted the key (Fix E pattern).
from routes._stripe_links import STRIPE_LINKS as _CANON_LINKS
# ★2026-09-10: this comment used to read "$299/mo (canon; ...)" beside a link
# that has charged $99 since r-price-collapse (2026-09-05). The comment was not
# describing the link, it was quoting the drift — and the page below rendered
# the same retired 299 as its VISIBLE price, so /connect/{chatgpt,gemini,...}
# advertised $299 while the button beside it charged $99. Do not re-type a price
# here; _pro_price_usd() derives it from tier_registry, which is the SSOT
# main._canonical_pricing() already reads.
_STRIPE_MONTHLY = _CANON_LINKS["pro"]         # canonical Pro link; price via _pro_price_usd()
_STRIPE_ANNUAL  = _CANON_LINKS["pro_annual"]  # $1,188/yr one-time (see _annual_save_html)


def _dev_price_usd() -> int:
    """Canonical Developer monthly price, DERIVED like _pro_price_usd."""
    try:
        from tier_registry import price as _price
        return int(_price("developer") or 0)
    except Exception:
        return 0


def _pack_price_and_credits() -> tuple:
    """($ price, "1,000") of the one-time pack, read from the module that owns it
    (routes.mcp_conversion_plays). (0, "") when unavailable."""
    try:
        from routes.mcp_conversion_plays import PACK10_PRICE_CENTS, PACK10_CREDITS
        return int(PACK10_PRICE_CENTS) // 100, format(int(PACK10_CREDITS), ",")
    except Exception:
        return 0, ""


def _pro_price_usd() -> int:
    """Canonical Pro monthly price, DERIVED. 0 when unavailable (fail-open to a
    price-free tile rather than a wrong number — same asymmetry canon_text uses).
    """
    try:
        from tier_registry import price as _price
        return int(_price("pro") or 0)
    except Exception:
        return 0


# The annual Payment Link is a fixed $1,188/yr one-time. There is no annual SSOT
# in tier_registry (it prices MONTHLY tiers only), so the yearly figure is bound
# to the link it opens and stated here once.
_ANNUAL_PRICE_USD = 1188


def _annual_save_html() -> str:
    """The 'N% off' badge, COMPUTED against the live monthly price.

    ★ Why this is not a literal: the badge said "50% off" — true when Pro was
    $199/mo, and false the moment r-price-collapse made Pro $99. At $99/mo the
    annual link is 12 x 99 = $1,188, i.e. EXACTLY the monthly cost and a 0%
    discount, so the page was making a discount claim its own two tiles
    disproved. Renders nothing when there is no saving.
    """
    monthly = _pro_price_usd()
    if not monthly:
        return ""
    full_year = monthly * 12
    if _ANNUAL_PRICE_USD >= full_year:
        return ""
    pct = int(round((full_year - _ANNUAL_PRICE_USD) * 100.0 / full_year))
    return f' <span class="save">{pct}% off</span>' if pct >= 1 else ""


# P0-D (frontend#1535, 2026-09-21): install pages no longer tile Pro, annual or
# monthly: they sell the pack and Developer, the plans agents buy. The annual
# figures above stay as the Stripe link's own price for the restore path that
# tier_registry.annual_save_display documents.


# ── Telemetry: best-effort DB write ──────────────────────────────────────
def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _record_view(client_key: str) -> int | None:
    """Insert a row in connect_landing_views, return its id (best-effort).

    main.get_db() hands out a pooled psycopg2 connection that is NOT
    autocommit — we must commit() before close() or the row is rolled
    back on connection return. We close the connection ourselves at the
    end so it returns to the pool cleanly.
    """
    db = _get_db()
    if db is None:
        return None
    new_id = None
    try:
        ua = (request.headers.get("User-Agent") or "")[:300]
        ref = (request.headers.get("Referer") or "")[:500]
        # r-page-onramp (2026-07-04): the crawl->tool crossover pack links
        # every facility/market/DCPI page here with ?src=page-onramp&entity=
        # <slug>. The recorder only stored the Referer header — absent for
        # most agent fetches — so fold the marker query-string into the same
        # TEXT column (zero DDL). Funnel queries can then filter
        # referer LIKE '%src=page-onramp%' and parse entity= for the page.
        # ★2026-09-15: the pages link the canonical /connect now (see the
        # r-page-onramp note in routes/seo_pages.py); this fold still records
        # any fetch that arrives carrying a src= marker.
        try:
            _qs = (request.query_string or b"").decode("utf-8", "ignore")
            if "src=" in _qs:
                ref = (ref + (" | " if ref else "") + "qs:" + _qs)[:500]
        except Exception:
            pass
        ip = (request.headers.get("CF-Connecting-IP")
              or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "")[:64]
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO connect_landing_views
                     (client, viewed_at, user_agent, referer, ip)
                   VALUES (%s, NOW() ON CONFLICT DO NOTHING, %s, %s, %s)
                   RETURNING id""",
                (client_key, ua, ref, ip),
            )
            row = cur.fetchone()
            new_id = int(row[0]) if row else None
        db.commit()
    except Exception as e:
        logger.warning("connect_landing_views insert failed: %s", e)
        try: db.rollback()
        except Exception: pass
    finally:
        try: db.close()
        except Exception: pass
    return new_id


# r-page-onramp (2026-07-04): bare /connect is a STATIC file served from
# main.py (send_from_directory('static','connect.html')) with NO telemetry —
# only the /connect/<client> pages call _record_view. The crossover-pack
# onramp lines pointed at /connect?src=page-onramp&entity=<slug>, so without
# this hook the measurement marker would never reach connect_landing_views.
# ★2026-09-15: those lines link the canonical /connect now, so this hook only
# sees the old URL fetched from cached pages, copies elsewhere, or crawlers
# re-walking what they already found.
# before_app_request fires even though the /connect route lives in main.py
# (Flask runs before_request hooks prior to dispatch). Cost: one string
# compare per request. Never raises.
@mcp_connect_bp.before_app_request
def _record_page_onramp_view():
    try:
        if request.path.rstrip("/") != "/connect":
            return None
        if request.args.get("src") != "page-onramp":
            return None
        _record_view("page-onramp")
    except Exception:
        pass
    return None


# ── Page render ─────────────────────────────────────────────────────────
# ★2026-09-10 — THE STALE-FLOOR BUG, and why this is no longer canon_text()'d
# here. This was `_PAGE_TEMPLATE = canon_text("""...""")`, which resolves every
# {canon_*} ONCE, at MODULE IMPORT. canon_nums() reads canonical_stats' cache;
# during app boot that cache is cold, so the placeholders froze at the PINNED
# cold-start floor and stayed frozen for the life of the process.
#
# Measured 2026-09-10, same process, same second: /api/v1/canon/phrases served
# the LIVE facility floor (source=resolve_public_floors, cold=false) while
# /connect/chatgpt and /connect/gemini both served the PINNED cold-start floor,
# seven hundred behind it. The resolver was working. The install pages had
# simply stopped asking it.
#
# ★ The digits of that divergence are deliberately NOT written here.
# tests/test_canonical_counts_drift.py scans this file for facility-floor
# literals and judges them against PINNED, so a comment quoting the live value
# fails CI — and a comment quoting the stale value is how a fix note turns into
# the next surface that lies. The dated numbers live in the guard's docstring,
# which is a point-in-time record; this file states the SHAPE.
#
# So the template keeps its placeholders and _render_page() resolves them per
# request — the same repair routes/agent_concierge.py made on 2026-08-25 when
# /agent alone served 18,500+ against a live 18,800+. This is cheap: canon_nums
# derives from canonical_stats' PEEK-ONLY cache and never triggers a query, and
# the page is edge-cached 300s besides.
#
# ★ ORDER MATTERS: canon_text() must run BEFORE .format(). The template is full
# of literal CSS/JS braces escaped as {{ }} for .format(); a {canon_*} left in
# place when .format() runs raises KeyError. Resolve canon first, format second.
#
# ★ routes/mcp_connect.py is deliberately NOT in test_canon_placeholders_resolved
# ._SWEPT any more — that lexical AST scan cannot tie a module-level raw constant
# to a canon_text() call in another function, exactly as documented there for
# agent_concierge. The stronger replacement is
# tests/test_connect_install_pages_derive_canon.py, which RENDERS the page and
# asserts no placeholder survives and the resolver beat the pin.
_PAGE_TEMPLATE_RAW = ("""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub for {NAME} — {canon_tools} MCP tools, free tier, 30s to install</title>
<meta name="description" content="Install DC Hub's MCP server in {NAME} in 30 seconds. {canon_tools} tools across {canon_facilities} distinct data centers, {canon_markets} power markets, live ISO grids, {canon_deals} tracked deals. Free trial — no credit card.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/connect/{KEY}">
<meta property="og:title" content="DC Hub MCP for {NAME}">
<meta property="og:description" content="{canon_tools} tools, 30 seconds to install, free trial — for AI agents that need real data center, grid, and infrastructure intelligence.">
<meta property="og:image" content="https://api.dchub.cloud/static/og/landing-architecture.png">
<style>
 :root{{--bg:#0a0a0f;--card:#15151c;--border:#2a2a35;--text:#e8e8f0;--muted:#9a9aa6;--accent:#7c5cff;--accent2:#22d3ee;--ok:#10b981;--warn:#f59e0b}}
 *{{box-sizing:border-box}}
 body{{background:var(--bg);color:var(--text);font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55;margin:0;padding:32px 20px}}
 .container{{max-width:880px;margin:0 auto}}
 .topbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:32px;font-size:.85rem}}
 .topbar a{{color:var(--muted);text-decoration:none}}
 .topbar a:hover{{color:var(--text)}}
 .eyebrow{{color:var(--accent);font-size:.78rem;letter-spacing:.18em;text-transform:uppercase;font-weight:600;margin-bottom:8px}}
 h1{{font-size:2.4rem;margin:.1em 0;letter-spacing:-.02em;line-height:1.15}}
 .tagline{{color:var(--muted);font-size:1.12rem;max-width:680px;margin:8px 0 28px}}
 .badges{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:28px}}
 .badge{{background:var(--card);border:1px solid var(--border);padding:6px 12px;border-radius:999px;font-size:.78rem;color:var(--muted)}}
 .badge b{{color:var(--text);font-weight:600}}
 .card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;margin:18px 0}}
 .step{{display:flex;align-items:center;gap:10px;font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.12em;font-weight:600;margin-bottom:14px}}
 .step-num{{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;background:var(--accent);color:#fff;font-size:.72rem;font-weight:700;letter-spacing:0}}
 .mint-btn{{display:inline-flex;align-items:center;gap:10px;background:var(--accent);color:#fff;border:none;padding:14px 24px;border-radius:10px;font-size:1.04rem;font-weight:600;cursor:pointer;transition:transform .1s,box-shadow .1s}}
 .mint-btn:hover{{transform:translateY(-1px);box-shadow:0 6px 20px rgba(124,92,255,.35)}}
 .mint-btn[disabled]{{opacity:.7;cursor:not-allowed;transform:none}}
 .key-box{{display:none;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:14px;margin-top:14px;font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:.88rem;color:var(--accent2);word-break:break-all}}
 .key-box.shown{{display:block}}
 .key-meta{{display:flex;gap:14px;margin-top:8px;font-size:.78rem;color:var(--muted)}}
 .snippet-wrap{{position:relative}}
 pre{{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:18px;overflow-x:auto;font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:.86rem;line-height:1.55;color:#e8e8f0;margin:0}}
 pre .comment{{color:#6e7681}}
 .copy-btn{{position:absolute;top:10px;right:10px;background:var(--border);color:var(--text);border:none;padding:6px 12px;border-radius:6px;font-size:.78rem;cursor:pointer}}
 .copy-btn:hover{{background:var(--accent);color:#fff}}
 .copy-btn.copied{{background:var(--ok);color:#fff}}
 .install-meta{{font-size:.85rem;color:var(--muted);margin-bottom:12px}}
 .install-meta code{{background:var(--border);color:var(--accent2);padding:1px 6px;border-radius:3px;font-family:ui-monospace,monospace;font-size:.88em}}
 .examples{{display:grid;grid-template-columns:1fr;gap:10px;margin-top:12px}}
 .example{{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-size:.94rem;color:#cdd0db;font-style:italic}}
 .example::before{{content:"> ";color:var(--accent2);font-style:normal;font-weight:600}}
 .upgrade-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}}
 @media (max-width:560px){{.upgrade-grid{{grid-template-columns:1fr}}}}
 .upgrade-grid.solo{{grid-template-columns:1fr}}
 .upgrade-tile{{background:#0d1117;border:1px solid var(--border);border-radius:10px;padding:18px;text-align:left;text-decoration:none;color:var(--text);transition:border-color .15s,transform .1s}}
 .upgrade-tile:hover{{border-color:var(--accent);transform:translateY(-1px)}}
 .upgrade-tile h3{{margin:0 0 6px;font-size:1.08rem}}
 .upgrade-tile .price{{font-size:1.4rem;font-weight:700;color:var(--accent2);letter-spacing:-.01em}}
 .upgrade-tile .save{{display:inline-block;background:rgba(16,185,129,.18);color:var(--ok);font-size:.74rem;padding:2px 8px;border-radius:999px;margin-left:8px;font-weight:600}}
 .upgrade-tile .desc{{font-size:.84rem;color:var(--muted);margin-top:4px}}
 .footer{{margin-top:48px;padding-top:24px;border-top:1px solid var(--border);font-size:.82rem;color:var(--muted);display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px}}
 .footer a{{color:var(--muted);text-decoration:none}}
 .footer a:hover{{color:var(--text)}}
 .deep-link{{display:inline-block;margin-left:14px;background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px 14px;border-radius:8px;font-size:.86rem;text-decoration:none}}
 .deep-link:hover{{border-color:var(--accent);color:var(--accent)}}
 .key-actions{{display:flex;gap:8px;margin-top:10px}}
 .key-action-btn{{background:var(--border);color:var(--text);border:none;padding:6px 12px;border-radius:6px;font-size:.78rem;cursor:pointer;font-family:inherit}}
 .key-action-btn:hover{{background:var(--accent);color:#fff}}
 .upgrade-note{{font-size:.84rem;color:var(--muted);margin-top:10px}}
 .return-nudge{{margin:12px 0 0;font-size:.9rem;color:var(--text)}}
 .bind-step{{margin-top:12px;background:#0d1117;border:1px solid var(--warn);border-radius:10px;padding:16px}}
 .bind-step.done{{border-color:var(--ok)}}
 .bind-why{{margin:0 0 10px;font-size:.92rem;color:var(--text)}}
 .bind-form{{display:flex;flex-wrap:wrap;gap:8px}}
 .bind-form input{{flex:1 1 220px;min-width:0;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:.95rem;font-family:inherit}}
 .bind-form button{{background:var(--accent);color:#fff;border:none;padding:10px 18px;border-radius:8px;font-size:.95rem;font-weight:600;cursor:pointer;font-family:inherit}}
 .bind-form button[disabled]{{opacity:.7;cursor:not-allowed}}
 .bind-note{{margin:8px 0 0;font-size:.78rem;color:var(--muted)}}
 .bind-status{{margin:8px 0 0;font-size:.85rem;color:var(--warn)}}
</style></head><body>
<div class="container">

<div class="topbar">
  <a href="https://dchub.cloud/">&larr; DC Hub</a>
  <span><a href="https://dchub.cloud/mcp">MCP docs</a> &middot; <a href="https://dchub.cloud/pricing">Pricing</a></span>
</div>

<div class="eyebrow">DC Hub MCP &middot; {NAME}</div>
<h1>DC Hub for {NAME}</h1>
<p class="tagline">{TAGLINE} &middot; {canon_tools} tools, free tier, 30 seconds to install.</p>

<div class="badges">
  <a href="/api/v1/mcp/quality" title="Live DC Hub operational quality score (transparent breakdown)" style="text-decoration:none"><img src="/api/v1/mcp/quality/badge.svg" alt="DC Hub quality score" style="height:22px;vertical-align:middle"></a>
  <span class="badge"><b>{canon_facilities}</b> facilities</span>
  <span class="badge"><b>311</b> DCPI markets</span>
  <span class="badge"><b>43</b> US ISO/BA grids live</span>
  <span class="badge"><b>1,400+</b> M&amp;A deals</span>
  <span class="badge"><b>Free tier</b> &middot; no credit card</span>
</div>

<!-- STEP 1: Mint a trial key ─────────────────────────────────────────── -->
<div class="card">
  <div class="step"><span class="step-num">1</span> Mint your free trial key</div>
  <p style="margin:0 0 14px;color:var(--muted);font-size:.95rem">
    One click, no email to start. The key works in {NAME}'s MCP config right away.
    {TRIAL_TERMS_STEP1_HTML}
  </p>
  <button id="mint-btn" class="mint-btn" onclick="mintKey()">
    Mint trial key &rarr;
  </button>
  <div id="key-box" class="key-box"></div>
  <div id="key-meta" class="key-meta" style="display:none"></div>
  <div id="bind-step" class="bind-step" style="display:none">
    <p id="bind-why" class="bind-why"></p>
    <form id="bind-form" class="bind-form" onsubmit="bindEmail(event)">
      <input id="bind-email" type="email" required autocomplete="email" placeholder="Your email" aria-label="Email to bind to this key">
      <button id="bind-btn" type="submit">Bind email &rarr;</button>
    </form>
    <p class="bind-note">Free, no card. Your email is used only to recover this key and send its receipts, never for marketing.</p>
    <p id="bind-status" class="bind-status" role="status"></p>
  </div>
  <div id="key-actions" class="key-actions" style="display:none">
    <button class="key-action-btn" onclick="copyKey()">Copy key</button>
    <button class="key-action-btn" onclick="mintKey(true)">Mint another</button>
  </div>
</div>

<!-- STEP 2: Install snippet ──────────────────────────────────────────── -->
<div class="card">
  <div class="step"><span class="step-num">2</span> Add to your {NAME} config</div>
  <div class="install-meta">
    Paste into <code>{INSTALL_PATH}</code> (macOS/Linux) &middot;
    <code>{INSTALL_PATH_WIN}</code> (Windows){DEEP_LINK_HTML}
  </div>
{CAVEAT_HTML}  <div class="snippet-wrap">
    <button class="copy-btn" id="copy-snippet" onclick="copySnippet()">Copy</button>
    <pre id="snippet-body">{SNIPPET_RENDERED}</pre>
  </div>
  <p style="margin:14px 0 0;color:var(--muted);font-size:.85rem">
    After saving, restart {NAME} and DC Hub will appear in the MCP connector list.
  </p>
{RETURN_NUDGE_HTML}</div>

<!-- STEP 3: Try-it prompts ──────────────────────────────────────────── -->
<div class="card">
  <div class="step"><span class="step-num">3</span> Try these prompts in {NAME}</div>
  <div class="examples">
{EXAMPLES_HTML}
  </div>
</div>

<!-- STEP 4: Upgrade ─────────────────────────────────────────────────── -->
<div class="card">
  <div class="step"><span class="step-num">4</span> Trial limits + upgrade</div>
  <p style="margin:0 0 6px;color:var(--muted);font-size:.95rem">
    {TRIAL_TERMS_STEP4_HTML}
    Need more? Agents buy one of two things: <strong>{canon_pack_offer}</strong> on this key
    (credits don't expire, no subscription), or <strong>Developer {canon_price_developer}</strong>
    for {canon_developer_mcp_calls} MCP calls/day on every tool except the Pro-only ones.
    Pro ({canon_price_pro}) is for a human screening sites: <a href="https://dchub.cloud/pricing">all plans</a>.
  </p>
  <div class="upgrade-grid">
    <!-- P0-D (frontend#1535, 2026-09-21): the two plans agents buy. Both hrefs
         point at the /api/v1/connect/click proxy, which stamps
         connect_landing_views.stripe_clicked_at before bouncing to Stripe;
         mintKey() re-points them at the minted key once one exists. Pro is
         not tiled here: it is the plan for a human screening sites. -->
    <a id="upg-pack" class="upgrade-tile" href="/api/v1/connect/click?platform={KEY}&plan=pack&view_id={VIEW_ID}">
      <h3>{PACK_CREDITS} API credits</h3>
      <div class="price">${PACK_PRICE}<span style="font-size:.7em;color:var(--muted)"> one-time</span></div>
      <div class="desc">Credits land on this key and never expire. No subscription.</div>
    </a>
    <a id="upg-developer" class="upgrade-tile" href="/api/v1/connect/click?platform={KEY}&plan=developer&view_id={VIEW_ID}">
      <h3>Developer</h3>
      <div class="price">${DEV_PRICE}<span style="font-size:.7em;color:var(--muted)">/mo</span></div>
      <div class="desc">{canon_developer_mcp_calls} MCP calls/day, every tool except the Pro-only ones. Cancel anytime; your Developer key arrives by email.</div>
    </a>
  </div>
  <p class="upgrade-note" id="ref-note" style="display:none">
    Upgrade links carry your trial key so the conversion attributes back to this page.
  </p>
</div>

<div class="footer">
  <span>&copy; DC Hub &middot; <a href="https://dchub.cloud/architecture">how it works</a></span>
  <span>
    <a href="https://dchub.cloud/connect/cursor">Cursor</a> &middot;
    <a href="https://dchub.cloud/connect/vscode">VS Code</a> &middot;
    <a href="https://dchub.cloud/connect/opencode">opencode</a> &middot;
    <a href="https://dchub.cloud/connect/cline">Cline</a> &middot;
    <a href="https://dchub.cloud/connect/continue">Continue</a> &middot;
    <a href="https://dchub.cloud/connect/claude-desktop">Claude Desktop</a>
  </span>
</div>

</div><!-- /container -->

<script>
const CLIENT_KEY  = {CLIENT_KEY_JSON};
const VIEW_ID     = {VIEW_ID_JSON};
const RAW_SNIPPET = {SNIPPET_JSON};
const KEY_SENTINEL = {KEY_SENTINEL_JSON};
// The trial terms this page was rendered with (_trial_terms), or null when
// routes.auto_trial could not be read.
const TRIAL_TERMS = {TRIAL_TERMS_JSON};
const STRIPE_M    = {STRIPE_M_JSON};
const STRIPE_A    = {STRIPE_A_JSON};
let mintedKey = "";

// r-connect-return (2026-09-14): a key's day 1 -> day 2, as Clarity events.
// connect_key_day1 fires when this page mints a key; connect_key_day2 when the
// same browser opens this client's page again on the next UTC day, and
// connect_key_return_later on any later day. It measures a RETURN VISIT to the
// install page, not a call made with the key. The key never reaches
// localStorage or Clarity: only the client name and the UTC day do.
const KEY_DAY_STORE = "dchub-connect-key-day1:" + CLIENT_KEY;
function utcDay() {{ return new Date().toISOString().slice(0, 10); }}
function clarityTag(kind, name, value) {{
  try {{ if (typeof window.clarity === "function") window.clarity(kind, name, value); }} catch (_) {{}}
}}
function markKeyDay1() {{
  clarityTag("set", "connect_client", CLIENT_KEY);
  clarityTag("event", "connect_key_day1");
  try {{ if (!localStorage.getItem(KEY_DAY_STORE)) localStorage.setItem(KEY_DAY_STORE, utcDay()); }} catch (_) {{}}
}}
(function keyDayReturn() {{
  let day1 = null;
  try {{ day1 = localStorage.getItem(KEY_DAY_STORE); }} catch (_) {{ return; }}
  const t1 = day1 ? Date.parse(day1) : NaN;
  if (isNaN(t1)) return;
  const days = Math.round((Date.parse(utcDay()) - t1) / 86400000);
  if (!(days >= 1)) return;
  clarityTag("set", "connect_client", CLIENT_KEY);
  clarityTag("set", "connect_key_day", days === 1 ? "d2" : "d3plus");
  clarityTag("event", days === 1 ? "connect_key_day2" : "connect_key_return_later");
}})();

// ★ r-connect-bind (2026-09-15). A mint can hand back a key that
// /api/v1/keys/validate refuses from its first call: this network's unbound
// key already past its free calls (reused), or a fresh key seeded with that
// count. routes/auto_trial.py marks both answers with bind_required and gate.
// The MCP server drops a refused key and serves the call anonymously, so such
// a key gets the bind step in place of the tier line, and stays out of the
// install snippet until POST /api/v1/keys/auto-trial/bind accepts an email.
let mintAnswer = null;

// ★ r-connect-quota-copy: an answer without daily_calls or trial_days got
// figures typed here. The caller now passes a daily figure it can stand
// behind, or none, and the trial length falls back to the rendered
// TRIAL_DAYS, which every trial key runs, bound or not. A figure neither
// source has is left out.
function showTierLine(j, calls) {{
  const days = (typeof j.trial_days === "number") ? j.trial_days
             : (TRIAL_TERMS ? TRIAL_TERMS.days : null);
  const parts = [(j.tier || "IDENTIFIED") + " tier"];
  if (typeof calls === "number") parts.push(calls + " req/day");
  if (typeof days === "number") parts.push(days + "-day trial");
  const meta = document.getElementById("key-meta");
  meta.style.display = "flex";
  meta.innerText = parts.join(" · ");
}}

// Swap the minted key into the install snippet. ★ r-connect-return
// (2026-09-14): the sentinel arrives as a format ARGUMENT. Typed into
// this template it went through .format(), which halved its braces, so
// replaceAll matched INSIDE the snippet's sentinel and every copied
// snippet carried the key with a brace left on each side of it.
function swapKeyIntoSnippet() {{
  document.getElementById("snippet-body").innerText = RAW_SNIPPET.replaceAll(KEY_SENTINEL, mintedKey);
}}

function showBindStep(j) {{
  document.getElementById("key-meta").style.display = "none";
  document.getElementById("snippet-body").innerText = RAW_SNIPPET;
  const n = (typeof j.free_calls_unbound === "number") ? j.free_calls_unbound + " " : "";
  const bound = j.daily_calls_when_email_bound
    ? " (" + j.daily_calls_when_email_bound + " req/day)" : "";
  document.getElementById("bind-why").innerText =
    (j.reused ? "This key has used its " + n + "free calls without an email."
              : "This network has used its " + n + "free calls without an email, and a new key starts from that count.")
    + " Until an email is bound, DC Hub answers this key anonymously, with trimmed previews."
    + " Bind one and the same key keeps working" + bound + ".";
  document.getElementById("bind-form").style.display = "";
  document.getElementById("bind-btn").disabled = false;
  document.getElementById("bind-status").innerText = "";
  const step = document.getElementById("bind-step");
  step.classList.remove("done");
  step.style.display = "block";
}}

async function bindEmail(ev) {{
  if (ev) ev.preventDefault();
  const status = document.getElementById("bind-status");
  const bindBtn = document.getElementById("bind-btn");
  const email = document.getElementById("bind-email").value.trim();
  if (!mintedKey || !email) {{
    status.innerText = "Enter the email to bind to this key.";
    return;
  }}
  bindBtn.disabled = true;
  status.innerText = "Binding...";
  try {{
    const r = await fetch("/api/v1/keys/auto-trial/bind", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ api_key: mintedKey, email: email }}),
    }});
    const b = await r.json();
    if (b && b.ok && b.bound) {{
      const j = mintAnswer || {{}};
      document.getElementById("bind-form").style.display = "none";
      document.getElementById("bind-why").innerText =
        "Email bound: the same key keeps working. A client that already holds it may take a few minutes to notice.";
      status.innerText = "";
      document.getElementById("bind-step").classList.add("done");
      // The key is bound now, so an answer without the bound figure gets the
      // one this page was rendered with.
      showTierLine(j, (typeof j.daily_calls_when_email_bound === "number")
                      ? j.daily_calls_when_email_bound
                      : (TRIAL_TERMS ? TRIAL_TERMS.daily_bound : null));
      swapKeyIntoSnippet();
      document.getElementById("mint-btn").innerText = "Email bound -- paste in step 2 ↑";
    }} else {{
      bindBtn.disabled = false;
      status.innerText = (b && b.error === "valid_email_required")
        ? "That email was not accepted. Check it and try again."
        : "Bind failed — try again.";
    }}
  }} catch (e) {{
    bindBtn.disabled = false;
    status.innerText = "Bind failed — try again.";
  }}
}}

async function mintKey(again) {{
  const btn = document.getElementById("mint-btn");
  btn.disabled = true;
  btn.innerText = again ? "Minting another..." : "Minting...";
  try {{
    const r = await fetch("/api/v1/keys/auto-mint?tool=connect_landing&platform=" + encodeURIComponent(CLIENT_KEY), {{
      method: "POST",
      headers: {{ "Content-Type": "application/json", "X-DC-Connect-Client": CLIENT_KEY }},
      body: JSON.stringify({{ client_name: CLIENT_KEY, intended_use: "connect_landing_page" }}),
    }});
    const j = await r.json();
    if (j && j.ok && j.api_key) {{
      mintedKey = j.api_key;
      mintAnswer = j;
      markKeyDay1();
      document.getElementById("key-box").innerText = mintedKey;
      document.getElementById("key-box").classList.add("shown");
      document.getElementById("key-actions").style.display = "flex";

      // ★ r-connect-bind (2026-09-15): a gated answer (see showBindStep) gets
      // the bind step where the tier line goes, and its key stays out of the
      // install snippet until an email is bound.
      const gated = !!(j.bind_required || j.gate);
      if (gated) {{
        showBindStep(j);
      }} else {{
        document.getElementById("bind-step").style.display = "none";
        showTierLine(j, j.daily_calls);
        swapKeyIntoSnippet();
      }}

      // Bind Stripe links via the /api/v1/connect/click proxy: it stamps
      // connect_landing_views.stripe_clicked_at BEFORE 302ing to Stripe so
      // the conversion-funnel dashboard can read viewed→clicked drop-off.
      // Carry the minted key as `key` query param → proxy threads it into
      // Stripe as client_reference_id (Fix-E attribution chain preserved).
      const viewQS = VIEW_ID ? ("&view_id=" + encodeURIComponent(VIEW_ID)) : "";
      const keyQS = "&key=" + encodeURIComponent(mintedKey);
      // ★ Guarded per tile: an unguarded getElementById().href on a tile that
      // is not on the page throws a TypeError and takes the rest of mintKey()
      // with it (the ref-note and the mint-update POST that attributes the key).
      for (const [tileId, tilePlan] of [["upg-pack", "pack"], ["upg-developer", "developer"]]) {{
        const tileEl = document.getElementById(tileId);
        if (tileEl) {{
          tileEl.href =
            "/api/v1/connect/click?platform=" + encodeURIComponent(CLIENT_KEY) +
            "&plan=" + tilePlan + viewQS + keyQS;
        }}
      }}
      document.getElementById("ref-note").style.display = "block";

      btn.innerText = gated ? "Key needs an email -- bind it below ↓"
                            : (again ? "Mint another" : "Key minted -- paste in step 2 ↑");

      // Best-effort tell the backend which key this view minted.
      // ★ r-connect-bind (2026-09-15): a gated key is attributed too. This view
      // handed it out, and /api/v1/connect/stats reports the validator's verdict
      // beside the count (keys_refused_now), so a key nobody binds shows there
      // as refused instead of dropping out of the stats. Bound later, it is the
      // same key, and its later calls count for this view.
      if (VIEW_ID) {{
        try {{
          await fetch("/api/v1/connect/mint-update", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ view_id: VIEW_ID, api_key: mintedKey }}),
          }});
        }} catch(_) {{}}
      }}
    }} else {{
      btn.disabled = false;
      btn.innerText = "Mint failed — try again";
      console.error("mint failed:", j);
    }}
  }} catch(e) {{
    btn.disabled = false;
    btn.innerText = "Mint failed — try again";
    console.error(e);
  }}
}}

function copyKey() {{
  if (!mintedKey) return;
  navigator.clipboard.writeText(mintedKey);
  const btns = document.querySelectorAll("#key-actions .key-action-btn");
  if (btns[0]) {{ btns[0].innerText = "Copied"; setTimeout(() => btns[0].innerText = "Copy key", 1500); }}
}}

function copySnippet() {{
  const body = document.getElementById("snippet-body").innerText;
  navigator.clipboard.writeText(body);
  const btn = document.getElementById("copy-snippet");
  btn.classList.add("copied");
  btn.innerText = "Copied";
  setTimeout(() => {{
    btn.classList.remove("copied");
    btn.innerText = "Copy";
  }}, 1500);
}}
</script>
</body></html>
""")


# r-toolcount (2026-07-06): single source for the connect-page tool count.
# Canonical live value = ai_surface_canon._mcp_tool_count() (MCP tools/list).
# Was hardcoded "48" in 5 template spots and went stale (live = 59).
#
# ★2026-08-19: and it went stale AGAIN, 48 -> 59 -> stuck, while tools/list
# reached 82. Consolidating five literals into one literal did not make the
# count derived; the comment above names the canonical source and the next
# line did not call it. Measured from outside: all NINE /connect/<client>
# pages served "59" five times each — 45 stale claims, under-claiming the
# catalog by 23, on the per-platform install pages.
# (Phrased "by 23" and not with the digits bolted to the noun because
#  TOOL_ALT_COUNT_RE reads that shape as an advertised count and fails this
#  file — which is the pattern working, so reword rather than allow-list.)
#
# ★WHY NO FENCE SAW IT, though this module IS in AGENT_CODE_SURFACES: the
# template said "{canon_tools} tools" (a placeholder, no digits) and the
# constant said "= 59" (digits, no noun). TOOL_COUNT_RE needs the digits and
# the word adjacent, so the count and its noun sat on opposite sides of a
# template boundary and every pattern read clean. Same structural blind spot
# as "Available Tools — 73 live" on /connect (#2959), one level up.
#
# The template carries {canon_tools} as a placeholder like {canon_facilities}
# beside it always has. No literal to go stale, and nothing left for .format()
# to fill. ★2026-09-10: this used to say "already wrapped in canon_text() at
# import" — that import-time wrap was itself the stale-floor bug; the wrap now
# happens per request in _render_page().
#
# ★ r-connect-quota-copy: step 4 said Pro "gets you unlimited daily quota" and
# the Pro Monthly tile said "Same unlimited access", while tier_registry gives
# Pro a finite mcp_daily. Step 4 now quotes it through canon's Pro MCP quota
# placeholder and names the MCP lane, the way /connect's Pro tier card does:
# Pro's REST rate limit is a different number. The tile states no figure.


# The literal every _CLIENTS snippet carries where the minted key goes. The
# page's script receives it as a format argument (KEY_SENTINEL_JSON), never as
# text typed into _PAGE_TEMPLATE_RAW, because .format() rewrites braces there.
TRIAL_KEY_SENTINEL = "{{TRIAL_KEY}}"


def _return_nudge_html(c: dict) -> str:
    """The one-line "come back tomorrow" nudge under the install step, or "".

    r-connect-return (2026-09-14). Only a client whose install puts the key
    somewhere the client keeps between chats carries one; on ChatGPT that is
    the connector URL. Escaped like the snippet: it is page text, not markup.
    """
    nudge = (c.get("return_nudge") or "").strip()
    if not nudge:
        return ""
    safe = nudge.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return '  <p class="return-nudge">&#8635; ' + safe + "</p>\n"


# ── Trial terms ─────────────────────────────────────────────────────────
# ★ r-connect-trial-terms (2026-09-15). Steps 1 and 4 typed the trial's terms
# into _PAGE_TEMPLATE_RAW, and what they typed were the EMAIL-BOUND terms, a
# line below a promise that no email was needed. routes/auto_trial.py enforces:
#   * an unbound key: TRIAL_DAILY_UNBOUND calls a day, and validate_trial_key
#     refuses it (bind_email_required) once it has used TRIAL_FREE_CALLS_UNBOUND
#     calls in all, until POST /api/v1/keys/auto-trial/bind takes an email;
#   * a bound key: TRIAL_DAILY_CALLS a day until expires_at, which the mint sets
#     TRIAL_DAYS out and binding does not move;
#   * a mint seeds a new key with its network's spent count once that reaches
#     the gate, so that key is refused from its first call.
# Read from routes.auto_trial on EVERY render, the way _keys_return reads
# TRIAL_FREE_CALLS_UNBOUND: both unbound figures are env-tunable without a
# deploy, and a value copied here would outlive a retune. No digits in this
# block: tests/test_canonical_counts_drift.py reads this file line by line for
# free-path quota claims.
_TRIAL_B = '<b style="color:var(--text)">'


def _trial_terms() -> dict | None:
    """The trial terms routes.auto_trial enforces right now, or None."""
    try:
        from routes.auto_trial import (TRIAL_DAILY_CALLS, TRIAL_DAILY_UNBOUND,
                                       TRIAL_DAYS, TRIAL_FREE_CALLS_UNBOUND)
        return {"free_calls": int(TRIAL_FREE_CALLS_UNBOUND),
                "daily_unbound": int(TRIAL_DAILY_UNBOUND),
                "daily_bound": int(TRIAL_DAILY_CALLS),
                "days": int(TRIAL_DAYS)}
    except Exception:
        return None


def _trial_clauses(t: dict | None, b: str = "", b_end: str = "") -> tuple[str, str]:
    """(free, keeps): what a trial key gets without an email, and what binding one keeps.

    b and b_end wrap each figure: the steps pass markup, the snippet passes none.
    With the terms unreadable (t is None) the clauses say the same things without
    a figure: no figure beats a wrong one, the asymmetry _pro_price_usd keeps for
    the price.
    """
    if t is None:
        return "a limited number of free calls", "the same key keeps working"
    # The unbound daily cap needs naming only when it bites before the free
    # calls run out, i.e. with TRIAL_FREE_CALLS_UNBOUND tuned above it.
    per_day = (f", at most {t['daily_unbound']} a day"
               if t["daily_unbound"] < t["free_calls"] else "")
    return (f"{b}{t['free_calls']} free calls{b_end}{per_day}",
            f"the same key keeps working, at {b}{t['daily_bound']} requests/day{b_end} "
            f"for the rest of its {t['days']}-day trial")


def _trial_terms_html(t: dict | None) -> tuple[str, str]:
    """(step 1, step 4) sentences stating the trial terms in t."""
    free, keeps = _trial_clauses(t, _TRIAL_B, "</b>")
    return (f"Without an email it gets {free}. Bind an email (free, no card) and "
            f"{keeps}. Free calls are counted per network: if this one has already "
            f"used them, a new key needs the email from its first call.",
            f"Without an email, a trial key gets {free}. Bind an email and {keeps}.")


def _snippet(c: dict, t: dict | None) -> str:
    """c's install snippet as the page serves it: in the <pre> and as RAW_SNIPPET.

    A client flagged snippet_trial_terms gets step 4's terms under its snippet,
    one sentence a line, as plain text. The key sentinel is left as it is, so the
    page script's swap still finds it, and these lines survive the swap.
    """
    if not c.get("snippet_trial_terms"):
        return c["snippet"]
    free, keeps = _trial_clauses(t)
    return (f"{c['snippet']}\nWithout an email, a trial key gets {free}.\n"
            f"Bind an email and {keeps}.")


def _render_page(client_key: str, view_id: int | None) -> str:
    c = _CLIENTS[client_key]
    # One read of the trial terms per render, for steps 1 and 4, the snippet and
    # the page script, so no two of them can state different terms.
    terms = _trial_terms()
    trial_step1_html, trial_step4_html = _trial_terms_html(terms)
    # Render examples as <div class="example"> lines
    examples_html = "\n".join(
        f'    <div class="example">{e}</div>' for e in c["examples"]
    )
    # ★ Optional per-client caveat, rendered ABOVE the snippet on purpose: a
    #   client whose consumer surface cannot use this config at all must say so
    #   before the reader copies something that will not work where they are.
    #   Absent key -> empty string, so every existing card renders byte-identically.
    caveat_html = ""
    if c.get("caveat"):
        caveat_html = (
            '  <p class="install-meta" style="margin:10px 0 0;line-height:1.55">'
            + c["caveat"] + "</p>\n"
        )
    deep_link_html = ""
    if c.get("deep_link"):
        deep_link_html = (
            f'<a class="deep-link" href="{c["deep_link"]}">{c.get("deep_link_label","Open in app")}</a>'
        )
    # The snippet body still has the literal {{TRIAL_KEY}} sentinel — JS
    # swaps it after the mint POST. Server-side we just escape any HTML
    # characters that would break <pre> rendering.
    snippet = _snippet(c, terms)
    snippet_rendered = (
        snippet
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    # Note: we use .format(...) on a heredoc but the template includes JS
    # with `${...}` template literals — wait, no, we deliberately wrote it
    # with no template literals. CSS uses doubled-braces `{{` / `}}` to
    # escape under .format(). The JSON placeholders are inserted via
    # repr-safe json.dumps so they're always valid JS literals.
    # ★ Resolve canon HERE, per request, then format. See the block above
    # _PAGE_TEMPLATE_RAW for why this is not done at import, and why the order
    # cannot be swapped (a surviving {canon_*} is a KeyError under .format()).
    return canon_text(_PAGE_TEMPLATE_RAW).format(
        PRO_PRICE=_pro_price_usd(),
        DEV_PRICE=_dev_price_usd(),
        PACK_PRICE=_pack_price_and_credits()[0],
        PACK_CREDITS=_pack_price_and_credits()[1],
        NAME=c["name"],
        KEY=client_key,
        TAGLINE=c["tagline"],
        INSTALL_PATH=c["install_path"],
        INSTALL_PATH_WIN=c["install_path_win"],
        SNIPPET_RENDERED=snippet_rendered,
        EXAMPLES_HTML=examples_html,
        DEEP_LINK_HTML=deep_link_html,
        CAVEAT_HTML=caveat_html,
        RETURN_NUDGE_HTML=_return_nudge_html(c),
        TRIAL_TERMS_STEP1_HTML=trial_step1_html,
        TRIAL_TERMS_STEP4_HTML=trial_step4_html,
        STRIPE_MONTHLY=_STRIPE_MONTHLY,
        STRIPE_ANNUAL=_STRIPE_ANNUAL,
        VIEW_ID=(str(view_id) if view_id else ""),
        CLIENT_KEY_JSON=json.dumps(client_key),
        VIEW_ID_JSON=json.dumps(view_id),
        SNIPPET_JSON=json.dumps(snippet),
        KEY_SENTINEL_JSON=json.dumps(TRIAL_KEY_SENTINEL),
        TRIAL_TERMS_JSON=json.dumps(terms),
        STRIPE_M_JSON=json.dumps(_STRIPE_MONTHLY),
        STRIPE_A_JSON=json.dumps(_STRIPE_ANNUAL),
    )


# ── Routes ──────────────────────────────────────────────────────────────
def _serve(client_key: str):
    if client_key not in _CLIENTS:
        return jsonify(ok=False, error="unknown_client",
                       known=list(_CLIENTS.keys())), 404
    view_id = _record_view(client_key)
    html = _render_page(client_key, view_id)
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    # 5-minute edge cache — pages are mostly static, the dynamic bit
    # (mint button) is fetched live from /api/v1/keys/auto-mint.
    resp.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    return resp


@mcp_connect_bp.route("/connect/cursor", methods=["GET"])
def connect_cursor():
    return _serve("cursor")


@mcp_connect_bp.route("/connect/cline", methods=["GET"])
def connect_cline():
    return _serve("cline")


@mcp_connect_bp.route("/connect/continue", methods=["GET"])
def connect_continue():
    return _serve("continue")


@mcp_connect_bp.route("/connect/claude-desktop", methods=["GET"])
def connect_claude_desktop():
    return _serve("claude-desktop")


@mcp_connect_bp.route("/connect/chatgpt", methods=["GET"])
def connect_chatgpt():
    return _serve("chatgpt")


@mcp_connect_bp.route("/connect/gemini", methods=["GET"])
def connect_gemini():
    return _serve("gemini")


@mcp_connect_bp.route("/connect/opencode", methods=["GET"])
def connect_opencode():
    return _serve("opencode")


@mcp_connect_bp.route("/connect/vscode", methods=["GET"])
def connect_vscode():
    return _serve("vscode")


@mcp_connect_bp.route("/connect/kimi", methods=["GET"])
def connect_kimi():
    return _serve("kimi")


@mcp_connect_bp.route("/connect/qwen", methods=["GET"])
def connect_qwen():
    return _serve("qwen")


@mcp_connect_bp.route("/connect/zai", methods=["GET"])
def connect_zai():
    return _serve("zai")


@mcp_connect_bp.route("/connect/zed", methods=["GET"])
def connect_zed():
    return _serve("zed")


@mcp_connect_bp.route("/connect/deepseek-harness", methods=["GET"])
def connect_deepseek_harness():
    return _serve("deepseek-harness")


# ── Deep links onto content we ALREADY publish ──────────────────────────────
# MEASURED 2026-09-09, at the edge and on this origin: /connect/{claude,
# perplexity, copilot, grok, windsurf} were 404 while nine siblings answered
# 200. Every one of the five already has VERIFIED setup instructions live
# somewhere else — the first four as sections of static/connect.html
# (id="claude-ai", "perplexity", "copilot", "grok"), Windsurf as the live
# /install/windsurf page carrying ~/.codeium/windsurf/mcp_config.json.
#
# ★ ALIASES, NOT NEW CARDS, and that is the point. Writing five fresh cards
#   would mean a SECOND copy of each config snippet and each count — which is
#   exactly how the published facility figure went stale in 34 files at once
#   while canon moved on (dchub-mcp-server#394). One source, many doors.
#
#   ★ And no, the figure is deliberately not quoted here. A comment that spells
#     out the stale literal re-trips the very scanner that catches it —
#     tests/test_canonical_counts_drift.py flagged exactly that on this line.
#
# ★ 302, NOT 301. These are provisional: any of them may later become a real
#   _CLIENTS card with its own trial-key mint. A permanently-cached redirect
#   would outlive that decision in browsers we cannot reach — and _worker.js
#   already carries the scar of a 301 that pointed into a 404
#   (/connect/mcp.html -> /connect/mcp).
_CONNECT_ALIASES = {
    "claude":     "/connect#claude-ai",
    "perplexity": "/connect#perplexity",
    "copilot":    "/connect#copilot",
    "grok":       "/connect#grok",
    "windsurf":   "/install/windsurf",
    # ★ r-connect-minimax (2026-09-09) — an alias to the generic start block,
    #   and NOT a card, because MiniMax could not be verified as an MCP CLIENT
    #   at all. Every MiniMax MCP artifact findable 2026-09-09 is MiniMax
    #   acting as a SERVER (MiniMax-MCP and MiniMax-Coding-Plan-MCP publish
    #   TTS / image / video / search tools for other clients to consume).
    #   Nothing documents MiniMax consuming a remote MCP endpoint.
    #   Writing a MiniMax card would therefore have asserted a capability we
    #   have no evidence exists — the failure mode the agent-instructions block
    #   on /connect names outright ("never invent tool names"), applied to a
    #   product instead of a tool. This kills the 404 and hands over the
    #   endpoint without claiming MiniMax can use it.
    "minimax":    "/connect#start",
    # ★ r-connect-deepseek (2026-09-18) — these two ARE aliases onto one card
    #   for the same reason the block above is aliases: /connect#deepseek and
    #   the card would otherwise carry two copies of one YAML snippet, and the
    #   next time the harness renames a field only one of them gets fixed.
    #   "harness" resolves here and NOT to harness.io — if that vendor ever
    #   warrants a card, it needs its own slug, because these are unrelated
    #   products that share a word.
    "deepseek":   "/connect/deepseek-harness",
    "harness":    "/connect/deepseek-harness",
}


def _alias_view(target):
    def _view():
        return redirect(target, code=302)
    return _view


for _slug, _target in _CONNECT_ALIASES.items():
    mcp_connect_bp.add_url_rule(
        "/connect/" + _slug,
        endpoint="connect_alias_" + _slug.replace("-", "_"),
        view_func=_alias_view(_target),
        methods=["GET"],
    )


# Best-effort "this view's key" hook the JS calls after a successful mint.
# Updates the connect_landing_views row with key_minted_for so the funnel
# can attribute trial keys -> page that minted them.
@mcp_connect_bp.route("/api/v1/connect/mint-update", methods=["POST"])
def mint_update():
    body = request.get_json(silent=True) or {}
    view_id = body.get("view_id")
    api_key = (body.get("api_key") or "").strip()
    if not view_id or not api_key:
        return jsonify(ok=False, error="missing_view_id_or_key"), 200
    db = _get_db()
    if db is None:
        return jsonify(ok=False, error="no_db"), 200
    try:
        with db.cursor() as cur:
            cur.execute(
                """UPDATE connect_landing_views
                       SET key_minted_for = %s,
                           key_minted_at  = NOW()
                     WHERE id = %s""",
                (api_key[:120], int(view_id)),
            )
        db.commit()
        return jsonify(ok=True), 200
    except Exception as e:
        try: db.rollback()
        except Exception: pass
        return jsonify(ok=False, error="update_failed",
                       detail=str(e)[:160]), 200
    finally:
        try: db.close()
        except Exception: pass


# ── r68-funnel-stamping (2026-06-06): stripe-click proxy ────────────────
# The Connect landing pages had direct buy.stripe.com links. Result: 0
# stripe_clicked_at stamps in connect_landing_views even though some
# operators DID click through. Same root cause as the redeem-page leak:
# in-page JS beacons race the cross-origin navigation and lose.
#
# This proxy stamps the per-view row first, THEN 302s to Stripe with
# client_reference_id wired. View_id comes from a query param so the
# stamp lands on the row that minted the trial key (= the row the
# funnel-attribution joins on).
# P0-D (frontend#1535, 2026-09-21): plan → (Payment Link, client_reference_id
# prefix). The key rides to Stripe as a HASH, never raw:
#   pk-<sha256(key)>  the pack: the webhook grants the credits to THIS key hash
#                     (after checking a pack price was paid), trial keys included.
#   k-<sha256(key)>   a subscription: the webhook lifts the tier of a durable
#                     (mcp_dev_keys) key with that hash. A trial key has none, so
#                     the Developer tile says the paid key arrives by email.
# The raw key used to go as the ref. conversion_attribution reads a raw ref as a
# bare MCP session id, which a trial key is not, so it bound nothing, while the
# agent's key sat in a third party's URL. pro_monthly / pro_annual are no longer
# tiled but stay routable for links already handed out.
_CLICK_PLANS = {
    "pack": (_CANON_LINKS["metered"], "pk-"),
    "developer": (_CANON_LINKS["developer"], "k-"),
    "pro_monthly": (_STRIPE_MONTHLY, "k-"),
    "pro_annual": (_STRIPE_ANNUAL, "k-"),
}


@mcp_connect_bp.route("/api/v1/connect/click", methods=["GET"])
def connect_click_proxy():
    """Stamp connect_landing_views.stripe_clicked_at then 302 to Stripe.

    Query params:
      platform — cursor|cline|continue|claude-desktop (audit only)
      plan     — pack | developer (the tiles); pro_monthly | pro_annual still
                 resolve for links already handed out. Anything else → pack.
      view_id  — connect_landing_views.id (set by the page after _record_view)
      key      — minted trial key, used as client_reference_id for Stripe
                 attribution (Fix-E pattern, mirrors redeem-page click).
    """
    platform = (request.args.get("platform") or "").strip().lower()
    plan = (request.args.get("plan") or "pack").strip().lower()
    if plan not in _CLICK_PLANS:
        plan = "pack"
    view_id = (request.args.get("view_id") or "").strip()
    key = (request.args.get("key") or "").strip()

    stripe_base, ref_prefix = _CLICK_PLANS[plan]

    # Stamp first.  Best-effort: a failure here must NEVER block the
    # redirect (the user is mid-click on the Upgrade button — making them
    # see an error page for a telemetry blip would be a bigger leak than
    # the missing stamp). Wrap tight, log, redirect.
    if view_id:
        db = _get_db()
        if db is not None:
            try:
                with db.cursor() as cur:
                    cur.execute(
                        """UPDATE connect_landing_views
                               SET stripe_clicked_at = COALESCE(stripe_clicked_at, NOW()),
                                   stripe_clicked_plan = COALESCE(stripe_clicked_plan, %s)
                             WHERE id = %s""",
                        (plan[:32], int(view_id)) if view_id.isdigit() else (plan[:32], -1),
                    )
                db.commit()
            except Exception as e:
                try: db.rollback()
                except Exception: pass
                # Pre-schema-repair DBs lack stripe_clicked_at/_plan →
                # retry just stamping the timestamp column (added second
                # below).  If both fail, log + redirect anyway.
                try:
                    with db.cursor() as cur:
                        cur.execute(
                            """UPDATE connect_landing_views
                                   SET stripe_clicked_at = COALESCE(stripe_clicked_at, NOW())
                                 WHERE id = %s""",
                            (int(view_id),) if view_id.isdigit() else (-1,),
                        )
                    db.commit()
                except Exception as e2:
                    try: db.rollback()
                    except Exception: pass
                    logger.warning(
                        "connect_click_proxy stamp failed platform=%s view_id=%s: %s",
                        platform, view_id, e2,
                    )
            finally:
                try: db.close()
                except Exception: pass

    # Carry the minted key forward to Stripe as its HASH (see _CLICK_PLANS).
    sep = "&" if "?" in stripe_base else "?"
    if key:
        import hashlib as _hl
        ref = ref_prefix + _hl.sha256(key.encode()).hexdigest()
        redirect_url = f"{stripe_base}{sep}client_reference_id={ref}"
    else:
        redirect_url = stripe_base
    return redirect(redirect_url, code=302)


# ── keys_used_day2: a /connect key used again on a later UTC day ────────
# r-connect-day2 (2026-09-14). The page script's Clarity events
# (connect_key_day1 / connect_key_day2) count a RETURN VISIT to the install
# page. This counts what the page exists for: the key it minted turning up in
# mcp_call_log on a UTC calendar day after the day it was minted.
#
# r-connect-return (2026-09-15). The same read answers days 4 and 7
# (keys_used_by_day) and says which minted keys the validator refuses now
# (keys_refused_now). Without that, a 0 cannot say whether anyone came back.
#
# ★ A call is logged under a key only when /api/v1/keys/validate ACCEPTED the
#   key for that call. The MCP server drops a refused key and serves the call
#   anonymously (server.mjs _effectiveCallerKey; dchub-mcp-server
#   test/connect-query-key-reaches-track.test.mjs). An unbound trial key is
#   refused once it has used its free unbound calls, and a key minted from an
#   IP that already used them is refused from its first call
#   (auto_trial.mint_trial_for_request). Such a key's return cannot count.
# ★ The join rides the api_key index. Production planned the day-2 statement
#   this read replaced (EXPLAIN, 2026-09-14) as a Nested Loop Semi Join over a
#   Bitmap Index Scan on idx_mcp_log_apikey; this read keeps the same
#   correlated EXISTS on m.api_key. Wrap m.api_key in a function and the plan
#   becomes a scan of the whole call log, behind a public route with a ~15s
#   edge timeout. tests/test_connect_keys_used_day2_sql.py EXPLAINs the text
#   this module sends.
# ★ Day boundaries are taken in timestamp space (AT TIME ZONE 'UTC', + N - 1
#   days, back to timestamptz), so neither the session TimeZone nor a DST
#   change can move them.
# ★ Only MCP calls count. mcp_call_log also holds page events (key_issued,
#   key_first_use) and bulk REST rows (bulk:<tier>). These four are the
#   event_type values flask_mcp_endpoints.track_tool_call maps a call status
#   to; a status it does not map is stored as NULL, and is still a call.
# ★ keys_refused_now follows validate_key: an active mcp_dev_keys row serves;
#   otherwise validate_trial_key refuses a key that is not a dch_trial_ key
#   with a row, then an expired one, then an unbound one at its free-call
#   allowance. That allowance is read from routes.auto_trial on each request,
#   the value the validator reads, never a copy of it.
MCP_CALL_EVENT_TYPES = ("tool_call", "tool_error", "paywall_block", "trial_preview")

# Day 1 is a key's mint day, so day 2 is keys_used_day2.
KEYS_USED_BY_DAY = (2, 4, 7)

# validate_trial_key's refusals, in its order. A daily cap ends at the next UTC
# day and is not one of them.
KEYS_REFUSED_REASONS = ("unknown_key", "expired", "bind_email_required")

# Well inside the edge's route timeout, on top of the stats read itself.
KEYS_RETURN_TIMEOUT_MS = 4000

# One row per distinct key: the days that have begun for it, the days on or
# after which it was called, and why the validator refuses it now (NULL when it
# serves). The key itself is never selected.
_KEYS_RETURN_SQL = """
    SELECT k.client,
           ARRAY(SELECT d.day
                   FROM unnest(%(days)s::int[]) AS d(day)
                  WHERE (k.mint_day + (d.day - 1) * INTERVAL '1 day') AT TIME ZONE 'UTC' <= NOW()
                  ORDER BY d.day) AS eligible_days,
           ARRAY(SELECT d.day
                   FROM unnest(%(days)s::int[]) AS d(day)
                  WHERE EXISTS (SELECT 1
                                  FROM mcp_call_log m
                                 WHERE m.api_key = k.key_minted_for
                                   AND m.timestamp >= (k.mint_day + (d.day - 1) * INTERVAL '1 day')
                                                      AT TIME ZONE 'UTC'
                                   AND (m.event_type IS NULL OR m.event_type = ANY(%(event_types)s)))
                  ORDER BY d.day) AS used_days,
           CASE
             WHEN EXISTS (SELECT 1
                            FROM mcp_dev_keys dk
                           WHERE dk.api_key = k.key_minted_for
                             AND dk.status = 'active') THEN NULL
             WHEN left(k.key_minted_for, 10) <> 'dch_trial_' OR t.api_key IS NULL THEN 'unknown_key'
             WHEN t.expires_at < NOW() THEN 'expired'
             WHEN COALESCE(t.signed_up_email, '') = ''
                  AND COALESCE(t.operator_email, '') = ''
                  AND COALESCE(t.call_count, 0) >= %(free_calls)s THEN 'bind_email_required'
           END AS refused
      FROM (SELECT client, key_minted_for,
                   date_trunc('day', MIN(key_minted_at) AT TIME ZONE 'UTC') AS mint_day
              FROM connect_landing_views
             WHERE viewed_at > NOW() - INTERVAL '30 days'
               AND key_minted_for IS NOT NULL
               AND key_minted_at IS NOT NULL
             GROUP BY client, key_minted_for) k
      LEFT JOIN auto_trial_keys t ON t.api_key = k.key_minted_for
"""

KEYS_USED_DAY2_BASIS = (
    "keys_used_day2 counts distinct API keys this client's /connect page "
    "attached to a page view inside the same 30-day window as keys_minted, "
    "that appear as mcp_call_log.api_key on at least one MCP call (event_type "
    "tool_call, tool_error, paywall_block, trial_preview, or NULL) timestamped "
    "on a UTC calendar day after the key's mint day (its earliest "
    "key_minted_at in the window). It measures reuse of a key in the MCP call "
    "log: not people, not devices, and not return visits to the page. A call "
    "is logged under a key only when DC Hub accepted the key for that call; a "
    "refused key is served anonymously and logged without it, so its reuse "
    "cannot count (see keys_refused_now). A key minted on the current UTC day "
    "cannot count before the next one. "
    "keys_minted counts page views carrying a key, and a second key minted on "
    "the same view replaces the first. Calls from DC Hub's own testing are not "
    "excluded. When keys_used_day2_status is not 'measured', keys_used_day2 is "
    "null for every client, never 0."
)

KEYS_USED_BY_DAY_BASIS = (
    "keys_used_by_day covers the same keys as keys_used_day2, for days 2, 4 "
    "and 7, where day 1 is a key's mint day and day N begins N - 1 UTC "
    "calendar days after it. eligible counts the keys whose day N has begun. "
    "used counts the eligible keys that appear on an MCP call timestamped on "
    "day N or later, under the rules of keys_used_day2, so used for day 2 is "
    "keys_used_day2. A key refused when it comes back cannot count (see "
    "keys_refused_now), and an unbound trial key is refused once its trial "
    "expires. keys_used_by_day comes from the same read as keys_used_day2: "
    "when keys_used_day2_status is not 'measured', it is null for every "
    "client, never 0."
)

KEYS_REFUSED_NOW_BASIS = (
    "keys_refused_now counts, among the same keys as keys_used_day2, those "
    "/api/v1/keys/validate refuses as of this read, by the first reason that "
    "applies in its order. A key with an active mcp_dev_keys row is served "
    "and never counted. unknown_key: not a dch_trial_ key with an "
    "auto_trial_keys row. expired: the trial's expires_at has passed. "
    "bind_email_required: a trial bound to no email that has used its free "
    "unbound calls. total is their sum. A daily-cap refusal, which ends at "
    "the next UTC day, is not counted. It describes each key now, not when it "
    "was used, so a key counted in keys_used_day2 may have been refused "
    "since. A refused key's calls are served anonymously and logged without "
    "the key, so they cannot count in keys_used_day2 or keys_used_by_day. "
    "keys_refused_now comes from the same read as keys_used_day2: when "
    "keys_used_day2_status is not 'measured', it is null for every client, "
    "never 0."
)


def _no_keys():
    return {"by_day": {day: {"eligible": 0, "used": 0} for day in KEYS_USED_BY_DAY},
            "refused": dict.fromkeys(KEYS_REFUSED_REASONS, 0)}


def _keys_return(db):
    """({client: {"by_day": {day: {"eligible", "used"}}, "refused": {reason: n}}},
    "measured"), or (None, "timeout" | "query_failed").

    Fail-soft: the stats read has already succeeded and this read must never
    take it down. It runs in a transaction of its own, because SET LOCAL is a
    no-op under autocommit, and main.get_db() lends a POOLED connection, so
    autocommit goes back to what it was before the connection returns.
    """
    prev = None
    try:
        from routes.auto_trial import TRIAL_FREE_CALLS_UNBOUND
        db.rollback()                    # end the stats read's transaction; it wrote nothing
        prev = db.autocommit
        db.autocommit = False
        with db.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %d" % KEYS_RETURN_TIMEOUT_MS)
            cur.execute(_KEYS_RETURN_SQL, {"days": list(KEYS_USED_BY_DAY),
                                           "event_types": list(MCP_CALL_EVENT_TYPES),
                                           "free_calls": int(TRIAL_FREE_CALLS_UNBOUND)})
            rows = cur.fetchall()
        db.rollback()                    # read only; also drops the SET LOCAL
        keys = {}
        for client, eligible_days, used_days, refused in rows:
            got = keys.setdefault(client, _no_keys())
            for day in eligible_days:
                got["by_day"][day]["eligible"] += 1
                if day in used_days:
                    got["by_day"][day]["used"] += 1
            if refused is not None:
                got["refused"][refused] += 1
        return keys, "measured"
    except Exception as e:
        try: db.rollback()
        except Exception: pass
        # statement_timeout cancels with SQLSTATE 57014 (query_canceled).
        status = "timeout" if getattr(e, "pgcode", None) == "57014" else "query_failed"
        logger.warning("connect_stats keys read %s: %s", status, e)
        return None, status
    finally:
        if prev is not None:
            try: db.autocommit = prev
            except Exception: pass


# Public admin/stats endpoint for the funnel dashboard.
@mcp_connect_bp.route("/api/v1/connect/stats", methods=["GET"])
def connect_stats():
    db = _get_db()
    if db is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with db.cursor() as cur:
            cur.execute("""
                SELECT client,
                       COUNT(*)                              AS views,
                       COUNT(key_minted_for)                 AS keys_minted,
                       COUNT(DISTINCT ip)                    AS unique_ips,
                       MIN(viewed_at)                        AS first_view,
                       MAX(viewed_at)                        AS last_view
                  FROM connect_landing_views
                 WHERE viewed_at > NOW() - INTERVAL '30 days'
              GROUP BY client
              ORDER BY views DESC
            """)
            rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "client":      r[0],
                "views_30d":   int(r[1] or 0),
                "keys_minted": int(r[2] or 0),
                "unique_ips":  int(r[3] or 0),
                "first_view":  r[4].isoformat() if r[4] else None,
                "last_view":   r[5].isoformat() if r[5] else None,
                "mint_rate":   round((r[2] / r[1]) * 100, 2) if r[1] else 0.0,
            })
        keys, keys_status = _keys_return(db)
        for row in out:
            if keys is None:
                row["keys_used_day2"] = None
                row["keys_used_by_day"] = None
                row["keys_refused_now"] = None
                continue
            got = keys.get(row["client"]) or _no_keys()
            row["keys_used_day2"] = got["by_day"][2]["used"]
            row["keys_used_by_day"] = {str(day): got["by_day"][day] for day in KEYS_USED_BY_DAY}
            row["keys_refused_now"] = dict(got["refused"], total=sum(got["refused"].values()))
        return jsonify(ok=True, by_client=out, window="30d",
                       keys_used_day2_status=keys_status,
                       keys_used_day2_basis=KEYS_USED_DAY2_BASIS,
                       keys_used_by_day_basis=KEYS_USED_BY_DAY_BASIS,
                       keys_refused_now_basis=KEYS_REFUSED_NOW_BASIS), 200
    except Exception as e:
        return jsonify(ok=False, error="query_failed",
                       detail=str(e)[:200]), 500
    finally:
        try: db.close()
        except Exception: pass
