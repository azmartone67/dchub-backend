"""Guard: /api/v1/energy/summary refuses a state or ISO it holds no rates for.

Measured live 2026-09-24 through MCP get_energy_prices (the MCP alias
region->state sends ?state=):
  state=ZZQX -> the NATIONAL average (0.1291 $/kWh), echoed as filter.state "ZZQX"
                (len > 3 skipped the state filter entirely)
  state=PJM  -> avg_rate_kwh 0 (no rows matched; zeros served as a rate)

Contract pinned here:
  - an unknown state / iso -> 400 naming what is accepted, never a rate
  - a real state with no rows -> 404 source_unavailable, never zeros
  - an ISO name in the state slot is read as the ISO (region=PJM works)
  - a full state name resolves to its code (state=Texas -> TX)
  - the plain national call is unchanged

The handler is extracted from main.py by AST and run in a bare Flask app
against a fake cursor, so no database and no main.py import.
"""
import ast
import logging
import pathlib
import sys
import types

import flask
import pytest

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"
NAMES = {"_ENERGY_ISO_HINT", "_energy_bad_filter", "cf_stub_energy_summary"}

# Fake eia_retail_rates: Texas and Virginia have rows, nothing else does.
ROWS = {"Texas": 8.1, "Virginia": 9.4}


class _Cur:
    def __init__(self):
        self._r = None

    def execute(self, sql, params=()):
        params = list(params or [])
        names = [p for p in params if isinstance(p, str) and p in ROWS]
        scoped = "state" in sql.split("FROM eia_retail_rates", 1)[-1]
        vals = [ROWS[n] for n in names] if scoped else list(ROWS.values())
        if "MAX(period) FROM" in sql and "AVG" not in sql:
            self._r = [("2025" if vals else None,)]
        elif "GROUP BY LOWER(sector)" in sql:
            self._r = [("industrial", sum(vals) / len(vals), "2025")] if vals else []
        elif vals:
            self._r = [(sum(vals) / len(vals), min(vals), max(vals), len(set(names or ROWS)), "2025")]
        else:
            self._r = [(None, None, None, 0, None)]

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return self._r or []


class _Conn:
    def cursor(self):
        return _Cur()


@pytest.fixture(scope="module")
def client():
    tree = ast.parse(MAIN.read_text())
    body = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in NAMES:
            n.decorator_list = []
            body.append(n)
        elif isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in NAMES for t in n.targets):
            body.append(n)
    assert {getattr(n, "name", None) or n.targets[0].id for n in body} == NAMES
    tg = types.ModuleType("map_tier_gating")
    tg._detect_caller_tier = lambda **_: ("internal", None)
    saved = sys.modules.get("map_tier_gating")
    sys.modules["map_tier_gating"] = tg
    ns = {"jsonify": flask.jsonify, "logger": logging.getLogger("t"), "JWT_SECRET": "x",
          "get_pg_connection": _Conn, "return_pg_connection": lambda c: None,
          "_ENERGY_SUMMARY_CACHE": {}, "_ENERGY_SUMMARY_TTL": 0}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MAIN), "exec"), ns)
    app = flask.Flask(__name__)
    app.add_url_rule("/api/v1/energy/summary", view_func=ns["cf_stub_energy_summary"])
    yield app.test_client()
    if saved is None:
        sys.modules.pop("map_tier_gating", None)
    else:
        sys.modules["map_tier_gating"] = saved


def _get(client, **q):
    r = client.get("/api/v1/energy/summary", query_string=q)
    return r.status_code, r.get_json()


def test_control_real_state_is_served(client):
    code, j = _get(client, state="TX")
    assert code == 200 and j["retail_rates"]["avg_cents_kwh"] == 8.1
    assert j["scope"] == "state" and j["filter"]["state"] == "TX"


def test_control_national_unchanged(client):
    code, j = _get(client)
    assert code == 200 and j["scope"] == "national" and j["retail_rates"]["states_covered"] == 2


@pytest.mark.parametrize("bad", ["ZZQX", "ZZ", "XYZ", "Atlantis"])
def test_unknown_state_is_refused_not_averaged(client, bad):
    code, j = _get(client, state=bad)
    assert code == 400, j
    assert j["error"] == "state not recognized" and j["requested_state"] == bad
    assert "avg_rate_kwh" not in j and "retail_rates" not in j


def test_unknown_iso_is_refused(client):
    code, j = _get(client, iso="DUK")
    assert code == 400 and j["error"] == "iso not recognized"


def test_iso_in_state_slot_reads_as_iso(client):
    code, j = _get(client, state="PJM")
    assert code == 200, j
    assert j["scope"] == "iso_footprint_avg" and j["filter"]["iso"] == "PJM"
    assert j["retail_rates"]["avg_cents_kwh"] == 9.4          # Virginia is the PJM row we hold


def test_full_state_name_resolves(client):
    code, j = _get(client, state="texas")
    assert code == 200 and j["filter"]["state"] == "TX" and j["retail_rates"]["avg_cents_kwh"] == 8.1


def test_real_state_without_rows_is_no_data_not_zero(client):
    code, j = _get(client, state="WY")
    assert code == 404, j
    assert j["source_unavailable"] is True and "avg_rate_kwh" not in j
