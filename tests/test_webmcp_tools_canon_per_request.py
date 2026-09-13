"""The WebMCP tools registered on /integrations follow canon PER REQUEST.

2026-09-13, measured live: the /integrations page body carried the live
facility floor five times, while the `search-datacenter-facilities` WebMCP tool
registered on that same page carried the cold-start pinned floor. The tool's
description was a canon_text() call nested INSIDE the module-level
_WEBMCP_TOOLS list, so it ran once at import, and
tests/test_canon_resolved_per_request.py could not see it: that scanner only
looks at `X = canon_text(...)` assignments.

A value comparison cannot catch this class (the test would read the same
latched value the page does). The check that separates derived from frozen is
to MOVE canon between two renders and require the served tool to follow.
"""
import json
import re

import pytest

_TOOL = "search-datacenter-facilities"


def _canon(facilities):
    def fake(text):
        return text.replace("{canon_facilities}", facilities)
    return fake


def _served_description(html):
    """The description string the page registers for the search tool."""
    m = re.search(r'name:("%s"),description:("(?:[^"\\]|\\.)*")' % re.escape(_TOOL), html)
    assert m, (
        "the rendered /integrations page registers no %s WebMCP tool — the "
        "injector changed shape, and this guard would otherwise pass on a page "
        "that carries no description at all" % _TOOL)
    return json.loads(m.group(2))


def test_the_search_tool_description_is_a_template_not_a_frozen_value():
    import routes.integrations_landing as il
    tool = next(t for t in il._WEBMCP_TOOLS if t["name"] == _TOOL)
    assert "@@CANON_FAC@@" in tool["description"], (
        "_WEBMCP_TOOLS carries a RESOLVED facility figure again: whatever canon "
        "said when the worker imported this module is what agents will read")


def test_the_served_tool_follows_canon_between_requests(monkeypatch):
    pytest.importorskip("flask")
    import routes.integrations_landing as il

    monkeypatch.setenv("WEBMCP_ORIGIN_TRIAL_TOKEN", "test-origin-trial-token")

    monkeypatch.setattr(il, "canon_text", _canon("FLOOR-BEFORE-WALK"))
    before = _served_description(il.integrations_mcp()[0])
    monkeypatch.setattr(il, "canon_text", _canon("FLOOR-AFTER-WALK"))
    after = _served_description(il.integrations_mcp()[0])

    assert "FLOOR-BEFORE-WALK" in before, before
    assert "FLOOR-AFTER-WALK" in after, (
        "canon moved between two requests and the served WebMCP tool did not "
        "follow: %r" % after)
    assert "{canon_" not in after, after
