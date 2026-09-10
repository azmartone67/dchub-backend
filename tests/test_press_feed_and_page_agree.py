r"""The public press feed and the press page must read the SAME source tables.

THE DEFECT (measured live 2026-09-09): `/api/v1/press/feed.json` UNIONed
`press_releases_queue` + `auto_press_releases`; `press_release_page()` read
`press_releases_queue` alone. So the feed advertised 10 releases dated
2026-09-05..09-09 whose `/press-release/<slug>` pages all returned 404:

    $ curl -s https://dchub.cloud/api/v1/press/feed.json | ... 10 dated >=09-05
    $ curl -s https://dchub.cloud/api/press-releases/list | ...  0 of those 10 present
    $ curl -o /dev/null -w '%{http_code}' .../press-release/<slug>   ->  404

It failed as SILENCE: both endpoints returned 200, and the only symptom was a
dead link in someone else's RSS reader. The brain re-filed it daily from 09-06
(#4051, #4078, #4147, #4301) with the count climbing, and never fixed it.

This test resolves what each function actually executes THROUGH the AST — it
follows `_UNIFIED_PRESS_CTE + "..."` to the constant's value — so it cannot be
satisfied by a mention in a comment or a docstring.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "press_queue.py"
MAIN = ROOT / "main.py"
READERS = ("press_feed_json", "press_feed_html", "press_release_page")
# The two endpoints the EDGE actually serves /press and /press-release/<slug>
# from. They live in main.py and import the resolver constant across modules,
# so their SQL only resolves if press_queue's constants are in scope.
EDGE_READERS = ("get_press_releases_list", "get_press_release")


def _module(path=None):
    return ast.parse((path or SRC).read_text())


def _string_constants(tree):
    """module-level NAME -> str value, so a Name in a query resolves to its SQL."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _resolve(node, consts):
    """Fold a str-valued expression (Constant / Name / BinOp+) into its text."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id, "")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _resolve(node.left, consts) + _resolve(node.right, consts)
    if isinstance(node, ast.JoinedStr):  # f-string: keep the literal parts
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def _tables_read_by(func_name, path=None):
    tree = _module(path)
    consts = _string_constants(tree)
    if path is not None and path != SRC:
        # cross-module: main.py does `from routes.press_queue import <CONST>`
        consts = {**_string_constants(_module(SRC)), **consts}
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == func_name), None)
    assert fn is not None, f"{func_name} not found in {(path or SRC).name}"
    sql = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "execute" and node.args:
            sql.append(_resolve(node.args[0], consts))
    joined = " ".join(sql)
    assert joined.strip(), f"{func_name} executes no resolvable SQL"
    names = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", joined, re.I))
    return {n.lower() for n in names} - {"unified"}   # the CTE alias is not a table


def test_both_readers_execute_resolvable_sql():
    """Guard the guard: if resolution silently yields nothing, everything below
    would compare empty sets and pass. cf scan-that-can-find-nothing-needs-a-floor."""
    for fn in READERS:
        assert _tables_read_by(fn), f"{fn}: resolved zero source tables"


@pytest.mark.parametrize("fn", READERS)
def test_reader_covers_both_press_tables(fn):
    assert _tables_read_by(fn) == {"press_releases_queue", "auto_press_releases"}, (
        f"{fn} reads {_tables_read_by(fn)!r}. The feed and the page must read the "
        "same tables or the feed advertises URLs the page cannot render."
    )


def test_every_reader_agrees_exactly():
    """All three readers, pairwise. press_feed_html was the sibling this guard
    did not cover on its first draft — it kept a THIRD copy of the union."""
    seen = {fn: _tables_read_by(fn) for fn in READERS}
    distinct = {frozenset(v) for v in seen.values()}
    assert len(distinct) == 1, (
        f"readers disagree: {seen!r} — every release in the difference is a "
        "404 behind a link we publish."
    )


# ── the EDGE half: containment, not equality ────────────────────────────────
# /press-release/<slug> is served by the worker from main.py, NOT by
# press_release_page(). Measured 2026-09-10: /press/<slug> 301s to
# /press-release/<slug> at the edge, so the origin route is never reached for
# the canonical URL. The legacy resolver must therefore cover everything the
# feed can advertise. It carries MORE (the 163-row press_releases archive), so
# the invariant is a superset, not equality.

@pytest.mark.parametrize("fn", EDGE_READERS)
def test_edge_reader_resolves_its_sql(fn):
    """Floor: cross-module constant resolution must actually yield tables, or
    every containment check below compares against an empty set and passes."""
    assert _tables_read_by(fn, MAIN), f"{fn}: resolved zero source tables"


@pytest.mark.parametrize("fn", EDGE_READERS)
def test_edge_reader_covers_everything_the_feed_advertises(fn):
    feed = _tables_read_by("press_feed_json")
    edge = _tables_read_by(fn, MAIN)
    missing = feed - edge
    assert not missing, (
        f"{fn} cannot resolve {sorted(missing)!r}, which press_feed_json "
        "publishes. Every release in that gap is a link we advertise and the "
        "edge renders as 404."
    )


def test_edge_readers_also_keep_the_archive():
    """press_releases holds 163 releases neither other table has. Dropping it
    to force equality with the feed would empty the /press archive."""
    for fn in EDGE_READERS:
        assert "press_releases" in _tables_read_by(fn, MAIN), (
            f"{fn} no longer reads press_releases — the archive would vanish "
            "from /press and every archived slug would stop resolving."
        )
