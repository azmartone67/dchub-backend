"""GET /mcp — the capabilities payload — must NAME Capacity Source.

WHY THIS EXISTS. GET /mcp (x-dc-hub-source: worker-mcp-get-health) is the first
thing an agent reads about DC Hub, before it ever calls tools/list, and it is the
most-crawled path on the domain. Measured 2026-09-16 it contained no mention of
"Capacity Source", "source_capacity", "listings" or "off-market", while Capacity
Source was LIVE (GET /api/v1/listings/summary reported program_status live) and
was already named on README, server.json, the official registry listing,
mcp-server.json, the GitHub About text, the Smithery copy and /capabilities.
Every surface an agent might read second said so; the one it reads FIRST did not.

★ WHAT THIS GUARD PINS, AND WHAT IT DELIBERATELY DOES NOT.

  It pins the sentence BYTE FOR BYTE. The wording is owner-given and already
  published from two other literals:

    - CAPACITY_BLURB          dchub-mcp-server lib/capacity-source-summary.mjs
                              (on main; pinned there by
                              test/capacity-source-pointers.test.mjs)
    - CAPACITY_SOURCE_BLURB   dchub-backend routes/mcp_presence_crawler.py
                              (NOT on main as of 2026-09-16 — it arrives with
                              PR #4651; pinned there by
                              tests/test_registry_capacity_blurb.py)

  Three surfaces, three literals, because they live in two repos and one of them
  is a JS file pasted into Cloudflare by hand. This guard cannot import any of
  them, so it carries its own copy and FAILS if worker.js drifts from it. When
  #4651 lands, the constant it adds must equal BLURB below.

  It also pins COUNT-FREENESS, which matters more here than on the registries.
  This response is read at the edge; a live_count or an MW total baked into it
  goes stale inside a cache no listing write invalidates. The sentence names the
  capability, the tool and the page — all three outlive any inventory.

★ SCOPE. The assertions read the GET /mcp response object ONLY — located from
  its own 'worker-mcp-get-health' header back to the `new Response(JSON.stringify`
  that opens it. worker.js is 4,300 lines and names source_capacity in several
  other places (MCP_FALLBACK_TOOLS carries its full toolspec), so a whole-file
  substring search for "source_capacity" would pass with the payload untouched —
  it would be satisfied by the manifest entry alone and pin nothing.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO, "worker.js")

# The owner-given sentence, byte for byte. Keep equal to CAPACITY_BLURB /
# CAPACITY_SOURCE_BLURB; see the module docstring.
BLURB = ("Capacity Source: powered land/shell/turnkey incl. off-market listings "
         "via source_capacity; browse dchub.cloud/listings.")

_MARKER = "'worker-mcp-get-health'"
_OPENER = "return new Response(JSON.stringify({"


def _read():
    with open(WORKER, encoding="utf-8") as fh:
        return fh.read()


def _mcp_get_payload(src):
    """The GET /mcp response object: from the `new Response(JSON.stringify({`
    that opens it to the header block that identifies it."""
    end = src.find(_MARKER)
    assert end != -1, (
        f"{_MARKER} not found in worker.js — the GET /mcp health handler is "
        "gone or its x-dc-hub-source marker was renamed. This guard cannot "
        "scope itself without it."
    )
    start = src.rfind(_OPENER, 0, end)
    assert start != -1, (
        "found the worker-mcp-get-health marker but no `new Response("
        "JSON.stringify({` opening a payload before it."
    )
    block = src[start:end]
    # Guard the guard: a window that swallowed half the file would make every
    # assertion below vacuous.
    assert len(block) < 6000, (
        f"GET /mcp payload window is {len(block)} bytes — too large to be just "
        "that response. The opener or marker probably moved; re-scope before "
        "trusting anything this file asserts."
    )
    return block


def _product_value(block):
    m = re.search(r"\n\s*product:\s*'((?:[^'\\]|\\.)*)'", block)
    assert m, "no `product:` single-quoted string in the GET /mcp payload"
    return m.group(1)


def test_get_mcp_payload_is_locatable_and_tight():
    """If this fails, every other test here is measuring the wrong bytes."""
    block = _mcp_get_payload(_read())
    assert "product:" in block
    assert "tools_sample:" in block


def test_product_names_capacity_source_in_the_owner_given_words():
    product = _product_value(_mcp_get_payload(_read()))
    assert BLURB in product, (
        "GET /mcp `product` does not carry the Capacity Source sentence byte "
        "for byte.\n"
        f"  expected to contain: {BLURB!r}\n"
        f"  product reads:       {product!r}\n"
        "Reuse the literal — do not paraphrase it. It is CAPACITY_BLURB in "
        "dchub-mcp-server lib/capacity-source-summary.mjs."
    )


def test_product_still_describes_the_data_layer():
    """Naming Capacity Source must ADD to the description, not replace it."""
    product = _product_value(_mcp_get_payload(_read()))
    for kept in ("DATA LAYER", "DCPI", "interconnection queues", "tracked M&A"):
        assert kept in product, (
            f"`product` lost {kept!r}. Capacity Source was meant to extend this "
            "sentence, not overwrite what the data layer already offered."
        )


def test_product_stays_count_free():
    """No digits in `product`. A count here is cached at the edge and goes
    stale invisibly — 'live_count 2' and '41.2 MW' were true on 2026-09-16 and
    are not a promise about tomorrow."""
    product = _product_value(_mcp_get_payload(_read()))
    digits = re.findall(r"\d", product)
    assert not digits, (
        f"`product` contains digits {digits} — it must stay count-free:\n"
        f"  {product!r}\n"
        "Inventory numbers belong at a canonical URL that can be re-read, not "
        "baked into a payload served from cache."
    )


def test_tools_sample_names_source_capacity():
    """Prose tells an agent the product exists; tools_sample gives it a name it
    can actually CALL."""
    block = _mcp_get_payload(_read())
    m = re.search(r"const want = \[(.*?)\];", block, re.S)
    assert m, "tools_sample's `want` array not found in the GET /mcp payload"
    want = re.findall(r"'([a-z_]+)'", m.group(1))
    assert "source_capacity" in want, (
        f"tools_sample's curated list does not name source_capacity: {want}"
    )


def test_tools_sample_cap_cannot_evict_a_curated_tool():
    """The curated list is filtered, then sliced. If the slice is shorter than
    the list, adding a tool silently drops one — the exact quiet failure the
    payload's own comment warns about."""
    block = _mcp_get_payload(_read())
    m = re.search(r"const want = \[(.*?)\];", block, re.S)
    want = re.findall(r"'([a-z_]+)'", m.group(1))
    s = re.search(r"out\.slice\(0,\s*(\d+)\)", block)
    assert s, "tools_sample's slice cap not found"
    cap = int(s.group(1))
    assert cap >= len(want), (
        f"tools_sample slices to {cap} but curates {len(want)} tools — "
        f"{want[cap:]} would be dropped without a word. Move the cap with the "
        "list."
    )
