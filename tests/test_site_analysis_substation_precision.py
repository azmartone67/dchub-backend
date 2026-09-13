"""GET /api/v1/energy/site-analysis serves substation detail at the caller's precision.

Callers that are not privileged get what the HIFLD substations feeder
(expanded_infrastructure_api.get_substations) gives them: coordinates rounded to
0.1 degree, no OWNER or ZIP, and the distance to the nearest substation as a band
instead of a figure, in the score details and in the recommendation text.
Privileged callers keep full precision. Both get the same scores and keys.

The route is registered by the module's own setup_energy_routes on a real Flask
app and the caller is classified by the real routes.tier_gate.caller_is_privileged.
Only the database connection is replaced.
"""

import json
import sys

import pytest
from flask import Flask

import energy_infrastructure_routes as eir

NEAR_SITE = "/api/v1/energy/site-analysis?lat=39.044&lng=-77.487&radius=10000"  # nearest ~5 km
FAR_SITE = "/api/v1/energy/site-analysis?lat=39.144&lng=-77.487&radius=10000"   # nearest ~15 km
PUBLIC = {"REMOTE_ADDR": "203.0.113.7"}
LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
ADMIN_KEY = "site-analysis-test-admin-key"

# name, city, state, zip, type, status, owner, max_volt, min_volt, lat, lng
SUBSTATION_ROWS = [
    ("Alpha Sub", "Ashburn", "VA", "20147", "SUBSTATION", "IN SERVICE",
     "Alpha Power Co", 500, 230, 38.957031, -77.507576),
    ("Beta Sub", "Sterling", "VA", "20166", "SUBSTATION", "IN SERVICE",
     "Beta Grid LLC", 230, 115, 39.013847, -77.442219),
]


class _Cursor:
    """The psycopg2 cursor calls the view makes, and nothing more."""

    def __init__(self):
        self._rows = []

    def execute(self, sql, params=None):
        text = " ".join(sql.split()).lower()
        if "from substations" in text:
            self._rows = list(SUBSTATION_ROWS)
        elif "information_schema.columns" in text and "infrastructure_layers" in text:
            self._rows = [(0,)]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def cursor(self):
        return _Cursor()

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    main_stub = sys.modules["main"]
    monkeypatch.setattr(main_stub, "get_pg_connection", lambda: _Conn(), raising=False)
    monkeypatch.setattr(main_stub, "return_pg_connection", lambda conn: None, raising=False)
    monkeypatch.setattr(eir, "_CACHE", {})
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_KEY)
    app = Flask(__name__)
    eir.setup_energy_routes(app)
    rules = [r for r in app.url_map.iter_rules() if r.rule == "/api/v1/energy/site-analysis"]
    assert [r.endpoint for r in rules] == ["energy_site_analysis"]
    return app.test_client()


def _get(client, environ, headers=None, url=NEAR_SITE):
    resp = client.get(url, environ_base=environ, headers=headers or {})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["success"] is True
    return body


def _substations(body):
    subs = body["data"]["infrastructure"]["substations"]
    assert len(subs) == len(SUBSTATION_ROWS)
    return subs


def _assert_exact(body):
    for sub, row in zip(_substations(body), SUBSTATION_ROWS):
        assert sub["geometry"] == {"x": row[10], "y": row[9]}
        assert sub["attributes"]["OWNER"] == row[6]
        assert sub["attributes"]["ZIP"] == row[3]
    assert body["data"]["scores"]["details"]["nearestSubstationKm"] is not None
    assert "_gated" not in body["data"]


def _assert_public(body):
    for sub, row in zip(_substations(body), SUBSTATION_ROWS):
        assert sub["geometry"] == {"x": round(row[10], 1), "y": round(row[9], 1)}
        attrs = sub["attributes"]
        assert attrs["OWNER"] is None
        assert attrs["ZIP"] is None
        assert (attrs["NAME"], attrs["CITY"], attrs["STATE"], attrs["TYPE"],
                attrs["STATUS"], attrs["MAX_VOLT"], attrs["MIN_VOLT"]) == (
                row[0], row[1], row[2], row[4], row[5], row[7], row[8])
    assert body["data"]["scores"]["details"]["nearestSubstationKm"] is None
    assert body["data"]["_gated"] is True
    text = json.dumps(body)
    for row in SUBSTATION_ROWS:
        for exact in (repr(row[9]), repr(row[10]), row[6], row[3]):
            assert exact not in text


def _key_paths(node, prefix=""):
    paths = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths |= _key_paths(value, path)
    elif isinstance(node, list):
        for item in node:
            paths |= _key_paths(item, prefix + "[]")
    return paths


@pytest.mark.parametrize("environ,headers", [
    (LOOPBACK, None),
    (PUBLIC, {"X-Admin-Key": ADMIN_KEY}),
], ids=["loopback", "admin-key"])
def test_privileged_caller_gets_exact_substations(client, environ, headers):
    _assert_exact(_get(client, environ, headers))


@pytest.mark.parametrize("headers", [None, {"X-Admin-Key": "not-the-admin-key"}],
                         ids=["no-credentials", "wrong-admin-key"])
def test_public_caller_gets_coarsened_substations(client, headers):
    _assert_public(_get(client, PUBLIC, headers))


def test_predicate_error_serves_public_precision(client, monkeypatch):
    import routes.tier_gate

    def _raises(*args, **kwargs):
        raise RuntimeError("tier lookup unavailable")

    monkeypatch.setattr(routes.tier_gate, "caller_is_privileged", _raises)
    _assert_public(_get(client, LOOPBACK))


@pytest.mark.parametrize("url", [NEAR_SITE, FAR_SITE], ids=["within-band", "away-band"])
def test_public_recommendations_carry_no_substation_distance(client, url):
    exact = _get(client, LOOPBACK, url=url)
    km = exact["data"]["scores"]["details"]["nearestSubstationKm"]
    figure = f"{km:.1f}km"
    exact_recs = exact["data"]["scores"]["recommendations"]
    assert any("Substation" in r and figure in r for r in exact_recs)

    eir._CACHE.clear()
    public = _get(client, PUBLIC, url=url)
    public_recs = public["data"]["scores"]["recommendations"]
    assert figure not in json.dumps(public)
    assert len(public_recs) == len(exact_recs)
    assert any("Substation" in r for r in public_recs)


def test_scores_and_keys_match_across_callers(client):
    exact = _get(client, LOOPBACK)
    eir._CACHE.clear()
    public = _get(client, PUBLIC)
    assert public["data"]["scores"]["powerScore"] == exact["data"]["scores"]["powerScore"]
    assert _key_paths(public) - {"data._gated", "data._upgrade_cta"} == _key_paths(exact)


def test_cache_does_not_carry_precision_between_callers(client):
    _assert_public(_get(client, PUBLIC))

    after_public = _get(client, LOOPBACK)
    assert after_public.get("cached") is True
    _assert_exact(after_public)

    after_privileged = _get(client, PUBLIC)
    assert after_privileged.get("cached") is True
    _assert_public(after_privileged)

    _assert_exact(_get(client, LOOPBACK))
