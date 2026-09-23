"""OSM energy lanes (2026-09-22): the "new since the federal snapshot" rule, the
per-loader outcome classifier, the Overpass client's failure classes, the
weekly driver's verdict, and the board registration. No network, no DB — the
write paths are proven against a real Postgres in test_osm_lane_baseline_pg.py.
"""
import ast
import io
import json
import os
import re
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import osm_overpass_loader as osm  # noqa: E402
import osm_refresh_drive as drive  # noqa: E402

TX = osm.TRANSMISSION_CUTOFF
WAY_FLOOR = osm.ID_FLOORS[TX]["way"]
NODE_FLOOR = osm.ID_FLOORS[TX]["node"]


def _way(wid, new_nodes, old_nodes, **tags):
    return {"type": "way", "id": wid,
            "nodes": [NODE_FLOOR + i for i in range(new_nodes)]
                     + [NODE_FLOOR - 1 - i for i in range(old_nodes)],
            "center": {"lat": 30.0, "lon": -97.0}, "tags": tags}


# ── the rule ────────────────────────────────────────────────────────────────
def test_way_created_before_the_snapshot_is_not_new():
    assert not osm.is_new_since(_way(WAY_FLOOR - 1, 10, 0), TX)


def test_way_at_the_floor_with_new_nodes_is_new():
    assert osm.is_new_since(_way(WAY_FLOOR, 10, 0), TX)


def test_split_of_an_older_line_is_not_new():
    # A split gets a fresh way id but keeps the old line's nodes.
    assert not osm.is_new_since(_way(WAY_FLOOR + 5, 1, 9), TX)


def test_node_share_threshold_is_inclusive_at_half():
    assert osm.is_new_since(_way(WAY_FLOOR + 5, 5, 5), TX)
    assert not osm.is_new_since(_way(WAY_FLOOR + 5, 4, 6), TX)


def test_non_way_and_nodeless_elements_are_not_new():
    assert not osm.is_new_since({"type": "node", "id": WAY_FLOOR + 1}, TX)
    assert not osm.is_new_since({"type": "way", "id": WAY_FLOOR + 1, "nodes": []}, TX)


def test_id_floors_are_ordered_by_date():
    g, t = osm.ID_FLOORS[osm.GAS_CUTOFF], osm.ID_FLOORS[osm.TRANSMISSION_CUTOFF]
    assert osm.GAS_CUTOFF < osm.TRANSMISSION_CUTOFF
    assert g["way"] < t["way"] and g["node"] < t["node"]


def test_voltage_takes_the_highest_circuit_and_min_kv_applies():
    assert osm._max_voltage_kv("138000;345000") == 345
    assert osm._transmission_row({"tags": {"voltage": "69000"}}) == (69,)
    assert osm._transmission_row({"tags": {"voltage": "34500"}}) is None


def test_lane_queries_filter_on_the_way_floor_server_side():
    src = open(os.path.join(ROOT, "osm_overpass_loader.py")).read()
    # the f-strings interpolate the floor into `(if: id() >= {floor})`
    assert src.count("(if: id() >= {floor})") == 3


def test_the_loader_never_writes_the_federal_tables():
    """Merging OSM into the EIA tables would count the same line twice."""
    tree = ast.parse(open(os.path.join(ROOT, "osm_overpass_loader.py")).read())
    writes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for m in re.finditer(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)",
                                 node.value, re.I):
                writes.add(m.group(1).lower())
    assert writes, "scan found no SQL writes at all — it is not reading the module"
    assert {"substations", "discovered_power_plants"} <= writes
    assert not writes & {"transmission_lines", "gas_pipelines", "pipelines"}
    assert not set(osm.LANES) & {"transmission_lines", "gas_pipelines"}


# ── the outcome classifier ──────────────────────────────────────────────────
def _res(**kw):
    base = {"states_total": 51, "states_done": 51, "fetched": 100,
            "inserted": 0, "failed_states": {}, "rejected": False}
    base.update(kw)
    return base


def test_complete_sweep_with_inserts_is_success():
    assert osm.classify(_res(inserted=3))[0] == "success"


def test_complete_sweep_with_nothing_new_is_no_new_data():
    assert osm.classify(_res())[0] == "no_new_data"


def test_any_db_write_failure_is_an_error():
    st, note = osm.classify(_res(inserted=9, failed_states={"TX": "db: UndefinedTable: x"}))
    assert st == "error" and "TX" in note


def test_rejected_client_is_an_error():
    assert osm.classify(_res(rejected=True))[0] == "error"


