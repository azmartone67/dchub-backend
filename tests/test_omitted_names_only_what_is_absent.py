"""`omitted_no_fabrication` must name only what is ACTUALLY absent.

WHY. The gas brief carries two independently-killable products behind two
separate env switches (util/gas_index.py: "deliberately two separate switches
so the cheaper fix can ship first"):

    DCHUB_GAS_INDEX_ENABLED     the DCGI composite
    DCHUB_GAS_TO_GRID_ENABLED   the gas-fired $/MWh

routes/gas_intelligence.py tested them with a single combined condition —
`if not (gas_index_enabled() and gas_to_grid_enabled())` — and then appended ONE
string naming BOTH. While the two switches agreed, that read correctly. On
2026-08-30 they diverged exactly as they were designed to: the DCGI was restored
and the $/MWh was not.

MEASURED on the live API 2026-08-31T09:11Z, get_gas_intelligence(TX) returned

    dcgi_score:   81.9
    dcgi_verdict: "GAS-ADVANTAGED"
    omitted_no_fabrication: [..., "DCGI composite / gas-to-grid $/MWh
                             (withdrawn 2026-08-08 pending correction ...)"]

— the response reporting a value as omitted in the same object that returned it.

That matters more here than in ordinary copy. `omitted_no_fabrication` is the
field the tool descriptions point agents at to learn what an answer does NOT
cover. It is the honesty layer, so a false entry in it is worse than no entry:
an agent that trusts it discards a score that is sitting right there.

These tests drive the two switches through all four combinations and assert the
array names exactly the absent products — never its neighbour's state.
"""
import ast
import importlib
import re

import pytest

import routes.gas_intelligence as gi

SRC = open(gi.__file__, encoding="utf-8").read()


def _code_only(src: str) -> str:
    """Source with comments and docstrings removed.

    ★ THIS FUNCTION EXISTS BECAUSE THE FIRST DRAFT OF THIS TEST FAILED ON ITS
    OWN FIX. The assertion below bans the combined condition
    `if not (gas_index_enabled() and gas_to_grid_enabled())` — and the comment
    that documents the defect QUOTES that expression verbatim, so a raw
    substring search flagged the record of the bug as the bug.

    Same distinction the canon heal had to learn on 2026-08-31: a comment is a
    dated record, not a live claim, and a check that cannot tell them apart
    punishes writing the history down.
    """
    out = re.sub(r"#[^\n]*", "", src)
    for m in re.findall(r'"""[\s\S]*?"""', out):
        out = out.replace(m, "")
    return out


CODE = _code_only(SRC)


def test_the_combined_condition_is_gone():
    """The single expression that shipped the contradiction."""
    assert "gas_index_enabled() and gas_to_grid_enabled()" not in CODE, (
        "two independent kill switches must not produce one combined omission "
        "claim — that is what let the response report dcgi_score as omitted "
        "while returning 81.9")


def test_each_omission_is_decided_by_its_own_switch():
    """A behaviour test alone can pass against a module hardcoding today's
    answer; this pins the mechanism that makes tomorrow's answer right."""
    assert "if not gas_index_enabled():" in CODE
    assert "if not gas_to_grid_enabled():" in CODE
    assert "see dcgi_status" in SRC and "see gas_to_grid_status" in SRC, \
        "each omission must name the status field carrying its own reason"


@pytest.mark.parametrize(
    "index_on,gas_to_grid_on,want_dcgi,want_mwh",
    [
        (True,  True,  False, False),   # both live       -> neither named
        (True,  False, False, True),    # TODAY (measured)-> only the $/MWh
        (False, True,  True,  False),   # index off only  -> only the DCGI
        (False, False, True,  True),    # both off        -> both named
    ],
)
def test_omitted_names_exactly_the_absent_products(
        monkeypatch, index_on, gas_to_grid_on, want_dcgi, want_mwh):
    """Drive the SHIPPED tail with the two switches set independently.

    The block under test is the last statement of _build_gas_intel, so it is
    exercised by extracting and executing that block against a minimal brief —
    the alternative is standing up the whole route, which needs a DB.
    """
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_build_gas_intel")
    # the omission block: every statement after the last `if not gas_to_grid_enabled()`
    # Anchor on the `_omitted = []` assignment, NOT on the last
    # `if not gas_to_grid_enabled()` — that phrase now appears twice (once to
    # strip the fields, once to name the omission), so "last match" silently
    # sliced off half the block and the extract raised NameError.
    idx = next(i for i, st in enumerate(fn.body)
               if isinstance(st, ast.Assign)
               and any(getattr(t, "id", None) == "_omitted" for t in st.targets))
    block = ast.Module(body=fn.body[idx:-1], type_ignores=[])   # drop `return out`
    assert len(block.body) >= 3, (
        f"isolated only {len(block.body)} statement(s) — the omission block moved, "
        "and a truncated extract would test almost nothing")

    out = {"omitted_no_fabrication": ["gas storage levels"]}
    ns = {"out": out,
          "gas_index_enabled": lambda: index_on,
          "gas_to_grid_enabled": lambda: gas_to_grid_on}
    exec(compile(block, "<omission-block>", "exec"), ns)     # noqa: S102

    text = " | ".join(out["omitted_no_fabrication"])
    assert ("DCGI composite" in text) is want_dcgi, (
        f"index_on={index_on} gas_to_grid_on={gas_to_grid_on}: DCGI composite "
        f"{'must' if want_dcgi else 'must NOT'} be named — got {text!r}")
    assert ("$/MWh" in text) is want_mwh, (
        f"index_on={index_on} gas_to_grid_on={gas_to_grid_on}: $/MWh "
        f"{'must' if want_mwh else 'must NOT'} be named — got {text!r}")
    # the caller's own omissions must survive
    assert "gas storage levels" in text, "the pre-existing omissions were dropped"
