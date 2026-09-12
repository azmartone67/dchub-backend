#!/usr/bin/env python3
"""GET /api/v1/carriers/<int:carrier_id>/facilities — EXECUTED, never read as text.

NO NETWORK, NO DB, NO main.py IMPORT. carrier_facility_ingestion.py is loaded
from disk and its real register_carrier_routes() is wired onto a bare Flask app
with a fake get_db. The fake answers the way Postgres does for these four
tables: only the columns a SELECT names, in that order, and the TYPE errors
Postgres raises when a bound parameter does not fit its column. So the SQL is
under test along with the JSON.

MEASURED LIVE 2026-09-11 (dchub.cloud, plain and cache-busted, cf-cache-status MISS):

  GET /api/v1/carriers/642/facilities
    HTTP 200  {"error": "operator does not exist: text = integer
               LINE 5:  WHERE carrier_pdb_id = 642 ...", "success": false}

  That was every call, for every carrier. carrier_facility_presence.carrier_pdb_id
  and carrier_profiles.pdb_id are TEXT — init_carrier_tables migrates both to
  TEXT through information_schema, and /api/v1/carriers serves pdb_id "642" as
  a JSON string — while the route bound Flask's <int:carrier_id>.

  The same handler emitted dchub_url = /facility/<id>, the legacy form, which
  301s for every row that has a slug. And a REBUILT slug is not the served one
  for a row frozen with the doubled provider prefix:
    /facilities/cloudhq-cloudhq-ashburn-08754197   200   (stored canonical_slug)
    /facilities/cloudhq-ashburn-08754197           301 -> the stored slug

  And a stored slug is still a ROW's slug. Measured 2026-09-11 18:21Z, HEAD with
  redirects not followed, over every URL this endpoint emitted for ten
  carriers: 59 of 678 were 301s, integer and hex ids alike, e.g.
    /facilities/equinix-inc-equinix-am4-amsterdam-science-park-457de6cc   301
      -> /facilities/equinix-equinix-am4-amsterdam-science-park-0a9c12c9  200
  The page answers a dedup twin's own slug with a 301 to its keeper
  (routes/facility_profile_page._twin_redirect_target, case B), so the
  endpoint must link the keeper.

THE ORACLE for every slug below is the stored canonical_slug in a fixture row —
for a dedup twin, its KEEPER's — which is what the live page serves; never the
helper under test. build_canonical_slug is called in one place: the control
proving the fixtures can tell a stored slug from a rebuilt one. The page's
lookups are answered from these same TABLES by tests/_served_slug_world.py; the
real served_slugs and _twin_redirect_target decide where each slug lands.

★ EXACT MATCHES ONLY. The rebuilt alias is a SUFFIX of the served slug, so a
  substring ban on the alias would fail on correct output.
★ dchub_facility_id is TEXT over two id-spaces (tests/test_carrier_facility_link.py):
  integer-as-text = discovered_facilities.id (INTEGER), hex16 = facilities.id
  (TEXT). The fixtures carry both, plus a hex16 id that happens to be all digits.

Run:  python3 -m pytest tests/test_carrier_facilities_api.py -v
"""
import ast
import importlib.util
import pathlib
import re
import sys

import pytest
from flask import Flask

from tests._served_slug_world import World

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "carrier_facility_ingestion.py"
FREEZE = ROOT / "routes" / "facility_slug_freeze.py"
PROFILE = ROOT / "routes" / "facility_profile_page.py"
DB_UTILS = ROOT / "db_utils.py"

# ── fixtures ────────────────────────────────────────────────────────────────
CARRIER, CARRIER_NAME = "642", "1-IX EU"

SERVED = "cloudhq-cloudhq-ashburn-08754197"               # stored; 200 live
ALIAS = "cloudhq-ashburn-08754197"                        # rebuilt; 301 live
SERVED_HEX = "digital-realty-digital-realty-ams11-f5d44402"
ALIAS_HEX = "digital-realty-ams11-f5d44402"
UNFROZEN_SLUG = "equinix-sc-55151879"      # no stored slug: the build IS the URL
SERVED_DIGITS = "interxion-interxion-fra8-5ba81d1d"
# Dedup twins measured live: each TWIN is a row's own stored slug and 301s to
# its KEEPER. The hex pair is the census's legacy id 7fb2abba31f0f7b4, which
# wears the same frozen slug as its discovered twin.
TWIN = "equinix-inc-equinix-am4-amsterdam-science-park-457de6cc"       # 301 live
KEEPER = "equinix-equinix-am4-amsterdam-science-park-0a9c12c9"         # 200
TWIN_HEX = "telehouse-global-data-centers-telehouse-london-docklands-south-1535329c"
KEEPER_HEX = "telehouse-telehouse-london-docklands-south-c0145e6c"

