"""tests/test_news_entity_spine.py — entity spine guards (2026-07-27).

Sibling to test_news_entity_resolution.py, which covers the PROMOTE-time
semantic dedup (_resolve_existing_semantic). This file covers the CANDIDATE
side — classification and the already-known resolver — i.e. Shell #36 lane 5's
actuators (r-ner-singletoken / r-ner-prefix / r-ner-purge-guard).

Three defects, each of which returned HTTP 200 and looked healthy:

  (1) _is_real_entity REJECTED EVERY SINGLE-TOKEN MIXED-CASE NAME. Any one-word
      name fell through to the "2+ word Title Case" branch, failed
      `len(tokens) >= 2`, and returned False — so the predicate rejected 287 of
      470 live entities including Google, Apple, Huawei, Vodafone, Comcast,
      CoreWeave, ByteDance, AirTrunk, Orange and Telus. 48 of those resolve to
      a real facility. The same branch also lost every acronym-bearing and
      lowercase-initial operator: "Colt DCS", "Pure DC", "SK Telecom",
      "LG Uplus", "nLighten", "euNetworks".
  (2) /api/v1/admin/news-ner/purge-noise applies that predicate DESTRUCTIVELY
      (status='rejected') across every row. It had never been run, which is the
      only reason the entity table survived intact. Ground truth
      (in_facilities) must always beat a heuristic.
  (3) _already_known matched EXACTLY, but our facility names carry the site —
      "Azure Korea Central (Seoul)", "Crown Castle Baltimore" — so an entity
      present in the graph 55 times ("Azure") resolved to nothing at all.

House rules (reference_dchub_green_main_0709): no `import main`; live
assertions skip without a DB URL.

Run:  python3 -m pytest tests/test_news_entity_spine.py -v
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest

import routes.news_entity_extraction as ner

_SRC = pathlib.Path(ner.__file__)


# ── (1) the predicate must not reject real operators ──────────────────

# Every one of these is a live entity that resolves to a facility or provider.
_REAL_OPERATORS = [
    "Google", "Apple", "Huawei", "Vodafone", "Comcast", "CoreWeave",
    "ByteDance", "AirTrunk", "Orange", "Telus", "Walmart", "Lambda",
    "CityFibre", "DayOne", "Vocus", "nLighten", "euNetworks",
    "Colt DCS", "Pure DC", "SK Telecom", "LG Uplus", "TSMC",
]


@pytest.mark.parametrize("name", _REAL_OPERATORS)
def test_is_real_entity_accepts_known_operators(name):
    """★ The regression that mattered: a False here means purge-noise would
    mark a real operator 'rejected' and cut it out of the entity spine."""
    assert ner._is_real_entity(name) is True, (
        f"{name!r} is a real operator in our facilities table and the entity "
        "filter rejects it")


def test_single_token_branch_exists():
    """Guards the SHAPE of the fix, so a refactor that drops the single-token
    branch fails loudly instead of silently re-rejecting Google."""
    assert "len(tokens) == 1 and len(p) >= 4" in _SRC.read_text(encoding="utf-8"), \
        "the single-token proper-noun branch is gone"


def test_is_real_entity_still_rejects_obvious_junk():
    """The permissive direction is deliberate, not unbounded."""
    for junk in ("", "   ", "abc", "the", "a", "x"):
        assert ner._is_real_entity(junk) is False, f"{junk!r} should be rejected"


# ── (2) ground truth beats the heuristic ──────────────────────────────

def test_purge_noise_never_touches_resolved_entities():
    """purge-noise applies a HEURISTIC destructively, and that heuristic has
    been wrong before. It must skip any row that resolves to a real facility:
    in_facilities is ground truth from the facilities table and a guess may
    never overrule it. This is the invariant Shell #36 lane 5b asserts."""
    src = _SRC.read_text(encoding="utf-8")
    m = re.search(r"def ner_purge_noise\(\).*?(?=\n@|\ndef )", src, re.S)
    assert m, "ner_purge_noise() not found"
    assert re.search(r"in_facilities.*?=\s*FALSE", m.group(0), re.S | re.I), (
        "purge-noise does not exclude resolved entities — one bad heuristic "
        "and it marks real operators 'rejected'")


# ── (3) the resolver's blind spot ─────────────────────────────────────

def _already_known_src() -> str:
    src = _SRC.read_text(encoding="utf-8")
    m = re.search(r"def _already_known.*?(?=\n\ndef )", src, re.S)
    assert m, "_already_known() not found"
    return m.group(0)


def test_already_known_has_token_boundary_prefix_match():
    body = _already_known_src()
    assert "LIKE" in body, \
        "the prefix match is gone — 'Azure' resolves to nothing without it"
    assert '" %"' in body, (
        "the prefix pattern lost its trailing space — the token boundary is "
        "what stops 'Dell' matching 'Dellwood'")
    assert "_GENERIC_PREFIX_STOP" in body, \
        "the stoplist guard is gone — 'Power' will claim half the fleet"
    assert "len(n) >= 4" in body, "the minimum-length guard is gone"


def test_prefix_percent_lives_in_the_parameter_not_the_sql():
    """The empty-tuple %-substitution trap: a literal % in the SQL STRING
    detonates, a % inside a bound parameter is fine."""
    for line in _already_known_src().splitlines():
        if "LIKE" in line and "SELECT" in line:
            assert "%s" in line, f"LIKE without a placeholder: {line.strip()}"
            assert "'%'" not in line and "%%" not in line, \
                f"literal % in a SQL string: {line.strip()}"


def test_generic_prefix_stoplist_covers_the_known_false_positive():
    # "Power" matched "power generator for government-linked data center ..."
    assert "power" in ner._GENERIC_PREFIX_STOP
    for w in ("data", "center", "energy", "grid", "cloud", "network"):
        assert w in ner._GENERIC_PREFIX_STOP


# ── live assertions ───────────────────────────────────────────────────

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))
_live = pytest.mark.skipif(not _DB, reason="no DB URL — live checks skipped")


@_live
def test_live_prefix_match_beats_a_decoy():
    """★ DECOY CONTROL. A prefix match that ALSO fires on deliberately-wrong
    names is matching by coincidence, not identity — the lesson from the
    carrier join, where a broken decoy still scored 99.6% of the real one."""
    import psycopg2
    c = psycopg2.connect(_DB, connect_timeout=10)
    c.autocommit = True
    q = ("SELECT count(DISTINCT e.entity_name)"
         " FROM news_discovered_entities e"
         " JOIN facilities f"
         "   ON lower(f.name) LIKE lower(trim(e.entity_name)) || {} || ' ' || chr(37)"
         " WHERE length(trim(e.entity_name)) >= 4")
    with c.cursor() as cur:
        cur.execute(q.format("''"))
        real = int(cur.fetchone()[0])
        cur.execute(q.format("'zzq'"))     # decoy: same names, junk suffix
        decoy = int(cur.fetchone()[0])
    c.close()
    assert decoy == 0, \
        f"decoy prefix matched {decoy} entities — the matches are coincidental"
    assert real > 0, "prefix matcher finds nothing at all — check facility names"


@_live
def test_live_no_resolved_entity_is_rejected():
    """The lane 5b invariant, asserted against live data."""
    import psycopg2
    c = psycopg2.connect(_DB, connect_timeout=10)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SELECT count(*) FROM news_discovered_entities"
                    " WHERE in_facilities = TRUE"
                    "   AND coalesce(status,'') = 'rejected'")
        bad = int(cur.fetchone()[0])
    c.close()
    assert bad == 0, f"{bad} resolved entities are marked rejected"
