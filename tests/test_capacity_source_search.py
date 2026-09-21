"""Capacity Source search by size and location (2026-09-15), exercised as
requests against the real blueprint.

Storage is a stand-in for Postgres behind the module's own _fetch seam, in the
style of test_capacity_source_summary.py: it splits the WHERE clause the
module actually sends into its predicates, recognises each by its exact text
(the module's own _size_sql / _COUNTRY_MATCH_SQL / _LOCATION_MATCH_SQL), and
evaluates it with the parameters bound to it. So which predicates a request
builds, how they combine and what their parameters say (region aliases,
country codes and names, state names, escaped market patterns) all come from
the module; any statement or predicate the stand-in does not know fails the
test. What each predicate means to Postgres is checked against a real database
in test_capacity_source_search_sql.py.

What these pin:
  * min_kw sizes a colocation listing by colocation.kw_available and any other
    listing by capacity_mw * 1000; min_kw with min_mw, or a min_kw that is not
    a number above 0, is refused before any query;
  * region keys and every alias the program names (na, latam, emea, apac,
    americas, mea, "middle east", africa, ...) resolve to the five keys, and an
    unknown value is refused with `allowed`;
  * the regions partition canonical_stats' country map;
  * location matches when ANY term matches; the filter families AND together;
  * `filters` echoes the normalised filters applied;
  * the standing-requirement count and _clean_lead_fields take capacity_kw,
    regions and countries through the same matchers.
"""
import json
import re
import sys
import types
from datetime import datetime, timezone

import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

import canonical_stats  # noqa: E402
import routes.exclusive_listings as el  # noqa: E402

_STATEMENT_RE = re.compile(
    r"^SELECT (?P<cols>.+?) FROM exclusive_listings WHERE (?P<where>.+?)"
    r"(?P<order> ORDER BY updated_at DESC LIMIT %s)?$")


def _listing(slug, **over):
    row = {"id": None, "slug": slug, "title": slug, "summary": None, "status": "pocket",
           "tier_required": "registered", "market": "Dallas", "state": "TX", "country": "US",
           "latitude": None, "longitude": None, "capacity_mw": 10.0, "asking_price": None,
           "asking_currency": "USD", "detail": None, "contact": None, "owner_id": None,
           "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
           "updated_at": datetime(2026, 9, 2, tzinfo=timezone.utc), "expires_at": None}
    row.update(over)
    return row


LISTINGS = [
    _listing("dfw-shell", capacity_mw=40.0, detail={"delivery_type": "powered_shell"}),
    _listing("dfw-colo", capacity_mw=40.0,
             detail={"delivery_type": "colocation", "colocation": {"kw_available": 1200}}),
    _listing("phx-colo-unsized", market="Phoenix", state="Arizona", country="United States",
             detail={"delivery_type": "colocation"}),
    _listing("fra-land", market="Frankfurt", state="HE", country="Germany", capacity_mw=0.3,
             detail={"delivery_type": "land"}),
    _listing("jnb", market="Johannesburg", state="GP", country="ZA", capacity_mw=25.0),
    _listing("sgp", market="Singapore", state=None, country=" sg ", capacity_mw=12.0),
    _listing("sao-unsized", market="Sao Paulo", state="SP", country="BR", capacity_mw=None),
    _listing("data-park", market="Data Park", state="VA", capacity_mw=5.0),
    _listing("draft-dfw", status="draft", capacity_mw=90.0),
]
NORTH_AMERICA = ["data-park", "dfw-colo", "dfw-shell", "phx-colo-unsized"]


# ── the stand-in ──────────────────────────────────────────────────────────

def _detail_text(row, key):
    """detail->>'key': text as stored, any other JSON value as its JSON text."""
    detail = row["detail"] if isinstance(row["detail"], dict) else {}
    value = detail.get(key)
    return value if value is None or isinstance(value, str) else json.dumps(value)


