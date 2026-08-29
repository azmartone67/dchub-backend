"""tests/test_facilities_country_filter_is_legible.py — ?country=United States
must not silently return zero facilities (2026-08-28).

Measured live on production BEFORE the fix, same key, same limit:

    /api/v1/facilities?country=US             ->  rows,  success: true
    /api/v1/facilities?country=United States  ->  0 rows, success: true
    /api/v1/facilities?country=us             ->  0 rows, success: true   <-- ALSO
    /api/v1/facilities?country=USA            ->  0 rows, success: true

`facilities.country` holds alpha-2 codes and the query parameter went into
`AND country = %s` raw and CASE-SENSITIVE, so every non-canonical shape matched
nothing. The response was `success: true` with an empty `data` — SUCCESS, EMPTY
— so an agent reads "DC Hub has no US data centers" and has no way to tell that
from "you used the wrong format". `search_facilities` is the #1 tool by agent
count and "United States" is the form an LLM reaches for first.

Same failure shape as util/state_codes.py (?state=TX -> [] because the column
held FIPS): not an error a caller can handle, but a confident wrong answer.

★WHY THE FIX IS NOT `canon_country()`. That is the WRITE-path rule and its alias
table already contains "UNITED STATES" -> "US". Routing the read path through it
would have SILENTLY ANSWERED A DIFFERENT QUESTION than the one asked — the caller
sends a value the tool schema calls invalid and gets rows as if it were fine. The
read path NAMES the bad filter instead. `canon_country` still supplies the
suggestion ("try US"), sourced from the one table, never applied.

★WHY `us` IS ACCEPTED BUT `USA` IS NOT. Folding case is lossless — `us` and `US`
are the same ISO code. Mapping a NAME to a code is a guess. Only the first is
done. Rejecting `us` would have converted a silent-empty into a wrong error.

THE CONTRACT
────────────
  C1. A valid alpha-2 resolves to the upper-cased code and NO error (the arm
      that must keep returning rows).
  C2. Case is folded: `us`, ` Us ` resolve to `US`.
  C3. A full country name yields an ERROR, not a code — and never `data: []`
      with `success: true`.
  C4. The error NAMES the parameter, the expected format, and the offending
      value, and suggests the code when the SoT knows one.
  C5. An absent/empty country is not an error — it means "do not filter".
  C6. BOTH facility listing paths in main.py — `_list_facilities_full`
      (authed) and `_list_facilities_free` — route `country` through
      `country_filter` and return before any query when it errors. The free
      path is a real twin, not a copy that got missed.
  C6b. Both paths answer 400 (read off the AST, not grepped —
      the first draft's grep was satisfied by a comment).
  C7. The valid-country path still reaches the SQL filter, so the guard does
      not break the working arm.

House rules: no DB, no network, never import main.py — it raises
"No database URL configured" at import. C6/C7 are therefore asserted
STRUCTURALLY over main.py's AST, which is also the only way to see a SQL
predicate a fake cursor would not model.

Run:  python3 -m pytest tests/test_facilities_country_filter_is_legible.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from util.country_codes import country_filter

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")

FACILITY_LISTERS = ("_list_facilities_full", "_list_facilities_free")


def _func_node(name):
    """AST node of one top-level function in main.py."""
    tree = ast.parse(MAIN_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(
        "main.py has no function %r — it was renamed or deleted, and this "
        "guard was silently covering nothing." % name
    )


def _func_source(name):
    """Source text of one top-level function in main.py, via AST.

    Parsing (not grepping) so a match cannot come from a comment or from a
    same-named helper in another module.
    """
    tree = ast.parse(MAIN_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(MAIN_SRC, node)
            assert seg, "no source segment for %s" % name
            return seg
    raise AssertionError(
        "main.py has no function %r — it was renamed or deleted, and this "
        "guard was silently covering nothing." % name
    )


# ─── C1/C2: the arm that must keep returning rows ────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("US", "US"),        # the working call from the live probe
    ("us", "US"),        # C2 — the SECOND silent-empty this fix closes
    (" Us ", "US"),
    ("GB", "GB"),
    ("sg", "SG"),
])
def test_valid_alpha2_resolves_and_does_not_error(raw, expected):
    code, err = country_filter(raw)
    assert err is None, "a valid alpha-2 must not error: %r -> %r" % (raw, err)
    assert code == expected


# ─── C3/C4: the arm that must stop being a confident empty ───────────────

@pytest.mark.parametrize("raw", [
    "United States",     # the reported bug, verbatim
    "united states",
    "USA",               # alpha-3
    "United Kingdom",
    "Freedonia",         # unknown to the alias table — still must not pass
    "U1",
])
def test_non_alpha2_errors_instead_of_filtering(raw):
    code, err = country_filter(raw)
    assert code is None, (
        "%r must NOT become a usable filter — returning a code here is the "
        "silent-coercion this fix exists to avoid" % raw
    )
    assert err, (
        "%r produced NO error: the handler would fall through to "
        "`AND country = %%s` and answer success/[] — the exact confident "
        "empty this guard exists to prevent" % raw
    )


def test_the_reported_case_names_the_problem_and_suggests_the_code():
    """C4 — measured verbatim: country='United States' returned 0 rows."""
    code, err = country_filter("United States")
    assert code is None
    assert "alpha-2" in err
    assert "United States" in err, "the error must quote the offending value"
    assert " — try US" in err, "the SoT knows this alias — say the code to use"


def test_an_unknown_name_still_errors_without_inventing_a_suggestion():
    """The suggestion comes from the alias table or not at all — never a guess."""
    code, err = country_filter("Freedonia")
    assert code is None
    assert "alpha-2" in err
    # NOT the bare needle "try" — that also matches "coun\u200btry expects".
    assert " — try " not in err, "no alias is known — the error must not invent one"


def test_full_name_is_not_silently_coerced_even_though_canon_country_knows_it():
    """★The heart of it. canon_country('United States') == 'US'.

    If the read path ever routes through canon_country, this test goes red —
    the caller would get rows for a question they did not ask.
    """
    from util.country_codes import canon_country
    assert canon_country("United States") == "US", (
        "premise changed: the alias table no longer maps this"
    )
    code, _ = country_filter("United States")
    assert code != "US", "read path silently coerced a name into a code"


# ─── C5: absent means do not filter, not an error ────────────────────────

@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_country_is_not_an_error(raw):
    code, err = country_filter(raw)
    assert code is None and err is None


# ─── C6/C7: BOTH twins, structurally ─────────────────────────────────────

@pytest.mark.parametrize("fn", FACILITY_LISTERS)
def test_both_listers_route_country_through_the_guard(fn):
    src = _func_source(fn)
    assert src.count("country_filter(") >= 1, (
        "%s does not call country_filter — this path still passes the raw "
        "parameter to SQL and answers success/[] for 'United States'" % fn
    )
    assert "from util.country_codes import country_filter" in src, (
        "%s must import the shared SoT helper, not re-implement the rule" % fn
    )


@pytest.mark.parametrize("fn", FACILITY_LISTERS)
def test_both_listers_return_before_querying_on_a_bad_country(fn):
    src = _func_source(fn)
    assert "_country_err" in src, "%s dropped the error arm" % fn
    guard = src.index("if _country_err:")
    where = src.index('" AND country = %s"')
    assert guard < where, (
        "%s evaluates the country filter BEFORE returning on a bad value — "
        "the bad-input path must not reach the query" % fn
    )


@pytest.mark.parametrize("fn", FACILITY_LISTERS)
def test_both_listers_answer_400_on_a_bad_country(fn):
    """The status is read from the AST, NOT grepped.

    ★This assertion was vacuous on its first draft: it grepped for "400" in the
    slice between the guard and the query, and the explanatory COMMENT in that
    slice ("# 400 so the MCP layer...") satisfied it. Mutating the real return
    to 200 left the suite fully green. A needle that appears twice fences
    neither occurrence — so read the literal off the Return node instead.
    """
    node = _func_node(fn)
    statuses = []
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.If) and "_country_err" in ast.dump(sub.test)):
            continue
        for ret in ast.walk(sub):
            if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Tuple):
                last = ret.value.elts[-1]
                if isinstance(last, ast.Constant) and isinstance(last.value, int):
                    statuses.append(last.value)
    assert statuses, (
        "%s has no `return ..., <status>` inside its `if _country_err:` block — "
        "a bad country would fall through to the query" % fn
    )
    assert all(s == 400 for s in statuses), (
        "%s answers %r for an invalid country; it must be 400 so the MCP layer's "
        "_upstreamError promotes it to _error_mitigation{error_code, "
        "deterministic_hint} instead of the agent seeing a 200 it reads as data"
        % (fn, statuses)
    )


@pytest.mark.parametrize("fn", FACILITY_LISTERS)
def test_the_valid_country_path_still_filters(fn):
    """C7 — the guard must not have broken the arm that returns rows."""
    src = _func_source(fn)
    assert '" AND country = %s"' in src, (
        "%s lost its country SQL predicate — country=US would now return "
        "UNFILTERED rows, a wrong answer worse than the empty one" % fn
    )
    assert "params.append(country)" in src, (
        "%s no longer binds the resolved code — the predicate would bind the "
        "wrong parameter" % fn
    )


def test_no_lister_passes_the_raw_request_value_into_the_filter():
    """The regression itself: `country = request.args.get('country')` feeding SQL."""
    for fn in FACILITY_LISTERS:
        src = _func_source(fn)
        assert "country = request.args.get('country')\n" not in src, (
            "%s rebound `country` straight from the request — that is the "
            "original bug, restored" % fn
        )
