"""
canonical_stats published COUNT(DISTINCT canonical_slug) WHERE a keeper exists
under the name `facilities_verified`. A keeper election is a DE-DUPLICATION
state, not a source verification — and two other public surfaces already use
`facilities_verified` for a DIFFERENT predicate:

    /api/v1/stats           data.facilities_verified   22,160   duplicate_of_id IS NULL
    /api/v1/stats/canonical      facilities_verified   22,166   duplicate_of_id IS NULL
    canonical_stats                                    22,949   DISTINCT slug, keeper

canon's published `facilities` floor ("22,900+") comes from the third one, so
DC Hub told registries and partner inboxes the keeper count under the word
"verified". Renamed to `facilities_with_keeper_distinct`.

★ NOT the bare `facilities_with_keeper`: routes/facilities_by_dims.py:191
already owns that name for COUNT(*) WHERE COALESCE(is_duplicate,0)=0 — ROWS
(22,955), not distinct slugs (22,949). Same filter, different population.
Taking the bare name would move the collision, not end it. The last test here
is what stops that happening later by accident.
"""
from __future__ import annotations

import ast
import os
import re

import canonical_stats as cs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NEW = "facilities_with_keeper_distinct"
OLD = "facilities_verified"


def test_the_published_floor_reads_the_renamed_metric():
    """★ THE RENAME. _PUBLIC_FLOOR_SPECS is the one place the honest name has
    to win: it is what turns this metric into canon's `facilities` phrase."""
    metric, _floor = cs._PUBLIC_FLOOR_SPECS["facilities"]
    assert metric == NEW, (
        f"canon's facilities floor is still published from {metric!r}. The "
        f"whole point of the rename is that this map stops saying 'verified'.")


def test_the_renamed_metric_has_a_fallback_seed():
    """Three gates: a spec, a query, and a seed. A spec whose metric has no
    _FALLBACK entry raises KeyError on a DB outage — the one moment the
    fallback exists for."""
    assert cs._FALLBACK[NEW] == cs._FALLBACK[OLD], (
        "the seed and its deprecated alias disagree, so a DB outage publishes "
        "two different floors for one number")


def test_the_query_writes_both_names_and_marks_both_live():
    """The alias is load-bearing, not decoration: routes/provenance.py:313
    gates on stat_is_live('facilities_verified'), and
    routes/facilities_by_dims.py:207 setdefaults the PUBLIC
    /api/v1/stats/canonical response off _cs.get('facilities_verified')."""
    src = open(os.path.join(ROOT, "canonical_stats.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_query_live")
    body = ast.unparse(fn)
    for name in (NEW, OLD):
        assert f"out['{name}']" in body, f"_query_live never writes {name!r}"
        assert f"_live_keys.add('{name}')" in body, (
            f"_query_live writes {name!r} without marking it live — "
            f"stat_is_live({name!r}) reads False forever and every publisher "
            f"gated on it suppresses a measured number")


def test_the_deprecated_alias_still_answers(monkeypatch):
    """★ ai_surface_canon:1374 imports facilities_verified_phrase BY NAME to
    build c['facilities_verified_live'], the _LIVE_WITNESS entry for
    public.facilities. An unmapped witness reads NOT live and fails closed, so
    dropping this helper would silently un-live the headline floor."""
    monkeypatch.setattr(cs, "get_canonical_stats",
                        lambda: {NEW: 22_949, OLD: 22_949}, raising=True)
    assert cs.facilities_verified_phrase() == cs.facilities_with_keeper_distinct_phrase()
    assert cs.facilities_with_keeper_distinct_phrase() == "22,900+"


def test_the_witness_import_still_resolves():
    """Guard the guard above against a rename that deletes the alias: the
    import ai_surface_canon actually performs must succeed."""
    from canonical_stats import facilities_verified_phrase  # noqa: F401
    src = open(os.path.join(ROOT, "ai_surface_canon.py"), encoding="utf-8").read()
    assert "facilities_verified_phrase" in src, (
        "ai_surface_canon stopped importing the alias — move _LIVE_WITNESS "
        "and this guard together, or public.facilities reads NOT live")


def test_the_new_name_does_not_collide_with_the_endpoints_keeper_count():
    """★ THE COLLISION THIS RENAME MUST NOT RECREATE.

    routes/facilities_by_dims.py owns `facilities_with_keeper` and it is
    COUNT(*) — rows. canonical_stats' quantity is COUNT(DISTINCT
    canonical_slug). Same WHERE, different population (22,955 vs 22,949), so
    the two names must stay distinct. If a later edit drops the `_distinct`
    suffix, this fails."""
    assert NEW != "facilities_with_keeper"

    fbd = open(os.path.join(ROOT, "routes", "facilities_by_dims.py"),
               encoding="utf-8").read()
    assert 'stats["facilities_with_keeper"]' in fbd, (
        "routes/facilities_by_dims no longer defines facilities_with_keeper — "
        "re-check whether the bare name is now free before relying on this")

    cs_src = open(os.path.join(ROOT, "canonical_stats.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(cs_src))
              if isinstance(n, ast.FunctionDef) and n.name == "_query_live")
    body = ast.unparse(fn)
    # the statement that writes our metric must be preceded by a DISTINCT read
    idx = body.index(f"out['{NEW}']")
    preceding = body[:idx]
    last_sql = preceding.rfind("SELECT COUNT")
    assert last_sql != -1, "no COUNT query precedes the write — re-point this guard"
    sql = re.sub(r"\s+", " ", preceding[last_sql:])
    assert "COUNT(DISTINCT canonical_slug)" in sql, (
        f"{NEW} is no longer sourced from a DISTINCT canonical_slug count "
        f"({sql[:120]!r}). If it became COUNT(*) it IS facilities_with_keeper "
        f"and the two names now lie about being different.")
