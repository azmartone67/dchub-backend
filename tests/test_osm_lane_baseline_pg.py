"""OSM energy lanes against a REAL Postgres: the backfill is never "+N new".

Runs in pre-merge.yml's db-parity job (postgres:16 service) with
OSM_LANE_PG_DSN set, and that job fails if this file SKIPS. Locally:
    OSM_LANE_PG_DSN=postgres://postgres@127.0.0.1:<port>/<db>?sslmode=disable

What it pins, in the order the owner asked for it:
  • a lane's first sweep of a state is BASELINE — its rows never count as new;
  • a later sweep counts only rows it INSERTS (not rows it refreshes), and a
    refresh never moves first_seen_at or flips in_baseline;
  • the board's +N (routes.infra_growth._first_seen_added) sees exactly
    the post-baseline rows inside the window;
  • a state whose sweep did not commit stays un-baselined;
  • a gas candidate on top of a pipeline point we already hold is skipped;
  • substations / power plants: held rows are left untouched, new rows carry
    source='osm' / a first-seen stamp.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DSN = os.environ.get("OSM_LANE_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="OSM_LANE_PG_DSN not set")

import osm_overpass_loader as osm  # noqa: E402

TX = osm.TRANSMISSION_CUTOFF
WF, NF = osm.ID_FLOORS[TX]["way"], osm.ID_FLOORS[TX]["node"]
GWF, GNF = osm.ID_FLOORS[osm.GAS_CUTOFF]["way"], osm.ID_FLOORS[osm.GAS_CUTOFF]["node"]


def _line(wid, kv="138000", old_nodes=0, lat=30.0, lng=-97.0):
    return {"type": "way", "id": wid, "version": 1,
            "timestamp": "2026-09-01T00:00:00Z",
            "nodes": [NF + 1, NF + 2, NF + 3] + [NF - 5 - i for i in range(old_nodes)],
            "center": {"lat": lat, "lon": lng},
            "tags": {"power": "line", "voltage": kv, "name": f"L{wid}"}}


@pytest.fixture
def conn():
    import psycopg2
    c = psycopg2.connect(DSN)
    with c.cursor() as cur:
        # Production sessions run in GMT; created_at columns are naive UTC.
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("""DROP TABLE IF EXISTS osm_transmission_lines, osm_gas_pipelines,
                       osm_lane_baseline, osm_load_runs, gas_pipelines, substations,
                       discovered_power_plants""")
        cur.execute("""CREATE TABLE gas_pipelines (id SERIAL PRIMARY KEY, lat REAL,
                       lng REAL, lon DOUBLE PRECISION)""")
        cur.execute("""CREATE TABLE substations (
            id SERIAL PRIMARY KEY, name TEXT, operator TEXT, voltage_kv REAL,
            lat REAL, lng REAL, city TEXT, state TEXT, country TEXT DEFAULT 'US',
            source TEXT, source_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("CREATE UNIQUE INDEX ON substations (name, lat, lng)")
        cur.execute("CREATE UNIQUE INDEX ON substations (source_id)")
        cur.execute("""CREATE TABLE discovered_power_plants (
            id TEXT PRIMARY KEY, name TEXT, fuel_type TEXT, capacity_mw REAL,
            operator TEXT, state TEXT, discovered_at TEXT, last_updated TEXT,
            source TEXT DEFAULT 'EIA', is_new INTEGER DEFAULT 1, lat REAL, lng REAL)""")
    c.commit()
    yield c
    c.close()


def _sweep(conn, writer, state, els):
    ins, counts = writer(conn, state, els)
    conn.commit()
    return ins, counts


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _board(conn, label="osm_transmission_new", table="osm_transmission_lines"):
    """(added_7d, added_1d) exactly as the board reads them."""
    from routes.infra_growth import _first_seen_added
    with conn.cursor() as cur:
        a1, a7 = _first_seen_added(cur, table, label)
    return a7, a1


def test_first_sweep_is_baseline_and_only_later_inserts_count(conn):
    w = osm.transmission_writer()
    first = [_line(WF + 10), _line(WF + 11), _line(WF + 12),
             _line(WF + 13, old_nodes=9),      # a split: old nodes
             _line(WF + 14, kv="34500"),        # below 69 kV
             _line(WF - 1)]                     # created before the snapshot
    ins, counts = _sweep(conn, w, "TX", first)
    assert ins == 3 and counts["baseline_rows"] == 3 and counts["rule_rejected"] == 3
    assert _rows(conn, "SELECT COUNT(*) FROM osm_transmission_lines WHERE in_baseline")[0][0] == 3
    assert _board(conn) == (0, 0), "the backfill must never be published as new"

    ins, counts = _sweep(conn, w, "TX", first[:3] + [_line(WF + 20)])
    assert ins == 1 and counts["new_rows"] == 1
    assert _rows(conn, "SELECT osm_id FROM osm_transmission_lines WHERE NOT in_baseline") == [(WF + 20,)]
    assert _board(conn) == (1, 1)


def test_refresh_never_restamps_first_seen_or_baseline(conn):
    w = osm.transmission_writer()
    _sweep(conn, w, "TX", [_line(WF + 10)])
    with conn.cursor() as cur:
        cur.execute("UPDATE osm_transmission_lines SET first_seen_at = NOW() - INTERVAL '30 days', "
                    "last_seen_at = NOW() - INTERVAL '30 days'")
    conn.commit()
    renamed = _line(WF + 10)
    renamed["tags"]["name"] = "Renamed"
    ins, _ = _sweep(conn, w, "TX", [renamed])
    assert ins == 0
    (name, baseline, fs_age, ls_age), = _rows(
        conn, "SELECT name, in_baseline, NOW() - first_seen_at > INTERVAL '29 days', "
              "NOW() - last_seen_at < INTERVAL '1 hour' FROM osm_transmission_lines")
    assert (name, baseline, fs_age, ls_age) == ("Renamed", True, True, True)


def test_board_window_excludes_old_post_baseline_rows(conn):
    w = osm.transmission_writer()
    _sweep(conn, w, "TX", [_line(WF + 10)])
    _sweep(conn, w, "TX", [_line(WF + 10), _line(WF + 11)])
    assert _board(conn) == (1, 1)
    with conn.cursor() as cur:
        cur.execute("UPDATE osm_transmission_lines SET first_seen_at = NOW() - INTERVAL '10 days' "
                    "WHERE NOT in_baseline")
    conn.commit()
    assert _board(conn) == (0, 0)


def test_each_state_has_its_own_baseline(conn):
    w = osm.transmission_writer()
    _sweep(conn, w, "TX", [_line(WF + 10)])
    ins, counts = _sweep(conn, w, "OK", [_line(WF + 30)])
    assert ins == 1 and counts.get("baseline_rows") == 1
    assert _board(conn) == (0, 0)


def test_a_sweep_that_did_not_commit_leaves_the_state_unbaselined(conn):
    w = osm.transmission_writer()
    w(conn, "TX", [_line(WF + 10)])
    conn.rollback()                     # e.g. the write raised, or the thread died
    assert _rows(conn, "SELECT COUNT(*) FROM osm_lane_baseline")[0][0] == 0
    ins, counts = _sweep(conn, w, "TX", [_line(WF + 10), _line(WF + 11)])
    assert counts.get("baseline_rows") == 2 and _board(conn) == (0, 0)


def test_gas_candidate_on_a_federal_point_is_skipped(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO gas_pipelines (lat, lng) VALUES (31.0, -100.0) ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO gas_pipelines (lat, lon) VALUES (33.0, -102.0) ON CONFLICT DO NOTHING")
    conn.commit()

    def pipe(wid, lat, lng):
        return {"type": "way", "id": wid, "version": 1, "timestamp": "2026-01-01T00:00:00Z",
                "nodes": [GNF + 1, GNF + 2], "center": {"lat": lat, "lon": lng},
                "tags": {"man_made": "pipeline", "substance": "gas"}}
    ins, counts = _sweep(conn, osm.gas_writer(), "TX", [
        pipe(GWF + 1, 31.001, -100.001),   # ~140 m from an EIA point
        pipe(GWF + 2, 33.002, -102.0),     # near a point stored in `lon`
        pipe(GWF + 3, 35.0, -104.0)])      # nothing nearby
    assert ins == 1 and counts["near_federal_skipped"] == 2
    assert _rows(conn, "SELECT osm_id FROM osm_gas_pipelines") == [(GWF + 3,)]


def test_substations_held_rows_untouched_new_rows_tagged_osm(conn):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO substations (name, lat, lng, created_at)
                       VALUES ('OSM-1', 30.0, -97.0, '2026-01-01') ON CONFLICT DO NOTHING""")
    conn.commit()
    els = [{"type": "node", "id": 1, "lat": 30.0, "lon": -97.0, "tags": {}},
           {"type": "node", "id": 2, "lat": 31.0, "lon": -98.0, "tags": {"name": "New Sub"}}]
    ins, _ = _sweep(conn, osm.write_substations, "TX", els)
    assert ins == 1
    assert _rows(conn, "SELECT name, source, source_id, created_at::date::text "
                       "FROM substations ORDER BY id") == [
        ("OSM-1", None, None, "2026-01-01"),
        ("New Sub", "osm", "osm_sub_2", _rows(conn, "SELECT CURRENT_DATE::text")[0][0])]
    # re-sweeping the same features inserts nothing
    assert _sweep(conn, osm.write_substations, "TX", els)[0] == 0


def test_power_plants_new_rows_stamped_held_rows_untouched(conn):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO discovered_power_plants (id, name, source)
                       VALUES ('osm-way-5', 'Old', 'osm_overpass') ON CONFLICT DO NOTHING""")
    conn.commit()
    els = [{"type": "way", "id": 5, "center": {"lat": 30.0, "lon": -97.0}, "tags": {}},
           {"type": "node", "id": 6, "lat": 31.0, "lon": -98.0,
            "tags": {"plant:output:electricity": "20 MW"}}]
    ins, _ = _sweep(conn, osm.write_power_plants, "TX", els)
    assert ins == 1
    rows = dict((r[0], r[1:]) for r in _rows(
        conn, "SELECT id, discovered_at, (discovered_at::timestamptz > NOW() - INTERVAL '1 hour'), "
              "capacity_mw FROM discovered_power_plants"))
    assert rows["osm-way-5"][0] is None
    assert rows["osm-node-6"][1] is True and rows["osm-node-6"][2] == 20.0


def test_board_summary_publishes_post_baseline_rows_not_the_snapshot_delta(conn):
    """End to end through routes.infra_growth._summary: a count snapshot that
    jumped 0 -> 4 (the backfill landing) must still publish +1, the one row
    first seen after the baseline — and state the rule with the layer."""
    import routes.infra_growth as ig
    w = osm.transmission_writer()
    _sweep(conn, w, "TX", [_line(WF + 10), _line(WF + 11), _line(WF + 12)])
    _sweep(conn, w, "TX", [_line(WF + 10), _line(WF + 11), _line(WF + 12), _line(WF + 13)])
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS infra_growth_snapshot")
        ig._ensure(cur)
        cur.execute("""INSERT INTO infra_growth_snapshot (snapshot_date, layer, count)
                       VALUES (CURRENT_DATE - 3, 'osm_transmission_new', 0) ON CONFLICT DO NOTHING,
                              (CURRENT_DATE, 'osm_transmission_new', 4)""")
    conn.commit()
    with conn.cursor() as cur:
        layers, _ = ig._summary(cur)
    rec = next(l for l in layers if l["layer"] == "osm_transmission_new")
    assert rec["count"] == 4
    assert rec["delta_window"] == 1 and rec["window_days"] == 7, rec
    assert rec["growth_basis"] == "first_seen_column"
    assert rec["status"] == "growing"
    assert osm.TRANSMISSION_CUTOFF in rec["inclusion_rule"]
    assert osm.TRANSMISSION_CUTOFF in rec["status_reason"]


def test_substation_near_one_we_hold_is_not_new(conn):
    """#5306's first live run inserted 809 OSM substations, 399 of them within
    50 m of a substation already held — the same station twice."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO substations (name, lat, lng, source)
                       VALUES ('Wilkins Substation', 33.0, -112.0, 'HIFLD') ON CONFLICT DO NOTHING""")
    conn.commit()

    def sub(i, lat, lng, name):
        return {"type": "node", "id": i, "lat": lat, "lon": lng, "tags": {"name": name}}
    ins, counts = _sweep(conn, osm.write_substations, "AZ", [
        sub(10, 33.0004, -112.0, "SRP Wilkins Substation"),   # ~45 m: same station, new name
        sub(11, 33.0100, -112.0, "Far Substation"),           # ~1.1 km: new
        sub(12, 33.0102, -112.0, "Far Substation (way)"),     # ~22 m from #11: same sweep dup
    ])
    assert ins == 1 and counts["near_held_skipped"] == 2
    assert _rows(conn, "SELECT name FROM substations WHERE source='osm'") == [("Far Substation",)]