HEX_ID = "a1b192d375769750"
DIGITS_HEX_ID = "5480394207366128"         # valid hex16, every character a digit
HEX_TWIN_ID = "7fb2abba31f0f7b4"
_AM4 = {"address": "Science Park 610", "latitude": 52.3564, "longitude": 4.9531}
_DOCKLANDS = {"address": "Coriander Avenue", "latitude": 51.5115, "longitude": 0.0023}


def _presence(facility_pdb_id, dchub_facility_id, carrier=CARRIER):
    """One carrier_facility_presence row."""
    return {
        "carrier_pdb_id": carrier, "carrier_name": "carrier " + carrier,
        "facility_pdb_id": facility_pdb_id,
        "facility_name": "Facility " + facility_pdb_id,
        "facility_city": "Ashburn", "facility_state": "VA",
        "facility_country": "US", "facility_lat": 39.0438,
        "facility_lng": -77.4874, "dchub_facility_id": dchub_facility_id,
    }


TABLES = {
    "carrier_profiles": [
        {"pdb_id": CARRIER, "name": CARRIER_NAME},
        {"pdb_id": "643", "name": "Another Carrier"},
    ],
    "carrier_facility_presence": [
        _presence("fac-frozen", "1101"),
        _presence("fac-frozen-again", "1101"),   # a second row, same building
        _presence("fac-unfrozen", "1103"),
        _presence("fac-noslug", "1104"),
        _presence("fac-hex", HEX_ID),
        _presence("fac-digits-hex", DIGITS_HEX_ID),
        _presence("fac-orphan", "999999"),       # resolves in neither table
        _presence("fac-unlinked", None),
        _presence("fac-twin", "1105"),           # its page 301s to 1106's
        _presence("fac-keeper", "1106"),
        _presence("fac-twin-hex", HEX_TWIN_ID),  # legacy copy wearing 1107's slug
        _presence("fac-other-carrier", "1101", carrier="643"),
    ],
    "discovered_facilities": [
        {"id": 1101, "provider": "CloudHQ", "name": "CloudHQ Ashburn",
         "canonical_slug": SERVED},
        {"id": 1103, "provider": "Equinix", "name": "Equinix SC",
         "canonical_slug": None},
        {"id": 1104, "provider": "Nobody", "name": "!!!",
         "canonical_slug": None},
        {"id": 1105, "provider": "Equinix, Inc.",
         "name": "Equinix AM4 - Amsterdam, Science Park",
         "canonical_slug": TWIN, "duplicate_of_id": 1106, **_AM4},
        {"id": 1106, "provider": "Equinix",
         "name": "Equinix AM4 Amsterdam Science Park",
         "canonical_slug": KEEPER, "duplicate_of_id": None, **_AM4},
        {"id": 1107, "provider": "Telehouse Global Data Centers",
         "name": "Telehouse - London (Docklands South)",
         "canonical_slug": TWIN_HEX, "duplicate_of_id": 1108, **_DOCKLANDS},
        {"id": 1108, "provider": "Telehouse",
         "name": "Telehouse London Docklands South",
         "canonical_slug": KEEPER_HEX, "duplicate_of_id": None, **_DOCKLANDS},
    ],
    "facilities": [
        {"id": HEX_ID, "provider": "Digital Realty",
         "name": "Digital Realty AMS11", "canonical_slug": SERVED_HEX},
        {"id": DIGITS_HEX_ID, "provider": "Interxion", "name": "Interxion FRA8",
         "canonical_slug": SERVED_DIGITS},
        {"id": HEX_TWIN_ID, "provider": "Telehouse Global Data Centers",
         "name": "Telehouse - London (Docklands South)",
         "canonical_slug": TWIN_HEX},
    ],
}

# The carrier's rows, and the URL each must carry. A row absent here carries none.
MINE = [r["facility_pdb_id"] for r in TABLES["carrier_facility_presence"]
        if r["carrier_pdb_id"] == CARRIER]
