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
import json
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

@pytest.mark.parametrize("slug", ["qwen", "zai", "zed"])
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


@pytest.mark.parametrize("slug", ["qwen", "zai", "zed"])
def test_cards_carry_the_full_contract(slug):
    """The renderer reads these unguarded — a missing key is a 500."""
    c = mc._CLIENTS[slug]
    for k in ("name", "tagline", "install_path", "install_path_win",
              "snippet", "examples", "deep_link", "deep_link_label"):
        assert k in c, f"{slug} missing {k}"
    assert len(c["examples"]) >= 3


@pytest.mark.parametrize("slug", ["qwen", "zai", "zed"])
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


# ── r-connect-zed (2026-09-10) ───────────────────────────────────────────
# MEASURED LIVE 2026-09-10, cache-busted, on dchub.cloud:
#     /connect/zed  ->  404
# the last pending platform that is a VERIFIED remote-MCP client.
#
# ★ WHY ZED GETS A CARD AND MINIMAX STILL DOES NOT — the same test above,
#   re-applied to CURRENT evidence rather than inherited from the earlier
#   verdict. Re-checked 2026-09-10 against each vendor's OWN docs:
#
#     MiniMax   platform.minimax.io/docs/guides/mcp-guide documents the nine
#               tools MiniMax EXPOSES (TTS, voice clone, image, video) and
#               ZERO configuration for registering an external MCP endpoint.
#               Still a server. The alias stands, unchanged.
#     Zed       zed.dev/docs/ai/mcp documents a remote entry verbatim —
#               "url" plus a "headers" map — and ships an explicit
#               "Add Remote Server" UI path. Client, with published keys.
#
#   So the difference is not that Zed is more popular. It is that one vendor
#   documents consuming a remote endpoint and the other documents serving
#   one, which is the only thing these cards are allowed to assert.


def test_zed_uses_context_servers_and_never_mcpservers():
    """THE REGRESSION THIS PINS, and it is silent.

    Zed reads "context_servers" — it shipped the feature under that name
    before "MCP" settled as the word. A "mcpServers" block, which is the
    muscle memory from every other card in this file, is not read at all:
    no error, no entry in the agent panel, nothing to debug.
    """
    snip = mc._CLIENTS["zed"]["snippet"]
    obj = json.loads(_first_json_object(snip).replace("{{TRIAL_KEY}}", "k"))
    assert list(obj.keys()) == ["context_servers"], (
        f"the copyable block must key on context_servers alone, got "
        f"{list(obj.keys())} — Zed reads nothing else")

    # ★ THE PROSE IS ALLOWED TO NAME THE WRONG KEY, and must. The first cut of
    # this guard asserted `"mcpServers" not in snip` and failed on the card's
    # own warning line — a guard that would have been "fixed" by deleting the
    # most useful sentence on the page. The invariant is about the block the
    # reader COPIES, not the note telling them why it looks unfamiliar.
    assert "mcpServers" in snip, (
        "the card no longer warns about the wrong key — that warning is the "
        "reason this card exists rather than a link to Zed's docs")


def _first_json_object(text):
    """The first brace-balanced object in a snippet, so this parses the block
    the reader copies rather than a regex's idea of it."""
    i = text.index("{")
    depth = 0
    for k in range(i, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[i:k + 1]
    raise AssertionError("unbalanced braces in snippet")


def test_zed_snippet_is_valid_json_in_the_shape_zed_documents():
    """PARSE it, do not eyeball it.

    The block IS the deliverable on this card — unlike Z.ai, which publishes
    a UI path precisely because its keys are undocumented. A trailing comma
    or a stray brace here ships a settings.json that Zed refuses to load, and
    a substring assertion would not notice.
    """
    block = _first_json_object(mc._CLIENTS["zed"]["snippet"])
    obj = json.loads(block.replace("{{TRIAL_KEY}}", "dch_live_test"))

    srv = obj["context_servers"]["dchub"]
    assert srv["url"] == "https://dchub.cloud/mcp"
    assert srv["headers"]["X-API-Key"] == "dch_live_test"
    assert "command" not in srv and "args" not in srv, (
        "this is Zed's REMOTE entry — a command/args pair is the local "
        "stdio shape and would try to launch a binary that does not exist")


def test_zed_does_not_publish_the_bearer_header_zeds_example_shows():
    """Zed's own doc example is `Authorization: Bearer <token>`, and copying
    it here would authenticate against nothing: this origin resolves a tool
    key from X-API-Key and has no Bearer branch for one. Zed's `headers` is
    a free-form map, so the documented shape and the working header coexist.
    """
    snip = mc._CLIENTS["zed"]["snippet"]
    assert "X-API-Key" in snip
    assert "Bearer" not in snip and "Authorization" not in snip


def test_zed_states_both_settings_paths():
    """Zed is the one card here whose Windows path is not a %USERPROFILE%
    dotfile — it is %APPDATA%\\Zed. Getting it wrong sends Windows readers
    to a file Zed never reads."""
    c = mc._CLIENTS["zed"]
    assert c["install_path"] == "~/.config/zed/settings.json"
    assert c["install_path_win"] == "%APPDATA%\\Zed\\settings.json"
