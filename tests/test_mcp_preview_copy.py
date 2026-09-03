"""tests/test_mcp_preview_copy.py — a preview may not serve its own placeholders.

MEASURED LIVE 2026-09-03, GET /api/v1/mcp/preview/get_grid_intelligence:

    "sample_answer": "PJM (2026-05-25): {load_mw} MW load, {reserve_pct}%
                      reserve, {verdict} verdict. Updated every 90s. ..."

That is the anonymous-agent-facing surface — the one page whose whole job is to
show a caller what it would unlock — handing back the format string. It shipped
2026-05-25 and served that for 101 days.

Three compounding reasons it could never have worked:
  · nothing in routes/mcp_funnel_upgrade.py ever calls .format() on
    `sample_answer_template`; line ~198 assigns it straight to the response;
  · `load_mw` and `reserve_pct` are not fields _live_preview_for() selects, so
    even a .format() call would have raised KeyError;
  · the hardcoded "2026-05-25" contradicted the "Updated every 90s" in its own
    sentence.

Four of the five entries happened to be fully literal, which is exactly why
this stayed invisible — the defect is only visible on the one entry that used
a placeholder. So the guard here is on the CLASS, not that entry.

Also fenced: the copy may not promise fields the payload does not carry. That
rule is already written into this manifest by hand on get_fiber_intel
("per-route lit capacity not tracked"); this makes it enforceable for the one
field we can check automatically — live_sample has no MW load, so the grid
entry must not promise a MW figure.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_mcp_preview_copy.py -v
"""
from __future__ import annotations

import inspect
import re

import pytest

from routes import mcp_funnel_upgrade as mfu

PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
TOOLS = sorted(mfu._TOOL_PREVIEWS)


@pytest.mark.parametrize("tool", TOOLS)
def test_no_preview_string_contains_an_unfilled_placeholder(tool):
    """★ THE CLASS GUARD. Nothing calls .format() on these, so a {placeholder}
    is served verbatim to an anonymous agent."""
    meta = mfu._TOOL_PREVIEWS[tool]
    for field in ("sample_answer_template", "you_unlock", "sample_question",
                  "category"):
        val = str(meta.get(field) or "")
        found = PLACEHOLDER.findall(val)
        assert not found, (
            "%s.%s ships %s — nothing formats these strings, so that reaches "
            "the caller as literal text: %r" % (tool, field, found, val))


def test_nothing_actually_formats_the_template():
    """The premise of the guard above. If a .format() call is ever added, this
    fails and the guard should be reconsidered rather than silently kept.

    ★ Keyed on AST, not text. The first draft grepped the source for
      ".format(" and failed on its OWN explanatory comment — the guard-writing
      trap this repo has now hit three times. A comment describing the defect
      is not the defect."""
    import ast

    tree = ast.parse(inspect.getsource(mfu))
    formats = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == "format"]
    assert not formats, (
        "a .format() call appeared in this module (line %s) — someone may have "
        "taught the manifest to substitute; re-read "
        "test_no_preview_string_contains_an_unfilled_placeholder before "
        "relaxing it" % (formats[0].lineno if formats else "?"))


def test_the_key_is_a_misnomer_and_says_so():
    """`sample_answer_template` formats nothing. Renaming it would touch every
    entry and the reader; naming the lie in a comment is the cheap honest
    alternative, and this test keeps that comment present."""
    src = inspect.getsource(mfu)
    head = src.split("_TOOL_PREVIEWS = {")[0][-700:]
    assert "format()" in head and "FINISHED sentence" in head


@pytest.mark.parametrize("tool", TOOLS)
def test_no_hardcoded_date_in_a_feed_that_claims_to_be_live(tool):
    """"PJM (2026-05-25): ... Updated every 90s" contradicted itself. A frozen
    date in copy about a live feed is stale the day after it ships."""
    val = str(mfu._TOOL_PREVIEWS[tool].get("sample_answer_template") or "")
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", val), (
        "%s pins a date into live-feed copy: %r" % (tool, val))


def test_the_grid_entry_promises_only_what_live_sample_carries():
    """PROVENANCE HONESTY — the rule this manifest already states by hand on
    get_fiber_intel. _live_preview_for() returns market_name, verdict,
    excess_power_score and reserve_margin_pct. There is no MW load in it, so
    the sample answer must not offer a MW figure."""
    val = mfu._TOOL_PREVIEWS["get_grid_intelligence"]["sample_answer_template"]
    assert not re.search(r"\d+\s*MW", val), (
        "live_sample carries no MW load — promising one is the fabricated-"
        "number shape: %r" % val)
    assert "reserve margin" in val.lower() and "verdict" in val.lower()


def test_the_queue_tool_the_board_named_now_has_a_preview():
    """plead_product_gap:get_interconnection_queue — the preview endpoint
    answered 404, so the surface meant to show an anonymous agent what it
    would unlock did not exist."""
    assert "get_interconnection_queue" in mfu._TOOL_PREVIEWS
    meta = mfu._TOOL_PREVIEWS["get_interconnection_queue"]
    for f in ("category", "you_unlock", "sample_question",
              "sample_answer_template"):
        assert str(meta.get(f) or "").strip(), "missing %s" % f


def test_the_queue_figures_are_the_ones_the_snapshot_published():
    """Every number in that copy came from
    /api/v1/interconnection-queue/snapshot read live 2026-09-03: NESO
    queued_load_total_gw=600.1, queued_load_data_center_gw=53.1,
    queued_load_dc_share_pct=8.8. Pinned so a future edit cannot drift the
    figures away from their source without this failing."""
    val = mfu._TOOL_PREVIEWS["get_interconnection_queue"]["sample_answer_template"]
    for fig in ("600.1", "53.1", "8.8"):
        assert fig in val, "%s is not the published figure set: %r" % (fig, val)
    assert "NESO" in val and "source" in val.lower()
