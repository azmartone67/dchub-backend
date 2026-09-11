"""tests/test_repair_step1_facility_records.py — the two-record repair script.

NO DATABASE, NO NETWORK. scripts/repair_step1_facility_records.py is loaded
from disk by path; its pure planner is driven with fixture rows, and main()
runs against FakeServer below.

★ THE FAKE IS DELIBERATELY NARROWER THAN psycopg2, never wider.
  * Its Python surface is a SUBSET of psycopg2's connection/cursor, asserted by
    test_the_fake_driver_has_no_capability_psycopg2_lacks. No __enter__, no
    catch-all __getattr__, so the script cannot come to rely on a protocol the
    real driver would not give it.
  * It models the property that matters for durability: writes are STAGED per
    connection, visible to that connection, published to other connections
    only by commit(), and discarded by rollback() or close().
  * It understands only the handful of SQL shapes the script emits. Anything
    else raises, so a statement the model cannot see fails loudly instead of
    being silently ignored.

Fixture strings are the real ones: MD5('Anthropic / Fluidstack|Anthropic New
York AI Campus')[:8] is 9df234ed and MD5('Compass Datacenters|Compass Goodyear
Campus (Phoenix)')[:8] is dc9da94a, so the hash8 path is exercised for real.

Run: python3 -m pytest tests/test_repair_step1_facility_records.py -rEf

MUTATION BATTERY (2026-09-11). Each defect applied once to the script (anchor
count exactly 1), __pycache__ purged, restore byte-identical, and a no-mutation
control green before and after. 17/17 killed; the test that goes red:
  M1  drop the Anthropic seeded-point precondition   rows_that_no_longer_carry_the_old_value...
  M2  drop the Compass power_mw = 100 precondition   rows_that_no_longer_carry_the_old_value...
  M3  add is_duplicate to the Anthropic SET          plan_matches_the_corrections_exactly...,
                                                     frozen_columns_never_appear_in_any_executed_write
  M4  _check_frozen finds nothing                    every_statement_builder_refuses... (x5),
                                                     the_planner_refuses_a_frozen_column...,
                                                     a_tampered_rollback_file_is_refused...
  M5  build_delete accepts any table                 only_carrier_rows_can_be_deleted_or_inserted
  M6  rollback file written AFTER the writes         apply_writes_the_rollback_file_before...
  M7  row cap 6 -> 10_000                            the_row_cap_aborts_before_any_write
  M8  dry run falls through to --apply               dry_run_executes_no_write... (x2)
  M9  carrier rows deleted without the flag          carrier_links_are_deleted_only_with_the_flag
  M10 rollback writes applied values, not prior      rollback_restores_every_prior_value... (x2)
  M11 read-back can never report a mismatch          a_commit_that_persists_nothing_is_caught...
  M12 replica guard removed                          writes_refuse_to_run_against_the_read_replica (x4)
  M13 no COMMIT at all                               apply_is_one_transaction_then_a_fresh_connection...
  M14 COMMIT after every statement                   apply_is_one_transaction_then_a_fresh_connection...
  M15 read-back on the write connection              apply_is_one_transaction_then_a_fresh_connection...
  M16 identity check removed                         a_row_at_the_seeded_point_that_is_another_facility...
  M17 UPDATE without the compare-and-swap predicate  rollback_will_not_clobber_a_row_edited_after_apply
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import re
import struct
from datetime import datetime, timezone
from decimal import Decimal

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "repair_step1_facility_records.py"


def _load_by_path(name, path, attr):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, attr), f"{path} loaded without {attr}: not the real module"
    return module


R = _load_by_path("_repair_step1_under_test", SCRIPT, "plan_correction")

DISCOVERED, LEGACY, CARRIER = R.DISCOVERED, R.LEGACY, R.CARRIER
FROZEN = {"canonical_slug", "name", "provider", "is_duplicate", "duplicate_of_id"}

A_SLUG = "anthropic-fluidstack-anthropic-new-york-ai-campus-9df234ed"
A_PROVIDER, A_NAME = "Anthropic / Fluidstack", "Anthropic New York AI Campus"
A_URL = ("https://www.anthropic.com/news/"
         "anthropic-invests-50-billion-in-american-ai-infrastructure")
C_SLUG = "compass-datacenters-compass-goodyear-campus-phoenix-dc9da94a"
C_PROVIDER, C_NAME = "Compass Datacenters", "Compass Goodyear Campus (Phoenix)"
C_URL = "https://www.compassdatacenters.com/data-center-markets/"
OSM_ATTRIBUTION = "© OpenStreetMap contributors, ODbL, way 723116259"

STAMP = "2026-09-11T12:00:00Z"
OLD_TS = "2026-01-02T03:04:05"


def CLOCK():
    return datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)


# Host-only DSNs: nothing here resembles a credential.
ENV = {"DATABASE_URL": "postgresql://ep-primary.example.invalid/neondb",
       "NEON_REPLICA_URL": "postgresql://ep-replica.example.invalid/neondb"}

# Column types as the DDL declares them (routes/discovery_routes.py:174-208,
# discovery_engine_v3.py:146-173, migrations/002_discovery_tables.sql:163-164,
# carrier_facility_ingestion.py:85-100).
SCHEMA = {
    DISCOVERED: {
        "id": "integer", "source": "text", "name": "text", "provider": "text",
        "city": "text", "state": "text", "country": "text", "market": "text",
        "latitude": "double precision", "longitude": "double precision",
        "power_mw": "real", "status": "text", "address": "text",
        "source_url": "text", "raw_data": "text", "last_updated": "text",
        "is_duplicate": "integer", "merged_facility_id": "text",
        "canonical_slug": "text", "duplicate_of_id": "integer",
    },
    LEGACY: {
        "id": "text", "name": "text", "provider": "text", "address": "text",
        "city": "text", "state": "text", "country": "text", "region": "text",
        "market": "text", "latitude": "real", "longitude": "real",
        "lat": "numeric(10,6)", "lon": "numeric(10,6)", "power_mw": "real",
        "sqft": "real", "status": "text", "source": "text", "source_url": "text",
        "raw_data": "text", "last_updated": "text", "canonical_slug": "text",
    },
    CARRIER: {
        "id": "integer", "carrier_pdb_id": "text", "carrier_name": "text",
        "facility_pdb_id": "text", "facility_name": "text",
        "dchub_facility_id": "text", "created_at": "timestamp without time zone",
    },
}


def disc(**over):
    row = dict(id=None, source="news_pipeline", name=A_NAME, provider=A_PROVIDER,
               city=None, state=None, country="US", market=None, latitude=None,
               longitude=None, power_mw=None, status=None, address=None,
               source_url=None, raw_data="{}", last_updated=OLD_TS, is_duplicate=0,
               merged_facility_id=None, canonical_slug=None, duplicate_of_id=None)
    row.update(over)
    return row


def leg(**over):
    row = dict(id=None, name=A_NAME, provider=A_PROVIDER, address=None, city=None,
               state=None, country="US", region=None, market=None, latitude=None,
               longitude=None, lat=None, lon=None, power_mw=None, sqft=0.0,
               status=None, source="news_pipeline", source_url=None, raw_data="{}",
               last_updated=OLD_TS, canonical_slug=None)
    row.update(over)
    return row


def cfp(**over):
    row = dict(id=None, carrier_pdb_id=None, carrier_name=None, facility_pdb_id=None,
               facility_name=None, dchub_facility_id=None,
               created_at="2026-07-17T00:00:00")
    row.update(over)
    return row


def fixture_tables():
    return {
        DISCOVERED: [
            disc(id=8055, city="New York", state="NY", market="NYC Tri-State",
                 latitude=40.7128, longitude=-74.006, power_mw=200.0,
                 status="Planned", source_url="https://www.anthropic.com",
                 merged_facility_id="proj_fixture_ny", canonical_slug=A_SLUG),
            # a duplicate ingest: same building, same point to 4dp, flagged
            disc(id=8056, city="New York", state="NY", market="NYC Tri-State",
                 latitude=40.712800004, longitude=-74.00600003, power_mw=200.0,
                 status="Planned", is_duplicate=1, canonical_slug=A_SLUG),
            disc(id=7631, name=C_NAME, provider=C_PROVIDER, source="providerwebsites",
                 city="Goodyear", state="AZ", market="Phoenix", latitude=33.4353,
                 longitude=-112.3587, power_mw=100.0, status="Under Construction",
                 source_url="https://example.invalid/old-compass",
                 merged_facility_id="fac_fixture_compass", canonical_slug=C_SLUG),
            # a sibling found by hash8 alone: no frozen slug, no coordinates
            disc(id=7632, name=C_NAME, provider=C_PROVIDER, source="osm",
                 city="Goodyear", state="AZ", power_mw=100.0),
            # CONTROL: the seeded point AND 200 MW, but a different facility
            disc(id=9000, name="NY5", provider="Equinix", city="Secaucus",
                 market="NYC Tri-State", latitude=40.7128, longitude=-74.006,
                 power_mw=200.0, canonical_slug="equinix-ny5-0a1b2c3d"),
        ],
        LEGACY: [
            leg(id="proj_fixture_ny", city="New York", state="NY",
                market="NYC Tri-State", latitude=40.7128, longitude=-74.006,
                power_mw=200.0, status="Planning", canonical_slug=A_SLUG),
            leg(id="fac_fixture_compass", name=C_NAME, provider=C_PROVIDER,
                city="Goodyear", market="Phoenix", latitude=33.4353,
                longitude=-112.3587, lat=33.4353, lon=-112.3587, power_mw=100.0,
                source="curated", canonical_slug=C_SLUG),
            leg(id="fac_unrelated", name="NY5", provider="Equinix",
                latitude=40.7128, longitude=-74.006, power_mw=200.0),
        ],
        CARRIER: [
            cfp(id=1, carrier_pdb_id="c1", carrier_name="Cogent", facility_pdb_id="p1",
                facility_name="60 Hudson", dchub_facility_id="8055"),
            cfp(id=2, carrier_pdb_id="c2", carrier_name="Zayo", facility_pdb_id="p1",
                facility_name="60 Hudson", dchub_facility_id="8055"),
            cfp(id=3, carrier_pdb_id="c3", carrier_name="Lumen", facility_pdb_id="p2",
                facility_name="111 8th Ave", dchub_facility_id="proj_fixture_ny"),
            cfp(id=4, carrier_pdb_id="c1", carrier_name="Cogent", facility_pdb_id="p3",
                facility_name="NY5", dchub_facility_id="9000"),
            cfp(id=5, carrier_pdb_id="c9", carrier_name="Cox", facility_pdb_id="p4",
                facility_name="Goodyear", dchub_facility_id="7631"),
        ],
    }


# ─── the fake driver ─────────────────────────────────────────────────────────

class FakeIntegrityError(Exception):
    pass


class FakeServer:
    """Committed state shared by every connection opened through connect()."""

    def __init__(self, tables=None, schema=None):
        tables = fixture_tables() if tables is None else tables
        self.schema = copy.deepcopy(SCHEMA if schema is None else schema)
        self.tables = {t: {str(r["id"]): dict(r) for r in rows}
                       for t, rows in tables.items()}
        self.events = []
        self.connect_calls = 0
        self.drop_commits = False      # model a commit that persists nothing
        self.before_first_write = None
        self._wrote = False

    def connect(self, dsn):
        self.connect_calls += 1
        conn = FakeConnection(self, self.connect_calls)
        self.events.append({"op": "connect", "conn": self.connect_calls})
        return conn

    # helpers for assertions
    def executed(self):
        return [e for e in self.events if e["op"] == "execute"]

    def writes(self):
        return [e for e in self.executed()
                if re.match(r"(UPDATE|DELETE|INSERT)\b", e["sql"])]

    def rows(self, table):
        return self.tables[table]


class FakeConnection:
    """A subset of psycopg2.extensions.connection."""

    def __init__(self, server, number):
        self._server = server
        self._number = number
        self._staged = None
        self._in_tx = False
        self._autocommit = False
        self.closed = 0

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        if self._in_tx:   # psycopg2 raises ProgrammingError here too
            raise RuntimeError("set_session cannot be used inside a transaction")
        self._autocommit = bool(value)

    def _open(self):
        if self.closed:
            raise RuntimeError("connection already closed")

    def _log(self, op):
        self._server.events.append({"op": op, "conn": self._number})

    def cursor(self):
        self._open()
        return FakeCursor(self)

    def commit(self):
        self._open()
        self._log("commit")
        if self._staged is not None and not self._server.drop_commits:
            self._server.tables = self._staged
        self._staged, self._in_tx = None, False

    def rollback(self):
        self._open()
        self._log("rollback")
        self._staged, self._in_tx = None, False

    def close(self):
        self._log("close")
        self._staged, self._in_tx = None, False   # uncommitted work is lost
        self.closed = 1


_SELECT = re.compile(r"^SELECT (?P<cols>.+?) FROM (?P<table>\w+)(?: WHERE (?P<where>.+))?$")
_UPDATE = re.compile(r"^UPDATE (?P<table>\w+) SET (?P<sets>.+?) WHERE (?P<where>.+)$")
_DELETE = re.compile(r"^DELETE FROM (?P<table>\w+) WHERE (?P<where>.+)$")
_INSERT = re.compile(r"^INSERT INTO (?P<table>\w+) \((?P<cols>[^)]+)\) "
                     r"VALUES \((?P<vals>[^)]+)\)$")
_ATOM_CAS = re.compile(r"^(?P<lhs>.+?) IS NOT DISTINCT FROM CAST\(%s AS (?P<type>.+)\)$")
_ATOM_ANY = re.compile(r"^(?P<lhs>.+?) = ANY\(%s(?:::text\[\])?\)$")
_ATOM_EQ = re.compile(r"^(?P<lhs>.+?) = %s$")
_HASH = "LEFT(MD5(COALESCE(provider,'')||'|'||COALESCE(name,'')),8)"


def _same(a, b, col_type=None):
    """The fake's own equality: independent of the script's values_equal."""
    if a is None or b is None:
        return a is None and b is None
    if col_type == "jsonb":
        load = lambda v: json.loads(v) if isinstance(v, str) else v  # noqa: E731
        return load(a) == load(b)
    numeric = (int, float, Decimal)
    if isinstance(a, numeric) and isinstance(b, numeric) and not isinstance(a, bool):
        if col_type == "real":
            f4 = lambda v: struct.unpack("f", struct.pack("f", float(v)))[0]  # noqa: E731
            return f4(a) == f4(b)
        return Decimal(str(a)) == Decimal(str(b))
    return a == b


class FakeCursor:
    """A subset of psycopg2.extensions.cursor."""

    def __init__(self, conn):
        self._conn = conn
        self._result = []
        self.description = None
        self.rowcount = -1

    def fetchone(self):
        return self._result.pop(0) if self._result else None

    def fetchall(self):
        out, self._result = self._result, []
        return out

    def close(self):
        pass

    def execute(self, query, vars=None):  # psycopg2's own parameter name
        conn = self._conn
        conn._open()
        server = conn._server
        sql = " ".join(query.split())
        params = list(vars) if vars is not None else []
        if sql.count("%s") != len(params):
            raise TypeError(f"{sql.count('%s')} placeholders, {len(params)} params: {sql}")
        server.events.append({"op": "execute", "conn": conn._number, "sql": sql,
                              "params": copy.deepcopy(params),
                              "autocommit": conn.autocommit})
        if not conn.autocommit:
            conn._in_tx = True
        self._result, self.description, self.rowcount = [], None, -1
        if sql.startswith("SET LOCAL "):
            return
        if "FROM pg_attribute" in sql:
            cols = list(server.schema.get(params[0], {}).items())
            self.description = (("attname",), ("format_type",))
            self._result, self.rowcount = cols, len(cols)
            return
        # An explicit flag, not `handler is not self._select`: every attribute
        # access builds a NEW bound method, so that identity test is always
        # true and fired the first-write hook on the first SELECT.
        for pattern, handler, is_write in ((_SELECT, self._select, False),
                                           (_UPDATE, self._update, True),
                                           (_DELETE, self._delete, True),
                                           (_INSERT, self._insert, True)):
            match = pattern.match(sql)
            if match:
                if is_write:
                    self._first_write_hook()
                handler(match, params)
                return
        raise AssertionError(f"FakeCursor does not understand this SQL: {sql}")

    # ── internals ──
    def _view(self):
        conn = self._conn
        return conn._staged if conn._staged is not None else conn._server.tables

    def _stage(self):
        conn = self._conn
        if conn._staged is None:
            conn._staged = copy.deepcopy(conn._server.tables)
        return conn._staged

    def _after_write(self):
        if self._conn.autocommit:
            self._conn.commit()

    def _first_write_hook(self):
        server = self._conn._server
        if not server._wrote:
            server._wrote = True
            server.events.append({"op": "first_write", "conn": self._conn._number})
            if server.before_first_write is not None:
                server.before_first_write()

    def _types(self, table):
        if table not in self._conn._server.schema:
            raise AssertionError(f"relation {table} does not exist")
        return self._conn._server.schema[table]

    def _value(self, table, row, expr):
        types = self._types(table)
        if expr == _HASH:
            text = (row.get("provider") or "") + "|" + (row.get("name") or "")
            return hashlib.md5(text.encode("utf-8")).hexdigest()[:8], "text"
        match = re.match(r"^(\w+)(?:::(text|jsonb))?$", expr)
        if not match or match.group(1) not in types:
            raise AssertionError(f"column expression not modelled: {expr!r} on {table}")
        col, cast = match.groups()
        value = row.get(col)
        if cast == "text" and value is not None:
            return str(value), "text"
        return value, (cast or types[col])

    def _where(self, table, where, params):
        """Compile `a OR b AND c` (no parentheses) into a row predicate."""
        terms, index = [], 0
        for term in where.split(" OR "):
            atoms = []
            for atom in term.split(" AND "):
                for kind, pattern in (("cas", _ATOM_CAS), ("any", _ATOM_ANY), ("eq", _ATOM_EQ)):
                    match = pattern.match(atom)
                    if match:
                        atoms.append((kind, match.group("lhs"), params[index]))
                        index += 1
                        break
                else:
                    raise AssertionError(f"WHERE atom not modelled: {atom!r}")
            terms.append(atoms)

        def test(row):
            for atoms in terms:
                ok = True
                for kind, lhs, param in atoms:
                    value, col_type = self._value(table, row, lhs)
                    if kind == "cas":
                        ok = _same(value, param, col_type)
                    elif kind == "any":
                        ok = value is not None and any(_same(value, p) for p in param)
                    else:
                        ok = value is not None and _same(value, param)
                    if not ok:
                        break
                if ok:
                    return True
            return False
        return test

    def _select(self, match, params):
        table = match.group("table")
        types = self._types(table)
        names, exprs = [], []
        for item in match.group("cols").split(", "):
            m = re.match(r"^(\w+)(?:::text AS (\w+))?$", item)
            if not m or m.group(1) not in types:
                raise AssertionError(f"select item not modelled: {item!r}")
            exprs.append(m.group(1))
            names.append(m.group(2) or m.group(1))
        test = self._where(table, match.group("where"), params) if match.group("where") else (lambda r: True)
        rows = [r for r in self._view()[table].values() if test(r)]
        self.description = tuple((n,) for n in names)
        self._result = [tuple(r.get(e) for e in exprs) for r in rows]
        self.rowcount = len(self._result)

    def _update(self, match, params):
        table = match.group("table")
        types = self._types(table)
        sets = []
        for item in match.group("sets").split(", "):
            m = re.match(r"^(\w+) = %s$", item)
            if not m or m.group(1) not in types:
                raise AssertionError(f"SET item not modelled: {item!r}")
            sets.append(m.group(1))
        values, rest = params[:len(sets)], params[len(sets):]
        test = self._where(table, match.group("where"), rest)
        count = 0
        for row in self._stage()[table].values():
            if test(row):
                row.update(dict(zip(sets, values)))
                count += 1
        self.rowcount = count
        self._after_write()

    def _delete(self, match, params):
        table = match.group("table")
        test = self._where(table, match.group("where"), params)
        staged = self._stage()[table]
        doomed = [key for key, row in staged.items() if test(row)]
        for key in doomed:
            del staged[key]
        self.rowcount = len(doomed)
        self._after_write()

    def _insert(self, match, params):
        table = match.group("table")
        types = self._types(table)
        cols = match.group("cols").split(", ")
        if any(c not in types for c in cols) or match.group("vals") != ", ".join(["%s"] * len(cols)):
            raise AssertionError(f"INSERT not modelled: {match.group(0)}")
        row = dict(zip(cols, params))
        staged = self._stage()[table]
        if str(row.get("id")) in staged:
            raise FakeIntegrityError(f"duplicate key id={row.get('id')}")
        staged[str(row["id"])] = row
        self.rowcount = 1
        self._after_write()


# ─── helpers ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _cwd_in_tmp_path(tmp_path, monkeypatch):
    """Every test runs with the working directory in its own tmp_path.

    --apply without --rollback-out writes into the working directory. The
    mutation battery showed a guard regression (the replica refusal removed)
    turning a refusal test into a real --apply that dropped a rollback file in
    the repository root. No test may be able to write into the repo.
    """
    monkeypatch.chdir(tmp_path)


def _row(tables, table, row_id):
    return next(r for r in tables[table] if r["id"] == row_id)


def _changes(plan):
    return {(c.table, c.id): (c.set, c.prior) for c in plan.changes}


def _run(server, *argv, env=ENV):
    return R.main(list(argv), connect=server.connect, environ=env, clock=CLOCK)


def _written_columns(sql):
    m = re.match(r"UPDATE (\w+) SET (.+?) WHERE ", sql)
    if m:
        return m.group(1), [item.split(" = ")[0] for item in m.group(2).split(", ")]
    m = re.match(r"INSERT INTO (\w+) \(([^)]+)\)", sql)
    if m:
        return m.group(1), m.group(2).split(", ")
    m = re.match(r"DELETE FROM (\w+) WHERE (.+)$", sql)
    if m:
        return m.group(1), re.findall(r"(\w+)(?:::\w+(?:\[\])?)? (?:=|IS)", m.group(2))
    raise AssertionError(f"not a write: {sql}")


# ─── the planner ─────────────────────────────────────────────────────────────

def test_plan_matches_the_corrections_exactly_for_both_tables():
    t = fixture_tables()
    anthropic = R.plan_correction(
        R.CORRECTIONS[0], [_row(t, DISCOVERED, 8055), _row(t, DISCOVERED, 8056)],
        [_row(t, LEGACY, "proj_fixture_ny")], SCHEMA, STAMP)
    nulled = {"latitude": None, "longitude": None, "city": None, "market": None,
              "power_mw": None}
    assert _changes(anthropic) == {
        (DISCOVERED, 8055): (
            {**nulled, "source_url": A_URL, "last_updated": STAMP},
            {"latitude": 40.7128, "longitude": -74.006, "city": "New York",
             "market": "NYC Tri-State", "power_mw": 200.0,
             "source_url": "https://www.anthropic.com", "last_updated": OLD_TS}),
        (DISCOVERED, 8056): (
            {**nulled, "source_url": A_URL, "last_updated": STAMP},
            {"latitude": 40.712800004, "longitude": -74.00600003, "city": "New York",
             "market": "NYC Tri-State", "power_mw": 200.0, "source_url": None,
             "last_updated": OLD_TS}),
        # region was already NULL and lat/lon never held the point: not written
        (LEGACY, "proj_fixture_ny"): (
            nulled,
            {"latitude": 40.7128, "longitude": -74.006, "city": "New York",
             "market": "NYC Tri-State", "power_mw": 200.0}),
    }
    for change in anthropic.changes:  # absent is NULL — never '' or 0
        assert all(change.set[c] is None for c in nulled)

    compass = R.plan_correction(
        R.CORRECTIONS[1], [_row(t, DISCOVERED, 7631), _row(t, DISCOVERED, 7632)],
        [_row(t, LEGACY, "fac_fixture_compass")], SCHEMA, STAMP)
    fixed = {"power_mw": 212, "latitude": 33.4418, "longitude": -112.3795,
             "source_url": C_URL, "last_updated": STAMP}
    assert _changes(compass) == {
        (DISCOVERED, 7631): (fixed, {
            "power_mw": 100.0, "latitude": 33.4353, "longitude": -112.3587,
            "source_url": "https://example.invalid/old-compass", "last_updated": OLD_TS}),
        (DISCOVERED, 7632): (fixed, {
            "power_mw": 100.0, "latitude": None, "longitude": None,
            "source_url": None, "last_updated": OLD_TS}),
        (LEGACY, "fac_fixture_compass"): (
            {"power_mw": 212, "latitude": 33.4418, "longitude": -112.3795,
             "lat": 33.4418, "lon": -112.3795},
            {"power_mw": 100.0, "latitude": 33.4353, "longitude": -112.3587,
             "lat": 33.4353, "lon": -112.3587}),
    }
    assert any("id=7632" in n and "not the researched" in n for n in compass.notes)


def test_a_row_at_the_seeded_point_that_is_another_facility_is_never_planned():
    control = _row(fixture_tables(), DISCOVERED, 9000)
    plan = R.plan_correction(R.CORRECTIONS[0], [control], [], SCHEMA, STAMP)
    assert plan.changes == []
    assert [s.reason for s in plan.skipped] == [
        "not this facility (no slug, hash8 or merged link)"]


def test_rows_that_no_longer_carry_the_old_value_are_skipped_and_reported():
    anthropic_disc = [
        disc(id=1, canonical_slug=A_SLUG, source_url=A_URL),              # corrected
        disc(id=2, canonical_slug=A_SLUG, city="Albany", latitude=42.65,  # someone else's edit
             longitude=-73.75, merged_facility_id="L1"),
    ]
    anthropic_leg = [leg(id="L1", canonical_slug=A_SLUG, latitude=42.65,
                         longitude=-73.75, power_mw=200.0)]
    plan = R.plan_correction(R.CORRECTIONS[0], anthropic_disc, anthropic_leg,
                             SCHEMA, STAMP)
    assert plan.changes == []
    changed = "changed: no longer carries the value being corrected"
    assert {(s.table, s.id): s.reason for s in plan.skipped} == {
        (DISCOVERED, 1): "already applied", (DISCOVERED, 2): changed,
        (LEGACY, "L1"): changed}
    assert len(plan.warnings) == 1
    assert "COALESCE(df.power_mw, f.power_mw)" in plan.warnings[0]

    compass_disc = [
        disc(id=3, name=C_NAME, provider=C_PROVIDER, power_mw=212.0,
             latitude=33.4418, longitude=-112.3795, source_url=C_URL),
        disc(id=4, name=C_NAME, provider=C_PROVIDER, power_mw=150.0,
             latitude=33.4353, longitude=-112.3587),
    ]
    compass_leg = [leg(id="L2", name=C_NAME, provider=C_PROVIDER, power_mw=212.0,
                       latitude=33.4418, longitude=-112.3795)]
    plan = R.plan_correction(R.CORRECTIONS[1], compass_disc, compass_leg, SCHEMA, STAMP)
    assert plan.changes == []
    assert {(s.table, s.id): s.reason for s in plan.skipped} == {
        (DISCOVERED, 3): "already applied", (DISCOVERED, 4): changed,
        (LEGACY, "L2"): "already applied"}


def test_osm_provenance_goes_into_raw_data_only_when_the_column_is_json():
    row = _row(fixture_tables(), DISCOVERED, 7631)
    row["raw_data"] = '{"source": "providerwebsites"}'
    json_schema = copy.deepcopy(SCHEMA)
    json_schema[DISCOVERED]["raw_data"] = "jsonb"
    (change,) = R.plan_correction(R.CORRECTIONS[1], [row], [], json_schema, STAMP).changes
    doc = json.loads(change.set["raw_data"])
    assert doc["source"] == "providerwebsites"
    assert doc["manual_correction"]["coordinates"]["attribution"] == OSM_ATTRIBUTION
    assert doc["manual_correction"]["power_mw"]["source_url"] == C_URL
    assert change.prior["raw_data"] == '{"source": "providerwebsites"}'

    text_plan = R.plan_correction(R.CORRECTIONS[1], [row], [], SCHEMA, STAMP)
    assert "raw_data" not in text_plan.changes[0].set
    assert any("not json/jsonb" in n for n in text_plan.notes)


# ─── frozen columns ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("column", sorted(FROZEN))
def test_every_statement_builder_refuses_a_frozen_column(column):
    types = dict(SCHEMA[DISCOVERED])
    with pytest.raises(R.FrozenColumnError):
        R.build_update(DISCOVERED, 1, {column: "x"}, {column: "y"}, types)
    with pytest.raises(R.FrozenColumnError):
        R.build_update(LEGACY, "a", {"city": None, column: "x"},
                       {"city": "b", column: "y"}, types)
    with pytest.raises(R.FrozenColumnError):
        R.build_insert(CARRIER, {"id": 1, column: "x"})
    sql, _ = R.build_update(DISCOVERED, 1, {"city": None}, {"city": "x"}, types)
    assert sql.startswith("UPDATE discovered_facilities SET city = %s WHERE id = %s")


def test_only_carrier_rows_can_be_deleted_or_inserted():
    with pytest.raises(R.FrozenColumnError):
        R.build_delete(DISCOVERED, [1], ["1"])
    with pytest.raises(R.FrozenColumnError):
        R.build_insert(LEGACY, {"id": "x"})
    with pytest.raises(R.FrozenColumnError):
        R.build_update(CARRIER, 1, {"carrier_name": None}, {"carrier_name": "x"},
                       SCHEMA[CARRIER])
    sql, _ = R.build_delete(CARRIER, [1], ["8055"])
    assert sql.startswith("DELETE FROM carrier_facility_presence WHERE id = ANY(%s)")


def test_the_planner_refuses_a_frozen_column_before_any_statement_exists():
    class Tampered(type(R.CORRECTIONS[0])):
        def decide(self, table, row, cols, stamp):
            state, target = super().decide(table, row, cols, stamp)
            if state == R.PLANNED:
                target["is_duplicate"] = row.get("is_duplicate")  # even a no-op write
            return state, target
    with pytest.raises(R.FrozenColumnError):
        R.plan_correction(Tampered(), [_row(fixture_tables(), DISCOVERED, 8055)], [],
                          SCHEMA, STAMP)


def test_frozen_columns_never_appear_in_any_executed_write(tmp_path):
    server = FakeServer()
    rb = tmp_path / "rb.json"
    assert _run(server, "--apply", "--include-carrier-links", "--rollback-out", str(rb)) == 0
    assert _run(server, "--rollback", str(rb)) == 0
    writes = server.writes()
    kinds = {w["sql"].split()[0] for w in writes}
    assert kinds == {"UPDATE", "DELETE", "INSERT"}, f"vacuous: only {kinds} ran"
    for write in writes:
        table, columns = _written_columns(write["sql"])
        assert columns and not set(columns) & FROZEN, write["sql"]
        if not write["sql"].startswith("UPDATE"):
            assert table == CARRIER, write["sql"]


def test_a_tampered_rollback_file_is_refused_before_any_write(tmp_path):
    server = FakeServer()
    rb = tmp_path / "rb.json"
    assert _run(server, "--apply", "--rollback-out", str(rb)) == 0
    payload = json.loads(rb.read_text(encoding="utf-8"))
    payload["changes"][0]["prior"]["is_duplicate"] = 1
    payload["changes"][0]["set"]["is_duplicate"] = 0
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    before = len(server.writes())
    assert _run(server, "--rollback", str(tampered)) == 2
    assert len(server.writes()) == before, "a write ran before the refusal"


# ─── safety rails ────────────────────────────────────────────────────────────

def _server_with_anthropic_siblings(n):
    t = fixture_tables()
    t[DISCOVERED] = ([r for r in t[DISCOVERED] if r["provider"] != A_PROVIDER]
                     + [disc(id=100 + i, canonical_slug=A_SLUG, city="New York",
                             latitude=40.7128, longitude=-74.006, power_mw=200.0)
                        for i in range(n)])
    t[LEGACY] = [r for r in t[LEGACY] if r["provider"] != A_PROVIDER]
    t[CARRIER] = []
    return FakeServer(t)


def test_the_row_cap_aborts_before_any_write(tmp_path, capsys):
    server = _server_with_anthropic_siblings(7)
    rb = tmp_path / "rb.json"
    assert _run(server, "--apply", "--rollback-out", str(rb)) == 3
    assert server.writes() == []
    assert not rb.exists()
    assert not any(e["op"] == "commit" for e in server.events)
    assert "more than 6 rows would change" in capsys.readouterr().err


def test_the_row_cap_lets_exactly_six_through(tmp_path):
    server = _server_with_anthropic_siblings(6)
    assert _run(server, "--apply", "--rollback-out", str(tmp_path / "rb.json")) == 0
    rows = server.rows(DISCOVERED)
    assert all(rows[str(100 + i)]["latitude"] is None for i in range(6))


@pytest.mark.parametrize("flags", [[], ["--include-carrier-links"]])
def test_dry_run_executes_no_write_and_leaves_no_file(tmp_path, monkeypatch, capsys, flags):
    monkeypatch.chdir(tmp_path)
    server = FakeServer()
    before = copy.deepcopy(server.tables)
    assert _run(server, *flags) == 0
    executed = [e["sql"] for e in server.executed()]
    assert any(s.startswith("SELECT") and "FROM discovered_facilities" in s
               for s in executed), "vacuous: the dry run never read"
    assert server.writes() == []
    assert not any(e["op"] == "commit" for e in server.events)
    assert list(tmp_path.iterdir()) == []
    assert server.tables == before
    out = capsys.readouterr().out
    assert "UPDATE discovered_facilities SET latitude = %s" in out, "vacuous: no plan printed"
    assert "DRY RUN: nothing written" in out


@pytest.mark.parametrize("flag", ["--apply", "--rollback"])
@pytest.mark.parametrize("replica_var", ["NEON_REPLICA_URL", "DATABASE_READ_URL"])
def test_writes_refuse_to_run_against_the_read_replica(tmp_path, capsys, flag, replica_var):
    rb = tmp_path / "rb.json"   # a REAL rollback file, so only the guard can stop the run
    assert _run(FakeServer(), "--apply", "--rollback-out", str(rb)) == 0
    capsys.readouterr()
    replica = "postgresql://ep-replica.example.invalid/neondb"
    server = FakeServer()
    argv = [flag, str(rb)] if flag == "--rollback" else [flag]
    assert _run(server, *argv, env={"DATABASE_URL": replica, replica_var: replica}) == 2
    assert server.connect_calls == 0
    err = capsys.readouterr().err
    assert replica_var in err and "example.invalid" not in err  # the name, never the value


def test_the_replica_guard_compares_endpoints_not_only_strings():
    assert R.replica_refusal({
        "DATABASE_URL": "postgresql://EP-Replica.example.invalid:5432/neondb?sslmode=require",
        "NEON_REPLICA_URL": "postgresql://ep-replica.example.invalid:5432/neondb"})
    assert R.replica_refusal(dict(ENV)) is None


def test_a_database_with_neither_facility_is_not_reported_as_success(tmp_path):
    t = fixture_tables()
    t[DISCOVERED] = [_row(t, DISCOVERED, 9000)]
    t[LEGACY] = []
    server = FakeServer(t)
    assert _run(server, "--apply", "--rollback-out", str(tmp_path / "rb.json")) == 4
    assert server.writes() == []


# ─── --apply ─────────────────────────────────────────────────────────────────

def test_apply_writes_the_rollback_file_before_the_first_update(tmp_path):
    server = FakeServer()
    rb = tmp_path / "rb.json"
    seen = {}

    def at_first_write():
        seen["exists"] = rb.exists()
        seen["payload"] = (json.loads(rb.read_text(encoding="utf-8"))
                           if rb.exists() else None)
    server.before_first_write = at_first_write
    assert _run(server, "--apply", "--include-carrier-links", "--rollback-out", str(rb)) == 0
    assert seen, "vacuous: no write reached the fake"
    assert seen["exists"], "the first write ran before the rollback file existed"
    payload = seen["payload"]
    priors = {(c["table"], c["id"]): c["prior"] for c in payload["changes"]}
    assert len(priors) == 6
    assert priors[(DISCOVERED, 8055)]["latitude"] == 40.7128
    assert priors[(DISCOVERED, 8055)]["power_mw"] == 200.0
    assert priors[(LEGACY, "fac_fixture_compass")]["power_mw"] == 100.0
    assert sorted(r["id"] for r in payload["carrier_rows"]) == [1, 2, 3]
    compass = next(c for c in payload["corrections"] if c["slug"] == C_SLUG)
    assert compass["provenance"]["coordinates"]["attribution"] == OSM_ATTRIBUTION


def test_apply_is_one_transaction_then_a_fresh_connection_reads_back(tmp_path):
    server = FakeServer()
    assert _run(server, "--apply", "--include-carrier-links",
                "--rollback-out", str(tmp_path / "rb.json")) == 0
    ev = server.events
    writes = [i for i, e in enumerate(ev) if e["op"] == "execute"
              and re.match(r"(UPDATE|DELETE|INSERT)\b", e["sql"])]
    assert len(writes) == 7                          # 6 UPDATEs + 1 DELETE
    (conn,) = {ev[i]["conn"] for i in writes}
    assert all(ev[i]["autocommit"] is False for i in writes)
    settings = [i for i, e in enumerate(ev) if e["op"] == "execute"
                and e["conn"] == conn and e["sql"].startswith("SET LOCAL")]
    assert len(settings) == 2 and max(settings) < min(writes)
    commits = [i for i, e in enumerate(ev) if e["op"] == "commit" and e["conn"] == conn]
    assert len(commits) == 1 and commits[0] > max(writes)
    later = [i for i, e in enumerate(ev) if e["op"] == "connect" and e["conn"] != conn]
    assert later and min(later) > commits[0], "read-back must use a NEW connection after COMMIT"
    reader = ev[min(later)]["conn"]
    assert any(e["op"] == "execute" and e["conn"] == reader
               and "FROM discovered_facilities WHERE id::text = ANY" in e["sql"] for e in ev)

    d = server.rows(DISCOVERED)
    assert (d["8055"]["latitude"], d["8055"]["source_url"], d["8055"]["last_updated"]) \
        == (None, A_URL, STAMP)
    assert (d["7631"]["power_mw"], d["7631"]["latitude"]) == (212, 33.4418)
    assert d["8055"]["status"] == "Planned" and d["7631"]["status"] == "Under Construction"
    assert d["9000"]["latitude"] == 40.7128, "the control facility was touched"
    assert server.rows(LEGACY)["fac_unrelated"]["power_mw"] == 200.0


def test_a_commit_that_persists_nothing_is_caught_by_the_read_back(tmp_path, capsys):
    server = FakeServer()
    server.drop_commits = True
    assert _run(server, "--apply", "--rollback-out", str(tmp_path / "rb.json")) == 1
    err = capsys.readouterr().err
    assert "READ-BACK MISMATCH" in err
    assert "discovered_facilities id=8055 latitude" in err


def test_carrier_links_are_deleted_only_with_the_flag(tmp_path):
    plain = FakeServer()
    assert _run(plain, "--apply", "--rollback-out", str(tmp_path / "a.json")) == 0
    assert not any(w["sql"].startswith("DELETE") for w in plain.writes())
    assert set(plain.rows(CARRIER)) == {"1", "2", "3", "4", "5"}
    assert json.loads((tmp_path / "a.json").read_text())["carrier_rows"] == []

    flagged = FakeServer()
    assert _run(flagged, "--apply", "--include-carrier-links",
                "--rollback-out", str(tmp_path / "b.json")) == 0
    assert len([w for w in flagged.writes() if w["sql"].startswith("DELETE")]) == 1
    assert set(flagged.rows(CARRIER)) == {"4", "5"}   # NY5 and Compass untouched
    exported = json.loads((tmp_path / "b.json").read_text())
    assert sorted(r["id"] for r in exported["carrier_rows"]) == [1, 2, 3]
    assert exported["carrier_facility_ids"] == ["8055", "8056", "proj_fixture_ny"]


def test_carrier_links_can_still_be_removed_after_the_records_were_fixed(tmp_path):
    server = FakeServer()
    assert _run(server, "--apply", "--rollback-out", str(tmp_path / "a.json")) == 0
    assert _run(server, "--apply", "--include-carrier-links",
                "--rollback-out", str(tmp_path / "b.json")) == 0
    assert set(server.rows(CARRIER)) == {"4", "5"}


def test_a_second_apply_is_a_no_op(tmp_path, capsys):
    server = FakeServer()
    assert _run(server, "--apply", "--rollback-out", str(tmp_path / "a.json")) == 0
    count = len(server.writes())
    assert _run(server, "--apply", "--rollback-out", str(tmp_path / "b.json")) == 0
    assert len(server.writes()) == count
    assert not (tmp_path / "b.json").exists()
    assert "Nothing to write" in capsys.readouterr().out


# ─── --rollback ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw_type", ["text", "jsonb"])
def test_rollback_restores_every_prior_value_and_carrier_row(tmp_path, raw_type):
    schema = copy.deepcopy(SCHEMA)
    schema[DISCOVERED]["raw_data"] = raw_type
    server = FakeServer(schema=schema)
    original = copy.deepcopy(server.tables)
    rb = tmp_path / "rb.json"
    assert _run(server, "--apply", "--include-carrier-links", "--rollback-out", str(rb)) == 0
    assert server.tables != original, "vacuous: --apply changed nothing"
    if raw_type == "jsonb":
        assert "manual_correction" in server.rows(DISCOVERED)["7631"]["raw_data"]
    assert _run(server, "--rollback", str(rb)) == 0
    assert server.tables == original


def test_rollback_will_not_clobber_a_row_edited_after_apply(tmp_path, capsys):
    server = FakeServer()
    rb = tmp_path / "rb.json"
    assert _run(server, "--apply", "--rollback-out", str(rb)) == 0
    server.tables[DISCOVERED]["7631"]["power_mw"] = 250.0   # a later, legitimate edit
    assert _run(server, "--rollback", str(rb)) == 1
    assert server.rows(DISCOVERED)["8055"]["latitude"] is None, "partially rolled back"
    assert server.rows(DISCOVERED)["7631"]["power_mw"] == 250.0
    assert "expected 1 row(s), got 0" in capsys.readouterr().err


# ─── pins and the fake itself ────────────────────────────────────────────────

def test_the_hash_expression_is_the_one_the_api_uses():
    slug_module = _load_by_path("_facility_slug_probe", ROOT / "routes" / "facility_slug.py",
                                "hash_sql")
    assert R.HASH8_SQL == slug_module.hash_sql("") == _HASH
    for (provider, name, slug), correction in zip(
            ((A_PROVIDER, A_NAME, A_SLUG), (C_PROVIDER, C_NAME, C_SLUG)), R.CORRECTIONS):
        digest = hashlib.md5(f"{provider}|{name}".encode("utf-8")).hexdigest()[:8]
        assert digest == slug[-8:] == correction.hash8
        assert correction.slug == slug


def test_the_dry_run_predicts_whether_the_anthropic_page_goes_contentless(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # a regressed dry run must not drop a rollback file in the repo
    assert _run(FakeServer()) == 0
    assert ("PAGE OUTCOME after plan: all 2 live row(s) carrying the slug are "
            "contentless") in capsys.readouterr().out
    t = fixture_tables()
    _row(t, DISCOVERED, 8055)["address"] = "New York, NY"
    assert _run(FakeServer(t)) == 0
    assert ("NOT contentless; evidence remains on discovered_facilities id=8055: "
            "address") in capsys.readouterr().out


def test_the_fake_driver_has_no_capability_psycopg2_lacks():
    import psycopg2.extensions as ext   # a real dependency (requirements.txt); never skipped
    assert callable(getattr(ext.cursor, "execute", None)), "psycopg2 is a stub here"
    conn = FakeServer().connect("dsn")
    cur = conn.cursor()
    for fake, real in ((conn, ext.connection), (cur, ext.cursor)):
        extra = {n for n in dir(fake) if not n.startswith("_")} - set(dir(real))
        assert not extra, f"{type(fake).__name__} offers {sorted(extra)} psycopg2 lacks"
    assert not hasattr(conn, "__enter__") and not hasattr(cur, "__enter__")
