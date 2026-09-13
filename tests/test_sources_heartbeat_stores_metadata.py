"""A heartbeat's `metadata` reaches its extraction_runs row.

POST /api/v1/sources/<id>/heartbeat passed its INSERT parameters as
`(... Json(metadata) ...) if False else (... None)`, so the metadata column was
NULL on every run whatever the caller sent (the brain's cycle_id, the
data-pulse step counts, the MCP server's trigger). These tests drive the real
route through a connection that records what the INSERT was given.
"""
import json

import psycopg2.extensions
import pytest
from flask import Flask
from psycopg2.extras import Json

from routes import sources

ADMIN_VALUE = "heartbeat-test-admin-value"
DSN = "postgresql://recording-fake.invalid/none"


class _Cursor:
    """The psycopg2 cursor methods the heartbeat path calls, and no others."""

    def __init__(self, log):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._log.append((sql, params))

    def fetchone(self):
        return (7,)


class _Connection:
    """The psycopg2 connection methods _conn() and the heartbeat path call."""

    def __init__(self, log):
        self._log = log
        self.commits = 0

    def cursor(self):
        return _Cursor(self._log)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _methods(cls):
    return {n for n, v in vars(cls).items() if callable(v) and n != "__init__"}


def test_fakes_model_only_methods_the_real_psycopg2_objects_have():
    assert _methods(_Cursor) == {"__enter__", "__exit__", "execute", "fetchone"}
    assert _methods(_Connection) == {"cursor", "commit", "close"}
    for name in _methods(_Cursor):
        assert hasattr(psycopg2.extensions.cursor, name), name
    for name in _methods(_Connection):
        assert hasattr(psycopg2.extensions.connection, name), name


@pytest.fixture
def beat(monkeypatch):
    log, conns = [], []

    def connect(dsn):
        assert dsn == DSN, "the route must never reach a real database here"
        conn = _Connection(log)
        conns.append(conn)
        return conn

    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_VALUE)
    monkeypatch.setattr(sources._pg, "connect", connect)
    monkeypatch.setattr(sources._ensure_tables, "_done", True, raising=False)
    app = Flask(__name__)
    app.register_blueprint(sources.sources_bp)
    client = app.test_client()

    def post(body, headers=None):
        if headers is None:
            headers = {"Authorization": f"Bearer {ADMIN_VALUE}"}
        resp = client.post("/api/v1/sources/test-source/heartbeat", json=body, headers=headers)
        inserts = [params for sql, params in log if "INSERT INTO extraction_runs" in sql]
        return resp, inserts, conns

    return post


def _stored_metadata(inserts):
    """The metadata parameter of the one INSERT, as psycopg2 would send it."""
    assert len(inserts) == 1, inserts
    params = inserts[0]
    assert len(params) == 7 and params[0] == "test-source", params
    value = params[6]
    if value is None:
        return None
    assert isinstance(value, Json), type(value)
    return json.loads(value.dumps(value.adapted))


def test_metadata_object_is_stored_on_the_run_row(beat):
    metadata = {"cycle_id": "cycle-42", "iso_rows": 12, "sec_ran": True, "trigger": "github-actions"}
    resp, inserts, conns = beat(
        {"status": "success", "rows_affected": 12, "duration_ms": 900, "metadata": metadata})
    assert resp.status_code == 200, resp.get_json()
    assert _stored_metadata(inserts) == metadata
    assert resp.get_json()["metadata"] == "stored"
    assert conns[-1].commits == 1


def test_no_metadata_stores_null(beat):
    resp, inserts, _ = beat({"status": "success"})
    assert resp.status_code == 200, resp.get_json()
    assert _stored_metadata(inserts) is None
    assert resp.get_json()["metadata"] == "none"


def test_secret_values_in_metadata_are_scrubbed(beat, monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "eia-value-for-scrub-test")
    resp, inserts, _ = beat({"status": "failure", "error": "upstream refused", "metadata": {
        "caller": {"echoed": ADMIN_VALUE},
        "upstream": ["refused eia-value-for-scrub-test"],
    }})
    assert resp.status_code == 200, resp.get_json()
    stored = _stored_metadata(inserts)
    assert stored == {"caller": {"echoed": "***"}, "upstream": ["refused ***"]}


def test_oversize_metadata_is_replaced_by_a_marker_and_the_run_still_recorded(beat):
    resp, inserts, conns = beat({"status": "success", "metadata": {"blob": "x" * 4100}})
    assert resp.status_code == 200, resp.get_json()
    stored = _stored_metadata(inserts)
    assert "blob" not in stored, "a payload over 4 KB must not be stored"
    assert stored["_dropped"] == "metadata over 4096 bytes" and stored["bytes"] > 4100
    assert resp.get_json()["metadata"] == "dropped: metadata over 4096 bytes"
    assert conns[-1].commits == 1


def test_non_object_metadata_is_replaced_by_a_marker(beat):
    resp, inserts, _ = beat({"status": "success", "metadata": ["cycle-42"]})
    assert resp.status_code == 200, resp.get_json()
    assert _stored_metadata(inserts) == {"_dropped": "metadata must be a JSON object", "type": "list"}
    assert resp.get_json()["metadata"] == "dropped: metadata must be a JSON object"


def test_wrong_credential_writes_nothing(beat):
    resp, inserts, conns = beat(
        {"status": "success", "metadata": {"a": 1}}, headers={"Authorization": "Bearer not-the-admin-value"})
    assert resp.status_code == 401
    assert inserts == [] and conns == []
