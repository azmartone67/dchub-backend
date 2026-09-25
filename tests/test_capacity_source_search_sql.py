"""Capacity Source search, run against a real Postgres (2026-09-15).

The size and place predicates are SQL a stand-in cursor cannot judge: a CASE
that must cast detail.colocation.kw_available only when it is a JSON number,
capacity_mw stored as REAL compared in kW, ILIKE ANY over escaped patterns,
country codes and names matched through text arrays. These tests seed
exclusive_listings and read it back through the module's own
_db_list_listings and _db_count_matching, so the statements checked are the
ones the feed and the requirement count send.

Set CAPACITY_SOURCE_SQL_DSN to run them, e.g. against a throwaway database:

    CAPACITY_SOURCE_SQL_DSN=postgresql://postgres@localhost:5432/capsrc \
        python3 -m pytest tests/test_capacity_source_search_sql.py -rEf

Without it the file skips. This file owns exclusive_listings in that database
and recreates it for every test.

  S1  min_kw sizes a colocation listing by kw_available and any other listing
      by capacity_mw * 1000; a colocation listing without a numeric
      kw_available never matches, and a text kw_available raises no cast error
  S2  a REAL capacity_mw matches at its exact kW boundary
  S3  region keys and aliases match ISO-2 codes and country names, spaces and
      case ignored
  S4  country matches a code or a name either way round
  S5  location matches when ANY term does: region, country, state code or
      name, or a market substring; LIKE wildcards in a term are literal
  S6  the families AND together, and LIMIT counts matching rows only
  S7  the requirement count ORs its places, uses the same size predicate and
      keeps a listing of unknown size as a possible match
  S8  a listing's teaser region and capacity_kw agree with what the SQL selects
"""
import json
import os
import sys
import types

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("flask")

import routes.exclusive_listings as el  # noqa: E402

DSN = os.environ.get("CAPACITY_SOURCE_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="CAPACITY_SOURCE_SQL_DSN not set — no Postgres to run against")

_LISTINGS = [
    # slug, status, market, state, country, capacity_mw, detail
    ("dfw-shell", "pocket", "Dallas", "TX", "US", 40.0, {"delivery_type": "powered_shell"}),
    ("dfw-colo", "pocket", "Dallas", "TX", "US", 40.0,
     {"delivery_type": "colocation", "colocation": {"kw_available": 1200}}),
    ("phx-colo-unsized", "pocket", "Phoenix", "Arizona", "United States", 10.0,
     {"delivery_type": "colocation"}),
    ("colo-text-kw", "public", "Reno", "NV", "US", None,
     {"delivery_type": "colocation", "colocation": {"kw_available": "lots"}}),
    ("fra-land", "pocket", "Frankfurt", "HE", "Germany", 0.3, {"delivery_type": "land"}),
    ("jnb", "pocket", "Johannesburg", "GP", "ZA", 25.0, None),
    ("sgp", "public", "Singapore", None, " sg ", 12.0, {"delivery_type": "turnkey"}),
    ("sao-unsized", "pocket", "Sao Paulo", "SP", "BR", None, {"delivery_type": "land"}),
    ("data-park", "pocket", "Data Park", "VA", "US", 5.0, None),
    ("draft-dfw", "draft", "Dallas", "TX", "US", 90.0, None),
]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.setattr(el, "_dsn", lambda: DSN)
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS exclusive_listings")
            cur.execute(el._SCHEMA_DDL)
            for slug, status, market, state, country, mw, detail in _LISTINGS:
                cur.execute(
                    "INSERT INTO exclusive_listings (slug, title, status, market, state, country, "
                    "capacity_mw, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                    (slug, slug, status, market, state, country, mw,
                     json.dumps(detail) if detail is not None else None))
        conn.commit()
    finally:
        conn.close()
    yield


def _slugs(**filters):
    return sorted(r["slug"] for r in el._db_list_listings(limit=200, **filters))


def test_s1_min_kw_sizes_colocation_by_kw_available_and_everything_else_by_mw(db):
    # phx-colo-unsized and colo-text-kw are colocation without a numeric
    # kw_available, so they never match; sao-unsized has no capacity at all.
    assert _slugs(min_kw=1000) == ["data-park", "dfw-colo", "dfw-shell", "jnb", "sgp"]
    # 1,200 kW of colocation space fails 1,500 kW even though its capacity_mw reads 40 MW.
    assert _slugs(min_kw=1500) == ["data-park", "dfw-shell", "jnb", "sgp"]
    assert _slugs(min_kw=1200) == ["data-park", "dfw-colo", "dfw-shell", "jnb", "sgp"]


def test_s2_a_real_capacity_mw_matches_at_its_exact_kw_boundary(db):
    assert "fra-land" in _slugs(min_kw=300)
    assert "fra-land" not in _slugs(min_kw=300.001)
    assert _slugs(min_kw=40000) == ["dfw-shell"]


