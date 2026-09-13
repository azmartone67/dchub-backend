"""discovered_transmission_lines is retired from service (2026-09-13). The two
routes that served it answer 410 before any database work, and the KMZ export
sends no transmission SQL.

Why these were retired rather than repointed, measured 2026-09-13:
  - the table is a March 2026 crawl with no writer and TEXT timestamps, and its
    2,821,162 rows repeat one line hundreds of times (the top 1,000 by voltage
    were 2 distinct lines);
  - the maintained transmission_lines (EIA, refreshed weekly) stores no geometry,
    so neither a map export nor a lat/lng route can be rebuilt on it;
  - Railway logged 0 export requests and 1 request to the v2 route in 7 days.
The one reader that could move — site_planner's endpoint-name match — did. Its
statement runs against a real Postgres in tests/test_transmission_readers_sql.py,
which also runs the export's statements; here only their absence is asserted.
"""
import os
import sys

import pytest

flask = pytest.importorskip("flask")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PROXIMITY = "/api/v1/grid/transmission-proximity?"


def _no_db(*_args, **_kwargs):
    raise AssertionError("a retired route touched the database")


class _RecordingCursor:
    """Records every statement and answers it with no rows / a zero. It cannot
    tell a valid statement from an invalid one — it is only used to show which
    tables the export no longer asks for."""

    def __init__(self, log):
        self.log = log
        self.description = None

    def execute(self, sql, params=None):
        self.log.append(sql)
        self.description = (("value",),)

    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)


class _RecordingConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _RecordingCursor(self.log)

    def close(self):
        pass


def _transmission_sql(log):
    return [s for s in log if "transmission" in s.lower()]


def test_the_v2_transmission_route_answers_410_without_a_database(monkeypatch):
    import db_utils
    import expanded_infrastructure_api as eia
    monkeypatch.setattr(db_utils, "get_db", _no_db)
    app = flask.Flask(__name__)
    app.register_blueprint(eia.expanded_infra_bp)

    r = app.test_client().get(
        "/api/v2/infrastructure/hifld/transmission?lat=39.04&lng=-77.49&market=dallas")

    assert r.status_code == 410
    body = r.get_json()
    assert body["retired"] is True and body["success"] is False
    assert body["instead"].startswith(PROXIMITY)
    # A retired route must not answer with an empty result that reads as "no lines".
    assert "transmission_lines" not in body and "count" not in body


@pytest.mark.parametrize("fmt", ["kml", "kmz"])
def test_the_kmz_transmission_type_answers_410_before_any_database_work(monkeypatch, fmt):
    import energy_kmz_export as ek
    monkeypatch.setattr(ek, "get_db", _no_db)
    app = flask.Flask(__name__)
    ek.register_kmz_export_routes(app)

    r = app.test_client().get(
        f"/api/energy-discovery/export/kmz?type=transmission-lines&format={fmt}&market=dallas")

    assert r.status_code == 410
    body = r.get_json()
    assert body["retired"] is True
    assert body["instead"].startswith(PROXIMITY)


@pytest.mark.parametrize("market", [None, "dallas"])
def test_the_combined_export_sends_no_transmission_sql(monkeypatch, market):
    import energy_kmz_export as ek
    log = []
    monkeypatch.setattr(ek, "get_db", lambda: _RecordingConn(log))

    kml, _total = ek.generate_all_kml(market)

    assert log, "the export sent no SQL at all, so the recorder is not wired in"
    assert not _transmission_sql(log), log
    assert "Transmission lines are not included" in kml


def test_the_export_summary_publishes_no_transmission_count(monkeypatch):
    import energy_kmz_export as ek
    log = []
    monkeypatch.setattr(ek, "get_db", lambda: _RecordingConn(log))
    app = flask.Flask(__name__)
    ek.register_kmz_export_routes(app)

    body = app.test_client().get("/api/energy-discovery/export/summary").get_json()

    assert body["success"] is True, body
    assert "transmission_lines" not in body["data"]
    assert "transmission" not in body["endpoints"]
    assert body["not_exported"]["transmission_lines"] == ek.TRANSMISSION_RETIRED["reason"]
    assert log and not _transmission_sql(log), log
