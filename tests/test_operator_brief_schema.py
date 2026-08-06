#!/usr/bin/env python3
"""The hero query must not reference columns discovered_facilities lacks.

★ WHY: `_section_hero` selected `MAX(COALESCE(last_seen, first_seen))`.
`last_seen` is not a column. Postgres raised UndefinedColumn, a bare
`except Exception: return None` swallowed it, and _build_brief turns None into
`operator_not_found` — so a SCHEMA ERROR rendered to the public as "we do not
track Equinix" while /api/v1/operators reported 543 facilities for Equinix in
the same second.

Proven 2026-08-06 against a throwaway Postgres seeded with the live shape:
  old query -> UndefinedColumn: column "last_seen" does not exist
  new query -> facility_count=543, duplicate row correctly excluded

The module's own docstring ALSO claimed `facility_name` and `eta_year`. Neither
exists. A schema note is not a schema.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "routes", "operator_brief.py"), encoding="utf-8").read()

# Columns proven absent from discovered_facilities (verified 2026-08-06 against a
# throwaway Postgres seeded with the live shape, and against the working
# /api/v1/operators which reads the same table).
ABSENT = ("last_seen", "facility_name", "eta_year")


def _hero_src() -> str:
    h = SRC[SRC.index("def _section_hero"):]
    return h[:h.index("\ndef ", 10)]


def test_the_hero_section_touches_no_maybe_missing_column():
    """★ THE INVARIANT: a LOAD-BEARING query may not reference a column that
    might not exist — because there is nothing to fall back to.

    Other sections DO reference facility_name/eta_year, deliberately, each
    behind a documented shard fallback that retries with `name`. Those degrade.
    _section_hero had no fallback: `MAX(COALESCE(last_seen, first_seen))` raised
    UndefinedColumn, a bare except returned None, and _build_brief turned that
    into `operator_not_found` — so a schema error rendered publicly as "we do
    not track Equinix" while /api/v1/operators reported 543 facilities for it in
    the same second.

    MUTATION: put `last_seen` back in the hero query -> this fails.
    """
    hero = _hero_src()
    sql = hero[hero.index("SELECT"):hero.index("FROM discovered_facilities")]
    hit = [c for c in ABSENT if c in sql]
    assert not hit, (
        f"the hero aggregate references {hit} — Postgres raises UndefinedColumn, "
        "the section returns None, and the entire brief 404s")


def test_sections_that_DO_use_shard_columns_still_have_their_fallback():
    """The other usages are fine BECAUSE they retry. Prove the retry is there,
    so nobody removes the fallback and leaves the column."""
    for col in ("facility_name", "eta_year"):
        if col in SRC:
            assert "shard fallback" in SRC or "may not exist on every shard" in SRC, (
                f"{col} is queried with no documented shard fallback")


def test_the_hero_failure_is_logged_not_swallowed_silently():
    """★ The bug was INVISIBLE, not merely present. Two PRs and a merged
    fleet-filter change shipped before anyone could tell "the query failed" from
    "this operator has no facilities" — both returned a bare None.

    The main query's handler must bind the exception AND log it. (The candidate
    loop below it keeps quiet excepts on purpose: trying the next column name is
    the expected path, not an error.)
    """
    hero = _hero_src()
    main_handler = hero[:hero.index("# \u2605 FRESHNESS IS DECORATION")]
    assert "except Exception as e:" in main_handler, \
        "_section_hero's main query still swallows the reason it returned None"
    assert "warning(" in main_handler, \
        "_section_hero must LOG why the hero query failed"


def test_freshness_is_not_fetched_inside_the_load_bearing_aggregate():
    """★ A decorative timestamp must not be able to take down the facility count.

    It was selected inside the same aggregate, so one wrong column name killed
    the section, the brief, and the page.
    """
    hero = SRC[SRC.index("def _section_hero"):]
    hero = hero[:hero.index("\ndef ", 10)]
    agg = hero[hero.index("SELECT"):hero.index("FROM discovered_facilities")]
    assert "MAX(" not in agg, (
        "the freshness MAX() is back inside the counting aggregate — a bad "
        "column name there 404s the whole surface again")
