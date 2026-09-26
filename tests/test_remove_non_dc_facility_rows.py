"""tests/test_remove_non_dc_facility_rows.py — "Golds Gym Ashburn" leaves the data.

Live 2026-09-25: /api/v1/facilities?query=Ashburn served id 10669 "Golds Gym
Ashburn" as v=verified, with 38 carriers borrowed from the adjacent DataBank
Ashburn (IAD1). scripts/remove_non_dc_facility_rows.py removes it (dry run by
default, rollback file first, one transaction), and discovery_engine_v3's
is_valid_datacenter refuses the name so re-ingestion cannot bring it back.

The Postgres half runs with NON_DC_REPAIR_DSN set, e.g.
  NON_DC_REPAIR_DSN=postgresql://postgres@localhost:55432/frozen_slug
"""
import importlib.util
import json
import os
import pathlib
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rm_non_dc", ROOT / "scripts" / "remove_non_dc_facility_rows.py")
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)


# ── the ingest guard ─────────────────────────────────────────────────────
def test_discovery_refuses_the_gym_even_from_a_trusted_source():
    from discovery_engine_v3 import is_valid_datacenter
    for src in ("osm", "peeringdb", ""):
        assert is_valid_datacenter("Golds Gym Ashburn", "", src) is False
        assert is_valid_datacenter("Gold's Gym", "", src) is False
    for name in ("Gym Data Center LLC", "Equinix DC2", "DataBank Ashburn (IAD1)", "Gymea Exchange"):
        assert is_valid_datacenter(name, "", "peeringdb") is True, name


def test_the_target_is_pinned_to_the_live_row():
    from routes.facility_slug import stable_hash8
    t = rm.TARGETS[0]
    assert t["id"] == 10669
    assert stable_hash8("Golds Gym Ashburn", "Golds Gym Ashburn") == t["hash8"]
    assert t["slug"].endswith("-" + t["hash8"])


def test_the_unknown_provider_twin_is_a_target_too():
    from routes.facility_slug import stable_hash8
    t = {x["key"]: x for x in rm.TARGETS}["golds-gym-ashburn-unknown"]
    assert t["id"] == 19001
    assert stable_hash8("Unknown", "Golds Gym Ashburn") == t["hash8"]
    assert t["slug"].endswith("-" + t["hash8"])


def test_replica_is_refused():
    env = {"NEON_REPLICA_URL": "postgresql://u@replica.example:5432/db"}
    with pytest.raises(rm.Refused) as e:
        rm.refuse_replica("postgresql://other@replica.example:5432/db", env)
    assert e.value.code == 2
    rm.refuse_replica("postgresql://u@primary.example:5432/db", env)


# ── Postgres: plan, apply, read back, roll back ──────────────────────────
DSN = os.environ.get("NON_DC_REPAIR_DSN", "").strip()


@pytest.fixture
def pg():
    if not DSN:
        pytest.skip("NON_DC_REPAIR_DSN not set — no Postgres to run against")
    import psycopg2
    schema = "nd_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE SCHEMA {schema}")
    dsn = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{schema}"
    conn = psycopg2.connect(dsn)
    c = conn.cursor()
    c.execute("""CREATE TABLE discovered_facilities (id INT PRIMARY KEY, name TEXT, provider TEXT,
                 city TEXT, source TEXT, canonical_slug TEXT, merged_facility_id TEXT,
                 latitude DOUBLE PRECISION, raw_data JSONB, last_updated TIMESTAMPTZ)""")
    c.execute("CREATE TABLE facilities (id TEXT PRIMARY KEY, name TEXT, provider TEXT)")
    c.execute("CREATE TABLE carrier_facility_presence (id SERIAL PRIMARY KEY, dchub_facility_id TEXT, carrier_name TEXT)")
    c.execute("CREATE TABLE facility_slug_aliases (old_slug TEXT PRIMARY KEY, canonical_slug TEXT NOT NULL)")
    slug = rm.TARGETS[0]["slug"]
    c.execute("INSERT INTO discovered_facilities VALUES (10669, 'Golds Gym Ashburn', 'Golds Gym Ashburn', "
              "'Ashburn', 'osm', %s, 'abc123', 39.02, '{\"k\": 1}', '2026-01-02T03:04:05Z')", (slug,))
    # A twin that was renamed into a real facility must be KEPT and reported.
    c.execute("INSERT INTO discovered_facilities VALUES (20001, 'DataBank Ashburn (IAD1)', 'DataBank', "
              "'Ashburn', 'peeringdb', %s, NULL, 39.016, NULL, NULL)", (slug,))
    c.execute("INSERT INTO discovered_facilities VALUES (1223, 'DataBank IAD1', 'DataBank', 'Ashburn', "
              "'peeringdb', 'databank-iad1-11111111', NULL, 39.016, NULL, NULL)")
    c.execute("INSERT INTO discovered_facilities VALUES (19001, 'Golds Gym Ashburn', 'Unknown', 'Ashburn', "
              "'osm', 'unknown-golds-gym-ashburn-b3e77583', NULL, 39.02, NULL, NULL)")
    c.execute("INSERT INTO facilities VALUES ('abc123', 'Golds Gym Ashburn', 'Golds Gym Ashburn')")
    c.execute("INSERT INTO carrier_facility_presence (dchub_facility_id, carrier_name) VALUES "
              "('10669', 'Cogent'), ('abc123', 'Akamai'), ('1223', 'Cogent')")
    c.execute("INSERT INTO facility_slug_aliases VALUES ('golds-gym-ashburn-deadbeef', %s)", (slug,))
    conn.commit()
    yield conn, dsn
    conn.close()
    admin.cursor().execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


