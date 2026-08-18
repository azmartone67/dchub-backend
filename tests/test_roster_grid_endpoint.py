"""The MCP channel must reach the endpoint the /ai GRID actually reads.

★ 2026-08-03, the second half of a two-part mistake worth pinning:

  #2183 added mcp_calls_30d to the roster builder in ai_tracking.py. That
  builder serves /api/ai/platforms. It worked — smithery came back with
  crawler=0 and mcp_calls_30d=2,580, exactly as designed.

  But the /ai page's platform grid renders `data.platforms` from
  /api/ai/tracking, which is a DIFFERENT builder living in main.py
  (ai_tracking_full) and returning a DICT keyed by platform, not a list. So
  the column shipped, verified green on an endpoint nobody was looking at,
  and the grid still showed nothing.

  Right change, wrong surface — the wrong-table class, one layer up. The
  cheap guard is not "read more carefully"; it is a test that names the
  endpoint the consumer reads.

Source-level: DB tests skip in CI, so a DB-backed assertion here would be
green-by-absence on the branch that matters.
"""
import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "main.py").read_text()


def _tracking_route_body() -> str:
    """Source of the /api/ai/tracking handler — the grid's actual source."""
    i = SRC.index("@app.route('/api/ai/tracking'")
    # next route decorator ends this handler
    j = SRC.index("@app.route(", i + 40)
    return SRC[i:j]


def test_module_parses_and_the_route_is_found():
    # Guards every assertion below from passing vacuously.
    assert ast.parse(SRC)
    body = _tracking_route_body()
    assert len(body) > 500
    assert "def ai_tracking_full" in body


def test_the_grid_endpoint_publishes_the_mcp_channel():
    """This is the assertion whose absence let the column ship to the wrong
    endpoint and read as done."""
    body = _tracking_route_body()
    assert '"mcp_calls_30d"' in body, (
        "/api/ai/tracking must publish mcp_calls_30d — it is what the /ai "
        "platform grid renders")


def test_it_reuses_the_single_mapping_not_a_copy():
    """One mapping, imported. A second copy here would drift from the roster
    builder's the first time a client name changed."""
    body = _tracking_route_body()
    assert "get_mcp_calls_by_roster_platform" in body
    assert "canonical_platform" not in body, (
        "the mapping must be imported, not re-implemented in main.py")


def test_mcp_only_platforms_are_admitted():
    """A platform with real tool calls and zero crawler hits has no
    ai_cumulative row and would vanish from the grid. Smithery is exactly
    that case: crawler 0, MCP 2,580."""
    body = _tracking_route_body()
    assert "for _k, _n in _mcp30.items()" in body
    assert "if _k in platforms or not _n" in body


def test_it_fails_open():
    """A roster missing the MCP column is worse than yesterday's; a roster
    that 500s is worse than both."""
    body = _tracking_route_body()
    i = body.index("get_mcp_calls_by_roster_platform")
    # ★ 2026-08-18: this window was a fixed i+400 and went red when a comment
    # was added above the call — the fail-open branch had not moved, it had
    # merely fallen off the end of the window. A guard that fails on a comment
    # is a guard that gets deleted. Anchor on the except clause instead, which
    # is the thing being asserted about.
    j = body.index("except Exception", i)
    window = body[max(0, i - 400): j + 300]
    assert "try:" in window
    assert "_mcp30, _APM = {}, {}" in window
    # r-selftraffic-roster: the sibling figures must fail open too, or a failed
    # import publishes the FILTERED number with no unfiltered figure beside it.
    assert "_mcp30_gross, _mcp_excluded = {}, {}" in window


def test_every_row_gets_the_field_not_just_matched_ones():
    """A row without the key must publish 0, not omit the field — an absent
    field renders as 'undefined' and reads as a bug."""
    body = _tracking_route_body()
    assert 'for _k, _row in platforms.items()' in body
    assert '_row["mcp_calls_30d"] = int(_mcp30.get(_k, 0))' in body