def _json_number_at(row, *path):
    """The JSON number at detail-><path>, or None when it is absent or is any
    other JSON type — what the module's `jsonb_typeof(...) = 'number'` guard
    decides before it casts."""
    value = row["detail"] if isinstance(row["detail"], dict) else {}
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _size(row, min_kw, unknown):
    """The fit rule _size_sql sends: min_kw clears the smallest contractable
    chunk and fits the largest contiguous block, or failing a declared block,
    the listing total."""
    min_contract = _json_number_at(row, "min_contract_kw")
    if min_contract is not None and min_contract > min_kw:
        return False
    contiguous = _json_number_at(row, "contiguous_kw")
    if contiguous is not None:
        return contiguous >= min_kw
    if _detail_text(row, "delivery_type") == "colocation":
        kw = _json_number_at(row, "colocation", "kw_available")
        return unknown if kw is None else kw >= min_kw
    if row["capacity_mw"] is None:
        return unknown
    return row["capacity_mw"] * 1000 >= min_kw


def _size_bound(row, params, unknown):
    """Every branch of the size predicate is bound the SAME requested size, so
    a helper that bound one branch a different value fails here."""
    assert len(set(params)) == 1, f"the size predicate binds one size: {params}"
    return _size(row, params[0], unknown=unknown)


def _text_in(value, codes, names):
    if value is None:
        return False
    text = value.strip(" ")
    return text.upper() in codes or text.lower() in names


def _ilike(value, pattern):
    if value is None:
        return False
    out, i = [], 0
    while i < len(pattern):
        if pattern[i] == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append({"%": ".*", "_": "."}.get(pattern[i], re.escape(pattern[i])))
        i += 1
    return re.fullmatch("".join(out), value, re.I | re.S) is not None


def _demo_row(row):
    """routes.exclusive_listings._DEMO_EXCLUDE_SQL evaluated against a row.

    Written from the SQL's own semantics -- detail->>'demo' as text, and the
    two LOWER(slug) tests -- rather than by calling el._is_demo_listing(), so
    the stand-in cannot agree with the production rule by construction.
    """
    detail = row.get("detail") or {}
    flag = detail.get("demo")
    flag = "" if flag is None else (flag if isinstance(flag, str) else json.dumps(flag))
    if flag.lower() in ("true", "t", "1", "yes"):
        return True
    slug = (row.get("slug") or "").lower()
    return slug.startswith("sample-listing-") or slug.endswith("-demo")


def _known_predicates():
    return {
        "status IN ('public', 'pocket')": (0, lambda row, p: row["status"] in ("public", "pocket")),
        # A demo listing is not supply: excluded from the feed, its counts and
        # the summary. Keyed by the module's own constant so a change to the
        # rule shows up here as a failure rather than as silent non-coverage.
        el._DEMO_EXCLUDE_SQL: (0, lambda row, p: not _demo_row(row)),
        "(expires_at IS NULL OR expires_at > NOW())": (
            0, lambda row, p: row["expires_at"] is None or row["expires_at"] > datetime.now(timezone.utc)),
        "LOWER(market) = LOWER(%s)": (
            1, lambda row, p: row["market"] is not None and row["market"].lower() == p[0].lower()),
        "UPPER(state) = %s": (1, lambda row, p: row["state"] is not None and row["state"].upper() == p[0]),
        # min_mw and a requirement's capacity_mw go through _size_sql too (x
        # 1000), so no bare capacity_mw comparison is registered: a size filter
        # that regressed to one would read as an unexpected predicate.
        "detail->>'delivery_type' = %s": (1, lambda row, p: _detail_text(row, "delivery_type") == p[0]),
        el._size_sql(1)[0]: (4, lambda row, p: _size_bound(row, p, unknown=False)),
        el._size_sql(1, unknown_matches=True)[0]: (4, lambda row, p: _size_bound(row, p, unknown=True)),
        el._COUNTRY_MATCH_SQL: (2, lambda row, p: _text_in(row["country"], p[0], p[1])),
        el._LOCATION_MATCH_SQL: (5, lambda row, p: (
            _text_in(row["country"], p[0], p[1]) or _text_in(row["state"], p[2], p[3])
            or any(_ilike(row["market"], pattern) for pattern in p[4]))),
    }


