"""All three Land & Power alert triggers were dead, each with a false reason.

fire_pending_alerts opens a RealDictCursor and passes it to three helpers. Every
one read its row POSITIONALLY, so every one raised KeyError(0) into a bare
except and returned its "nothing here" value:

  _current_dcpi_for_market      -> None  -> caller reports "no_current_value"
  _current_capacity_for_market  -> None  -> caller reports "no_market_capacity"
  _new_facilities_within_radius -> 0     -> caller reports "only_0_new_within_50km"

Three different messages, all plausible, none true. Measured live 2026-09-06
after the schema-probe fix landed: checked=1, fired=[],
skipped=[{'alert_id': 1, 'reason': 'no_current_value'}] — the cron ran, reached
its only alert, and skipped it for a reason it had not measured.

★ THE THIRD IS THE WORST. `(r or [0])[0]` reads as a null guard and is not one:
a non-empty RealDictRow is TRUTHY so the fallback never substitutes, [0] raises,
and the except returns 0 — reported to the user as "only 0 new facilities within
50km", a count that was never performed.

★ WHY THE SWEEP IN #3980 MISSED THESE. Its scanner is function-scoped, and these
helpers receive `cur` as a PARAMETER — the cursor is created in the caller. That
limitation was documented in the scanner's own docstring and bit within a day,
which is why a cursor-param pass now exists.
"""
import ast
import io
import os
import sys
import tokenize

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_SRC_PATH = os.path.join(_ROOT, "routes", "lp_alerts_cron.py")
HELPERS = ["_current_dcpi_for_market",
           "_current_capacity_for_market",
           "_new_facilities_within_radius"]


class RealDictRow(dict):
    """psycopg2's row type is a dict subclass — integer subscripts are keys."""


def _code(fn_name):
    """Helper body, comments stripped, joined with no separator.

    Comments are removed because each fix quotes the expression it replaced; a
    raw scan would count the explanation as the defect. No separator because
    " ".join renders r.get("v") as r . get ( "v" ) and every check misses.
    """
    src = open(_SRC_PATH, encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    body = ast.get_source_segment(src, fn) or ""
    return "".join(t.string for t in
                   tokenize.generate_tokens(io.StringIO(body).readline)
                   if t.type != tokenize.COMMENT)


@pytest.mark.parametrize("fn", HELPERS)
def test_no_positional_row_access_survives(fn):
    """★ THE REGRESSION. Each of these is handed a RealDictCursor by
    fire_pending_alerts, so any integer subscript on a row raises KeyError(0)."""
    src = open(_SRC_PATH, encoding="utf-8").read()
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == fn)
    bad = [n.lineno for n in ast.walk(node)
           if isinstance(n, ast.Subscript)
           and isinstance(n.slice, ast.Constant)
           and isinstance(n.slice.value, int)]
    assert not bad, (
        f"{fn} reads a row by position at line(s) {bad} — it is called with a "
        f"RealDictCursor, so this raises KeyError(0) and the alert silently dies")


@pytest.mark.parametrize("fn,key", [
    # dcpi now reads four components and composites them (see
    # test_dcpi_alert_column_exists), so its key is the first of the four.
    ("_current_dcpi_for_market", '"excess"'),
    ("_current_capacity_for_market", '"v"'),
    ("_new_facilities_within_radius", '"n"'),
])
def test_each_helper_reads_its_aliased_column(fn, key):
    code = _code(fn)
    assert f"get({key})" in code, f"{fn} no longer reads its column by name"


@pytest.mark.parametrize("fn", HELPERS)
def test_the_select_aliases_its_scalar(fn):
    """An unaliased scalar leaves the dict key to libpq's default labelling."""
    code = _code(fn).upper().replace(" ", "")
    assert any(a in code for a in ("ASV", "ASN", "ASEXCESS")), (
        f"{fn}'s SELECT no longer aliases its scalar column")


def test_the_or_fallback_idiom_is_gone_from_the_count_helper():
    """`(r or [0])[0]` looked like a guard, raised anyway, and turned into
    'only 0 new within 50km' — a count that never ran."""
    code = _code("_new_facilities_within_radius")
    assert "or[0])[0]" not in code and "or [0])[0]" not in code


def test_a_truthy_dict_row_defeats_the_or_fallback():
    """Pin the mechanism itself, independent of this file."""
    row = RealDictRow({"n": 5})
    assert bool(row) is True
    with pytest.raises(KeyError):
        _ = (row or [0])[0]
    assert (row and row.get("n")) == 5


def test_the_scanner_now_has_a_cursor_param_pass():
    """These three were invisible to the #3980 sweep because the cursor is
    created in the caller. If that pass is removed, the class goes unseen again."""
    src = open(os.path.join(_ROOT, "scripts", "scan_realdict_positional.py"),
               encoding="utf-8").read()
    assert "cursor_param_hits" in src
    assert "heuristic" in src.lower()