def test_a_stalled_run_is_resumed_not_restarted(conn, monkeypatch):
    """Bots merge to main every 10-15 min and each merge redeploys Railway,
    killing the loader thread. The next start must carry the dead run's
    finished states and close the dead row, so the sweep completes."""
    import psycopg2
    monkeypatch.setattr(osm, "_connect", lambda: psycopg2.connect(DSN))
    osm.ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO osm_load_runs (loader, status, states_total, heartbeat_at, detail)
                       VALUES ('osm_substations', 'running', 51, NOW() ON CONFLICT DO NOTHING - INTERVAL '20 minutes',
                               '{"done_states": ["AK", "AL"]}') RETURNING id""")
        dead = cur.fetchone()[0]
        # a stalled run of ANOTHER loader, and a stale one, must not be picked
        cur.execute("""INSERT INTO osm_load_runs (loader, status, heartbeat_at, detail)
                       VALUES ('osm_power_plants', 'running', NOW() ON CONFLICT DO NOTHING - INTERVAL '20 minutes',
                               '{"done_states": ["TX"]}')""")
    conn.commit()
    rid = osm.start_run("osm_substations")
    assert osm._carried_for(rid) == ["AK", "AL"]
    assert _rows(conn, "SELECT status FROM osm_load_runs WHERE id=%s", (dead,)) == [("abandoned",)]
    # a live (heartbeating) run is never resumed-over or closed
    osm._update_run(rid, {"states_done": 3, "states_total": 51,
                          "done_states": ["AK", "AL", "AZ"]})
    assert osm.active_run("osm_substations") == rid
    rid2 = osm.start_run("osm_substations")
    assert osm._carried_for(rid2) == [], "a live run must not be treated as dead"
    assert _rows(conn, "SELECT status FROM osm_load_runs WHERE id=%s", (rid,)) == [("running",)]



def _snap(conn, layer, rows):
    import routes.infra_growth as ig
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS infra_growth_snapshot")
        ig._ensure(cur)
        for d, n, cap in rows:
            cur.execute("""INSERT INTO infra_growth_snapshot (snapshot_date, layer, count, captured_at)
                           VALUES (CURRENT_DATE - %s, %s, %s, %s) ON CONFLICT DO NOTHING""", (d, layer, n, cap))
    conn.commit()


def test_substation_backfill_is_never_published_as_new(conn):
    """The first sweep of a state after #5306 stored OSM history the broken
    loader never had; the board must net it out of the substations delta and
    count only what a LATER sweep of a baselined state adds."""
    import routes.infra_growth as ig
    with conn.cursor() as cur:
        cur.execute("SELECT NOW() - INTERVAL '1 second'")
        t0 = cur.fetchone()[0]
        cur.execute("""INSERT INTO substations (name, lat, lng, source)
                       VALUES ('Held', 30.0, -97.0, 'HIFLD') ON CONFLICT DO NOTHING""")
    conn.commit()

    def sub(i, lat):
        return {"type": "node", "id": i, "lat": lat, "lon": -100.0, "tags": {}}
    # first (baseline) sweep of TX: 3 rows of OSM history
    ins, _ = _sweep(conn, osm.write_substations, "TX", [sub(1, 31.0), sub(2, 32.0), sub(3, 33.0)])
    assert ins == 3
    assert _rows(conn, "SELECT lane, state FROM osm_lane_baseline") == [("osm_substations", "TX")]
    # an OSM row in a state never swept successfully is backfill too
    _sweep(conn, osm.write_substations, "OK", [sub(9, 35.0)])
    with conn.cursor() as cur:
        cur.execute("DELETE FROM osm_lane_baseline WHERE state = 'OK'")
    conn.commit()
    # a LATER sweep of TX finds one genuinely new substation
    ins, _ = _sweep(conn, osm.write_substations, "TX",
                    [sub(1, 31.0), sub(2, 32.0), sub(3, 33.0), sub(4, 34.0)])
    assert ins == 1
    with conn.cursor() as cur:
        cur.execute("SELECT NOW()")
        t1 = cur.fetchone()[0]
        assert ig._backfill_between(cur, "substations", t0, t1) == 4

    # end to end: the snapshot jumped by 5 (4 backfill + 1 new) + the HIFLD row
    _snap(conn, "substations", [(3, 0, t0), (0, 6, t1)])
    with conn.cursor() as cur:
        layers, _ = ig._summary(cur)
    rec = next(l for l in layers if l["layer"] == "substations")
    assert rec["delta_window"] == 2, rec          # HIFLD row + the one new OSM row
    assert rec["backfill_excluded"] == 4
    assert "backfilled" in rec["status_reason"]


def test_power_plant_backfill_is_stamped_at_its_own_baseline(conn):
    """A baseline row's stamp must not land AFTER its baseline mark (a Python
    clock read would), or the whole backfill would count as new."""
    import routes.infra_growth as ig
    with conn.cursor() as cur:
        cur.execute("SELECT NOW() - INTERVAL '1 second'")
        t0 = cur.fetchone()[0]
    conn.commit()

    def plant(i):
        return {"type": "node", "id": i, "lat": 30.0 + i, "lon": -97.0, "tags": {}}
    _sweep(conn, osm.write_power_plants, "TX", [plant(1), plant(2)])
    _sweep(conn, osm.write_power_plants, "TX", [plant(1), plant(2), plant(3)])
    with conn.cursor() as cur:
        cur.execute("SELECT NOW()")
        t1 = cur.fetchone()[0]
        assert ig._backfill_between(cur, "power_plants_discovered", t0, t1) == 2


def test_an_incomplete_run_is_resumed_and_never_beats(conn, monkeypatch):
    import psycopg2
    monkeypatch.setattr(osm, "_connect", lambda: psycopg2.connect(DSN))
    osm.ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO osm_load_runs (loader, status, detail)
                       VALUES ('osm_transmission_lines', 'incomplete',
                               '{"done_states": ["AL", "AK"]}') ON CONFLICT DO NOTHING RETURNING id""")
        inc = cur.fetchone()[0]
        # a FINISHED run (error / success) is an outcome, never resumed
        cur.execute("""INSERT INTO osm_load_runs (loader, status, detail)
                       VALUES ('osm_pipelines', 'error', '{"done_states": ["TX"]}') ON CONFLICT DO NOTHING""")
    conn.commit()
    rid = osm.start_run("osm_transmission_lines")
    assert osm._carried_for(rid) == ["AK", "AL"]
    assert _rows(conn, "SELECT status FROM osm_load_runs WHERE id=%s", (inc,)) == [("abandoned",)]
    assert osm._carried_for(osm.start_run("osm_pipelines")) == []

    beats = []
    import routes.ingest_runs as ir
    monkeypatch.setattr(ir, "record_beat", lambda *a, **k: beats.append((a, k)))
    monkeypatch.setitem(osm.TRACKED, "osm_substations",
                        ("osm-substations", lambda progress=None, carried=None:
                         {"states_total": 51, "states_done": 10, "budget_exhausted": True,
                          "not_reached": ["TX"], "failed_states": {}, "inserted": 3}))
    out = osm.run_tracked("osm_substations")
    assert out["status"] == "incomplete" and beats == [], "an incomplete sweep must not beat"
