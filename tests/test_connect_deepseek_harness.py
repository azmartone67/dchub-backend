#!/usr/bin/env python3
"""The DeepSeek recipe must hand over a config that works WHERE IT WORKS.

NO NETWORK, NO DB — renders the pages and reads what they serve.

★ The whole point of this card is a capability DeepSeek does NOT have.
chat.deepseek.com ships no MCP UI, so a card headed plain "DeepSeek" with a
paste-this block would be the MiniMax mistake (recorded in _CONNECT_ALIASES)
turned inside out: asserting a product can consume a remote MCP endpoint
because we would like it to. The three doors that do exist — the DeepSeek
Harness, Cursor pointed at a DeepSeek model, and the tool-calling API behind
a bridge — are what the page hands over, and the caveat naming them is load-
bearing copy, not a disclaimer.

★ The harness config shape is verified against
deepseek-ai/deepseek-harness@master packages/mcp/mcp-client/README.md
(plugin `@deepseek-ai/dsh-mcp-client`; `transport: streamable-http` takes
`url` + `headers`). Third-party write-ups of the same block publish `!js`
where the README says `!!js` — a literal key needs neither, which is why the
snippet carries a plain quoted string.

★ WHAT THIS PINS IS ONE ENDPOINT, MANY DOORS. The URL is asserted equal to
the one the Cursor card already serves rather than spelled out twice, so a
typo'd or forked endpoint fails here instead of shipping a card that points
somewhere real but wrong.
"""
import pathlib
import re
import sys

import pytest
from flask import Flask

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import routes.mcp_connect as mc  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_KEY = "deepseek-harness"


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(mc.mcp_connect_bp)
    return app.test_client()


def _page(key=_KEY):
    return mc._render_page(key, 4242)


def _snippet_block(key=_KEY):
    """The <pre> the reader actually copies.

    ★ Scoped on purpose. Asserting a token is "in the page" is satisfied by
    the page script, the RAW_SNIPPET JSON and any prose that happens to use
    the same word — a first pass of this file asserted the key sentinel that
    way and a mutation replacing it in the snippet body still read green.
    """
    html = _page(key)
    i = html.index('<pre id="snippet-body">')
    return html[i:html.index("</pre>", i)]


# ── the config is the one the harness actually reads ─────────────────────

def test_snippet_is_the_verified_dsh_mcp_client_block():
    html = _snippet_block()
    for needed in (
        "@deepseek-ai/dsh-mcp-client",   # the plugin, not harness.io
        "transport: streamable-http",    # the transport that takes a url
        "serverName: dchub",
        "X-API-Key",
    ):
        assert needed in html, f"the harness snippet lost {needed!r}"


def test_the_endpoint_is_the_same_one_cursor_gets():
    """One endpoint, many doors. A second URL here is a second source."""
    url = re.search(r"https://[a-z0-9.\-]+/mcp\b", _snippet_block())
    assert url, "the DeepSeek snippet names no MCP endpoint at all"
    assert url.group(0) in mc._CLIENTS["cursor"]["snippet"], (
        "the DeepSeek card points at %s, which the Cursor card does not serve"
        % url.group(0))


def test_the_key_sentinel_survives_into_the_page():
    """The page script swaps this after the mint POST; without it the reader
    copies a config with a literal placeholder in the header."""
    assert mc.TRIAL_KEY_SENTINEL in _snippet_block(), (
        "the snippet the reader copies carries no key sentinel; the page "
        "script has nothing to swap and the header ships a placeholder")


# ── the caveat is the card ───────────────────────────────────────────────

def test_the_page_says_consumer_chat_cannot_use_this():
    html = _page()
    assert "no MCP UI" in html, (
        "the card no longer tells the reader that DeepSeek's consumer chat "
        "cannot use an MCP endpoint — the one thing it exists to say")


def test_the_caveat_names_all_three_doors_that_do_work():
    html = _page()
    for door in ("/connect/cursor", "tool-calling API", "harness"):
        assert door in html, f"the caveat stopped naming {door!r}"


def test_the_caveat_is_above_the_snippet():
    """Advice printed under the block is advice given after the paste."""
    html = _page()
    assert html.index("no MCP UI") < html.index("snippet-body"), (
        "the caveat renders below the snippet")


# ── the new slot must not touch the eleven cards that predate it ─────────

