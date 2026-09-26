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


def test_the_published_floor_says_nothing_about_verification():
    """★2026-09-20 SUPERSEDED IN PART. This guard used to require the floor to
    read `facilities_with_keeper_distinct`. The floor has since been REBASED
    again, onto `facilities_distinct` — the citeable population — so pinning
    the keeper metric here would now pin the under-claim.

    What survives is the invariant the rename was for: canon's published
    facility floor must not be sourced from anything calling itself
    'verified', because no count in this module is a source verification."""
    metric, _floor = cs._PUBLIC_FLOOR_SPECS["facilities"]
    assert metric == "facilities_distinct", (
        f"canon's facilities floor is published from {metric!r}; both "
        f"/api/v1/stats and /api/v1/stats/canonical name facilities_distinct "
        f"as the citeable field.")
    assert "verified" not in metric


def test_the_renamed_metric_has_a_fallback_seed():
    """Three gates: a spec, a query, and a seed. A metric with no _FALLBACK
    entry raises KeyError on a DB outage — the one moment the fallback exists
    for. The seed is citation-safe (400) and must stay far below reality."""
    assert cs._FALLBACK[NEW] == 400


# ── ★2026-09-25 the deprecated alias is RETIRED ──────────────────────────────
#
# The alias was not harmless: routes/facilities_by_dims.stats_canonical
# setdefault'ed the PUBLIC /api/v1/stats/canonical `facilities_verified`
# (duplicate_of_id IS NULL, ~22,414) from canonical_stats' alias (the keeper
# count, ~23,172 — or its 400 seed) whenever its own query failed. One name,
# two populations, on a public surface. Every internal reader moved to NEW;
# these guards keep the old name from coming back into canonical_stats.


class _FakeCursor:
    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return (23_172,)

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    autocommit = False

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass

    def rollback(self):
        pass

    def commit(self):
        pass


