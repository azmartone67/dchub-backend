"""signals_per_session has a FLOOR set by the caller (2026-09-20).

/api/v1/admin/funnel/leakage published `signals_per_session` beside each tool
and its comment read a value near 1.0 as "many single-shot sessions ... not an
agent working a problem". On a BYO-MCP surface the HOST mints one session per
tool call, so 1.00 is the only value that ratio can take there and it says
nothing at all about the caller.

But "BYO means 1.00" is ALSO wrong, and the same board disproves it. Measured
on live `real_top_clients`, 30d, 2026-09-20 (signals / sessions):

    connectors-manager   200 / 200  ->   1.00    <- structurally pinned
    chatgpt               73 /   4  ->  18.25
    perplexity            84 /  14  ->   6.00
    grok                  22 /   5  ->   4.40
    smithery              92 /  21  ->   4.38

So the fix is not to apply the platform list as a rule -- it is to PUBLISH the
share (`byo_client_pct`) and let the reader judge which case a row is in.

These tests pin: the copied set, that it is reported and never subtracted, the
parameter order that would otherwise break the query at runtime, and the tuple
index the published field is read from.

CI-SAFETY: pure imports + source-level assertions, no network, no DB.
"""
import os
import re

from ai_platform_canon import BYO_MCP_PLATFORMS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _leak_query(src):
    """The top_leak_tools SELECT plus its execute() params, as source text.

    Anchored on `AS byo_client_signals` so it cannot silently match one of the
    three OTHER queries in this file that also close with
    `\"\"\", (f"{days} days",))`.
    """
    i = src.index("AS byo_client_signals")
    j = src.index("))", src.index('"""', i))
    return src[src.rindex("cur.execute(", 0, i):j + 2]


# -- the copied set --------------------------------------------------

def test_byo_set_is_pinned_to_a_literal():
    """This is a COPY of _BYO_MCP_PLATFORMS in dchub-mcp-server/server.mjs and
    no test in this repo can see that file -- the two repos share no module.
    Pinning the literal here does not prove the copies agree; it makes a
    silent edit to the Python side FAIL, so that whoever changes it has to go
    and look at the JS. Same reasoning as signal_class's 'unclassified'.
    """
    assert set(BYO_MCP_PLATFORMS) == {
        "chatgpt", "connectors-manager", "grok", "perplexity", "smithery",
    }


def test_the_canon_cites_where_the_real_definition_lives():
    src = _read("ai_platform_canon.py")
    assert "_BYO_MCP_PLATFORMS" in src and "server.mjs" in src, (
        "the copy must name its source, or the next reader cannot find it"
    )


def test_membership_is_not_documented_as_a_pin():
    """A future reader must not be able to take this set as 'these run at
    1.00'. The measured counterexample has to travel with the list."""
    src = _read("ai_platform_canon.py")
    assert "18.25" in src, "the chatgpt counterexample is what stops the rule"
    assert re.search(r"does NOT imply|never be used as if", src)


# -- reported, never subtracted --------------------------------------

def test_byo_is_a_filter_and_never_a_row_exclusion():
    """BYO surfaces carry real users (15 bound emails came through Smithery
    alone). If this list ever reaches the WHERE clause the board silently
    deletes real demand -- the exact mistake the smithery flag exists to
    avoid.
    """
    q = _leak_query(_read("routes/schema_repair.py"))
    where = q[q.index("FROM mcp_funnel_canonical"):]
    assert "%s" in where, "the interval param should still be here"
    assert "ANY(" not in where, (
        "BYO_MCP_PLATFORMS reached the outer WHERE: the board is now "
        "SUBTRACTING BYO traffic instead of reporting it"
    )
    assert "ANY(%s)" in q[:q.index("FROM mcp_funnel_canonical")]


# -- the runtime bug this guard exists for ---------------------------

def test_execute_params_are_in_placeholder_order():
    """psycopg2 binds %s positionally. The BYO array's placeholder sits ABOVE
    `INTERVAL %s` in the SELECT list, so it must be FIRST in the params tuple.
    Swap them and the query raises at runtime -- taking the whole board down,
    not just this field.
    """
    q = _leak_query(_read("routes/schema_repair.py"))
    sql = q[:q.rindex('"""')]
    assert sql.count("%s") == 2, f"expected exactly 2 placeholders, got {sql.count('%s')}"
    assert sql.index("ANY(%s)") < sql.index("INTERVAL %s")

    params = q[q.rindex('"""') + 3:]
    assert params.index("BYO_MCP_PLATFORMS") < params.index("days"), (
        "params tuple is in the wrong order for the placeholders above"
    )


def test_published_field_reads_the_column_it_added():
    """byo_client_signals is the 7th column selected, so it is r[6]. Reading
    any other index publishes a different number under this name.
    """
    src = _read("routes/schema_repair.py")
    q = _leak_query(src)
    sql = q[:q.rindex('"""')]
    cols = re.findall(r"\bAS ([a-z_]+)", sql)
    assert cols[-1] == "byo_client_signals"
    # tool_requested is selected without an alias, hence the +1
    assert len(cols) + 1 == 7, f"column count moved: {cols}"
    assert '"byo_client_pct": _safe_ratio(100 * r[6], r[1], 1)' in src


def test_the_floor_is_published_where_a_reader_will_see_it():
    """A caveat only this file's authors read is not a caveat. It has to ride
    in the RESPONSE, next to the number it qualifies."""
    src = _read("routes/schema_repair.py")
    note = src[src.index('out["composition_note"]'):]
    note = note[:note.index("out[", 10)]
    assert "byo_client_pct" in note and "FLOOR" in note
    assert "200" in note and "18.25" in note, (
        "the note asserts a floor; it has to carry the measurement AND the "
        "counterexample or it is just another unfalsifiable claim"
    )
