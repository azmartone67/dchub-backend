"""Pins the two GA attach paths on /integrations/copilot-studio.

A 2026-07-31 audit found the live page carried ZERO occurrences of 'wizard',
'x-ms-agentic-protocol', 'mcp-streamable' and 'custom connector' — neither of
the attach methods Microsoft documents for GA MCP support (the Tools onboarding
wizard, and the pro-dev Power Platform custom connector built from an OpenAPI
spec tagged x-ms-agentic-protocol: mcp-streamable-1.0) was described anywhere
on the page that exists to describe them.

These tests pin both paths on the RENDERED page, keep the tool count
canon-bound (ai_surface_canon.PINNED, never a fresh literal — the
dchub-mcp-server #108/#112 rule), and keep the front-door guidance
(execute_plan first, plan_query inspect-only) that every recipe page carries.

Pure functions: no DB, no network, and never imports main (tests/ must not).
"""
import pytest

il = pytest.importorskip("routes.integrations_landing")


def test_wizard_path_is_described():
    html = il.COPILOT_RECIPE_HTML
    assert "wizard" in html
    assert "Model Context Protocol server" in html
    assert "Streamable HTTP" in html
    assert "https://dchub.cloud/mcp" in html


def test_custom_connector_alternative_carries_protocol_tag():
    html = il.COPILOT_RECIPE_HTML
    assert "custom connector" in html
    assert "x-ms-agentic-protocol: mcp-streamable-1.0" in html
    # The OpenAPI snippet must point the tagged operation at the real endpoint.
    assert "host: dchub.cloud" in html
    assert "/mcp:" in html


def test_tool_count_is_canon_bound_not_a_fresh_literal():
    canon = pytest.importorskip("ai_surface_canon")
    html = il.COPILOT_RECIPE_HTML
    assert "__CANON_TOOLS_APPEAR__" not in html  # placeholder substituted
    assert f"all {canon.PINNED['tools_advertised']} DC Hub tools appear" in html


def test_front_door_guidance_survives():
    # The operator-prompt/front-door block must stay: execute_plan first,
    # plan_query inspect-only. _recipe_page injects it unconditionally; this
    # pins that the Copilot Studio page still renders it.
    html = il.COPILOT_RECIPE_HTML
    assert 'id="front-door"' in html
    assert "execute_plan" in html
    assert "plan_query" in html