# The requirement count ORs its place predicates inside one parenthesised group.
_PLACE_PREDICATES = {
    "LOWER(market) = ANY(%s) OR UPPER(state) = ANY(%s)": (
        2, lambda row, p: (row["market"] or "").lower() in p[0] or (row["state"] or "").upper() in p[1]),
    el._COUNTRY_MATCH_SQL: (2, lambda row, p: _text_in(row["country"], p[0], p[1])),
}


def _place_group(predicate):
    """(param count, evaluator) for "(<place> OR <place> ...)", or None."""
    if not (predicate.startswith("(") and predicate.endswith(")")):
        return None
    rest, parts = predicate[1:-1], []
    while rest:
        match = next((text for text in _PLACE_PREDICATES if rest.startswith(text)), None)
        if match is None:
            return None
        parts.append(_PLACE_PREDICATES[match])
        rest = rest[len(match):]
        if rest and not rest.startswith(" OR "):
            return None
        rest = rest[len(" OR "):] if rest else rest

    def evaluate(row, params):
        at, hit = 0, False
        for count, fn in parts:
            hit = hit or fn(row, params[at:at + count])
            at += count
        return hit
    return sum(count for count, _ in parts), evaluate


class _Store:
    def __init__(self):
        self.rows, self.statements = [dict(r, id=i + 1) for i, r in enumerate(LISTINGS)], []

    def fetch(self, sql, params, cols):
        statement = " ".join(sql.split())
        self.statements.append((statement, list(params)))
        m = _STATEMENT_RE.match(statement)
        assert m, f"unexpected SQL: {statement[:160]}"
        known, rows, params = _known_predicates(), list(self.rows), list(params)
        for predicate in m.group("where").split(" AND "):
            count, fn = known.get(predicate) or _place_group(predicate) or (None, None)
            assert fn is not None, f"unexpected predicate: {predicate}"
            bound, params = params[:count], params[count:]
            rows = [r for r in rows if fn(r, bound)]
        if m.group("order"):
            limit, params = params[0], params[1:]
            rows = sorted(rows, key=lambda r: r["updated_at"], reverse=True)[:limit]
        assert params == [], f"unbound parameters left over: {params}"
        if m.group("cols") == "COUNT(*)":
            return [{"n": len(rows)}]
        assert m.group("cols") == ", ".join(el._LISTING_COLS)
        return [{c: r[c] for c in cols} for r in rows]


def _no_connection():
    raise AssertionError("search reaches storage only through _fetch")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = _Store()
    monkeypatch.setattr(el, "_fetch", store.fetch)
    monkeypatch.setattr(el, "_conn", _no_connection)
    app = Flask(__name__)
    app.register_blueprint(el.exclusive_listings_bp)
    store.client = app.test_client()
    return store


def _feed(env, query):
    r = env.client.get("/api/v1/listings?" + query)
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["ok"] is True, j
    return sorted(item["slug"] for item in j["items"]), j


# ── size ──────────────────────────────────────────────────────────────────

def test_min_kw_sizes_colocation_by_kw_available_and_other_listings_by_mw(env):
    assert _feed(env, "min_kw=1000")[0] == ["data-park", "dfw-colo", "dfw-shell", "jnb", "sgp"]
    # The colocation listing offers 1,200 kW, so 1,500 kW leaves it out though it reads 40 MW.
    assert _feed(env, "min_kw=1500")[0] == ["data-park", "dfw-shell", "jnb", "sgp"]
    assert _feed(env, "min_kw=300")[0] == ["data-park", "dfw-colo", "dfw-shell", "fra-land", "jnb", "sgp"]
    # min_mw is the same question in the bigger unit (2026-09-16), so it sizes
    # the colocation listing by its 1,200 kW of space, not by the 40 MW on its
    # row: 30 MW of colocation space is not what that listing has.
    assert _feed(env, "min_mw=30")[0] == ["dfw-shell"]
    assert _feed(env, "min_mw=1")[0] == _feed(env, "min_kw=1000")[0]