EXPECTED_URLS = {
    "fac-frozen": "/facilities/" + SERVED,
    "fac-frozen-again": "/facilities/" + SERVED,
    "fac-unfrozen": "/facilities/" + UNFROZEN_SLUG,
    "fac-hex": "/facilities/" + SERVED_HEX,
    "fac-digits-hex": "/facilities/" + SERVED_DIGITS,
    "fac-twin": "/facilities/" + KEEPER,
    "fac-keeper": "/facilities/" + KEEPER,
    "fac-twin-hex": "/facilities/" + KEEPER_HEX,
}


# ── the fake database ───────────────────────────────────────────────────────
class PgError(Exception):
    """What psycopg2 raises. PGCursorWrapper rolls back and re-raises it, and
    the handler turns it into {"success": false, "error": ...}."""


# Live column types. The carrier columns are forced to TEXT by
# init_carrier_tables; the two id-spaces are per test_carrier_facility_link.py.
COLUMN_TYPES = {
    "carrier_facility_presence": {
        "carrier_pdb_id": "text", "carrier_name": "text",
        "facility_pdb_id": "text", "facility_name": "text",
        "facility_city": "text", "facility_state": "text",
        "facility_country": "text", "facility_lat": "double precision",
        "facility_lng": "double precision", "dchub_facility_id": "text"},
    "carrier_profiles": {"pdb_id": "text", "name": "text"},
    "discovered_facilities": {"id": "integer", "provider": "text",
                              "name": "text", "canonical_slug": "text"},
    "facilities": {"id": "text", "provider": "text", "name": "text",
                   "canonical_slug": "text"},
}

# The one statement shape this fake answers. Anything else is reported as
# unreadable rather than guessed at: extend the fake, never loosen it.
_STATEMENT = re.compile(
    r"^\s*SELECT\s+(?P<cols>[\w\s,]+?)\s+FROM\s+(?P<table>\w+)\s+"
    r"WHERE\s+(?P<col>\w+)\s*=\s*(?P<rhs>ANY\s*\(\s*%s\s*\)|%s)"
    r"(?:\s+ORDER\s+BY\s+[\w\s,]+?)?\s*$", re.I | re.S)


def _bound_type(value):
    """How psycopg2 binds a Python value. An int is a bare integer literal; a
    str is an untyped quoted literal, which Postgres coerces to the column."""
    return "integer" if isinstance(value, int) else "unknown"


class FakeDatabase:
    def __init__(self, tables):
        self.tables, self.statements, self.unreadable = tables, [], []

    def connect(self):
        """Stands in for get_db()."""
        return FakeConnection(self)

    def run(self, sql, params):
        self.statements.append((sql, params))
        m = _STATEMENT.match(sql)
        if not m:
            self.unreadable.append(sql)
            raise PgError("the fake database cannot read this statement")
        table, col = m["table"], m["col"]
        types = COLUMN_TYPES[table]
        cols = [c.strip() for c in m["cols"].split(",")]
        for name in cols + [col]:
            if name not in types:
                raise PgError(f'column "{name}" does not exist')
        assert isinstance(params, tuple) and len(params) == 1, params
        want = types[col]
        if m["rhs"].upper().startswith("ANY"):
            values = params[0]
            assert isinstance(values, list), "psycopg2 binds a list as an ARRAY"
            # ARRAY[1, 2] is integer[]; ARRAY['a', 'b'] resolves to text[].
            kinds = {"integer" if isinstance(v, int) else "text" for v in values}
            if kinds and kinds != {want}:
                raise PgError(f"operator does not exist: {want} = {kinds.pop()}")
        else:
            value = params[0]
            if _bound_type(value) == "integer" and want != "integer":
                raise PgError(f"operator does not exist: {want} = integer")
            if want == "integer" and not isinstance(value, int):
                if not re.fullmatch(r"\s*[+-]?\d+\s*", str(value)):
                    raise PgError(f'invalid input syntax for type integer: "{value}"')
                value = int(value)
            values = [value]
        return [tuple(row.get(c) for c in cols)
                for row in self.tables[table] if row.get(col) in values]


