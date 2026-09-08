""""Has this customer ever called?" must be asked of every surface.

MEASURED 2026-09-07. The white-glove roster answered that question from two
sources — users.api_calls_total (web) and mcp_call_log (MCP) — and ignored the
third: api_keys.calls_total, where every dashboard-issued REST key records its
usage.

    14 users with invisible calls · 3,656 calls · 7 of them ACTIVE subscribers

Of 26 active payers, stranded went 16 -> 13 once the third surface counted:
THREE were never stranded at all. One of them, marvinvitcu@gmail.com, had
1,340 calls and a key last used 2026-08-08 while sitting in the escalation
queue as "still zero calls" — with a human about to be asked to phone him and
ask why he never got started.

Recency had the identical hole: `last_used_at = last_mcp or last_login`, so a
customer whose only activity is a REST key read as idle since their last
LOGIN. That inflated idle_days on exactly the people the board then called
stranded.

★ WHAT THIS GUARDS. Not the arithmetic — the COMPLETENESS. A total assembled
  from a subset of sources is the shape that produced a 47.6% stranded ratio,
  a systemic_activation_failure flag, and a call list with active customers on
  it. Dropping a surface must fail here, loudly.

★ Stdlib only. Pure function + AST over the source; no DB, no network.
"""
import ast
import datetime as dt
import inspect
import textwrap

import pytest

from routes import customer_white_glove as m


# ── 1 · the total must draw on all three surfaces ────────────────────────────

def _total_assignment():
    src = textwrap.dedent(inspect.getsource(m))
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Subscript)):
            t = node.targets[0]
            if isinstance(t.slice, ast.Constant) and t.slice.value == "total_calls":
                return ast.unparse(node.value)
    raise AssertionError("no total_calls assignment found")


@pytest.mark.parametrize("surface", ["web_calls", "mcp_calls", "key_calls"])
def test_the_total_includes_every_surface(surface):
    """★ Dropping any one of these re-creates the false-stranded bug."""
    expr = _total_assignment()
    assert surface in expr, (
        f"total_calls does not read {surface} — a customer active only on that "
        f"surface reads as never having called. expr={expr}")


def test_the_third_surface_is_selected_from_api_keys():
    src = textwrap.dedent(inspect.getsource(m))
    sql = " ".join(" ".join(
        n.value for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "api_keys" in n.value).split()).lower()
    assert "sum(k.calls_total)" in sql, "key_calls is not summed from api_keys"


def test_key_calls_is_exposed_per_customer():
    """A total you cannot decompose hides WHICH surface is dead.

    ★ Bound to the PUBLISHED dict, not to the source blob. The first version
      asserted '"key_calls"' in inspect.getsource(m) and survived deleting the
      field outright — the name still occurs in the SQL alias, in the total,
      and in the comments. Mutation caught it.
    """
    src = textwrap.dedent(inspect.getsource(m))
    published = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "mcp_calls" in keys and "total_calls" in keys:
                published = keys
                break
    assert published, "could not find the per-customer record dict"
    for k in ("web_calls", "mcp_calls", "key_calls"):
        assert k in published, (
            f"{k} is not published on the customer record — the total cannot "
            f"be decomposed, so a dead surface is invisible. got={sorted(published)}")


# ── 2 · recency must see the same three surfaces ─────────────────────────────

def test_recency_considers_key_usage():
    src = textwrap.dedent(inspect.getsource(m))
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and node.targets
                and isinstance(node.targets[0], ast.Subscript)):
            t = node.targets[0]
            if isinstance(t.slice, ast.Constant) and t.slice.value == "last_used_at":
                expr = ast.unparse(node.value)
                assert "last_key_used" in expr, (
                    f"idle_days ignores REST-key activity: {expr}")
                return
    raise AssertionError("no last_used_at assignment found")


# ── 3 · the helper, where the real crash risk lives ──────────────────────────

def test_max_ts_picks_the_latest():
    a = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    b = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    assert m._max_ts(a, b) == b
    assert m._max_ts(b, a) == b


def test_max_ts_survives_mixed_awareness():
    """★ THE CRASH. last_mcp/last_key_used are tz-aware (timestamptz) and
    last_login can be naive; comparing them raises TypeError, which the
    caller's except would swallow into an empty board — the exact
    '0 payers, perfectly healthy' failure this module was hardened against."""
    aware = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    naive = dt.datetime(2026, 7, 1)
    got = m._max_ts(aware, naive)
    assert got == naive, "the later naive value should win"
    assert m._max_ts(naive, aware) == naive


@pytest.mark.parametrize("args", [(), (None,), (None, None, None)])
def test_max_ts_with_nothing_is_none(args):
    assert m._max_ts(*args) is None


def test_max_ts_ignores_none_among_real_values():
    a = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
    assert m._max_ts(None, a, None) == a