@pytest.mark.parametrize("query,message", [
    ("min_kw=0", "min_kw"), ("min_kw=-5", "min_kw"), ("min_kw=abc", "min_kw"), ("min_kw=inf", "min_kw"),
    ("min_kw=nan", "min_kw"), ("min_kw=500&min_mw=1", "min_mw or min_kw"),
])
def test_a_malformed_size_filter_is_refused_before_any_query(env, query, message):
    r = env.client.get("/api/v1/listings?" + query)
    assert (r.status_code, r.get_json()["error"]) == (400, "invalid_request")
    assert message in r.get_json()["message"]
    assert env.statements == []


def test_teaser_capacity_kw_agrees_with_the_min_kw_filter(env):
    _, j = _feed(env, "")
    kw = {item["slug"]: item["capacity_kw"] for item in j["items"]}
    assert kw == {"dfw-shell": 40000, "dfw-colo": 1200, "phx-colo-unsized": None, "fra-land": 300,
                  "jnb": 25000, "sgp": 12000, "sao-unsized": None, "data-park": 5000}
    for threshold in (1, 300, 1200, 1201, 25000, 40001):
        expected = sorted(slug for slug, value in kw.items() if value is not None and value >= threshold)
        assert _feed(env, f"min_kw={threshold}")[0] == expected, threshold


# ── regions ───────────────────────────────────────────────────────────────

# Every alias the program names, spelled with spaces, hyphens and mixed case.
@pytest.mark.parametrize("value,keys", [
    ("north_america", ["north_america"]), ("North America", ["north_america"]),
    ("NA", ["north_america"]), ("north-america", ["north_america"]),
    ("latin_america", ["latin_america"]), ("LatAm", ["latin_america"]),
    ("Latin America", ["latin_america"]), ("south america", ["latin_america"]),
    ("europe", ["europe"]), ("EMEA", ["europe", "middle_east_africa"]),
    ("asia_pacific", ["asia_pacific"]), ("Asia-Pacific", ["asia_pacific"]), ("apac", ["asia_pacific"]),
    ("Americas", ["north_america", "latin_america"]),
    ("middle_east_africa", ["middle_east_africa"]), ("MEA", ["middle_east_africa"]),
    ("Middle East", ["middle_east_africa"]), ("africa", ["middle_east_africa"]),
])
def test_every_region_alias_resolves_to_its_keys(value, keys):
    assert el._region_keys([value]) == (keys, [])


def test_region_aliases_filter_the_feed_and_echo_as_keys(env):
    slugs, j = _feed(env, "region=emea")
    assert slugs == ["fra-land", "jnb"] and j["filters"] == {"regions": ["europe", "middle_east_africa"]}
    assert _feed(env, "region=APAC")[0] == ["sgp"]
    assert _feed(env, "region=latam")[0] == ["sao-unsized"]
    assert _feed(env, "region=north%20america")[0] == NORTH_AMERICA
    slugs, j = _feed(env, "region=na,europe&region=Middle-East")
    assert slugs == sorted(NORTH_AMERICA + ["fra-land", "jnb"])
    assert j["filters"]["regions"] == ["north_america", "europe", "middle_east_africa"]


def test_an_unknown_region_is_refused_with_the_allowed_keys(env):
    r = env.client.get("/api/v1/listings?region=europe,mars")
    j = r.get_json()
    assert (r.status_code, j["error"]) == (400, "invalid_request") and "mars" in j["message"]
    assert j["allowed"] == ["north_america", "latin_america", "europe", "asia_pacific", "middle_east_africa"]
    assert env.statements == []


def test_the_regions_partition_canonical_stats_country_map():
    assert set(canonical_stats._REGION_ORDER) == set(el._CANONICAL_REGION_KEY)
    assert set(el._CANONICAL_REGION_KEY.values()) == set(el.REGION_KEYS)
    assert len(canonical_stats._COUNTRY_NAME) > 30
    for code, name in canonical_stats._COUNTRY_NAME.items():
        key = el._CANONICAL_REGION_KEY[canonical_stats._COUNTRY_REGION[name]]
        assert el._region_of(code) == el._region_of(name) == key, (code, name)
        codes, names = el._region_countries([key])
        assert code in codes and name.lower() in names
        others = [k for k in el.REGION_KEYS if k != key]
        assert code not in el._region_countries(others)[0]


