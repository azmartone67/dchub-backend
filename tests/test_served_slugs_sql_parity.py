#!/usr/bin/env python3
"""served_slugs against a REAL Postgres: the page's own lookups and the batch
lookups, run on the same rows, agree row for row and slug for slug.

tests/test_served_slugs_batch.py proves the walk and the single copy of the
redirect rules against an in-memory world, and compares the two sides'
statements as text. Only a database can show what those statements DO:

  · DISTINCT ON (key) ... ORDER BY key, <the page's order> picks the row the
    page's LIMIT 1 picks — suppressed rows last, then power, then id — for
    every slug at once;
  · `facilities` without is_duplicate (the LIVE shape) makes the page's
    exact-slug lookup on it raise, so a legacy slug falls through to the hash8
    lookups; with the column (the other shape) that lookup answers instead.
    The batch has to land in the same place in both;
  · `k.id = %s` coerces a digit-string pointer to an integer and cannot bind a
    hex one; `k.id = ANY(%s)` has to end up with the same keepers;
  · both walks end to end, and the carrier endpoint's own id lookups feeding
    them.

The per-request resolver runs with _resolve_legacy_slug switched off: that is
the one step served_slugs does not take (its module comment says why).

Set SERVED_SLUG_PARITY_DSN to run. CI's db-parity job sets it together with
SERVED_SLUG_PARITY_REQUIRE=1 and fails the job if anything here skipped. Each
shape gets its own schema, dropped afterwards.

Run:  SERVED_SLUG_PARITY_DSN=postgresql:///served_slug_parity \\
        python3 -m pytest tests/test_served_slugs_sql_parity.py -v
"""
import importlib.util
import os
import pathlib
import sys
import types
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.facility_profile_page as fpp  # noqa: E402
import routes.facility_slug_freeze as fsf  # noqa: E402
from routes.facility_slug import stable_hash8  # noqa: E402
from routes.facility_slug_freeze import build_canonical_slug  # noqa: E402

DSN = os.environ.get("SERVED_SLUG_PARITY_DSN", "").strip()
REQUIRE = os.environ.get("SERVED_SLUG_PARITY_REQUIRE") == "1"

K_AM4 = "equinix-equinix-am4-amsterdam-science-park-0a9c12c9"
T_AM4 = "equinix-inc-equinix-am4-amsterdam-science-park-457de6cc"
K_FR5 = "equinix-equinix-fr5-frankfurt-kleyerstrasse-3366f937"
K_TH = "telehouse-telehouse-london-docklands-south-c0145e6c"
T_TH = "telehouse-global-data-centers-telehouse-london-docklands-south-1535329c"
H_IRON = stable_hash8("Iron Mountain", "Iron Mountain LON-3")
H_ARK = stable_hash8("Ark Data Centres", "Ark Cody Park")
H_NTT = stable_hash8("NTT", "NTT Tokyo 5")
H_OVH = stable_hash8("OVH", "OVH RBX-8")
H_CYR1 = stable_hash8("CyrusOne", "CyrusOne AM1")
H_CYR2 = stable_hash8("CyrusOne", "CyrusOne AM2")


def _d(fid, slug, dup=None, **over):
    row = dict(id=fid, name=f"Parity Site {fid}", provider="Parity Operator",
               city="Amsterdam", state="", country="NL", market="Amsterdam",
               latitude=52.3564, longitude=4.9531, power_mw=10.0,
               status="operational", address="Science Park 610",
               is_duplicate=None, duplicate_of_id=dup, canonical_slug=slug)
    row.update(over)
    return row


def _f(fid, slug, **over):
    row = dict(id=fid, name=f"Legacy Site {fid}", provider="Legacy Operator",
               city="London", state="", country="GB", latitude=51.5115,
               longitude=0.0023, power_mw=5.0, status="operational",
               address="Coriander Avenue", duplicate_of_id=None,
               canonical_slug=slug)
    row.update(over)
    return row


_DOCKLANDS = dict(city="London", country="GB", latitude=51.5115,
                  longitude=0.0023, address="Coriander Avenue")

