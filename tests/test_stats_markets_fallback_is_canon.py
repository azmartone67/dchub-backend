"""r-one-dcpi-universe (2026-09-04) — /api/v1/stats may not fall open to a literal.

THE DEFECT
----------
The public /api/v1/stats handler resolved its `markets` field from canon, then
fell open to TWO hand-typed integers — one as a dict default, one behind an
`or`. Both are on ai_surface_canon's stale_markers denylist: one as a bare-number
entry, the other scoped as a claim shape because its bare form collides with
record IDs.

So a canon read failure made a PUBLIC, 60s-cacheable endpoint publish precisely
the retired counts ai_surface_sentinel scans served bodies to catch.

★ This is the same shape as the retired DCPI market count (#3816): the project
  denylists a number in one place and keeps a live code path that can emit it in
  another. A denylist entry is evidence something still emits the value.

WHY THE FENCE IS SOURCE-SHAPED, NOT VALUE-SHAPED
------------------------------------------------
A bare integer fallback is invisible to any regex that hunts for a claim: `232`
in `or 232` is indistinguishable from a page size, a port, or an ID. There is no
rendered string to match. So this fences the SOURCE — the fallback must name
canon's own pin — plus the one observable behaviour (a zero omits the key).

That asymmetry was learned the expensive way: a shape-matching fence added for
the market count could not see the sibling call-quota binding at all, because a
quota has no distinguishing shape either.

NO FIGURE APPEARS IN THIS FILE
------------------------------
The retired values are read FROM the denylist at run time rather than retyped
here. Typing them would put the very literals this fences back into the tree,
in a file whose job is to keep them out.
"""
from __future__ import annotations

import ast
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")


def _stats_handler_source() -> str:
    """The v1_stats handler body, comments and docstrings stripped.

    Prose must neither satisfy nor trip this fence: the comment explaining the
    fix necessarily talks about literals it must not contain.
    """
    with open(MAIN, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            src = ast.unparse(node)
            if "_canon_markets" in src and "v1_stats" in src:
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                        ast.ClassDef, ast.Module)):
                        if (sub.body and isinstance(sub.body[0], ast.Expr)
                                and isinstance(sub.body[0].value, ast.Constant)
                                and isinstance(sub.body[0].value.value, str)):
                            sub.body[0].value.value = ""
                return ast.unparse(node)
    raise AssertionError(
        "could not locate the v1_stats handler by its _canon_markets binding — "
        "if it was renamed, retarget this fence rather than deleting it"
    )


def _retired_market_counts() -> set[str]:
    """Digit runs the project itself calls retired, read from the denylist.

    Read, never retyped: this file must not become a place those literals live.
    """
    import re
    from ai_surface_canon import PINNED
    out = set()
    for marker in PINNED.get("stale_markers") or []:
        m = str(marker)
        # ★ Quota markers are excluded on purpose. A retired CALL CAP shares the
        #   denylist but not the shape: its bare value is a small round number
        #   that legitimately appears as a page size, a percentage or a limit,
        #   so treating it as a forbidden literal would fail this fence the next
        #   time someone writes limit=<that number>. Retired MARKET counts have
        #   no such second life in this handler.
        if "call" in m.lower():
            continue
        for run in re.findall(r"\d[\d,]*", m):
            digits = run.replace(",", "")
            if len(digits) == 3:          # a market-count-shaped run
                out.add(digits)
    return out


def test_the_markets_fallback_names_canons_own_pin():
    """★ The binding. The fallback must resolve from canonical_stats, not a literal.

    Source-shaped on purpose — see the module docstring. A bare integer has no
    claim shape for a value-matching fence to find.
    """
    src = _stats_handler_source()
    assert "_FALLBACK" in src, (
        "the markets fallback no longer names canonical_stats._FALLBACK. It "
        "must fall open to canon's maintained cold-start pin — which is "
        "documented, re-floored when reality moves, and rounds DOWN — never to "
        "an integer typed at this call site."
    )
    assert "get_canonical_stats" in src, (
        "the live path no longer reads canon at all"
    )


@pytest.mark.parametrize("_case", ["denylisted-literals"])
def test_no_retired_count_can_be_emitted_from_this_handler(_case):
    """★ The regression. No denylisted count may appear as a literal here.

    The values come FROM ai_surface_canon's stale_markers, so this fence widens
    by itself the moment another count is retired — no edit here, which is the
    only way a denylist and its enforcement stay in step.
    """
    src = _stats_handler_source()
    retired = _retired_market_counts()
    assert retired, (
        "read no market-count-shaped values out of stale_markers — the denylist "
        "changed shape and this fence is now vacuous. Fix the extraction; do "
        "not delete the test."
    )
    found = sorted(n for n in retired
                   if any(tok == n for tok in _tokens(src)))
    assert not found, (
        f"the v1_stats handler carries retired count literal(s) {found} that "
        f"ai_surface_canon denylists. A canon failure would publish them on a "
        f"public, cacheable endpoint — the exact value the sentinel scans "
        f"served bodies to catch."
    )


def _tokens(src: str) -> list[str]:
    """Integer literals in the source, as bare digit strings."""
    return [str(n.value) for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, int)
            and not isinstance(n.value, bool)]


def test_an_unresolvable_count_omits_the_key_rather_than_publishing_zero():
    """★ The observable half. Zero must DROP the field, not publish it.

    "markets": 0 is a confident lie a client cannot distinguish from a real
    collapse; an absent key is visible. The pop must also run BEFORE the
    degradation store and the 5-minute memo, or a zeroed payload gets cached
    and outlives the outage that produced it.
    """
    src = _stats_handler_source()
    assert "result.pop('markets'" in src or 'result.pop("markets"' in src, (
        "nothing drops the markets key when it cannot be resolved"
    )
    pop_at = max(src.find("result.pop('markets'"), src.find('result.pop("markets"'))
    cache_at = src.find("cache_for_degradation")
    # ★ The memo WRITE, not the memo read. This handler reads _STATS_CACHE at
    #   the top to serve a warm response, so a bare "_STATS_CACHE" search finds
    #   that read and reports a false ordering failure — which is exactly what
    #   the first draft of this assertion did.
    memo_at = src.find("_STATS_CACHE['value'] =")
    if memo_at == -1:
        memo_at = src.find('_STATS_CACHE["value"] =')
    assert cache_at == -1 or pop_at < cache_at, (
        "the markets key is dropped AFTER cache_for_degradation — a zeroed "
        "payload would be stored and served through the outage"
    )
    assert memo_at == -1 or pop_at < memo_at, (
        "the markets key is dropped AFTER the 5-minute memo is written — the "
        "memo would serve a zeroed count for five minutes past recovery"
    )
