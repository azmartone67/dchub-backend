"""The proposer's replacement carried indentation the search text did not.

WHY (2026-09-08). L5 captures `search_text` from the first non-space character
of a line, so the file's own indentation stays OUTSIDE the match. It emits
`replace_text` WITH that indentation on the first line. `str.replace` then
leaves the file's indent in place and prepends the replacement's, doubling it,
and the patched file fails `ast.parse` with "unexpected indent".

Measured that day, reproduced against the real files on main:

    id      file                          lead(search)  lead(replace)  result
    265     infrastructure_discovery.py              0             4   unexpected indent @ 340
    100589  dchub_daily_automation.py                0            12   unexpected indent @ 743
    100833  api_server.py                            0             8   unexpected indent @ 1264

All three parse cleanly once realigned. They sat at draft_skips=2 of
ROTTEN_SKIP_THRESHOLD=3 -- one run from being expired as `stale`, three good
fixes about to be discarded over whitespace. That is what the queue had left
after the threshold work opened the lane, so this was the whole remaining
blocker.

The two properties that keep this safe are pinned below:
  * only the FIRST line moves -- the body lines of both texts already agree,
    so dedenting everything by the first-line delta would break the block;
  * it never ADDS indent -- a shallower replacement may be a deliberate
    dedent, and guessing would corrupt a valid patch.

Stdlib + pytest only.
"""
import ast
import textwrap

import pytest

from routes.brain_backlog_admin import _realign_replacement as realign


def _file(body):
    return textwrap.dedent(body).lstrip("\n")


# A realistic host file: the call sits at 12 spaces inside a with/if block.
HOST = _file("""
    def run(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING",
                (1,)
            )
        return True
    """)

SEARCH = ('cur.execute(\n'
          '            "INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING",\n'
          '            (1,)\n'
          '        )')

# what the proposer emits: first line carries the host's 12 spaces
REPLACE_BAD = ('        cur.execute(\n'
               '            "INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING",\n'
               '            (1,)\n'
               '        )\n'
               '        conn.commit()')


def test_the_unpatched_host_parses():
    """Control: the fixture itself must be valid, or the rest proves nothing."""
    ast.parse(HOST)


def test_as_is_the_patch_breaks_the_file():
    """Must-fail control for the bug this fixes."""
    patched = HOST.replace(SEARCH, REPLACE_BAD, 1)
    assert patched != HOST, "fixture did not match — the test is vacuous"
    with pytest.raises(SyntaxError):
        ast.parse(patched)


def test_realigned_the_patch_parses():
    patched = HOST.replace(SEARCH, realign(SEARCH, REPLACE_BAD), 1)
    assert patched != HOST
    ast.parse(patched)
    assert "conn.commit()" in patched


@pytest.mark.parametrize("lead", [4, 8, 12])
def test_the_three_live_indent_deltas(lead):
    """265 saw 4, 100833 saw 8, 100589 saw 12."""
    search = "cur.execute(x)"
    replace = " " * lead + "cur.execute(x)\nconn.commit()"
    assert realign(search, replace).startswith("cur.execute(")


def test_body_lines_are_never_touched():
    """THE guard against over-correcting. Dedenting every line by the
    first-line delta would wreck a block whose body is already correct."""
    search = "a = 1"
    replace = "        a = 1\n        b = 2\n            c = 3"
    out = realign(search, replace)
    assert out == "a = 1\n        b = 2\n            c = 3"


def test_never_adds_indentation():
    """A replacement shallower than the search may be a deliberate dedent.

    The first line must be LONGER than the indent delta. With a short line
    a negative slice returns the whole string by accident, so a guard that
    has dropped its `delta <= 0` check still looks correct -- the mutation
    that exposed this survived the first version of this test.
    """
    search = "        some_function_call(argument)"   # 8 leading spaces
    replace = "some_function_call(argument)"          # 0 lead, 28 chars
    out = realign(search, replace)
    assert out == replace, f"realign truncated a dedent to {out!r}"
    assert out.startswith("some_function_call"), "first line was sliced"


def test_equal_indent_is_left_alone():
    search = "    a = 1"
    replace = "    a = 1\n    b = 2"
    assert realign(search, replace) == replace


@pytest.mark.parametrize("search,replace", [
    ("", "  x"), ("x", ""), (None, "  x"), ("x", None),
])
def test_degenerate_inputs_return_the_replacement_unchanged(search, replace):
    assert realign(search, replace) == replace


def test_single_line_replacement_still_works():
    assert realign("a = 1", "        a = 2") == "a = 2"


def test_the_apply_site_uses_it():
    """The helper is worthless if the apply site still passes the raw text."""
    import inspect

    from routes import brain_backlog_admin as m
    src = inspect.getsource(m)
    assert "cr_aligned = _realign_replacement(cs, cr)" in src
    assert "content.replace(cs, cr_aligned, 1)" in src, (
        "apply site no longer uses the realigned text")


def test_the_syntax_gate_still_runs_after_the_realign():
    """Realigning must not become a way to skip verification: ast.parse has to
    stay downstream so an unfixable patch is still rejected."""
    import inspect

    from routes import brain_backlog_admin as m
    src = inspect.getsource(m)
    apply_at = src.index("content.replace(cs, cr_aligned, 1)")
    gate_at = src.index("ast.parse(new_content)")
    assert gate_at > apply_at, "the syntax gate must run AFTER the patch is applied"