DISCOVERED = [
    _d(101, K_AM4, power_mw=20.0),
    _d(102, T_AM4, dup=101),
    _d(103, K_FR5, address=None, latitude=50.09885, longitude=8.632004),
    _d(104, "equinix-equinix-fr5-ad94b281", dup=103, address="Kleyerstraße",
       latitude=50.0988, longitude=8.632103),
    _d(105, "equinix-equinix-fr2-5b1d0001", address="Hanauer Landstrasse 300"),
    _d(106, "equinix-inc-equinix-fr2-5b1d0002", dup=105, address="Kleyerstrasse 90"),
    _d(107, "cloudhq-cloudhq-lc3-5b1d0003", address=None, latitude=50.2, longitude=8.9),
    _d(108, "unknown-cloudhq-lc3-5b1d0004", dup=107, address=None, latitude=50.0,
       longitude=8.6),
    _d(109, "qts-qts-ric1-5b1d0005", dup=101),
    _d(110, "qts-realty-trust-inc-qts-ric1-5b1d0006", dup=109),
    _d(111, "digital-realty-digital-realty-ams1-5b1d0007", power_mw=30.0),
    _d(112, "digital-realty-digital-realty-ams1-5b1d0007", power_mw=1.0),
    _d(113, "digital-realty-trust-digital-realty-ams1-5b1d0008", dup=111),
    _d(114, "coresite-coresite-sv3-5b1d0009", is_duplicate=1),
    _d(115, "coresite-inc-coresite-sv3-5b1d000a", dup=114),
    _d(116, ""),
    _d(117, "switch-ltd-switch-reno-5b1d000b", dup=116),
    _d(118, "ragingwire-ntt-va2-5b1d000c", dup=99999),
    # ── the page's ORDER BY, which only a real database shows ──
    # suppressed rows last: the SUPPRESSED row has the power and the pointer
    _d(119, "interxion-interxion-ams5-5b1d000d", dup=101, is_duplicate=1, power_mw=50.0),
    _d(120, "interxion-interxion-ams5-5b1d000d", power_mw=1.0),
    # then power: the stronger row carries the pointer
    _d(121, "nikhef-nikhef-amsterdam-5b1d000e", dup=101, power_mw=5.0),
    _d(122, "nikhef-nikhef-amsterdam-5b1d000e", power_mw=1.0),
    # then id: no power on either
    _d(123, "evoswitch-evoswitch-haarlem-5b1d000f", dup=101, power_mw=None),
    _d(124, "evoswitch-evoswitch-haarlem-5b1d000f", power_mw=None),
    # the hex shape measured live: a twin wearing the slug a legacy copy wears
    _d(125, T_TH, dup=126, **_DOCKLANDS),
    _d(126, K_TH, **_DOCKLANDS),
    # case A by hash8: today's build of a row frozen with the doubled prefix
    _d(127, "iron-mountain-iron-mountain-lon-3-" + H_IRON, provider="Iron Mountain",
       name="Iron Mountain LON-3"),
    # hash8 ORDER BY: one provider|name frozen twice; power picks the row
    _d(128, "ark-data-centres-ark-cody-park-" + H_ARK, provider="Ark Data Centres",
       name="Ark Cody Park", power_mw=10.0),
    _d(129, "ark-ark-cody-park-" + H_ARK, provider="Ark Data Centres",
       name="Ark Cody Park", power_mw=90.0),
    # an unfrozen twin: its link is today's build, found by hash8
    _d(130, None, dup=101, provider="Switch", name="Switch Tahoe Reno 2"),
    # the discovered partner of legacy F2 below
    _d(131, "ntt-tokyo-5-" + H_NTT, provider="NTT", name="NTT Tokyo 5",
       city="Tokyo", country="JP", address="Otemachi 1-1", latitude=35.68,
       longitude=139.76),
]
FACILITIES = [
    # F1: the census's hex id, a legacy copy wearing its discovered twin's slug
    _f("7fb2abba31f0f7b4", T_TH, provider="Telehouse Global Data Centers",
       name="Telehouse - London (Docklands South)"),
    # F2: legacy frozen form differs from the discovered row's; the shape decides
    _f("bbbbbbbbbbbbbbb2", "ntt-ntt-tokyo-5-" + H_NTT, provider="NTT",
       name="NTT Tokyo 5"),
    # F3: legacy-only, reached by hash8 under another name-part
    _f("ccccccccccccccc3", "ovh-rbx-8-" + H_OVH, provider="OVH", name="OVH RBX-8"),
    # F4: a digit-string pointer into the discovered id space, same building
    _f("ddddddddddddddd4", "cyrusone-cyrusone-am1-" + H_CYR1, provider="CyrusOne",
       name="CyrusOne AM1", duplicate_of_id="101", address="Science Park 610",
       latitude=52.3564, longitude=4.9531),
    # F5: a hex pointer, which the keeper lookup cannot bind
    _f("eeeeeeeeeeeeeee5", "cyrusone-cyrusone-am2-" + H_CYR2, provider="CyrusOne",
       name="CyrusOne AM2", duplicate_of_id="7fb2abba31f0f7b4",
       address="Science Park 610", latitude=52.3564, longitude=4.9531),
]
ALIASES = {
    "equinix-am4-old-0badf00d": T_AM4,
    "cycle-a-00000001": "cycle-b-00000002",
    "cycle-b-00000002": "cycle-a-00000001",
    "long-a-0000000a": "long-b-0000000b",
    "long-b-0000000b": "long-c-0000000c",
    "long-c-0000000c": "long-d-0000000d",
    "long-d-0000000d": "long-e-0000000e",
    "self-alias-0000000f": "self-alias-0000000f",
    "not-a-facility-slug": K_AM4,
}


