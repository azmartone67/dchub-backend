"""find_alternatives must resolve the slug search_facilities hands out.

Measured live 2026-09-24: search_facilities returned
slug=qts-qts-ashburn-mega-campus-9838ab12 for QTS Ashburn Mega Campus (id 10780).
score_facility resolved that slug; find_alternatives answered 404 "facility not
found", while the integer id worked on both. The trailing 8 hex characters are
facility_slug.stable_hash8(provider, name), and only score_facility's own copy
of the lookup matched on it.

Both routes now resolve through _facility_match. This file runs the REAL helper
source (not a re-typed copy) and checks that both routes call it.
No DB and no network.
"""
import ast
import io
import pathlib

from routes.facility_slug import hash_sql, stable_hash8

SRC_PATH = pathlib.Path(__file__).resolve().parents[1] / "routes" / "mcp_tier1_tools.py"
SRC = io.open(SRC_PATH, encoding="utf-8").read()
TREE = ast.parse(SRC)

SERVED_SLUG = "qts-qts-ashburn-mega-campus-9838ab12"
PROVIDER, NAME = "QTS", "QTS Ashburn Mega Campus"


def _func(name):
    for n in ast.walk(TREE):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name}() not found in {SRC_PATH.name}")


def _load_match():
    ns = {"hash_sql": hash_sql}
    exec(compile(ast.get_source_segment(SRC, _func("_facility_match")), "<match>", "exec"), ns)
    return ns["_facility_match"]


def test_the_measured_slug_carries_the_stable_hash():
    # The premise of this file: the served slug's suffix IS the provider|name hash.
    assert SERVED_SLUG.rsplit("-", 1)[1] == stable_hash8(PROVIDER, NAME)


def test_served_slug_matches_on_the_stable_hash():
    where, params = _load_match()(SERVED_SLUG)
    assert hash_sql("") in where
    assert params[3] == params[4] == stable_hash8(PROVIDER, NAME)


def test_placeholders_and_params_agree():
    for fid in (SERVED_SLUG, "10780", 10780, "stack-stafford-technology-campus"):
        where, params = _load_match()(fid)
        assert where.count("%s") == len(params) == 5, fid
        assert "%" not in where.replace("%s", ""), "a bare % breaks psycopg2 param binding"


def test_id_and_name_slug_still_resolve_and_do_not_hash_match():
    where, params = _load_match()(10780)
    assert "CAST(id AS TEXT) = %s" in where
    assert params[:3] == ("10780",) * 3
    assert params[3] == "", "an id must not arm the hash clause"
    # a name-slug whose last segment is not 8 hex stays on the name match
    assert _load_match()("stack-stafford-technology-campus")[1][3] == ""


def _calls(fn, name):
    return [n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def test_both_routes_resolve_through_the_one_helper():
    for route in ("find_alternatives", "score_facility"):
        fn = _func(route)
        assert _calls(fn, "_facility_match"), f"{route} does not call _facility_match"
        body = ast.get_source_segment(SRC, fn)
        # no private copy of the lookup left behind to drift again
        assert "CAST(id AS TEXT) = %s" not in body.split("FROM facilities")[0], route
