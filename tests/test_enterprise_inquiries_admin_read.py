"""The only admin read surface for enterprise_inquiries must actually return rows.

★ MEASURED 2026-09-05 against the Railway origin:

    GET /api/v1/admin/enterprise/inquiries   ->   500  {"error": "0"}

on every call, regardless of data. `"0"` is `str(KeyError(0))`.

The handler opens a RealDictCursor, reads the rows correctly with dict(r) — and
then builds the status histogram with POSITIONAL access:

    by_status = {r[0]: int(r[1]) for r in cur.fetchall()}

A RealDictRow is keyed by column NAME, so r[0] raises KeyError(0), the bare
except stringified it to "0", and the whole response died AFTER the rows query
had already succeeded.

★ WHY IT MATTERED MORE THAN A 500. This is the only admin surface that reads
enterprise_inquiries. #3894 found that one of the two writers had been inserting
against columns that do not exist since 2026-06-30, while submit_inquiry
swallowed the exception and returned {"ok": true, "We'll be in touch within
24h."}. The lead vanished, the submitter was told otherwise, and the one place
an operator could have SEEN the empty table answered "0" — three independent
failures stacked on one revenue path.

★ THESE TESTS USE A ROW TYPE THAT BEHAVES LIKE RealDictRow. A fixture built from
plain tuples passes against the broken code and proves nothing; the bug only
exists because the row is dict-like. That is the whole point of the fixture
below.
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


class RealDictRow(dict):
    """psycopg2.extras.RealDictRow is a dict subclass — integer subscripts are
    KeyErrors, not positions. Reproducing that is what makes this test real."""


def test_positional_access_on_a_dict_row_is_the_bug():
    """Pin the mechanism, so the fix is never 'simplified' back."""
    r = RealDictRow({"status": "new", "n": 3})
    with pytest.raises(KeyError) as ei:
        _ = r[0]
    assert str(ei.value) == "0", (
        "str(KeyError(0)) is '0' — that is the entire error body the endpoint "
        "returned for two months")


def _handler_source():
    return open(os.path.join(_ROOT, "routes", "enterprise_inquiry.py"),
                encoding="utf-8").read()


def _handler_code():
    """list_inquiries' body with COMMENTS STRIPPED.

    ★ The first version of this test scanned raw text and failed on the FIXED
    code, because the comment explaining the bug necessarily quotes it ("r[0]
    raises KeyError(0)"). A text scan counts the explanation as the defect,
    which would make the rule unfixable except by deleting the note saying why
    it exists. Same trap tests/test_shell_scheduler_coverage.py documents.
    """
    import ast as _ast, io, tokenize
    src = _handler_source()
    tree = _ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, _ast.FunctionDef) and n.name == "list_inquiries")
    body = _ast.get_source_segment(src, fn) or ""
    # Joined with NO separator: " ".join would render r["status"] as
    # r [ "status" ] and every substring check below would miss.
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(body).readline):
        if tok.type != tokenize.COMMENT:
            out.append(tok.string)
    return "".join(out)


def test_the_histogram_reads_by_name_not_position():
    """★ THE REGRESSION. Positional access anywhere in a RealDictCursor block
    500s the endpoint on every call."""
    body = _handler_code()
    assert "r[0]" not in body and "r[1]" not in body, (
        "positional row access is back in list_inquiries — the cursor is a "
        "RealDictCursor and this returns 500 {\"error\": \"0\"} for all data")
    assert 'r["status"]' in body, "the histogram no longer keys by column name"


def test_the_count_column_is_aliased():
    """Without an alias the key depends on libpq's default labelling for
    COUNT(*). Naming it makes the read independent of that."""
    assert "COUNT(*) AS" in _handler_code(), (
        "COUNT(*) is unaliased; the dict key is implicit")


def test_the_error_handler_names_the_exception_type():
    """`str(KeyError(0))` is '0'. An error body of "0" reads as a value, not a
    fault, which is why nobody chased it."""
    body = _handler_code()
    assert "type(e).__name__" in body, (
        "the handler still returns a bare str(e) — a KeyError would again be "
        "reported as the three-character body \"0\"")


# ── the histogram comprehension, driven for real ──────────────────────

def _build_histogram(rows):
    """Exactly the expression the handler uses, exercised against dict-like
    rows. If the handler changes shape this test is the thing that notices."""
    src = _handler_source()
    line = next(l for l in src.splitlines() if "by_status = {" in l)
    expr = line.split("by_status = ", 1)[1].strip()
    return eval(expr, {}, {"cur": type("C", (), {"fetchall": lambda self: rows})(),
                           "int": int})


def test_the_histogram_expression_works_on_dict_rows():
    rows = [RealDictRow({"status": "new", "n": 4}),
            RealDictRow({"status": "contacted", "n": 1})]
    assert _build_histogram(rows) == {"new": 4, "contacted": 1}


def test_the_histogram_expression_survives_a_null_status():
    """Rows inherited from the other writer's table shape arrive NULL-filled;
    #3894 deliberately did not promote status to NOT NULL."""
    rows = [RealDictRow({"status": None, "n": 2})]
    assert _build_histogram(rows) == {None: 2}


def test_an_empty_table_is_an_empty_histogram_not_an_error():
    """An empty result must read as 'no inquiries', which is a real answer —
    distinct from the 500 that made 'no inquiries' unobservable."""
    assert _build_histogram([]) == {}
