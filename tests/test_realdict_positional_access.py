"""Seven RealDictCursor rows were read by position. Each one is pinned here.

A RealDictRow is a dict SUBCLASS, so row[0] is a KEY lookup and raises
KeyError(0) — never the first column. Found after
/api/v1/admin/enterprise/inquiries was measured returning 500 {"error": "0"} on
every call ("0" is str(KeyError(0))), then swept for the same shape.

THE SEVEN, and the three DIFFERENT symptoms they produced:

  routes/lp_alerts_cron.py:240      feature silently disabled, WRONG reason
  routes/enterprise_leads_sweep.py  HTTP 500 {"error": "0"}
  routes/press_queue.py:140         HTTP 500 + the INSERT rolled back
  routes/outcome_verifier.py:82     unhandled Flask 500 (try has only `finally`)
  ai_wars.py:193 / :198             re-raised on EVERY request; seeding never ran
  routes/funnel_attribution.py:200  swallowed; the value was silently ALWAYS 0

★ THE `or [default]` IDIOM IS THE SUBTLE ONE, and three of the seven used it:

      (cur.fetchone() or [None])[0]

It reads as a null guard and is not one. A non-empty RealDictRow is TRUTHY, so
the fallback never substitutes; [0] still runs and still raises. The guard made
the bug harder to see, not less likely.

★ 22 OTHER SITES OF THE SAME TEXTUAL SHAPE ARE CORRECT and must stay untouched:
they sit under a second, PLAIN cursor in the same function, where positional
access is right. This file pins the fixed seven only — asserting "no r[0]
anywhere near a RealDictCursor" would fail on 22 pieces of working code.
"""
import ast
import io
import os
import sys
import tokenize

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


class RealDictRow(dict):
    """psycopg2.extras.RealDictRow is a dict subclass — this is the whole bug."""


# ── the mechanism ─────────────────────────────────────────────────────

def test_positional_access_on_a_dict_row_raises_keyerror_zero():
    with pytest.raises(KeyError) as ei:
        _ = RealDictRow({"id": 7})[0]
    assert str(ei.value) == "0"


def test_the_or_fallback_idiom_does_not_protect():
    """★ Three of the seven used `(row or [None])[0]` and it reads as a guard.
    A non-empty dict is truthy, so the fallback is never taken."""
    row = RealDictRow({"to_regclass": "public.saved_lp_alerts"})
    assert bool(row) is True, "a populated RealDictRow is truthy"
    with pytest.raises(KeyError):
        _ = (row or [None])[0]
    # ...and the empty case does NOT raise, which is why it looked safe.
    assert (RealDictRow({}) or [None])[0] is None


# ── each fixed site, by source ────────────────────────────────────────

def _code(path, fn_name):
    """A function's body with comments stripped.

    Comments are removed because each fix carries a comment QUOTING the old
    broken expression; a raw text scan would count the explanation as the
    defect and make the rule unfixable except by deleting the note. Joined with
    NO separator — " ".join renders row["id"] as row [ "id" ] and every
    substring check silently misses.
    """
    src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == fn_name)
    body = ast.get_source_segment(src, fn) or ""
    return "".join(t.string for t in tokenize.generate_tokens(io.StringIO(body).readline)
                   if t.type != tokenize.COMMENT)


FIXED = [
    ("routes/lp_alerts_cron.py",       "fire_pending_alerts", '_probe.get("reg")',
     '(cur.fetchone()or[None])[0]'),
    ("routes/press_queue.py",          "scan_for_drafts",     'cur.fetchone()["id"]',
     'cur.fetchone()[0]'),
    ("routes/outcome_verifier.py",     "verify_pending",      'latest["value"]',
     'latest[0]'),
    ("routes/funnel_attribution.py",   "trace_chain",         '_g.get("n")',
     '(cur.fetchone()or[0])[0]'),
    ("routes/enterprise_leads_sweep.py", "run_sweep",         'cur.fetchone()["id"]',
     'cur.fetchone()[0]'),
    ("ai_wars.py",                     "_init_tables",        'cur.fetchone()["n"]'.replace("cur", "c"),
     'c.fetchone()[0]'),
]


@pytest.mark.parametrize("path,fn,present,absent", FIXED,
                         ids=[f"{p}:{f}" for p, f, _, _ in FIXED])
def test_the_fixed_site_reads_by_key_not_position(path, fn, present, absent):
    code = _code(path, fn)
    assert present in code, f"{path}:{fn} no longer reads the row by key"
    assert absent not in code, (
        f"{path}:{fn} regressed to positional access — this raises KeyError(0) "
        f"on a RealDictRow")


def test_ai_wars_fixed_BOTH_counts():
    """:198 was unreachable only because :193 raised first. Fixing one without
    the other just moves the failure."""
    code = _code("ai_wars.py", "_init_tables")
    assert code.count('c.fetchone()["n"]') == 2
    assert "c.fetchone()[0]" not in code


# ── the COUNT(*) alias ────────────────────────────────────────────────

@pytest.mark.parametrize("path,fn", [
    ("routes/funnel_attribution.py", "trace_chain"),
    ("ai_wars.py", "_init_tables"),
])
def test_count_star_is_aliased_where_read_by_key(path, fn):
    """Unaliased COUNT(*) makes the dict key depend on libpq's default
    labelling. Naming it removes the assumption."""
    code = _code(path, fn)
    assert "COUNT(*)AS" in code.replace(" ", "").upper().replace("COUNT(*) AS", "COUNT(*)AS")


# ── the scanner that found them, and its honesty about itself ─────────

def test_the_scanner_documents_that_it_is_not_authoritative():
    """It flagged 18 sites that were ALL false positives and missed all seven
    real ones. Shipping it without that caveat would hand the next person a
    confident, wrong list."""
    src = open(os.path.join(_ROOT, "scripts", "scan_realdict_positional.py"),
               encoding="utf-8").read()
    assert "not authoritative" in src.lower()
    assert "false positive" in src.lower()
    for shape in ("fetchone", "BoolOp"):
        assert shape in src, f"the scanner no longer matches the {shape} shape"
