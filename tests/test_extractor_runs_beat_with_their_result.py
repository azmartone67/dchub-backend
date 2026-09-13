"""A source-registry heartbeat reports a run: the rows it wrote and its own verdict.

Companion to tests/test_no_import_time_heartbeat.py, which proves that importing
an extractor and exiting reports nothing. Here, each entry point that now carries
dchub_heartbeat.with_heartbeat sends exactly one beat when it RUNS:
rows_affected from what the run wrote, and `failure` when the run's own summary
says success=False or the run raises. Error text is scrubbed before it can reach
the public registry (GET /api/v1/sources/<id> serves it to anyone).

★ What is recorded is the CALL to heartbeat(), patched in the globals the
  decorator resolves it from (with_heartbeat.__globals__). The HTTP side of
  heartbeat() is covered by tests/test_heartbeat_no_credential_is_loud.py. A
  module whose `with_heartbeat` is its ImportError fallback fails the
  precondition by name, instead of passing with no beat to look at.

★ No network, no database: every entry point runs with its I/O replaced.
"""
import inspect
import json
import secrets
import sys
import types
import urllib.request

import pytest

import carrier_facility_ingestion
import dchub_heartbeat
import eia_gas_bulk_loader
import fiber_integration
import network_ix_ingestion
import news_engine
import news_facility_extractor
import subsea_cable_ingestion

_HEARTBEAT_PARAMS = ["source_id", "status", "rows_affected", "duration_ms",
                     "error", "metadata"]


def _record_beats(monkeypatch, module):
    """Replace heartbeat() where `module`'s with_heartbeat looks it up."""
    hb_globals = module.with_heartbeat.__globals__
    assert hb_globals.get("__name__") == "dchub_heartbeat", (
        "%s.with_heartbeat is not dchub_heartbeat's decorator (it resolves names "
        "in %r), so its entry point reports nothing"
        % (module.__name__, hb_globals.get("__name__")))
    assert list(inspect.signature(hb_globals["heartbeat"]).parameters) == \
        _HEARTBEAT_PARAMS, "heartbeat() changed shape; match the recorder to it"
    beats = []

    def recorder(source_id, status="success", rows_affected=None,
                 duration_ms=None, error=None, metadata=None):
        beats.append({"source": source_id, "status": status,
                      "rows": rows_affected, "duration_ms": duration_ms,
                      "error": error})
        return True

    monkeypatch.setitem(hb_globals, "heartbeat", recorder)
    return beats


def _only_beat(beats, source, status, rows):
    assert len(beats) == 1, beats
    beat = beats[0]
    assert (beat["source"], beat["status"], beat["rows"]) == (source, status, rows), beat
    assert isinstance(beat["duration_ms"], int), beat
    return beat


# ── the decorator ────────────────────────────────────────────────────────────

def test_a_returned_success_false_is_a_failure_carrying_its_rows(monkeypatch):
    beats = _record_beats(monkeypatch, dchub_heartbeat)

    @dchub_heartbeat.with_heartbeat("unit-source", rows_key="written")
    def run():
        return {"success": False, "written": 3,
                "cables": {"success": False, "error": "feed unreachable"},
                "points": {"success": True}}

    assert run()["written"] == 3
    beat = _only_beat(beats, "unit-source", "failure", 3)
    assert "cables: feed unreachable" in beat["error"], beat


def test_a_returned_success_reports_rows_from_rows_key(monkeypatch):
    beats = _record_beats(monkeypatch, dchub_heartbeat)

    @dchub_heartbeat.with_heartbeat("unit-source", rows_key="written")
    def run():
        return {"success": True, "written": 12}

    run()
    assert _only_beat(beats, "unit-source", "success", 12)["error"] is None


@pytest.mark.parametrize("returned, rows_key, rows", [
    (7, None, 7),                      # an int return is the row count
    (True, None, None),                # a bool is not a count
    ({"flag": True}, "flag", None),
    ({"written": "12"}, "written", None),
    ({"written": 4}, None, None),      # a dict is only read through rows_key
], ids=["int-return", "bool-return", "bool-under-rows-key", "string-under-rows-key",
        "dict-without-rows-key"])
def test_only_a_real_count_becomes_rows_affected(monkeypatch, returned, rows_key, rows):
    beats = _record_beats(monkeypatch, dchub_heartbeat)
    run = dchub_heartbeat.with_heartbeat("unit-source", rows_key=rows_key)(
        lambda: returned)
    assert run() is returned
    _only_beat(beats, "unit-source", "success", rows)


