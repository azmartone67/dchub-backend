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
    assert _slugs(state="TX", min_mw=40, regions=["north_america"]) == ["dfw-colo", "dfw-shell"]
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