def _ids(conn, table, col="id"):
    c = conn.cursor()
    c.execute(f"SELECT {col} FROM {table} ORDER BY 1")
    return [r[0] for r in c.fetchall()]


def test_pg_plan_takes_the_gym_and_nothing_else(pg):
    conn, _ = pg
    plans = rm.build_plan(conn.cursor())
    plan, kept = plans["golds-gym-ashburn"]
    assert [r["id"] for r in plan["discovered_facilities"]] == [10669]
    assert [r["id"] for r in plan["facilities"]] == ["abc123"]
    assert sorted(r["dchub_facility_id"] for r in plan["carrier_facility_presence"]) == ["10669", "abc123"]
    assert [r["old_slug"] for r in plan["facility_slug_aliases"]] == ["golds-gym-ashburn-deadbeef"]
    assert [r["id"] for r in kept] == [20001]
    assert [r["id"] for r in plans["golds-gym-ashburn-unknown"][0]["discovered_facilities"]] == [19001]


def test_pg_apply_then_rollback_round_trips(pg, tmp_path, monkeypatch):
    conn, dsn = pg
    monkeypatch.setenv("DATABASE_URL", dsn)
    for var in rm.REPLICA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert rm.main([]) == 0                                    # dry run writes nothing
    assert _ids(conn, "discovered_facilities") == [1223, 10669, 19001, 20001]
    out = tmp_path / "rb.json"
    assert rm.main(["--apply", "--rollback-out", str(out)]) == 0
    conn.rollback()
    assert _ids(conn, "discovered_facilities") == [1223, 20001]            # both gym rows gone
    assert _ids(conn, "facilities") == []
    assert _ids(conn, "carrier_facility_presence", "dchub_facility_id") == ["1223"]
    assert _ids(conn, "facility_slug_aliases", "old_slug") == []
    doc = json.loads(out.read_text())
    assert doc["format"] == rm.FORMAT
    assert rm.main([]) == 4                                    # nothing left to match
    assert rm.main(["--apply", "--rollback-out", str(out)]) == 4
    assert rm.main(["--rollback", str(out)]) == 0
    conn.rollback()
    assert _ids(conn, "discovered_facilities") == [1223, 10669, 19001, 20001]
    assert _ids(conn, "facilities") == ["abc123"]
    assert len(_ids(conn, "carrier_facility_presence")) == 3
    assert _ids(conn, "facility_slug_aliases", "old_slug") == ["golds-gym-ashburn-deadbeef"]
    c = conn.cursor()
    c.execute("SELECT raw_data, latitude FROM discovered_facilities WHERE id = 10669")
    assert c.fetchone() == ({"k": 1}, 39.02)


def test_pg_apply_refuses_to_overwrite_a_rollback_file(pg, tmp_path, monkeypatch):
    conn, dsn = pg
    monkeypatch.setenv("DATABASE_URL", dsn)
    out = tmp_path / "rb.json"
    out.write_text("{}")
    assert rm.main(["--apply", "--rollback-out", str(out)]) == 2
    conn.rollback()
    assert 10669 in _ids(conn, "discovered_facilities")


def test_pg_row_cap_aborts_before_any_write(pg, monkeypatch):
    conn, dsn = pg
    monkeypatch.setattr(rm, "MAX_ROWS", 1)
    monkeypatch.setenv("DATABASE_URL", dsn)
    assert rm.main(["--apply"]) == 3
    conn.rollback()
    assert 10669 in _ids(conn, "discovered_facilities")


def test_pg_a_row_that_moved_since_the_plan_rolls_everything_back(pg, tmp_path):
    conn, _ = pg
    plans = rm.build_plan(conn.cursor())
    conn.rollback()
    c = conn.cursor()
    c.execute("DELETE FROM carrier_facility_presence WHERE dchub_facility_id = '10669'")   # moved out of band
    conn.commit()
    with pytest.raises(RuntimeError):
        rm.apply(conn, plans, str(tmp_path / "rb.json"))
    assert 10669 in _ids(conn, "discovered_facilities")
    assert _ids(conn, "facility_slug_aliases", "old_slug") == ["golds-gym-ashburn-deadbeef"]