@pytest.mark.parametrize("key", ["cursor", "cline", "continue", "claude-desktop"])
def test_cards_without_a_caveat_render_no_empty_paragraph(key):
    html = _page(key)
    assert "{CAVEAT_HTML}" not in html, "the slot leaked as a literal token"
    assert '<p class="install-meta" style="margin:10px 0 0' not in html, (
        f"{key} grew an empty caveat paragraph")


def test_no_placeholder_survives_any_card():
    """tests/test_canon_placeholders_resolved.py makes this rule global; this
    keeps the new card honest even if it is ever rendered on its own."""
    # The key sentinel is the ONE brace token that belongs in the output —
    # the page script swaps it after the mint POST. Drop it first, so this
    # cannot be satisfied by the sentinel and cannot fail because of it.
    html = _page().replace(mc.TRIAL_KEY_SENTINEL, "")
    assert not re.search(r"\{canon_[a-z_]+\}", html)
    assert not re.search(r"\{[A-Z_]{4,}\}", html)


# ── the doors open ───────────────────────────────────────────────────────

def test_the_card_serves_200(client):
    assert client.get("/connect/deepseek-harness").status_code == 200


@pytest.mark.parametrize("slug", ["deepseek", "harness"])
def test_aliases_land_on_the_card(client, slug):
    r = client.get(f"/connect/{slug}")
    assert r.status_code == 302, (
        f"/connect/{slug} returned {r.status_code}; these are provisional "
        "redirects and a 301 would outlive the decision in browsers we "
        "cannot reach")
    assert r.headers["Location"].endswith("/connect/deepseek-harness")


# ── the published surfaces carry the same recipe ─────────────────────────

def test_connect_page_has_the_deepseek_section():
    html = (_ROOT / "static" / "connect.html").read_text(encoding="utf-8")
    assert 'id="deepseek"' in html
    assert "dsh-mcp-client" in html
    assert "no MCP UI" in html, "/connect states the capability without the limit"


def test_connect_page_tool_count_is_canon_bound():
    """A hand-typed count is how the published facility figure went stale in
    34 files at once. The DeepSeek copy names a tool count; it must resolve."""
    html = (_ROOT / "static" / "connect.html").read_text(encoding="utf-8")
    section = html[html.index('id="deepseek"'):html.index('id="huggingface"')]
    assert "{canon_tools}" in section, (
        "the DeepSeek section's tool count is not canon-bound")
    assert not re.search(r"\b\d{2,3} tools\b", section), (
        "a literal tool count was hand-typed into the DeepSeek section")


def test_the_hugging_face_space_is_published_as_a_door_not_a_source():
    html = (_ROOT / "static" / "connect.html").read_text(encoding="utf-8")
    assert "huggingface.co/spaces/dchubcloud/dchub" in html
    assert "dchub.cloud/mcp" in html[html.index('id="huggingface"'):], (
        "the Space is listed without pointing agents back at the canonical "
        "endpoint — that is how a mirror becomes a second source of truth")


def test_ai_hub_list_carries_the_same_harness_block():
    """/ai-hub's own connect list — and ONLY /ai-hub's.

    ★ main.py 301s /ai-agents here, and that route NEVER RUNS: /ai-agents is a
    Cloudflare Pages asset (dchub-frontend/ai-agents.html) that shadows it.
    Measured this session, cache-busted: live /ai-agents is 39,528 b titled
    "AI Agent Grounding Pack"; this file is a different page at 37,036 b. So
    editing ai-hub.html does NOT reach /ai-agents — that recipe ships in
    dchub-frontend#1507. Same trap as the surface-truth serving map: the route
    a repo declares is not evidence of the bytes a URL serves.
    """
    html = (_ROOT / "static" / "ai-hub.html").read_text(encoding="utf-8")
    assert "dsh-mcp-client" in html
    assert "streamable-http" in html


# ── the ladder was reclassified, not just annotated ──────────────────────

def test_ladder_works_deepseek_as_a_crawl_host():
    from routes.agent_onboarding_master_shell import PLATFORMS
    entry = next(p for p in PLATFORMS if p["key"] == "deepseek_qwen_zai")
    assert "crawl" in entry["paths"], (
        "DeepSeek is still ladder-classified as an MCP-only host, which its "
        "consumer surface cannot be")
    assert "mcp" in entry["paths"], (
        "the MCP path is real for the harness, Cursor and a bridged API")
    assert entry["recipe_published"] is True