# ── country and location ──────────────────────────────────────────────────

def test_country_matches_a_code_or_a_name(env):
    slugs, j = _feed(env, "country=germany")
    assert slugs == ["fra-land"] and j["filters"] == {"countries": ["DE"]}
    assert _feed(env, "country=DE")[0] == ["fra-land"]
    assert _feed(env, "country=us")[0] == _feed(env, "country=United%20States")[0] == NORTH_AMERICA
    slugs, j = _feed(env, "country=SG,Atlantis")
    assert slugs == ["sgp"] and j["filters"]["countries"] == ["SG", "Atlantis"]


def test_location_matches_when_any_term_matches(env):
    slugs, j = _feed(env, "location=Frankfurt,%20TX")
    assert slugs == ["dfw-colo", "dfw-shell", "fra-land"]
    assert j["filters"] == {"location": ["Frankfurt", "TX"]}
    assert _feed(env, "location=Texas")[0] == ["dfw-colo", "dfw-shell"]
    assert _feed(env, "location=arizona")[0] == ["phx-colo-unsized"]
    assert _feed(env, "location=AZ")[0] == ["phx-colo-unsized"]
    assert _feed(env, "location=EMEA")[0] == ["fra-land", "jnb"]
    assert _feed(env, "location=Germany")[0] == ["fra-land"]
    assert _feed(env, "location=ohannes")[0] == ["jnb"]
    # A LIKE wildcard in a term is literal, and two letters never match a market substring.
    assert _feed(env, "location=a_p")[0] == [] and _feed(env, "location=a%25p")[0] == []
    assert _feed(env, "location=a%20p")[0] == ["data-park"]
    assert _feed(env, "location=al")[0] == []


def test_filter_families_and_together(env):
    slugs, j = _feed(env, "min_kw=1000&location=Dallas&delivery_type=colocation")
    assert slugs == ["dfw-colo"]
    assert j["filters"] == {"min_kw": 1000, "location": ["Dallas"], "delivery_type": "colocation"}
    assert _feed(env, "region=emea&min_kw=1000")[0] == ["jnb"]
    assert _feed(env, "country=US&location=Phoenix,Dallas&min_kw=1")[0] == ["dfw-colo", "dfw-shell"]
    assert _feed(env, "state=TX&region=europe")[0] == []
    # Control: the location family alone returns more than the combination.
    assert _feed(env, "location=Phoenix,Dallas")[0] == ["dfw-colo", "dfw-shell", "phx-colo-unsized"]


def test_the_filters_block_echoes_what_was_applied(env):
    slugs, j = _feed(env, "min_kw=500&region=na,europe&location=Dallas")
    assert j["filters"] == {"min_kw": 500, "regions": ["north_america", "europe"], "location": ["Dallas"]}
    assert slugs == ["dfw-colo", "dfw-shell"]
    assert _feed(env, "")[1]["filters"] == {}
    # Every key the feed answered with before is still there.
    assert {"ok", "citation", "program", "viewer", "count", "items", "pocket_locked_count",
            "caller_tier", "can_see_pocket", "filters"} <= set(j)
    assert {"region", "capacity_kw"} <= set(j["items"][0])


def test_the_plain_catalog_fetch_carries_an_empty_filters_object(env):
    """GET /api/v1/listings with no query string at all still answers
    `filters`, as {}: the frontend contract scanner reads the key from that
    fetch."""
    r = env.client.get("/api/v1/listings")
    j = r.get_json()
    assert (r.status_code, j["ok"], j["filters"], j["count"]) == (200, True, {}, 8)
    signed_out_with_blank_values = env.client.get("/api/v1/listings?region=&location=%20&min_kw=")
    assert signed_out_with_blank_values.get_json()["filters"] == {}


@pytest.mark.parametrize("query", [
    "country=" + ",".join(f"C{i}" for i in range(21)),
    "location=" + ",".join(f"place{i}" for i in range(21)),
    "region=" + ",".join(["europe"] * 21),
    "location=" + "x" * 81,
])
def test_too_many_or_too_long_location_values_are_refused(env, query):
    r = env.client.get("/api/v1/listings?" + query)
    assert (r.status_code, r.get_json()["error"]) == (400, "invalid_request")
    assert env.statements == []


