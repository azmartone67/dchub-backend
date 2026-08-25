"""`search` must be an accepted query alias on every facility-listing path.

2026-08-25, measured live against https://dchub.cloud/api/v1/facilities:

    limit=3                          total_matching=17170   <- baseline
    limit=3&search=Hyperion          total_matching=17170   <- identical
    limit=3&search=zzzznotarealplace total_matching=17170   <- gibberish, identical
    limit=3&q=Hyperion               total_matching=11      <- the real param

`search` was the one name the alias chain did not accept, so the filter was
silently dropped and the endpoint returned the entire fleet. A dropped filter
that 400s is visible; one that returns every row reads to an agent as "all
17,170 matched" — a wrong answer delivered with full confidence.

Asserts on the AST of the alias chain, not on a comment: a guard that reads
prose passes on deleted behaviour.
"""
import ast
import io
import os

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")

# Every name a caller may use for the free-text query on a facility listing.
# `market` predates the 08-25 fix and is locked in too: dropping ANY accepted
# alias fails the same silent way — the caller's filter is ignored and the full
# fleet comes back looking like a match.
REQUIRED_ALIASES = {"q", "query", "search", "market"}


def _query_alias_chains():
    """Alias-name sets for every `q = (request.args.get(...) or ...).strip()`."""
    tree = ast.parse(io.open(MAIN, encoding="utf-8").read())
    chains = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "q"):
            continue
        names = set()
        for sub in ast.walk(node.value):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get"
                    and isinstance(sub.func.value, ast.Attribute)
                    and sub.func.value.attr == "args"):
                if sub.args and isinstance(sub.args[0], ast.Constant):
                    names.add(sub.args[0].value)
        if names:
            chains.append((node.lineno, names))
    return chains


def test_the_chains_are_actually_found():
    """Guard the guard — an AST drift here would make the rest vacuous."""
    chains = _query_alias_chains()
    assert len(chains) >= 2, (
        "expected the free AND full listing paths to parse a query alias chain; "
        "found %d" % len(chains)
    )


def test_search_is_accepted_on_every_listing_path():
    for lineno, names in _query_alias_chains():
        missing = REQUIRED_ALIASES - names
        assert not missing, (
            "main.py:%d accepts %s but not %s — a caller using a missing name gets "
            "the UNFILTERED fleet back, not an error." % (lineno, sorted(names), sorted(missing))
        )