def test_too_many_failed_states_is_an_error_a_few_is_not():
    many = {s: "timeout" for s in osm.US_STATES[:11]}   # 11/51 > 20%
    few = {s: "timeout" for s in osm.US_STATES[:3]}
    assert osm.classify(_res(failed_states=many, inserted=5))[0] == "error"
    st, note = osm.classify(_res(failed_states=few, inserted=5))
    assert st == "success" and "failed" in note


def test_loader_error_or_no_result_is_an_error():
    assert osm.classify({"error": "no DATABASE_URL"})[0] == "error"
    assert osm.classify(None)[0] == "error"
    assert osm.classify(_res(states_total=0))[0] == "error"


# ── the Overpass client ─────────────────────────────────────────────────────
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_200_with_a_timeout_remark_is_not_a_sweep(monkeypatch):
    body = json.dumps({"elements": [{"id": 1}],
                       "remark": "runtime error: Query timed out in \"query\" at line 3"})
    monkeypatch.setattr(osm.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(body.encode()))
    monkeypatch.setattr(osm.time, "sleep", lambda s: None)
    assert osm._overpass_fetch("q", retries=2) == (None, "timeout")


def test_406_is_rejected_without_retrying(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise urllib.error.HTTPError("u", 406, "Not Acceptable", {}, None)
    monkeypatch.setattr(osm.urllib.request, "urlopen", boom)
    monkeypatch.setattr(osm.time, "sleep", lambda s: None)
    assert osm._overpass_fetch("q", retries=3) == (None, "rejected")
    assert len(calls) == 1


def test_clean_200_is_ok(monkeypatch):
    monkeypatch.setattr(osm.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b'{"elements": []}'))
    assert osm._overpass_fetch("q")[1] == "ok"


# ── the weekly driver ───────────────────────────────────────────────────────
def test_driver_is_green_only_when_every_loader_ends_ok():
    good = [(n, {"state": "success"}, "") for _e, n in drive.LOADERS]
    assert drive.verdict(good)[0] == 0
    for bad_state in ("stalled", "error", "timeout", "running", None):
        rows = list(good)
        rows[2] = (rows[2][0], {"state": bad_state} if bad_state else None, "HTTP 500")
        assert drive.verdict(rows)[0] == 1, bad_state


def test_driver_wait_reports_a_stall_and_a_deadline(monkeypatch):
    monkeypatch.setattr(drive, "_call", lambda m, p: (200, {"runs": [
        {"id": 7, "state": "stalled"}]}))
    assert drive.wait(7, sleep=lambda s: None)["state"] == "stalled"
    monkeypatch.setattr(drive, "_call", lambda m, p: (200, {"runs": [
        {"id": 7, "state": "running"}]}))
    clock = iter(range(0, 10 ** 6, 1000))
    assert drive.wait(7, sleep=lambda s: None, now=lambda: next(clock))["state"] == "timeout"


def test_driver_refuses_a_start_without_a_run_id(monkeypatch):
    monkeypatch.setattr(drive, "_call", lambda m, p: (200, {"started": True}))
    assert drive.fire("load-osm-substations-live")[0] is None


# ── the board ───────────────────────────────────────────────────────────────
def test_osm_layers_are_fully_registered_on_the_board():
    import routes.infra_growth as ig
    labels = {l[0]: l[1] for l in ig._LAYERS}
    for label, table in (("osm_transmission_new", "osm_transmission_lines"),
                         ("osm_gas_pipelines_new", "osm_gas_pipelines")):
        assert labels.get(label) == table
        assert table in osm.LANES
        for reg in (ig._FRESH_COL, ig._EXPECTED_CADENCE, ig._FRIENDLY,
                    ig._PROVENANCE, ig._INCLUSION_RULE):
            assert label in reg, (label, reg is ig._FRIENDLY)
        # +N is first-seen based and excludes each state's baseline sweep —
        # the column names must be the ones the loader's DDL creates.
        assert ig._FIRST_SEEN_COLUMN[label] == ("first_seen_at", "in_baseline")
        assert "first_seen_at" in osm._LANE_DDL and "in_baseline" in osm._LANE_DDL


def test_published_rule_matches_the_loader_constants():
    import routes.infra_growth as ig
    tx, gas = ig._INCLUSION_RULE["osm_transmission_new"], ig._INCLUSION_RULE["osm_gas_pipelines_new"]
    assert osm.TRANSMISSION_CUTOFF in tx and osm.GAS_CUTOFF in gas
    assert f"{osm.TRANSMISSION_MIN_KV} kV" in tx
    assert f"{osm.GAS_NEAR_FEDERAL_KM:g} km" in gas
    assert "half" in tx and "half" in gas and osm.NEW_NODE_SHARE == 0.5
