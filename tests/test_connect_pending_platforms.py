#!/usr/bin/env python3
"""/connect/{qwen,zai,minimax} answer — and answer with VERIFIED setup only.

NO NETWORK, NO DB.

MEASURED LIVE 2026-09-09, cache-busted, on dchub.cloud:
    /connect/{claude,perplexity,copilot,grok,windsurf}  302 -> 200   (aliased)
    /connect/{chatgpt,cursor,gemini,kimi}               200          (cards)
    /connect/{minimax,qwen,zai}                         404          <- this

★ WHY TWO CARDS AND ONE ALIAS, rather than the three cards the handoff asked
  for. The alias doctrine in mcp_connect.py is explicit — "ALIASES, NOT NEW
  CARDS ... one source, many doors" — but it has a precondition: an alias
  points at instructions we ALREADY publish. static/connect.html carries
  sections for claude-ai / perplexity / copilot / grok / chatgpt / cursor /
  gemini / mistral / vscode. It carries NOTHING for Qwen or Z.ai, so for those
  two there is no door to point at and a card is the only honest option.

★ MiniMax gets the alias, and not because a door exists — because the CLAIM
  does not. Every MiniMax MCP artifact findable 2026-09-09 is MiniMax acting
  as a SERVER (MiniMax-MCP, MiniMax-Coding-Plan-MCP: TTS, image, video,
  search). Nothing documents MiniMax CONSUMING a remote MCP endpoint. A
  MiniMax card would have asserted a capability with no evidence behind it.

★ The Qwen card's `httpUrl` is the whole reason these are not copy-paste jobs.
  Qwen Code reserves plain `url` for SSE. The mcpServers muscle-memory from
  Claude/Cursor/Kimi produces a server that registers and never answers.

THE LIMIT: this pins what we PUBLISH. Whether Qwen Code and ZCode actually
connect against a live key is an end-to-end check against third-party clients,
which this cannot do and did not do.
"""
import pathlib
import re
import sys

import pytest
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_connect as mc  # noqa: E402


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(mc.mcp_connect_bp)
    return app.test_client()


# ── the 404s are gone ────────────────────────────────────────────────────

@pytest.mark.parametrize("slug", ["qwen", "zai"])
def test_new_cards_answer_200(client, slug):
    r = client.get("/connect/" + slug)
    assert r.status_code == 200, f"/connect/{slug} -> {r.status_code}"


def test_minimax_redirects_instead_of_404(client):
    r = client.get("/connect/minimax")
    assert r.status_code == 302, r.status_code
    assert r.headers["Location"] == "/connect#start"


def test_every_alias_is_302_not_301():
    """A 301 outlives the decision to promote an alias into a card — the
    _worker.js scar (/connect/mcp.html -> /connect/mcp) is the precedent."""
    app = Flask(__name__)
    app.register_blueprint(mc.mcp_connect_bp)
    c = app.test_client()
    for slug in mc._CONNECT_ALIASES:
        assert c.get("/connect/" + slug).status_code == 302, slug


# ── what the cards actually say ──────────────────────────────────────────

def test_qwen_uses_httpUrl_and_never_the_sse_url_key():
    """THE REGRESSION THIS PINS. `url` here is SSE — it registers and hangs."""
    snip = mc._CLIENTS["qwen"]["snippet"]
    assert '"httpUrl": "https://dchub.cloud/mcp"' in snip
    assert '"url": "https://dchub.cloud/mcp"' not in snip, (
        "plain `url` is Qwen's SSE field — this snippet would never answer")


def test_zai_publishes_no_guessed_remote_json_key():
    """Z.ai documents the UI form, not the remote key names. Publishing a
    guessed key is worse than the 404 it replaces: it fails silently."""
    snip = mc._CLIENTS["zai"]["snippet"]
    for guessed in ('"httpUrl"', '"streamableHttp"',
                    '"url": "https://dchub.cloud/mcp"'):
        assert guessed not in snip, f"{guessed} is not documented by Z.ai"
    assert "https://dchub.cloud/mcp" in snip          # the endpoint IS known
    assert "mcp" in snip and "servers" in snip        # nested shape IS known


def test_no_minimax_card_asserts_client_capability():
    assert "minimax" not in mc._CLIENTS, (
        "MiniMax is not a verified MCP client — it publishes MCP servers")


@pytest.mark.parametrize("slug", ["qwen", "zai"])
def test_cards_carry_the_full_contract(slug):
    """The renderer reads these unguarded — a missing key is a 500."""
    c = mc._CLIENTS[slug]
    for k in ("name", "tagline", "install_path", "install_path_win",
              "snippet", "examples", "deep_link", "deep_link_label"):
        assert k in c, f"{slug} missing {k}"
    assert len(c["examples"]) >= 3


@pytest.mark.parametrize("slug", ["qwen", "zai"])
def test_cards_mint_the_trial_key_placeholder(slug):
    """{{TRIAL_KEY}} is what _serve swaps for a real minted key."""
    assert "{{TRIAL_KEY}}" in mc._CLIENTS[slug]["snippet"]


def test_no_card_hardcodes_a_facility_count():
    """One source, many doors. A second copy of a count is how 34 files went
    stale at once while canon moved on."""
    for slug, c in mc._CLIENTS.items():
        blob = f"{c.get('tagline','')} {c.get('snippet','')}"
        assert not re.search(r"\b\d{2},\d{3}\+", blob), (
            f"{slug} hardcodes a count — read canon instead")