def _stays(*slugs):
    return {s: s for s in slugs}


EXPECTED_COMMON = {
    K_AM4: K_AM4,
    T_AM4: K_AM4,
    "equinix-equinix-fr5-ad94b281": K_FR5,
    **_stays("equinix-inc-equinix-fr2-5b1d0002", "unknown-cloudhq-lc3-5b1d0004",
             "qts-realty-trust-inc-qts-ric1-5b1d0006",
             "digital-realty-digital-realty-ams1-5b1d0007",
             "digital-realty-trust-digital-realty-ams1-5b1d0008",
             "coresite-inc-coresite-sv3-5b1d000a", "switch-ltd-switch-reno-5b1d000b",
             "ragingwire-ntt-va2-5b1d000c"),
    "qts-qts-ric1-5b1d0005": K_AM4,
    # the LIVE row wins the slug, not the suppressed twin that has the pointer
    **_stays("interxion-interxion-ams5-5b1d000d"),
    "nikhef-nikhef-amsterdam-5b1d000e": K_AM4,
    "evoswitch-evoswitch-haarlem-5b1d000f": K_AM4,
    T_TH: K_TH,
    build_canonical_slug("Iron Mountain", "Iron Mountain LON-3"):
        "iron-mountain-iron-mountain-lon-3-" + H_IRON,
    "ark-cody-park-" + H_ARK: "ark-ark-cody-park-" + H_ARK,
    build_canonical_slug("Switch", "Switch Tahoe Reno 2"): K_AM4,
    "ovhcloud-ovh-rbx-8-" + H_OVH: "ovh-rbx-8-" + H_OVH,
    "equinix-am4-old-0badf00d": K_AM4,
    **_stays("cycle-a-00000001", "long-a-0000000a", "long-b-0000000b",
             "self-alias-0000000f", "nothing-matches-deadbeef",
             T_AM4[:-8] + T_AM4[-8:].upper(),
             "cyrusone-cyrusone-am2-" + H_CYR2),
    "long-c-0000000c": "long-e-0000000e",
    "not-a-facility-slug": K_AM4,
}
EXPECTED_BY_SHAPE = {
    # facilities has no is_duplicate: its exact lookup raises, hash8 answers
    "live": {"ntt-ntt-tokyo-5-" + H_NTT: "ntt-tokyo-5-" + H_NTT,
             **_stays("cyrusone-cyrusone-am1-" + H_CYR1)},
    # with the column the exact lookup answers, pointer and all
    "facilities_has_is_duplicate": {**_stays("ntt-ntt-tokyo-5-" + H_NTT),
                                    "cyrusone-cyrusone-am1-" + H_CYR1: K_AM4},
}
MATRIX = sorted({r["canonical_slug"] for r in DISCOVERED + FACILITIES if r["canonical_slug"]}
                | set(ALIASES) | set(EXPECTED_COMMON)
                | set(EXPECTED_BY_SHAPE["live"]))

DDL_DISCOVERED = """
    CREATE TABLE discovered_facilities (
      id INTEGER PRIMARY KEY, name TEXT, provider TEXT, city TEXT, state TEXT,
      country TEXT, market TEXT, latitude DOUBLE PRECISION,
      longitude DOUBLE PRECISION, power_mw DOUBLE PRECISION, status TEXT,
      address TEXT, is_duplicate INTEGER, duplicate_of_id INTEGER,
      canonical_slug TEXT)"""
