"""get_dchub_recommendation must not hand an agent a brochure as an answer.

★2026-08-29. `/api/agents/recommend` builds `recs`, a dict keyed by FOUR
LITERAL category strings, then does `recs.get(context, recs['general'])`. Every
natural-language context an agent actually sends misses and silently falls
through to the generic blurb.

Measured live against production that day:

    context="cheapest power in Texas for 50MW"          }  byte-identical
    context="lowest latency to New York City for finance"}  md5 8303ac30a35c3c6e

It is the #5 paid-demand tool — 244 calls / 95 distinct free users in 30d — and
it answered none of them. ★A silently-generic answer is worse than an error:
the error would have been retried. The agent cannot distinguish a brochure from
an answer, so it hands the brochure to its human as one.

The tool's tools/list DESCRIPTION also promised a return shape that has never
existed: top_markets[], candidate_facilities[], factor_breakdown{}, summary_text,
citation_url. None are emitted. That half is guarded here too, because the
description is what an agent reads BEFORE deciding to call.

This does NOT add a siting engine — site_selection_canvas / rank_markets already
are one. It makes the fallthrough VISIBLE and points at them.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CATEGORIES = ("general", "investment", "site-selection", "technical")


# ── the route: a free-text miss must announce itself ─────────────────

def _payload_keys_and_flags(src: str) -> str:
    """The route body, so these are static-source assertions that do not need
    a DB, a Flask app, or the network."""
    i = src.index("def api_agents_recommend()")
    return src[i:i + 12000]


@pytest.fixture(scope="module")
def route_src():
    return _payload_keys_and_flags((REPO / "main.py").read_text())


def test_a_free_text_miss_is_reported_not_hidden(route_src):
    """THE guard. matched_category must be None on a fallthrough and must be
    published, so an agent can branch on it."""
    assert "matched_category = context if context in recs else None" in route_src
    assert "'matched_category': matched_category" in route_src
    assert "'context_understood': matched_category is not None" in route_src


def test_the_generic_answer_is_labelled_as_generic(route_src):
    assert "'is_generic_answer'" in route_src
    assert "matched_category is None" in route_src


def test_a_miss_names_the_tools_that_do_compute_an_answer(route_src):
    """A dead end that names the next hop is not a dead end."""
    assert "'next_tools'" in route_src
    for tool in ("site_selection_canvas", "rank_markets",
                 "get_market_dcpi_rank", "analyze_site"):
        assert tool in route_src, tool


def test_the_categories_are_published_so_an_agent_can_retry(route_src):
    assert "'available_categories'" in route_src


# ── the description: stop promising a shape we never emit ────────────

@pytest.fixture(scope="module")
def tool_desc():
    w = (REPO / "worker.js").read_text()
    i = w.index('name: "get_dchub_recommendation"')
    return w[i:w.index("inputSchema", i)]


@pytest.mark.parametrize("phantom", [
    "top_markets", "candidate_facilities", "factor_breakdown",
    "summary_text", "citation_url",
])
def test_description_does_not_promise_a_field_we_never_return(tool_desc, phantom):
    """Each of these was in the shipped `Returns:` contract and has never been
    emitted by /api/agents/recommend."""
    assert phantom not in tool_desc, (
        f"tools/list still promises {phantom!r}; the route emits "
        f"recommendation/matched_category/top_pocket/related_intel/next_tools")


def test_description_states_the_four_literal_categories(tool_desc):
    for c in CATEGORIES:
        assert c in tool_desc, c


def test_description_warns_that_free_text_does_not_parse(tool_desc):
    low = tool_desc.lower()
    assert "does not parse" in low or "not parse" in low
    assert "context_understood" in tool_desc


def test_the_example_is_not_the_call_that_silently_fails(tool_desc):
    """The shipped example was itself the free-text form that falls through —
    the description demonstrated the broken usage as the correct one."""
    m = re.search(r"Example: (.{0,200})", tool_desc)
    assert m, "no Example: in description"
    example = m.group(1)
    assert any(c in example for c in CATEGORIES), \
        f"example must show a category that actually matches; got {example!r}"


# ── control ──────────────────────────────────────────────────────────

def test_must_fail_control_the_shipped_lookup_had_no_miss_signal():
    """CONTROL: the shipped one-liner carries no way to detect a fallthrough.
    If this ever stops being true the guards above are asserting nothing."""
    shipped = "    rec = recs.get(context, recs['general'])\n"
    assert "matched_category" not in shipped
    assert "context_understood" not in shipped