def test_search_text_reaches_the_sql_only_as_bound_parameters(env):
    _feed(env, "min_kw=250&region=apac&country=Germany&location=Dal'las%25,TX")
    statement, params = env.statements[0]
    for text in ("250", "Germany", "germany", "Dal'las", "dal'las", "SG"):
        assert text not in statement, text
    # The size predicate binds the requested size once per branch that
    # compares against it (2026-09-16: the contract floor, the contiguous
    # ceiling, and the two totals).
    assert ["%dal'las\\%%"] in params and params.count(250.0) == 4


# ── requirements ──────────────────────────────────────────────────────────

BUYER = {"name": "Jane Doe", "company": "Acme Capital"}


@pytest.mark.parametrize("requirement,expected", [
    ({"capacity_kw": "750"}, {"capacity_kw": 750}),
    ({"regions": ["EMEA", "apac"]}, {"regions": ["europe", "asia_pacific", "middle_east_africa"]}),
    ({"regions": "latam"}, {"regions": ["latin_america"]}),
    ({"countries": ["de", "Germany", "Atlantis"]}, {"countries": ["DE", "Atlantis"]}),
    ({"countries": "US, SG"}, {"countries": ["US", "SG"]}),
])
def test_a_requirement_takes_capacity_kw_regions_and_countries(requirement, expected):
    fields, problems = el._clean_lead_fields({**BUYER, "requirement": requirement}, need_requirement=True)
    assert problems == {} and fields["requirement"] == expected


@pytest.mark.parametrize("requirement,field", [
    ({"capacity_kw": 0}, "requirement.capacity_kw"),
    ({"capacity_kw": 5000001}, "requirement.capacity_kw"),
    ({"capacity_kw": "lots"}, "requirement.capacity_kw"),
    ({"regions": ["europe", "mars"]}, "requirement.regions"),
    ({"regions": {"europe": True}}, "requirement.regions"),
    ({"countries": [f"C{i}" for i in range(21)]}, "requirement.countries"),
    ({"countries": [["US"]]}, "requirement.countries"),
    ({"timeline": "soon"}, "requirement"),
])
def test_a_requirement_refuses_bad_or_missing_values(requirement, field):
    _, problems = el._clean_lead_fields({**BUYER, "requirement": requirement}, need_requirement=True)
    assert field in problems, problems


def test_the_largest_accepted_requirement_values_are_accepted():
    fields, problems = el._clean_lead_fields(
        {**BUYER, "requirement": {"capacity_kw": 5000000, "countries": [f"C{i}" for i in range(20)]}},
        need_requirement=True)
    assert problems == {} and len(fields["requirement"]["countries"]) == 20


@pytest.mark.parametrize("requirement,expected", [
    # A listing of unknown size is a possible match for a requirement.
    ({"capacity_kw": 1000}, ["data-park", "dfw-colo", "dfw-shell", "jnb", "phx-colo-unsized",
                             "sao-unsized", "sgp"]),
    ({"capacity_kw": 30000}, ["dfw-shell", "phx-colo-unsized", "sao-unsized"]),
    ({"regions": ["europe"], "markets": ["Dallas"]}, ["dfw-colo", "dfw-shell", "fra-land"]),
    ({"countries": ["ZA"], "capacity_kw": 30000}, []),
    ({"countries": ["ZA", "SG"], "states": ["tx"]}, ["dfw-colo", "dfw-shell", "jnb", "sgp"]),
    ({"regions": ["asia_pacific"], "capacity_mw": 50}, []),
])
def test_the_requirement_count_uses_the_feed_matchers(env, requirement, expected):
    assert el._db_count_matching(requirement) == len(expected)
    statement, _ = env.statements[-1]
    if "capacity_kw" in requirement:
        assert el._size_sql(1, unknown_matches=True)[0] in statement
    if "regions" in requirement or "countries" in requirement:
        assert el._COUNTRY_MATCH_SQL in statement
