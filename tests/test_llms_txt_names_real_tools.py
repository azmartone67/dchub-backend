#!/usr/bin/env python3
"""tests/test_llms_txt_names_real_tools.py — llms.txt must never teach a tool
that does not exist, and its literal-names warning must stay true.

NO NETWORK, NO DB.

WHAT HAPPENED (measured 2026-09-06). Two AI assistants were asked why they do
not call DC Hub's MCP tools. Both answered with a connector manifest for DC Hub
naming `get_grid_intel` and `site_selection` as capabilities. Live tools/list
(83 tools) has NEITHER. Neither assistant had called the server — both stated
they have no network access — so the names came from reading THIS FILE and
normalising it.

The catalog invites that mistake: `get_fiber_intel` and `get_grid_intelligence`
are siblings five lines apart that abbreviate differently. An agent that tidies
the list up is wrong whichever direction it tidies.

An agent that follows such a manifest gets tool-not-found and concludes DC Hub
does not work. Same failure family as the <em>-eats-underscores defect (#3948):
the published surface teaches a tool that is not there.

TWO GUARDS, POINTING OPPOSITE WAYS:
  1. every tool named in the flagship list must EXIST in the catalog
  2. the three names the warning block calls NOT-tools must NOT exist —
     so renaming a tool into one of them turns the warning into a lie and
     fails here rather than shipping.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "ai_discovery_routes.py")
sys.path.insert(0, ROOT)

from routes.mcp_tool_catalog import _merged_tools  # noqa: E402

CATALOG = {name for name, _cat, _tier, _summary, _ex in _merged_tools()}

# The names the warning block teaches as WRONG. Kept here so deleting the block
# from llms.txt fails a test rather than passing one.
NOT_TOOLS = {
    "get_grid_intel": "get_grid_intelligence",
    "get_fiber_intelligence": "get_fiber_intel",
    "site_selection": "site_selection_canvas",
}


def _llms_body():
    """The llms.txt template only, comments stripped."""
    s = open(SRC, encoding="utf-8").read()
    i = s.index("def serve_llms_txt(")
    j = s.index("\n    @app.route(", i + 10)
    return "\n".join(l for l in s[i:j].splitlines()
                     if not l.lstrip().startswith("#"))


# "- tool_name -> what it returns" is the shape of the flagship list; it is the
# only place llms.txt asserts a name is callable.
BULLET = re.compile(r"^- ([a-z][a-z0-9_]{4,}) -> ", re.M)


def test_catalog_and_bullets_are_non_empty():
    """Floor. Without this, an import that yields nothing or a regex that stops
    matching makes every assertion below range over an empty set and pass."""
    assert len(CATALOG) > 50, f"tool catalog collapsed to {len(CATALOG)}"
    names = BULLET.findall(_llms_body())
    assert len(names) > 10, f"flagship bullet regex matched {len(names)}"


def test_every_tool_named_in_llms_txt_exists():
    unknown = sorted({n for n in BULLET.findall(_llms_body()) if n not in CATALOG})
    assert not unknown, (
        f"llms.txt teaches tool name(s) that do not exist: {unknown}. "
        "An agent that copies one gets tool-not-found and concludes DC Hub is broken."
    )


def test_the_warning_block_is_still_true():
    """The block names three non-tools. If a real tool is ever renamed to one of
    them the warning becomes false — catch that here, not in a partner's manifest."""
    for wrong, right in NOT_TOOLS.items():
        assert wrong not in CATALOG, (
            f"llms.txt tells agents `{wrong}` is not a tool, but it now is. "
            "Update the warning block."
        )
        assert right in CATALOG, (
            f"llms.txt points agents at `{right}` as the real name, and it is gone."
        )


def test_llms_txt_carries_the_warning_and_the_catalog_pointer():
    body = _llms_body()
    for wrong, right in NOT_TOOLS.items():
        assert re.search(rf"{wrong}\b.*\b{right}\b|{right}\b.*\b{wrong}\b", body), (
            f"llms.txt no longer contrasts {wrong} with {right}"
        )
    # The excerpt above is 15 of 83 tools. The complete machine-readable catalog
    # is what a manifest should be generated from, and llms.txt named no such
    # pointer at all before 2026-09-06.
    assert "/.well-known/mcp.json" in body, (
        "llms.txt does not point at the full tool catalog, so an agent building "
        "a connector manifest has only the 15-tool excerpt to work from."
    )