DDL_FACILITIES = """
    CREATE TABLE facilities (
      id TEXT PRIMARY KEY, name TEXT, provider TEXT, city TEXT, state TEXT,
      country TEXT, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
      power_mw DOUBLE PRECISION, status TEXT, address TEXT,
      duplicate_of_id TEXT, canonical_slug TEXT{extra})"""
DDL_ALIASES = """
    CREATE TABLE facility_slug_aliases (old_slug TEXT PRIMARY KEY,
                                        canonical_slug TEXT)"""


def test_a_database_is_configured_when_ci_requires_one():
    if not DSN and not REQUIRE:
        pytest.skip("SERVED_SLUG_PARITY_DSN not set — no Postgres to run against")
    assert DSN, ("SERVED_SLUG_PARITY_REQUIRE=1 but SERVED_SLUG_PARITY_DSN is empty "
                 "— every test in this file would skip and prove nothing")


def _insert(cur, table, rows):
    cols = sorted({k for r in rows for k in r})
    cur.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [tuple(r.get(c) for c in cols) for r in rows])


def _connect(schema_name):
    return psycopg2.connect(DSN, options=f"-c search_path={schema_name}")


@pytest.fixture(scope="module", params=sorted(EXPECTED_BY_SHAPE))
def schema(request):
    if not DSN:
        pytest.skip("SERVED_SLUG_PARITY_DSN not set — no Postgres to run against")
    shape = request.param
    name = f"served_slug_parity_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {name}")
            cur.execute(f"SET search_path TO {name}")
            cur.execute(DDL_DISCOVERED)
            cur.execute(DDL_FACILITIES.format(
                extra="" if shape == "live" else ", is_duplicate INTEGER"))
            cur.execute(DDL_ALIASES)
            _insert(cur, "discovered_facilities", DISCOVERED)
            _insert(cur, "facilities", FACILITIES)
            _insert(cur, "facility_slug_aliases",
                    [{"old_slug": k, "canonical_slug": v} for k, v in ALIASES.items()])
        yield shape, name
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


@pytest.fixture
def db(schema, monkeypatch):
    """main.get_read_db / get_db open connections into this shape's schema, the
    way the route's helpers import them. Yields (shape, a test connection)."""
    shape, name = schema
    opened = []

    def open_conn():
        conn = _connect(name)
        opened.append(conn)
        return conn

    main = types.ModuleType("main")
    main.get_read_db = open_conn
    main.get_db = open_conn
    monkeypatch.setitem(sys.modules, "main", main)
    # the page warms the NER noindex cache first; this schema has no such table
    import util.facility_ner_noindex as ner
    monkeypatch.setattr(ner, "refresh_suppressed_slugs", lambda *_a, **_k: frozenset())
    conn = _connect(name)
    try:
        yield shape, conn
    finally:
        for c in opened + [conn]:
            try:
                c.close()
            except Exception:
                pass


_ROW_FIELDS = ("id", "canonical_slug", "duplicate_of_id", "is_duplicate",
               "address", "latitude", "longitude", "_src_table")


def _project(row):
    return None if row is None else tuple(row.get(k) for k in _ROW_FIELDS)


def test_the_batch_fetches_the_row_the_page_fetches(db):
    shape, conn = db
    page = {s: _project(fpp._fetch_facility_by_slug(s)) for s in MATRIX}
    cur = conn.cursor()
    rows = fpp._batch_page_rows(conn, cur, set(MATRIX),
                                fpp._canonical_slug_tables(conn, cur))
    batch = {s: _project(rows.get(s)) for s in MATRIX}
    differ = {s: (page[s], batch[s]) for s in MATRIX if page[s] != batch[s]}
    assert not differ, f"[{shape}] rows differ, as (page, batch): {differ}"
    # a floor: every lookup the page can make answered at least one slug here
    by = {(p[-1], p[1] == s) for s, p in page.items() if p}
    assert ("discovered_facilities", True) in by, by        # frozen slug, exactly
    assert ("discovered_facilities", False) in by, by       # hash8
    assert ("facilities", False) in by, by                  # hash8 on legacy
    f4 = page["cyrusone-cyrusone-am1-" + H_CYR1]
    if shape == "live":
        assert f4[2] is None, "the legacy exact lookup answered — it must raise live"
    else:
        assert f4[2] == "101", "the legacy exact lookup did not answer"


POINTERS = [101, 103, 105, 107, 109, 111, 114, 116, 126, 99999, "101", " 101 ",
            "7fb2abba31f0f7b4", "5480394207366128", None, 0]