@pytest.mark.parametrize("values,expected", [
    (["emea"], ["fra-land", "jnb"]),
    (["Europe"], ["fra-land"]),
    (["middle-east"], ["jnb"]),
    (["APAC"], ["sgp"]),
    (["americas"], ["data-park", "dfw-colo", "dfw-shell", "phx-colo-unsized", "sao-unsized"]),
    (["north america"], ["data-park", "dfw-colo", "dfw-shell", "phx-colo-unsized"]),
    (["latam", "south_america"], ["sao-unsized"]),
])
def test_s3_region_keys_and_aliases_match_codes_and_names(db, values, expected):
    keys, unknown = el._region_keys(values)
    assert unknown == []
    # colo-text-kw is in the US too; its kw_available is text, which only size filters care about.
    expected = sorted(set(expected) | ({"colo-text-kw"} if "north_america" in keys else set()))
    assert _slugs(regions=keys) == expected


def test_s4_country_matches_a_code_or_a_name_either_way_round(db):
    assert _slugs(countries=["DE"]) == ["fra-land"]
    assert _slugs(countries=["germany"]) == ["fra-land"]
    assert _slugs(countries=["United States"]) == _slugs(countries=["us"]) == [
        "colo-text-kw", "data-park", "dfw-colo", "dfw-shell", "phx-colo-unsized"]
    assert _slugs(countries=["SG", "South Africa"]) == ["jnb", "sgp"]


def test_s5_location_matches_when_any_term_does(db):
    assert _slugs(location=["Frankfurt", "TX"]) == ["dfw-colo", "dfw-shell", "fra-land"]
    assert _slugs(location=["Texas"]) == ["dfw-colo", "dfw-shell"]
    assert _slugs(location=["arizona"]) == ["phx-colo-unsized"]     # a state stored as its name
    assert _slugs(location=["AZ"]) == ["phx-colo-unsized"]
    assert _slugs(location=["EMEA"]) == ["fra-land", "jnb"]
    assert _slugs(location=["singapore"]) == ["sgp"]                # market substring and country name
    assert _slugs(location=["ohannes"]) == ["jnb"]
    assert _slugs(location=["Germany"]) == ["fra-land"]
    # A LIKE wildcard in a term is literal: "a_p" is not "a?p", "%" is not "anything".
    assert _slugs(location=["a_p"]) == [] and _slugs(location=["a%p"]) == []
    assert _slugs(location=["a p"]) == ["data-park"]
    # Two-letter terms never match a market substring ("as" is inside no market here, "al" is in Dallas).
    assert _slugs(location=["al"]) == []


def test_s6_filter_families_and_together_and_limit_counts_matches(db):
    assert _slugs(min_kw=1000, location=["Dallas"], delivery_type="colocation") == ["dfw-colo"]
    assert _slugs(regions=["europe", "middle_east_africa"], min_kw=1000) == ["jnb"]
    assert _slugs(countries=["US"], location=["Phoenix", "Reno"], min_kw=1) == []
    assert _slugs(countries=["US"], location=["Phoenix", "Reno"]) == ["colo-text-kw", "phx-colo-unsized"]
    # min_mw is min_kw in the bigger unit (2026-09-16): it asks the same
    # "can this deliver 40 MW?" question, so the colocation listing is sized by
    # its 1,200 kW of colocation space and not by the 40 MW on its row.
    assert _slugs(state="TX", min_mw=40, regions=["north_america"]) == ["dfw-shell"]
    rows = el._db_list_listings(min_kw=1000, limit=2)
    assert len(rows) == 2 and {r["slug"] for r in rows} <= {"dfw-colo", "dfw-shell", "jnb", "sgp"}


@pytest.mark.parametrize("requirement,expected", [
    # 5 listings of at least 1,000 kW, plus the 3 whose size is not recorded.
    ({"capacity_kw": 1000}, 8),
    # Only dfw-shell reaches 30 MW; the 3 of unknown size stay possible matches.
    ({"capacity_kw": 30000}, 4),
    ({"regions": ["europe"], "markets": ["Dallas"]}, 3),
    ({"countries": ["ZA"], "capacity_kw": 30000}, 0),
    ({"countries": ["ZA", "SG"], "states": ["tx"]}, 4),
    ({"regions": ["asia_pacific"], "capacity_mw": 50}, 0),
])
def test_s7_the_requirement_count_uses_the_same_matchers(db, requirement, expected):
    assert el._db_count_matching(requirement) == expected


def test_s8_teaser_region_and_capacity_kw_agree_with_the_sql(db):
    rows = el._db_list_listings(limit=200)
    access = {"required": "registered", "granted": False, "reason": "sign_in_required"}
    by_region = {key: set(_slugs(regions=[key])) for key in el.REGION_KEYS}
    for row in rows:
        teaser = el._teaser(row, access)
        in_regions = [key for key, slugs in by_region.items() if row["slug"] in slugs]
        assert in_regions == ([teaser["region"]] if teaser["region"] else []), row["slug"]
        kw = teaser["capacity_kw"]
        for threshold in (1, 299.999, 300, 1200, 1200.5, 40000):
            selected = row["slug"] in _slugs(min_kw=threshold)
            assert selected == (kw is not None and kw >= threshold), (row["slug"], threshold, kw)


