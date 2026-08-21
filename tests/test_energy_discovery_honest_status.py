#!/usr/bin/env python3
"""tests/test_energy_discovery_honest_status.py — the energy-discovery job must
not report ✅ over a run that wrote nothing, and a failed log INSERT must not
abort the transaction the data path is using.

★ THE DEFECT (worker log, 2026-08-21 18:21Z, and every run before it):

    WARNING - Sync log error: column "source" of relation "energy_sync_log" does not exist
    ERROR   - ArcGIS error from …/Power_Plants/FeatureServer/0: {'code': 400, 'message': 'Invalid URL'}
    WARNING - Sync log error: current transaction is aborted, commands ignored until end of transaction block
    INFO    -   richmond_va: +0 plants, +0 substations, +0 gas
    … x23 markets …
    INFO    - Full sync complete: 23 markets, +0 plants, +0 substations, +0 gas
    INFO    - JOB energy-discovery: ✅

Two stacked defects: (1) the live energy_sync_log has the columns
sync_type/items_found/new_items/errors, not the source/records_found/
records_new/error the repo DDL declares, so the log INSERT raised; (2) nothing
rolled the aborted transaction back, so every later statement on that
connection — the actual data INSERTs — failed too, and the run concluded ✅.
Every HIFLD source is ALSO dead (4xx), which the old code turned into a 0.

These tests EXECUTE the shipped functions against a transaction-aware stub.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

import energy_auto_discovery_pg as ead  # noqa: E402

LIVE_COLS = {'id', 'sync_type', 'market', 'items_found', 'new_items',
             'updated_items', 'errors', 'duration_seconds', 'synced_at'}
REPO_COLS = {'id', 'source', 'market', 'records_found', 'records_new',
             'duration_seconds', 'error', 'synced_at'}


class _Cur:
    rowcount = 1

    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if 'information_schema.columns' in sql:
            self._rows = [(c,) for c in sorted(self.conn.live_cols)]
            return
        if self.conn.aborted:
            raise Exception("current transaction is aborted, commands ignored "
                            "until end of transaction block")
        if 'INSERT INTO energy_sync_log' in sql and self.conn.fail_log:
            self.conn.aborted = True
            raise Exception('column "source" of relation "energy_sync_log" does not exist')
        self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return None

    def close(self):
        pass


class _Conn:
    """A psycopg2-shaped stub that models the one thing that mattered: a
    failed statement aborts the transaction until rollback()."""

    def __init__(self, live_cols=LIVE_COLS, fail_log=False):
        self.live_cols = live_cols
        self.fail_log = fail_log
        self.executed = []
        self.rollbacks = 0
        self.commits = 0
        self.aborted = False

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        self.aborted = False

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_module_state():
    ead._SYNC_LOG_COLS.clear()
    ead._RUN_SOURCE_ERRORS.clear()
    yield
    ead._SYNC_LOG_COLS.clear()
    ead._RUN_SOURCE_ERRORS.clear()


def _log_inserts(conn):
    return [s for s, _ in conn.executed if 'INSERT INTO energy_sync_log' in s]


def test_log_sync_writes_the_LIVE_column_shape():
    conn = _Conn(LIVE_COLS)
    ead._log_sync(conn, 'power_plants', 'ashburn', 10, 2, 1.5)
    ins = _log_inserts(conn)
    assert ins, "no INSERT reached energy_sync_log"
    assert 'sync_type' in ins[0] and 'items_found' in ins[0] and 'errors' in ins[0]
    assert 'records_found' not in ins[0], "wrote the repo DDL's columns against the live table"
    assert conn.commits == 1 and conn.rollbacks == 0


def test_log_sync_still_writes_the_repo_shape_where_that_is_what_exists():
    conn = _Conn(REPO_COLS)
    ead._log_sync(conn, 'power_plants', 'ashburn', 10, 2, 1.5)
    ins = _log_inserts(conn)
    assert ins and 'records_found' in ins[0] and 'sync_type' not in ins[0]


def test_a_failed_log_insert_rolls_back_so_the_data_path_is_not_poisoned():
    conn = _Conn(LIVE_COLS, fail_log=True)
    ead._log_sync(conn, 'power_plants', 'ashburn', 0, 0, 1.0, "ArcGIS 499")
    assert conn.rollbacks == 1, (
        "the failed log INSERT left the transaction aborted — every later data "
        "INSERT on this connection will fail with 'current transaction is aborted'")
    assert not conn.aborted
    # Control: the data path can still write after a failed log.
    cur = conn.cursor()
    cur.execute("INSERT INTO discovered_power_plants (name) VALUES (%s)", ("x",))


def test_a_fetch_error_is_recorded_as_a_source_error():
    conn = _Conn(LIVE_COLS)
    ead._log_sync(conn, 'substations', 'reno_nv', 0, 0, 0.2, "{'code': 400, 'message': 'Invalid URL'}")
    assert ead._RUN_SOURCE_ERRORS and ead._RUN_SOURCE_ERRORS[0].startswith("substations/reno_nv")


def _stub_sources(monkeypatch, features, error):
    monkeypatch.setattr(ead, 'query_arcgis', lambda *a, **k: (features, error))
    monkeypatch.setattr(ead, 'init_energy_tables', lambda c: None)
    monkeypatch.setattr(ead.time, 'sleep', lambda s: None)


def test_every_source_dead_and_nothing_written_is_NOT_ok(monkeypatch):
    _stub_sources(monkeypatch, None, "{'code': 499, 'message': 'Item does not exist or is inaccessible.'}")
    r = ead.run_full_sync(_Conn(LIVE_COLS), markets={'ashburn': (39.0, -77.5, 50)})
    assert r['markets_synced'] == 1
    assert r['ok'] is False, "23 markets x 5 dead sources used to conclude ✅"
    # power_plants + substations + 2 gas sources, one market
    assert r['source_error_count'] == 4, r['source_errors']


def test_a_quiet_day_with_no_source_errors_is_ok(monkeypatch):
    _stub_sources(monkeypatch, [], None)
    r = ead.run_full_sync(_Conn(LIVE_COLS), markets={'ashburn': (39.0, -77.5, 50)})
    assert r['ok'] is True and r['source_error_count'] == 0


def test_job_route_returns_500_when_the_run_wrote_nothing(monkeypatch):
    from flask import Flask
    import routes.jobs_routes as jr
    app = Flask(__name__)
    app.register_blueprint(jr.jobs_bp)
    # Built at runtime so no credential-shaped literal sits in a tracked file
    # (scripts/check_no_leaked_credentials.py fires on 64-char hex). Long,
    # 36 distinct chars, no dictionary token — passes is_weak_credential().
    key = "".join("abcdefghijklmnopqrstuvwxyz0123456789"[(i * 11) % 36] for i in range(64))
    monkeypatch.setenv('DCHUB_ADMIN_KEY', key)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://stub')
    monkeypatch.setattr(jr, '_reg_update', lambda *a, **k: None)
    monkeypatch.setattr(jr.psycopg2, 'connect', lambda *a, **k: _Conn())
    monkeypatch.setattr(ead, 'run_full_sync',
                        lambda conn: {'ok': False, 'source_error_count': 4,
                                      'source_errors': ['power_plants/ashburn: dead'],
                                      'markets_synced': 1})
    c = app.test_client()
    r = c.post('/api/jobs/energy-discovery', headers={'X-Admin-Key': key})
    assert r.status_code == 500, r.data[:300]
    body = r.get_json()
    assert body['success'] is False and body['source_error_count'] == 4

    monkeypatch.setattr(ead, 'run_full_sync',
                        lambda conn: {'ok': True, 'source_error_count': 0,
                                      'source_errors': [], 'markets_synced': 1})
    r = c.post('/api/jobs/energy-discovery', headers={'X-Admin-Key': key})
    assert r.status_code == 200 and r.get_json()['success'] is True