class FakeConnection:
    """No more capable than db_utils.PGConnectionWrapper (asserted below)."""

    def __init__(self, db):
        self._db = db

    def cursor(self):
        return FakeCursor(self._db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class FakeCursor:
    """No more capable than db_utils.PGCursorWrapper: no __enter__, no
    __iter__, and rows are plain tuples (the real PGRowProxy answers by index
    AND by name, so a tuple is the weaker of the two)."""
    description = None  # a real cursor always has it (None before a statement)
    rowcount = -1  # psycopg2: -1 = no statement / not determinable

    def __init__(self, db):
        self._db, self._rows = db, []

    def execute(self, sql, params=None):
        self._rows = self._db.run(sql, params)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


def _load(path, name):
    """`path` executed from disk under a private name, never a sys.modules stub."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _methods(path, cls_name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = [n for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == cls_name]
    assert len(hits) == 1, f"{path.name}: expected one class {cls_name}"
    return {n.name for n in hits[0].body if isinstance(n, ast.FunctionDef)}


def _profile_page():
    """The real routes/facility_profile_page.py — the module the handler imports."""
    fpp = importlib.import_module("routes.facility_profile_page")
    assert pathlib.Path(fpp.__file__).resolve() == PROFILE.resolve(), fpp.__file__
    return fpp


@pytest.fixture
def api(monkeypatch):
    # The handler imports routes.facility_slug_freeze and
    # routes.facility_profile_page at request time.
    monkeypatch.syspath_prepend(str(ROOT))
    db = FakeDatabase(TABLES)
    # The page's lookups, answered from the same TABLES; the real served_slugs
    # and _twin_redirect_target decide where each slug lands.
    world = World(TABLES["discovered_facilities"], TABLES["facilities"]
                  ).install_batch(monkeypatch, _profile_page())
    app = Flask(__name__)
    _load(MODULE, "_carrier_api_under_test").register_carrier_routes(app, db.connect)
    return app.test_client(), db, world


def _get(api):
    client, db, world = api
    resp = client.get(f"/api/v1/carriers/{CARRIER}/facilities")
    casts = [sql for sql, _ in db.statements if "::" in sql]
    assert not casts, (
        "a column is cast in the SQL — cast the PARAMETER, or the index on that "
        f"column cannot be used: {casts}")
    assert not db.unreadable, f"the fake could not read: {db.unreadable}"
    assert not world.refused, f"slug resolution went around its lookups: {world.refused}"
    body = resp.get_json()
    assert resp.status_code == 200 and body.get("success") is True, (
        f"the endpoint failed: {body.get('error')!r}")
    return resp, body, db, world


# ── controls ────────────────────────────────────────────────────────────────
def test_the_fixtures_tell_a_stored_slug_from_a_rebuilt_one():
    """If the builder already agreed with the stored slug, the served-slug test
    below would pass on a handler that rebuilds."""
    build = _load(FREEZE, "_carrier_api_slug_freeze").build_canonical_slug
    assert build("CloudHQ", "CloudHQ Ashburn") == ALIAS
    assert build("Digital Realty", "Digital Realty AMS11") == ALIAS_HEX
    assert build("Interxion", "Interxion FRA8") != SERVED_DIGITS
    assert SERVED != ALIAS and SERVED.endswith(ALIAS)          # the suffix trap
    assert build("Equinix", "Equinix SC") == UNFROZEN_SLUG
    assert build("Nobody", "!!!") is None
    assert re.fullmatch(r"[0-9a-f]{16}", DIGITS_HEX_ID) and DIGITS_HEX_ID.isdigit()


def test_the_twin_fixtures_reproduce_the_live_redirect():
    """If the page did not 301 the twins' own slugs, the keeper URLs asserted
    below would prove nothing — and the hex id must wear its twin's slug."""
    fpp = _profile_page()
    world = World(TABLES["discovered_facilities"], TABLES["facilities"])
    for twin, keeper in ((TWIN, KEEPER), (TWIN_HEX, KEEPER_HEX)):
        row = world.page_row(twin)
        assert row["canonical_slug"] == twin and row["duplicate_of_id"], row
        assert fpp._twin_redirect_target(row, twin, keeper_row=world.keeper) == keeper
        assert fpp._twin_redirect_target(world.page_row(keeper), keeper,
                                         keeper_row=world.keeper) is None
    frozen = _load(FREEZE, "_carrier_api_twin_freeze").frozen_slug_for_row
    legacy = next(r for r in TABLES["facilities"] if r["id"] == HEX_TWIN_ID)
    assert frozen(legacy) == TWIN_HEX


def test_the_fake_database_raises_what_postgres_raises():
    """The defect is a TYPE error; a fake that compared loosely would pass the
    unfixed handler."""
    cur = FakeDatabase(TABLES).connect().cursor()
    with pytest.raises(PgError, match=r"operator does not exist: text = integer"):
        cur.execute("SELECT name FROM carrier_profiles WHERE pdb_id = %s", (642,))
    cur.execute("SELECT name FROM carrier_profiles WHERE pdb_id = %s", ("642",))
    assert cur.fetchall() == [(CARRIER_NAME,)]
    with pytest.raises(PgError, match=r"operator does not exist: integer = text"):
        cur.execute("SELECT id FROM discovered_facilities WHERE id = ANY(%s)",
                    (["1101"],))
    cur.execute("SELECT id, canonical_slug FROM discovered_facilities "
                "WHERE id = ANY(%s)", ([1101, 1103],))
    assert cur.fetchall() == [(1101, SERVED), (1103, None)]   # named columns only
    with pytest.raises(PgError, match=r'column "slug" does not exist'):
        cur.execute("SELECT slug FROM facilities WHERE id = %s", (HEX_ID,))


def test_the_fake_is_no_more_capable_than_the_pooled_wrappers():
    """get_db() hands back db_utils.PGConnectionWrapper, whose cursor is a
    PGCursorWrapper. A fake offering a method those lack would let this file
    pass against a handler that cannot work in production."""
    for fake, cls in ((FakeCursor, "PGCursorWrapper"),
                      (FakeConnection, "PGConnectionWrapper")):
        offered = {n for n in vars(fake) if callable(vars(fake)[n])
                   and (not n.startswith("_")
                        or n in ("__enter__", "__exit__", "__iter__", "__getattr__"))}
        real = _methods(DB_UTILS, cls)
        assert offered, f"{fake.__name__} offers nothing — this check is vacuous"
        assert offered <= real, (
            f"{fake.__name__} offers {sorted(offered - real)}, which "
            f"db_utils.{cls} does not have")


# ── the endpoint ────────────────────────────────────────────────────────────
def test_the_carrier_id_reaches_both_text_columns_as_text(api):
    _resp, body, db, _world = _get(api)
    assert body["carrier_name"] == CARRIER_NAME, (
        "carrier_profiles.pdb_id is TEXT too; the name lookup must bind text")
    assert body["carrier_id"] == 642
    assert sorted(f["pdb_id"] for f in body["facilities"]) == sorted(MINE)
    assert body["total"] == len(MINE) == 11
    bound = [params for sql, params in db.statements
             if re.search(r"\bpdb_id\s*=|\bcarrier_pdb_id\s*=", sql)]
    assert bound == [(CARRIER,), (CARRIER,)], bound


def test_dchub_url_is_the_served_slug_never_the_legacy_form(api):
    resp, body, _db, world = _get(api)
    assert "/facility/" not in resp.get_data(as_text=True), (
        "the response carries the legacy /facility/<id> form, which 301s for "
        "every row that has a slug")
    by_pdb = {f["pdb_id"]: f for f in body["facilities"]}
    urls = {pdb: f["dchub_url"] for pdb, f in by_pdb.items() if "dchub_url" in f}
    rebuilt = sorted(u for u in urls.values()
                     if u in ("/facilities/" + ALIAS, "/facilities/" + ALIAS_HEX,
                              "/facilities/interxion-fra8-5ba81d1d"))
    assert not rebuilt, (
        f"served a REBUILT slug {rebuilt} — live it 301s to the stored "
        "canonical_slug, which the lookup must select and prefer")
    twins = sorted(u for u in urls.values()
                   if u in ("/facilities/" + TWIN, "/facilities/" + TWIN_HEX))
    assert not twins, (
        f"linked a dedup twin's own stored slug {twins} — live that page 301s to "
        "its keeper, so the slugs must be resolved past the page's redirects")
    assert urls == EXPECTED_URLS, urls
    assert world.rounds["page_rows"] == 2, (
        f"{world.rounds}: the list resolves in one batch per hop (every slug, "
        "then the two keepers), never a lookup per facility")
    for pdb in ("fac-noslug", "fac-orphan"):
        assert by_pdb[pdb].get("dchub_facility_id"), by_pdb[pdb]
        assert "dchub_url" not in by_pdb[pdb], (
            f"{pdb} has no slug, so it must carry no URL: {by_pdb[pdb]}")
    assert "dchub_facility_id" not in by_pdb["fac-unlinked"]

    freeze = sys.modules.get("routes.facility_slug_freeze")
    assert freeze is not None and (
        pathlib.Path(freeze.__file__).resolve() == FREEZE.resolve()), (
        "the slugs came from something other than routes/facility_slug_freeze.py")