def test_query_live_no_longer_writes_the_alias(monkeypatch):
    """BEHAVIOUR, not source text: run the real _query_live against a fake
    connection whose every COUNT answers 23,172, and read the snapshot it
    returns. NEW must be measured (so the guard is not vacuous — a
    snapshot with neither name would pass a bare absence check); OLD must be
    absent from the snapshot AND from the live set."""
    monkeypatch.setattr(cs, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(cs, "_cache", None)
    monkeypatch.setattr(cs, "_cache_ts", 0.0)
    monkeypatch.setattr(cs, "_live_keys", set())
    out = cs._query_live()
    assert out.get(NEW) == 23_172, (
        f"_query_live did not measure {NEW} from the fake connection "
        f"({out.get(NEW)!r}) — this guard would pass against nothing")
    assert cs.stat_is_live(NEW)
    assert OLD not in out, (
        f"_query_live writes the retired alias {OLD!r} again. On the public "
        f"stats endpoints that name means duplicate_of_id IS NULL — a "
        f"different population from the keeper count. Read {NEW!r}.")
    assert not cs.stat_is_live(OLD)


def test_the_alias_has_no_seed_and_no_read_alias():
    assert OLD not in cs._FALLBACK, (
        f"_FALLBACK[{OLD!r}] is back — get_canonical_stats() snapshots are "
        f"built from _FALLBACK, so every snapshot would carry the old name")
    assert all(OLD not in names for names in cs._METRIC_ALIASES.values()), (
        f"_METRIC_ALIASES resolves {OLD!r} again")
    assert OLD not in cs._METRIC_ALIASES
    # the alias table is empty but the resolver still works through it
    assert cs._metric_names(NEW) == (NEW,)
    assert cs._read_metric({NEW: 7}, NEW) == 7
    assert cs._read_metric({OLD: 7}, NEW) is None, (
        "a mapping carrying only the old name must NOT resolve as the keeper "
        "count — that is the cross-population read this retirement ends")


def test_the_alias_phrase_helper_is_gone():
    assert not hasattr(cs, "facilities_verified_phrase"), (
        "canonical_stats.facilities_verified_phrase is defined again — use "
        "facilities_with_keeper_distinct_phrase()")


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


# ── the rebase: canon publishes the CITEABLE count ──────────────────────────
#
# ★2026-09-20. canon's facilities floor was sourced from the keeper count, an
# is_duplicate-based population that SUPPRESSES any facility with no
# is_duplicate=0 row — brain_consistency_radar measured 9,318 of 14,686
# distinct facilities in that state, including Meta Hyperion, Stargate
# Abilene, CoreWeave Project Horizon and Microsoft Wisconsin. Both
# /api/v1/stats/canonical's `purpose` and /api/v1/stats'
# _facility_count_notes.primary name facilities_distinct as the field to cite.

DISTINCT = "facilities_distinct"


def _offline(monkeypatch):
    """resolve_canon() and canon_nums() probe /api/v1/stats and a tools/list
    over the network. The suite's no-network hook refuses those and the
    unit-tests step fails on the refusal, so every caller here stubs them."""
    import ai_surface_canon as asc
    monkeypatch.setattr(asc, "_get",
                        lambda path, **kw: {"facilities": 24449, "markets": 330},
                        raising=False)
    monkeypatch.setattr(asc, "_mcp_tool_count", lambda *a, **kw: 91, raising=False)
    monkeypatch.setattr(asc, "_mcp_server_version", lambda *a, **kw: "2.12.16",
                        raising=False)


def _warm_distinct(n):
    """Populate the cache as a real query would, and mark the metric live."""
    snap = dict(cs._FALLBACK)
    snap[DISTINCT] = n
    cs._cache = snap
    cs._cache_ts = 1e18
    cs._live_keys.add(DISTINCT)


def test_the_citeable_count_has_all_three_gates():
    """A spec, a QUERY, and a seed. This repo has shipped each of the three
    alone for the same number; the query is the one canonical_stats did not
    have — main.py:23002 records 'canonical_stats returns None for
    facilities_distinct' as its reason for re-deriving it locally."""
    assert cs._PUBLIC_FLOOR_SPECS["facilities"][0] == DISTINCT
    assert DISTINCT in cs._FALLBACK, "no seed — a DB outage raises KeyError"
    src = open(os.path.join(ROOT, "canonical_stats.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_query_live")
    body = ast.unparse(fn)
    assert f"out['{DISTINCT}']" in body, "_query_live never measures it"
    assert f"_live_keys.add('{DISTINCT}')" in body, (
        "measured but never marked live — live_public_floors() skips every "
        "key stat_is_live() denies, so the floor would never publish")


def test_both_painters_of_public_facilities_agree(monkeypatch):
    """★ TWO PAINTERS, ONE NUMBER. resolve_canon() writes
    c['public']['facilities'] from a phrase helper; live_public_floors()
    derives the same publication key from _PUBLIC_FLOOR_SPECS. Rebasing one
    and not the other publishes two different facility counts from one module
    depending on which door the caller came through."""
    import ai_surface_canon as asc
    _offline(monkeypatch)
    _warm_distinct(24_449)
    try:
        assert cs.facilities_distinct_phrase() == "24,400+"
        assert cs.live_public_floors()["facilities"] == "24,400+"
        # ★ resolve_canon() DIRECTLY, not through canon_nums(). canon_nums()
        # resolves via live_public_floors() -> _PUBLIC_FLOOR_SPECS, i.e. the
        # SAME painter as the assertion above, so asserting through it tested
        # one painter twice and called it agreement. Measured: the mutation
        # "rebase the spec but leave the resolver on the old phrase" SURVIVED
        # that version of this test.
        assert asc.resolve_canon()["public"]["facilities"] == "24,400+", (
            "resolve_canon() still paints the facilities phrase from the old "
            "helper — the rebase reached the spec but not the resolver, and "
            "/api/v1/canon/phrases would serve a different number from "
            "/llms.txt and every surface that reads the floors")
    finally:
        cs._cache = None
        cs._cache_ts = 0.0
        cs._live_keys.discard(DISTINCT)


def test_the_live_witness_names_a_token_resolve_canon_actually_writes(monkeypatch):
    """★ FAILS CLOSED, SO IT FAILS SILENTLY. canon_is_live() reads the token
    _LIVE_WITNESS maps a publication key to. An unmapped or misspelled token
    reads NOT live — which is exactly the false 'pinned' label #4868 landed to
    end, arriving this time by way of a rename."""
    import ai_surface_canon as asc
    token = asc._LIVE_WITNESS["public.facilities"]
    src = open(os.path.join(ROOT, "ai_surface_canon.py"), encoding="utf-8").read()
    assert f'c["{token}"]' in src, (
        f"_LIVE_WITNESS points public.facilities at {token!r}, which "
        f"resolve_canon() never assigns — canon_is_live() reads False forever "
        f"and the headline floor publishes as 'pinned' while being measured")
    _offline(monkeypatch)
    _warm_distinct(24_449)
    try:
        resolved = asc.resolve_canon()
        assert asc.canon_is_live(resolved, "public.facilities"), (
            "public.facilities resolves but does not read as live")
    finally:
        cs._cache = None
        cs._cache_ts = 0.0
        cs._live_keys.discard(DISTINCT)


def test_the_citeable_count_is_never_below_the_keeper_count():
    """Direction check on the rebase. facilities_distinct drops the
    is_duplicate filter, so it is a SUPERSET by construction — if a future
    edit made it narrower, the 'rebase raises the floor' argument in the PR
    would have quietly become false."""
    src = open(os.path.join(ROOT, "canonical_stats.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_query_live")
    body = ast.unparse(fn)
    idx = body.index(f"out['{DISTINCT}']")
    sql = re.sub(r"\s+", " ", body[:idx][body[:idx].rfind("SELECT COUNT"):])
    assert "COUNT(DISTINCT canonical_slug)" in sql, sql[:140]
    assert "is_duplicate" not in sql, (
        f"facilities_distinct now filters on a de-duplication state ({sql[:140]!r}) "
        f"— that makes it a SUBSET, and it is no longer the citeable count")