def test_a_raised_failure_is_reported_scrubbed_then_re_raised(monkeypatch):
    token = secrets.token_hex(12)
    monkeypatch.setenv("EIA_API_KEY", token)
    beats = _record_beats(monkeypatch, dchub_heartbeat)

    @dchub_heartbeat.with_heartbeat("unit-source")
    def run():
        raise RuntimeError("upstream refused key " + token)

    with pytest.raises(RuntimeError):
        run()
    beat = _only_beat(beats, "unit-source", "failure", None)
    # The message survives (so this is the scrubber, not the text-free
    # fallback) and the secret value does not.
    assert beat["error"].startswith("RuntimeError: upstream refused key"), beat
    assert token not in beat["error"], beat


def test_a_result_that_cannot_be_read_is_not_reported_as_a_success(monkeypatch):
    beats = _record_beats(monkeypatch, dchub_heartbeat)

    class Unreadable(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("no")

    returned = Unreadable()
    run = dchub_heartbeat.with_heartbeat("unit-source", rows_key="n")(lambda: returned)
    assert run() is returned
    assert beats == []


# ── subsea_cable_ingestion.run_subsea_sync ──────────────────────────────────

def _subsea(monkeypatch, cables, points):
    monkeypatch.setattr(subsea_cable_ingestion, "init_subsea_tables", lambda get_db: None)
    monkeypatch.setattr(subsea_cable_ingestion, "ingest_cables", lambda get_db: cables)
    monkeypatch.setattr(subsea_cable_ingestion, "ingest_landing_points", lambda get_db: points)
    return _record_beats(monkeypatch, subsea_cable_ingestion)


def test_subsea_sync_reports_the_rows_it_upserted(monkeypatch):
    beats = _subsea(monkeypatch, {"success": True, "upserted": 7},
                    {"success": True, "upserted": 5})
    result = subsea_cable_ingestion.run_subsea_sync(lambda: None)
    assert (result["success"], result["total_new"]) == (True, 12)
    _only_beat(beats, "backend-subsea-cable", "success", 12)


def test_subsea_sync_that_could_not_fetch_is_a_failure(monkeypatch):
    beats = _subsea(monkeypatch, {"success": False, "error": "Failed to fetch cable data"},
                    {"success": True, "upserted": 5})
    subsea_cable_ingestion.run_subsea_sync(lambda: None)
    beat = _only_beat(beats, "backend-subsea-cable", "failure", 5)
    assert "cables: Failed to fetch cable data" in beat["error"], beat


def test_subsea_sync_that_raises_is_a_failure_and_still_raises(monkeypatch):
    beats = _subsea(monkeypatch, {"success": True, "upserted": 1},
                    {"success": True, "upserted": 1})

    def tables_unavailable(get_db):
        raise RuntimeError("tables unavailable")

    monkeypatch.setattr(subsea_cable_ingestion, "init_subsea_tables", tables_unavailable)
    with pytest.raises(RuntimeError):
        subsea_cable_ingestion.run_subsea_sync(lambda: None)
    beat = _only_beat(beats, "backend-subsea-cable", "failure", None)
    assert beat["error"].startswith("RuntimeError"), beat


# ── network_ix_ingestion.run_peeringdb_full_sync ────────────────────────────

_PEERINGDB_STEPS = ("ingest_networks", "ingest_network_facilities", "ingest_ix",
                    "ingest_ix_facilities", "ingest_campus")


def _peeringdb(monkeypatch, **outcomes):
    monkeypatch.setattr(network_ix_ingestion, "init_network_ix_tables", lambda get_db: None)
    for step in _PEERINGDB_STEPS:
        outcome = outcomes.get(step, {"success": True, "upserted": 1})
        monkeypatch.setattr(network_ix_ingestion, step, lambda get_db, _o=outcome: _o)
    return _record_beats(monkeypatch, network_ix_ingestion)


def test_peeringdb_full_sync_reports_the_rows_it_upserted(monkeypatch):
    beats = _peeringdb(monkeypatch, ingest_networks={"success": True, "upserted": 40})
    result = network_ix_ingestion.run_peeringdb_full_sync(lambda: None)
    assert (result["success"], result["total_records"]) == (True, 44)
    _only_beat(beats, "backend-network-ix-ingestion", "success", 44)


def test_peeringdb_full_sync_with_a_failed_step_is_a_failure(monkeypatch):
    beats = _peeringdb(monkeypatch, ingest_campus={
        "success": False, "error": "Failed to fetch campus data from PeeringDB"})
    network_ix_ingestion.run_peeringdb_full_sync(lambda: None)
    beat = _only_beat(beats, "backend-network-ix-ingestion", "failure", 4)
    assert "campus: Failed to fetch campus data" in beat["error"], beat


def test_partial_peeringdb_syncs_do_not_report_the_whole_source(monkeypatch):
    beats = _peeringdb(monkeypatch)
    network_ix_ingestion.run_network_sync(lambda: None)
    network_ix_ingestion.run_ix_sync(lambda: None)
    network_ix_ingestion.run_campus_sync(lambda: None)
    assert beats == []


# ── fiber_integration: the carrier sync behind both job endpoints ───────────

@pytest.fixture
def fiber_client(monkeypatch):
    import flask

    monkeypatch.setattr(subsea_cable_ingestion, "init_subsea_tables", lambda get_db: None)
    monkeypatch.setattr(subsea_cable_ingestion, "register_subsea_routes", lambda app, get_db: None)
    monkeypatch.setattr(carrier_facility_ingestion, "init_carrier_tables", lambda get_db: None)
    monkeypatch.setattr(carrier_facility_ingestion, "register_carrier_routes", lambda app, get_db: None)
    internal = secrets.token_hex(8)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", internal)

    def build(carrier_result):
        monkeypatch.setattr(carrier_facility_ingestion, "run_carrier_sync",
                            lambda get_db: carrier_result)
        app = flask.Flask("fiber_heartbeat_test")
        fiber_integration.register_fiber_intelligence(app, lambda: None)
        return app.test_client(), {"X-Internal-Key": internal}

    return build


def test_carrier_sync_endpoint_reports_the_fiber_layer_run(monkeypatch, fiber_client):
    beats = _record_beats(monkeypatch, fiber_integration)
    client, headers = fiber_client({"success": True, "total_records": 42})
    resp = client.post("/api/jobs/carrier-sync", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    _only_beat(beats, "backend-fiber-integration", "success", 42)


def test_fiber_full_sync_reports_its_carrier_failure(monkeypatch, fiber_client):
    monkeypatch.setattr(subsea_cable_ingestion, "run_subsea_sync",
                        lambda get_db: {"success": True, "total_new": 2})
    beats = _record_beats(monkeypatch, fiber_integration)
    client, headers = fiber_client({
        "success": False, "total_records": 3,
        "carrier_facilities": {"success": False, "error": "PeeringDB answered 429"}})
    resp = client.post("/api/jobs/fiber-full-sync", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    beat = _only_beat(beats, "backend-fiber-integration", "failure", 3)
    assert "carrier_facilities: PeeringDB answered 429" in beat["error"], beat


# ── news_engine.sync_all_news ────────────────────────────────────────────────

class _CountingCursor:
    rowcount = 0

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (9,)


class _CountingConn:
    def cursor(self):
        return _CountingCursor()

    def commit(self):
        pass

    def close(self):
        pass


def _news(monkeypatch, google):
    monkeypatch.setattr(news_engine, "init_news_db", lambda db: None)
    monkeypatch.setattr(news_engine, "fetch_all_rss_feeds",
                        lambda db: [{"title": "Operator breaks ground on an Ohio campus"}])
    monkeypatch.setattr(news_engine, "fetch_all_google_news", google)
    monkeypatch.setattr(news_engine, "save_articles", lambda articles, db: len(articles))
    monkeypatch.setattr(news_engine, "sync_to_announcements", lambda articles, db: len(articles))
    monkeypatch.setattr(news_engine, "get_db", lambda db: _CountingConn())
    return _record_beats(monkeypatch, news_engine)


def test_news_sync_reports_the_articles_it_saved(monkeypatch):
    beats = _news(monkeypatch, google=lambda: [])
    result = news_engine.sync_all_news()
    assert (result["success"], result["new_saved"]) == (True, 1)
    _only_beat(beats, "backend-news-engine", "success", 1)


def test_news_sync_with_a_failed_step_is_a_failure(monkeypatch):
    def google_down():
        raise RuntimeError("google news unreachable")

    beats = _news(monkeypatch, google=google_down)
    result = news_engine.sync_all_news()
    assert result["success"] is False
    beat = _only_beat(beats, "backend-news-engine", "failure", 1)
    assert "google_news: google news unreachable" in beat["error"], beat


# ── news_facility_extractor.scan_news_sources ───────────────────────────────

class _Page:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "<title>Operator announces a 300 MW data center campus in Ohio</title>"


def _facility_scan(monkeypatch, status_code):
    import requests

    monkeypatch.setattr(news_facility_extractor, "NEWS_SOURCES",
                        [{"name": "Wire", "url": "https://news.invalid/"}])
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: _Page(status_code))
    monkeypatch.setattr(news_facility_extractor, "extract_facility_from_article",
                        lambda *args: {"name": "Ohio campus"})
    monkeypatch.setattr(news_facility_extractor, "insert_discovered_facility",
                        lambda conn, facility, failures=None: 101)
    return _record_beats(monkeypatch, news_facility_extractor)


def test_facility_scan_reports_the_facilities_it_inserted(monkeypatch):
    beats = _facility_scan(monkeypatch, 200)
    result = news_facility_extractor.scan_news_sources(conn=object())
    assert (result["success"], result["sources_read"], result["facilities_inserted"]) == (True, 1, 1)
    _only_beat(beats, "backend-news-facility-extractor", "success", 1)


def test_a_scan_no_source_answered_is_a_failure_not_an_empty_success(monkeypatch):
    beats = _facility_scan(monkeypatch, 503)
    result = news_facility_extractor.scan_news_sources(conn=object())
    assert (result["success"], result["sources_read"]) == (False, 0)
    _only_beat(beats, "backend-news-facility-extractor", "failure", 0)


def test_a_scan_without_a_database_connection_is_a_failure(monkeypatch):
    beats = _facility_scan(monkeypatch, 200)

    def primary_pool_exhausted():
        raise RuntimeError("primary pool exhausted")

    monkeypatch.setitem(sys.modules, "main",
                        types.SimpleNamespace(get_db=primary_pool_exhausted))
    result = news_facility_extractor.scan_news_sources()
    assert (result["success"], result["sources_read"]) == (False, 0)
    beat = _only_beat(beats, "backend-news-facility-extractor", "failure", 0)
    assert "primary pool exhausted" in beat["error"], beat


# ── eia_gas_bulk_loader.main ─────────────────────────────────────────────────

class _PipelineCursor:
    rowcount = 0

    def execute(self, sql, params=None):
        self.rowcount = 1 if sql.lstrip().upper().startswith("INSERT") else 0
        self.last = sql

    def fetchone(self):
        return (5,)

    def fetchall(self):
        return [("TX", 1)]


class _PipelineConn:
    autocommit = False

    def cursor(self):
        return _PipelineCursor()

    def close(self):
        pass


class _MaxFidResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({"features": [{"attributes": {"FID": 1500}}]}).encode()


def _gas_load(monkeypatch, connect):
    monkeypatch.setenv("NEON_URL", "postgresql://pipelines.neon.invalid/gas")
    monkeypatch.setattr(eia_gas_bulk_loader, "psycopg2", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(eia_gas_bulk_loader, "urllib", types.SimpleNamespace(
        request=types.SimpleNamespace(Request=urllib.request.Request,
                                      urlopen=lambda req, timeout=None: _MaxFidResponse())))
    monkeypatch.setattr(eia_gas_bulk_loader, "time", types.SimpleNamespace(sleep=lambda s: None))
    monkeypatch.setattr(eia_gas_bulk_loader, "fetch_batch", lambda start, end: [{
        "attributes": {"Operator": "Gulf Line", "TYPEPIPE": "Interstate",
                       "Status": "Operating", "FID": 7},
        "geometry": {"paths": [[[-95.0, 30.0], [-95.2, 30.1], [-95.4, 30.2]]]},
    }])
    return _record_beats(monkeypatch, eia_gas_bulk_loader)


def test_gas_bulk_load_reports_the_pipelines_it_inserted(monkeypatch):
    beats = _gas_load(monkeypatch, connect=lambda url: _PipelineConn())
    assert eia_gas_bulk_loader.main() == 1
    _only_beat(beats, "backend-eia-bulk-loader", "success", 1)


def test_gas_bulk_load_that_cannot_connect_is_a_failure(monkeypatch):
    def refuse(url):
        raise RuntimeError("could not connect")

    beats = _gas_load(monkeypatch, connect=refuse)
    with pytest.raises(RuntimeError):
        eia_gas_bulk_loader.main()
    _only_beat(beats, "backend-eia-bulk-loader", "failure", None)


def test_gas_bulk_load_that_refuses_to_start_reports_no_run(monkeypatch):
    beats = _gas_load(monkeypatch, connect=lambda url: _PipelineConn())
    monkeypatch.delenv("NEON_URL")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        eia_gas_bulk_loader.main()
    assert beats == []
