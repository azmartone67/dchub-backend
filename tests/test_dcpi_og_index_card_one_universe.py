"""r-one-dcpi-universe — the index social card must not define "a scored
market" for itself.

`/dcpi` and `/api/v1/dcpi/scores` both read the PUBLISHED rows of
market_power_scores. `/dcpi/og.svg` ran its own
`SELECT COUNT(*) FROM (SELECT DISTINCT ON (market_slug) ...)` over the same
table with no `published` predicate, so it counted the alias-twin rows
r-twin-unpublish retires — rows deliberately kept in the table (the flag is a
VISIBILITY bit, not a delete, so direct links still resolve) — as live markets.
Both numbers the card drew were inflated: the market count and the BUILD count.

Two competing definitions of the same thing, on a surface social platforms and
AI crawlers scrape — so the wrong one was the more widely quoted.

These tests read the shipped source rather than importing main.py, per the house
rule, and they assert the CONTRACT a future refactor would break: the card owns
no query of its own, it reads the shared accessor, and that accessor is the
published universe. Deleting a leg here re-opens the drift.
"""
import ast
import copy
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "routes" / "dcpi.py"

# The index card route, and the shared accessor that owns the universe.
INDEX_CARD_ROUTE = "/dcpi/og.svg"
SHARED_ACCESSOR = "_scores_rows_cached"
UNIVERSE_QUERY_FN = "_fetch_scores_rows"
SCORES_TABLE = "market_power_scores"


@pytest.fixture(scope="module")
def src():
    return SRC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(src):
    return ast.parse(src)


def _fn_source(src, tree, route=None, name=None):
    """The CODE of the function under `route` (or named `name`) — no prose.

    ★ Deliberately AST-unparsed with the docstring dropped, not sliced out of
    the file as text. The first cut of this fence read raw source, and the
    comment in the fix that EXPLAINS why the card must not query the scores
    table contains the table's name — so the ban below failed on the very
    change it exists to protect. A guard that a comment can trip is a guard
    that gets "fixed" by rewording the comment.

    String literals survive unparsing, so a real SQL query is still visible;
    only the prose is gone.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if name is not None and node.name != name:
            continue
        if route is not None and not any(
                route in ast.dump(dec) for dec in node.decorator_list):
            continue
        node = copy.deepcopy(node)
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            node.body.pop(0)          # docstring is prose too
        return node.name, ast.unparse(node)
    return None, None


def test_the_index_card_renderer_is_found(src, tree):
    """If this route is renamed or moved, the rest of the file proves nothing."""
    name, body = _fn_source(src, tree, route=INDEX_CARD_ROUTE)
    assert body, f"no function is registered under {INDEX_CARD_ROUTE}"
    assert "markets" in body, (
        f"{name} no longer draws a market count — move this fence with it")


def test_index_card_owns_no_query_over_the_scores_table(src, tree):
    """The defect, stated directly: a second, independent count."""
    name, body = _fn_source(src, tree, route=INDEX_CARD_ROUTE)
    assert SCORES_TABLE not in body, (
        f"{name} queries {SCORES_TABLE} directly again. The index card must "
        f"read {SHARED_ACCESSOR}() — a second query is a second definition of "
        f'"a scored market", and it drifts from the page silently.')


def test_index_card_reads_the_shared_published_universe(src, tree):
    """Positive form: the fix must be PRESENT, not merely the defect absent.

    Without this, deleting the card's body entirely would pass the test above.
    """
    name, body = _fn_source(src, tree, route=INDEX_CARD_ROUTE)
    assert f"{SHARED_ACCESSOR}()" in body, (
        f"{name} does not read {SHARED_ACCESSOR}() — it is no longer bound to "
        f"the universe /dcpi and /api/v1/dcpi/scores serve.")


def test_the_shared_accessor_is_the_published_universe(src, tree):
    """Bind the fence to WHY the number is right, not just to the plumbing.

    The card is only correct because the accessor it now reads is filtered to
    published rows. If that predicate is ever dropped, the card silently
    inherits the retired alias-twins again — and the two surfaces would agree
    on the WRONG number, which is worse than disagreeing.
    """
    name, body = _fn_source(src, tree, name=UNIVERSE_QUERY_FN)
    assert body, f"{UNIVERSE_QUERY_FN} is gone; the card's universe is unpinned"
    assert SCORES_TABLE in body, (
        f"{UNIVERSE_QUERY_FN} no longer reads {SCORES_TABLE}")
    normalized = " ".join(body.split()).lower()
    assert "where published = true" in normalized, (
        f"{UNIVERSE_QUERY_FN} no longer filters to published rows. Every "
        f"surface reading it — including {INDEX_CARD_ROUTE} — would start "
        f"counting the retired alias-twin rows as live markets.")
