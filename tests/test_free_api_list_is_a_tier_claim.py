""""Free, no key" is a CLAIM ABOUT TIER, and nothing was checking it (2026-09-20).

/llms.txt published 12 endpoints under "## FREE API — No Auth, No Signup, Start
Now"; /llms-full.txt published the same set under "### Free Endpoints (No Auth)"
and closed with "All of the above endpoints work WITHOUT any API key". Probed
anonymously, cache-busted, with a browser UA, FOUR of the twelve were gated:

    403  /api/grid/fuel-mix?iso=ERCOT   {"error":"plan_required", "...requires a Pro plan or higher."}
    403  /api/energy/prices/TX          {"error":"plan_required", "...requires a Pro plan or higher."}
    403  /api/v1/pipeline               {"error":"plan_required", "...requires a Identified plan or higher."}
    402  /api/site-score?lat=...        {"error":"upgrade_required", "...free map session for this period (1/30 days)"}

Worse than a broken link: the no-MCP policy's own rule 4 told an agent to GET
"grid fuel-mix", so an agent that obeyed us got a 403 and fell back to the
directories the same policy had just told it not to trust.

★ WHY THIS GUARD IS A MEASURED LIST AND NOT A DERIVATION.
The tier of a route is a RUNTIME property — `@require_plan(...)` resolved against
a plan lookup — and the routes are spread over modules plus several legacy patch
files that no longer register anything. There is no importable URL->tier map to
bind to, so deriving one would mean trusting a scan that can silently find
nothing. Instead each entry below carries the status code and the error body that
was measured, and the guard is paired with a FLOOR so it cannot pass by matching
an empty list. The durable check is a LIVE probe; see the note at the bottom.

Run:  python3 -m pytest tests/test_free_api_list_is_a_tier_claim.py -v
"""
from __future__ import annotations

import re

import pytest

# path -> (measured status, measured error code) on an anonymous GET, 2026-09-20
_MEASURED_GATED = {
    "/api/grid/fuel-mix":   (403, "plan_required"),
    "/api/energy/prices":   (403, "plan_required"),
    "/api/v1/pipeline":     (403, "plan_required"),
    "/api/site-score":      (402, "upgrade_required"),
}

# (path, heading that opens the keyless list, heading that closes it)
_SURFACES = (
    ("/llms.txt",      "## FREE API",                  "## KEY REQUIRED"),
    ("/llms-full.txt", "### Free Endpoints (No Auth)", "### Key required"),
)

# A free list this short would mean the block moved or emptied — at which point
# "no gated path in it" is true and meaningless.
_MIN_FREE_URLS = 6


@pytest.fixture(scope="module")
def client():
    flask = pytest.importorskip("flask")
    from ai_discovery_routes import register_discovery_routes

    app = flask.Flask(__name__)
    register_discovery_routes(app)
    return app.test_client()


def _free_block(client, path: str, open_h: str, close_h: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, "%s -> %s" % (path, r.status_code)
    body = r.get_data(as_text=True)
    i, j = body.find(open_h), body.find(close_h)
    assert i != -1, "%s no longer carries %r" % (path, open_h)
    assert j != -1, (
        "%s no longer carries the %r section. The four gated endpoints were "
        "moved there; without it they have nowhere to live but the free list."
        % (path, close_h))
    assert i < j, (
        "%s: the keyless list (%d) no longer precedes %r (%d) — this slice is "
        "not reading what it thinks it is." % (path, i, close_h, j))
    return body[i:j]


@pytest.mark.parametrize("path,open_h,close_h", _SURFACES)
def test_the_keyless_list_is_long_enough_to_be_worth_checking(client, path, open_h, close_h):
    """The floor. Without it every assertion below passes on an empty block."""
    urls = re.findall(r"https://dchub\.cloud(/[^\s)\]]+)", _free_block(client, path, open_h, close_h))
    assert len(urls) >= _MIN_FREE_URLS, (
        "%s: the keyless list yielded only %d URL(s) (%s). Either the block "
        "emptied or the heading moved — the gated-path assertions would then be "
        "vacuously true." % (path, len(urls), urls))


@pytest.mark.parametrize("path,open_h,close_h", _SURFACES)
def test_no_measured_gated_endpoint_sits_in_the_keyless_list(client, path, open_h, close_h):
    block = _free_block(client, path, open_h, close_h)
    leaked = sorted(p for p in _MEASURED_GATED if p in block)
    assert not leaked, (
        "%s advertises %d endpoint(s) as keyless that were MEASURED gated: %s. "
        "Anonymous GET returns %s. Either move it under the key-required "
        "heading, or open the gate and update _MEASURED_GATED with a fresh "
        "measurement." % (path, len(leaked), leaked,
                          {p: _MEASURED_GATED[p] for p in leaked}))


@pytest.mark.parametrize("path,open_h,close_h", _SURFACES)
def test_the_key_required_section_names_every_gated_endpoint(client, path, open_h, close_h):
    """Moved OUT is not the same as documented. An agent still needs the door."""
    body = client.get(path).get_data(as_text=True)
    section = body[body.find(close_h):]
    missing = sorted(p for p in _MEASURED_GATED if p not in section)
    assert not missing, (
        "%s removed %s from the keyless list but does not name them under %r — "
        "an agent that needs them now has no path at all." % (path, missing, close_h))
    assert "/api/v1/keys/claim" in section, (
        "%s: the key-required section does not say HOW to get a key. The claim "
        "endpoint is one POST with no email; omitting it turns a conversion "
        "moment into a dead end." % path)


def test_the_no_mcp_policy_does_not_send_a_keyless_agent_to_a_gated_endpoint(client):
    """This rule named `grid fuel-mix`, which 403s. That is how this was found.

    Scoped to the fetch instruction itself, not the whole file: /llms.txt names
    these paths elsewhere on purpose, under the key-required heading.

    ★ ANCHORED ON THE RULE'S TEXT, NOT ITS NUMBER (2026-09-20). It was
    `body.find("4. NO MCP…")`, and inserting one rule above it — the as_of rule —
    moved it to 5 and broke the anchor, i.e. a guard that fails on a renumbering
    it does not care about. The heading text is what identifies the rule; the
    slice runs to whatever numbered rule comes next.
    """
    body = client.get("/llms.txt").get_data(as_text=True)
    i = body.find("NO MCP, BUT YOU CAN FETCH URLS")
    assert i != -1, (
        "the no-MCP policy no longer carries a 'NO MCP, BUT YOU CAN FETCH URLS' "
        "rule — this guard anchors to it")
    m = re.search(r"\n\d+\. ", body[i:])
    assert m, "that rule is not followed by another numbered rule — the slice is wrong"
    rule = body[i:i + m.start()]
    named = sorted(p for p in _MEASURED_GATED if p.rsplit("/", 1)[-1] in rule)
    assert not named, (
        "the no-MCP policy tells an agent with no MCP to GET %s, which is "
        "gated. An agent that obeys gets a 403, and the fallback is the stale "
        "snapshot rule 1 exists to talk it out of." % named)


# ★ WHAT THIS FILE CANNOT DO, stated so the next reader does not assume it did:
#   it never calls the endpoints. _MEASURED_GATED is a snapshot, so OPENING a
#   gate makes this file wrong in the safe direction (it keeps an endpoint out
#   of the free list that could now be in it). The live half belongs in the
#   surface-truth shell, which already fetches these surfaces through the edge:
#   probe each URL in the keyless block anonymously and compare to this table.