# ── the fit rule, against real Postgres (2026-09-16) ──────────────────────
# contiguous_kw and min_contract_kw are JSON numbers a stand-in cannot judge:
# the predicate has to cast each only behind jsonb_typeof, compare it with a
# REAL capacity_mw in kW, and fall through to the total when a key is absent or
# holds text. These seed extra rows on top of the fixture, so S1..S8 keep
# describing the same set.

_FIT_LISTINGS = [
    # 2 MW of colocation space, largest single block 500 kW.
    ("fit-colo-2mw-500", 2.0, {"delivery_type": "colocation",
                               "colocation": {"kw_available": 2000}, "contiguous_kw": 500}),
    # 40 MW, cut up no smaller than 1 MW.
    ("fit-shell-40mw-from-1mw", 40.0,
     {"delivery_type": "powered_shell", "min_contract_kw": 1000}),
    # Both: deals between 250 kW and 5 MW.
    ("fit-band-250-5000", 30.0,
     {"delivery_type": "turnkey", "contiguous_kw": 5000, "min_contract_kw": 250}),
    # Text in both fields: no cast may be attempted, and the total decides.
    ("fit-text-blocks", 10.0,
     {"delivery_type": "land", "contiguous_kw": "half the hall", "min_contract_kw": "1 MW"}),
]


@pytest.fixture
def fit_db(db):
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            for slug, mw, detail in _FIT_LISTINGS:
                cur.execute(
                    "INSERT INTO exclusive_listings (slug, title, status, market, state, "
                    "country, capacity_mw, detail) VALUES (%s, %s, 'pocket', 'Austin', 'TX', "
                    "'US', %s, %s::jsonb) ON CONFLICT DO NOTHING", (slug, slug, mw, json.dumps(detail)))
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.mark.parametrize("slug,min_kw,matches", [
    # contiguous only: the ceiling is the block, not the 2 MW total.
    ("fit-colo-2mw-500", 400, True), ("fit-colo-2mw-500", 500, True),
    ("fit-colo-2mw-500", 501, False), ("fit-colo-2mw-500", 1000, False),
    ("fit-colo-2mw-500", 2000, False),
    # min_contract only: the floor is the chunk, the ceiling the total.
    ("fit-shell-40mw-from-1mw", 500, False), ("fit-shell-40mw-from-1mw", 999, False),
    ("fit-shell-40mw-from-1mw", 1000, True), ("fit-shell-40mw-from-1mw", 2000, True),
    ("fit-shell-40mw-from-1mw", 40000, True), ("fit-shell-40mw-from-1mw", 40001, False),
    # both: the requirement sits in the band.
    ("fit-band-250-5000", 249, False), ("fit-band-250-5000", 250, True),
    ("fit-band-250-5000", 5000, True), ("fit-band-250-5000", 5001, False),
    # text in both fields falls through to capacity_mw * 1000, raising nothing.
    ("fit-text-blocks", 10000, True), ("fit-text-blocks", 10001, False),
])
def test_s9_the_fit_rule_holds_in_postgres(fit_db, slug, min_kw, matches):
    assert (slug in _slugs(min_kw=min_kw)) is matches


def test_s9_min_mw_is_the_same_rule_in_the_bigger_unit(fit_db):
    for mw in (0.25, 0.5, 1, 2, 5, 30, 40):
        assert _slugs(min_mw=mw) == _slugs(min_kw=mw * 1000), mw


def test_s9_the_requirement_count_agrees_with_the_feed(fit_db):
    """One matcher: the count is the feed's rows plus the listings whose size
    is not recorded, which a standing requirement keeps as possible matches."""
    unsized = {"phx-colo-unsized", "colo-text-kw", "sao-unsized"}
    for kw in (250, 500, 501, 1000, 2000, 5000, 40000, 40001):
        shown = set(_slugs(min_kw=kw))
        assert shown & unsized == set(), kw
        assert el._db_count_matching({"capacity_kw": kw}) == len(shown) + len(unsized), kw


def test_s9_a_listing_declaring_neither_key_is_sized_exactly_as_before(fit_db):
    """The original rule for every listing that declares neither key: the feed
    returns it when its total reaches the requirement, and not otherwise."""
    declaring = {slug for slug, _, detail in _FIT_LISTINGS
                 if isinstance(detail.get("contiguous_kw"), (int, float))
                 or isinstance(detail.get("min_contract_kw"), (int, float))}
    access = {"required": "registered", "granted": False, "reason": "sign_in_required"}
    rows = [r for r in el._db_list_listings(limit=200) if r["slug"] not in declaring]
    for row in rows:
        kw = el._teaser(row, access)["capacity_kw"]
        for threshold in (1, 300, 1200, 10000, 40000, 40001):
            assert (row["slug"] in _slugs(min_kw=threshold)) == (
                kw is not None and kw >= threshold), (row["slug"], threshold, kw)
