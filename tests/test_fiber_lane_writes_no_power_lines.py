"""FiberRouteDiscovery.sync() must not store power transmission lines as fiber routes.

MEASURED 2026-09-13 on production (BEGIN READ ONLY ... ROLLBACK):

    SELECT source, route_type, COUNT(*) FROM fiber_routes
     WHERE route_type ~* '(transmission|power|electric)' GROUP BY 1, 2;
    -> hifld | transmission | 9695

All 9,695 came from FiberRouteDiscovery._sync_hifld_transmission_lines. It read
the HIFLD Electric_Power_Transmission_Lines layer with return_geometry=False and
saved each feature as {"type": "transmission"} at its market's centroid. Every
name is "<utility> <kV>kV Line - <market>", none has coordinates or an end point,
and 101 of the 102 providers own no other row. Every COUNT(*) FROM fiber_routes
surface counted them. /api/v1/fiber/sources listed the utilities as carriers,
and start-point proximity readers returned them as nearby fiber.

This compiles the REAL FiberRouteDiscovery class from source and runs sync()
with upstream stubs that answer every lane; the HIFLD stub returns a
transmission line. It records what reaches _save_route. The class is compiled
rather than imported for the reason tests/test_hifld_transmission_layer_canon.py
gives: the unit-tests job installs a light dependency set, and importing
infrastructure_discovery pulls in db_utils.
"""
import ast
import collections
import datetime
import hashlib
import json
import os
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "infrastructure_discovery.py")
TRANSMISSION_LAYER = "stub://hifld/Electric_Power_Transmission_Lines"
MARKETS = [
    {"name": "Northern Virginia", "lat": 39.0438, "lng": -77.4874, "state": "VA"},
    {"name": "Dallas-Fort Worth", "lat": 32.7767, "lng": -96.797, "state": "TX"},
    {"name": "Houston", "lat": 29.7604, "lng": -95.3698, "state": "TX"},
]


class _Resp:
    ok = True

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _LearnedCursor:
    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return [{"name": "Learned metro fiber", "location": "Ashburn, VA",
                 "metadata": json.dumps({"OWNER": "Example Fiber", "TYPE": "fiber"})}]


def _run_sync():
    saved, layers_queried = [], []

    def _query_hifld_nearby(api_url, lat, lng, **kw):
        layers_queried.append(api_url)
        # The feature shape the transmission lane consumed.
        return [{"attributes": {"ID": "141463", "OBJECTID": 541, "VOLTAGE": 230,
                                "OWNER": "VIRGINIA ELECTRIC & POWER CO",
                                "SUB_1": "BENNING (69KV)", "SUB_2": "BENNING (230KV)"}}]

    ns = {
        "DC_MARKETS": MARKETS,
        "HIFLD_APIS": collections.defaultdict(lambda: "stub://hifld/other",
                                              transmission_lines=TRANSMISSION_LAYER),
        "_query_hifld_nearby": _query_hifld_nearby,
        "hifld_voltage": lambda v: v,
        "hifld_owner": lambda owner, operator=None: owner or operator or "Unknown",
        "hifld_line_name": lambda owner, voltage, market: f"{owner} {voltage}kV Line - {market}",
        "requests": types.SimpleNamespace(
            get=lambda *a, **k: _Resp({"data": [{"id": 1, "name": "Example IX"}]}),
            post=lambda *a, **k: _Resp({"elements": [{
                "id": 42, "center": {"lat": 39.05, "lon": -77.48},
                "tags": {"name": "Example telecom line", "operator": "Example Fiber"}}]})),
        "get_db": lambda: types.SimpleNamespace(cursor=_LearnedCursor, close=lambda: None),
        "json": json,
        "hashlib": hashlib,
        "datetime": datetime.datetime,
        "logger": types.SimpleNamespace(info=lambda *a, **k: None,
                                        warning=lambda *a, **k: None),
        "time": types.SimpleNamespace(sleep=lambda *a: None),
    }
    tree = ast.parse(open(SRC).read())
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "FiberRouteDiscovery"), None)
    assert cls is not None, "FiberRouteDiscovery not found in infrastructure_discovery.py"
    exec(compile(ast.Module(body=[cls], type_ignores=[]), SRC, "exec"), ns)

    frd = ns["FiberRouteDiscovery"](market_index=0)
    frd._save_route = lambda route, source="discovery": saved.append((source, route))
    frd._save_fiber_endpoint = lambda ix: saved.append(("peeringdb_ix", {"ix": ix}))
    frd.sync()
    return saved, layers_queried


def test_sync_stores_no_transmission_line_as_a_fiber_route():
    saved, _ = _run_sync()
    routes = [(src, r) for src, r in saved if src != "peeringdb_ix"]
    # Positive control: without routes flowing through _save_route, "no
    # transmission line was saved" would also be true of a harness that saved
    # nothing.
    assert routes, "sync() saved no routes at all; a stub drifted and this test is blind"
    power = [r for _, r in routes if str(r.get("type", "")).lower() == "transmission"]
    assert not power, (
        f"sync() stored {len(power)} power transmission line(s) as fiber routes, e.g. "
        f"{power[0].get('name')!r}. Production held 9,695 such rows on 2026-09-13.")


def test_sync_does_not_query_the_transmission_layer():
    _, layers = _run_sync()
    assert TRANSMISSION_LAYER not in layers, (
        "sync() queries the HIFLD transmission layer; nothing it returns is fiber")


@pytest.mark.xfail(strict=True, reason="control: proves this file actually runs")
def test_zzz_must_fail_control():
    assert False, "control"