def test_the_batch_names_the_keeper_the_page_names(db):
    _shape, conn = db
    page = {repr(p): fpp._canonical_twin_row(p) for p in POINTERS}
    keepers = fpp._batch_keeper_rows(conn.cursor(), POINTERS)
    batch = {repr(p): keepers.get(fpp._twin_key(p)) for p in POINTERS}
    assert page == batch
    assert sum(1 for k in page.values() if k) >= 6, "too few keepers to compare"
    assert page["'101'"] == page["101"], "a digit-string pointer must bind as the id"


def test_the_batch_follows_the_alias_the_page_follows(db):
    _shape, conn = db
    page = {s: fsf.resolve_alias(s) for s in MATRIX}
    targets = fpp._batch_alias_targets(conn.cursor(), MATRIX)
    assert page == {s: targets.get(s) for s in MATRIX}
    assert sum(1 for t in page.values() if t) >= 7, "too few aliases to compare"


def test_served_slugs_lands_where_the_page_lands(db, monkeypatch):
    shape, _conn = db
    monkeypatch.setattr(fpp, "_resolve_legacy_slug", lambda _s: None)
    page = {s: fpp.resolve_final_slug(s) for s in MATRIX}
    batch = fpp.served_slugs(MATRIX)
    differ = {s: (page[s], batch.get(s)) for s in MATRIX if page[s] != batch.get(s)}
    assert not differ, f"[{shape}] served_slugs differs, as (page, batch): {differ}"
    expected = dict(EXPECTED_COMMON, **EXPECTED_BY_SHAPE[shape])
    wrong = {s: (batch[s], want) for s, want in expected.items() if batch[s] != want}
    assert not wrong, f"[{shape}] both agree on a wrong answer, as (got, expected): {wrong}"


class _CountingConnection:
    """A real psycopg2 connection that counts statements — and exactly as capable
    as what it wraps. The per-request keeper lookup opens its cursor with `with`;
    a wrapper without that protocol made the lookup raise into its own except,
    so a batch that fell back to one keeper query per twin counted ZERO extra
    statements and passed. Caught by mutation, not by a run."""

    def __init__(self, conn, statements):
        self._conn, self._statements = conn, statements

    def cursor(self, *a, **k):
        return _CountingCursor(self._conn.cursor(*a, **k), self._statements)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def rollback(self):
        return self._conn.rollback()

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()


class _CountingCursor:
    def __init__(self, cur, statements):
        self._cur, self._statements = cur, statements

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cur.close()
        return False

    def execute(self, sql, params=None):
        self._statements.append(" ".join(str(sql).split())[:90])
        return self._cur.execute(sql, params)

    @property
    def description(self):
        return self._cur.description

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        return self._cur.close()


def test_the_batch_costs_statements_per_hop_not_per_slug(db, schema, monkeypatch):
    _shape, name = schema
    statements = []
    main = types.ModuleType("main")
    main.get_read_db = lambda: _CountingConnection(_connect(name), statements)
    monkeypatch.setitem(sys.modules, "main", main)
    # control: the wrapper does what the real connection does, `with` included,
    # or a per-request lookup fails inside its own except and is never counted
    probe = main.get_read_db()
    with probe.cursor() as cur:
        cur.execute("SELECT 1")
    probe.close()
    assert statements == ["SELECT 1"], statements
    statements.clear()
    fpp.served_slugs(MATRIX)
    assert 4 <= len(statements) <= 1 + 6 * 3, (
        f"{len(statements)} statements for {len(MATRIX)} slugs: {statements}")


def _carrier_module():
    spec = importlib.util.spec_from_file_location(
        "_served_slug_parity_carrier", ROOT / "carrier_facility_ingestion.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_carrier_endpoint_links_each_id_at_the_page_it_lands_on(db):
    shape, conn = db
    got = _carrier_module()._facility_slugs(
        conn.cursor(), ["102", "101", "130", "7fb2abba31f0f7b4", "bbbbbbbbbbbbbbb2",
                        "ccccccccccccccc3", "999999", None])
    assert got == {
        "102": K_AM4, "101": K_AM4, "130": K_AM4, "7fb2abba31f0f7b4": K_TH,
        "bbbbbbbbbbbbbbb2": ("ntt-tokyo-5-" + H_NTT if shape == "live"
                             else "ntt-ntt-tokyo-5-" + H_NTT),
        "ccccccccccccccc3": "ovh-rbx-8-" + H_OVH,
    }
