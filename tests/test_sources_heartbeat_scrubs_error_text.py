"""A heartbeat's `error` text is scrubbed and length-capped before it is stored.

POST /api/v1/sources/<id>/heartbeat writes the body's `error` to
extraction_runs.error on every run and to source_registry.last_error on a
failure, and the registry's GET endpoints return both. These tests drive the
real route through a connection that records the parameters the INSERT and the
UPDATE were given.
"""
import psycopg2.extensions
import pytest
from flask import Flask

from routes import _iso_common, sources

SOURCE_ID = "test-source"
ADMIN_VALUE = "heartbeat-error-test-admin-value"
EIA_VALUE = "eia-value-for-error-scrub-test"
DSN = "postgresql://recording-fake.invalid/none"
ADMIN_ENV = ("DCHUB_ADMIN_SECRET", "DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY")


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

    # Only the values a test sets may be present: a credential exported in the
    # shell running the suite would otherwise change what gets scrubbed.
    for name in ADMIN_ENV + tuple(_iso_common.SECRET_ENV_KEYS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_VALUE)
    monkeypatch.setattr(sources._pg, "connect", connect)
    monkeypatch.setattr(sources._ensure_tables, "_done", True, raising=False)
    app = Flask(__name__)
    app.register_blueprint(sources.sources_bp)
    client = app.test_client()

    def post(body, token=ADMIN_VALUE):
        """(INSERT parameters, UPDATE parameters) of one recorded heartbeat."""
        resp = client.post(f"/api/v1/sources/{SOURCE_ID}/heartbeat", json=body,
                           headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.get_json()
        inserts = [params for sql, params in log if "INSERT INTO extraction_runs" in sql]
        updates = [params for sql, params in log if "UPDATE source_registry" in sql]
        assert len(inserts) == 1 and len(updates) == 1, log
        assert len(conns) == 1 and conns[0].commits == 1
        return inserts[0], updates[0]

    return post


def _run_row_error(insert):
    """The `error` parameter of the extraction_runs INSERT."""
    assert len(insert) == 7 and insert[0] == SOURCE_ID, insert
    return insert[5]


def test_failure_error_is_scrubbed_on_the_run_row_and_the_source_row(beat, monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", EIA_VALUE)
    insert, update = beat({"status": "failure",
                           "error": f"HTTPError: 403 fetching {EIA_VALUE} (sent {ADMIN_VALUE})"})
    expected = "HTTPError: 403 fetching *** (sent ***)"
    assert _run_row_error(insert) == expected
    assert update == (expected, SOURCE_ID)


@pytest.mark.parametrize("env_name", ADMIN_ENV)
def test_each_admin_credential_the_route_accepts_is_scrubbed(beat, monkeypatch, env_name):
    value = env_name.lower().replace("_", "-") + "-error-test"
    monkeypatch.setenv(env_name, value)
    # Authenticating with the value proves the route accepts it.
    insert, update = beat({"status": "failure", "error": f"echoed {value} back"}, token=value)
    assert _run_row_error(insert) == "echoed *** back"
    assert update == ("echoed *** back", SOURCE_ID)


def test_a_secret_across_the_length_cap_is_scrubbed_before_the_cut(beat):
    insert, update = beat({"status": "failure", "error": "x" * 495 + ADMIN_VALUE + "y" * 100})
    expected = "x" * 495 + "***" + "yy"
    assert len(expected) == 500
    assert _run_row_error(insert) == expected
    assert update == (expected, SOURCE_ID)


@pytest.mark.parametrize("status, source_row_params", [
    ("success", (None, SOURCE_ID)),
    ("partial", (SOURCE_ID,)),
    ("running", (SOURCE_ID,)),
])
def test_error_on_a_beat_that_is_not_a_failure_is_scrubbed_too(beat, status, source_row_params):
    insert, update = beat({"status": status, "error": f"retried after {ADMIN_VALUE}"})
    assert _run_row_error(insert) == "retried after ***"
    assert update == source_row_params


def test_no_error_stores_null(beat):
    insert, update = beat({"status": "failure"})
    assert _run_row_error(insert) is None
    assert update == (None, SOURCE_ID)


def test_admin_values_under_8_characters_are_not_blanked_out(beat, monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "1234567")
    monkeypatch.setenv("DCHUB_ADMIN_SECRET", " abcdefgh\n")  # 8 once stripped
    insert, _ = beat({"status": "failure", "error": "row 1234567 failed; abcdefgh expired"})
    assert _run_row_error(insert) == "row 1234567 failed; *** expired"


@pytest.mark.parametrize("raw, kind", [
    ({"detail": ADMIN_VALUE}, "dict"),
    ([ADMIN_VALUE], "list"),
    (500, "int"),
])
def test_an_error_that_is_not_a_string_is_stored_as_a_marker(beat, raw, kind):
    insert, update = beat({"status": "failure", "error": raw})
    marker = f"(error omitted: expected a string, got {kind})"
    assert _run_row_error(insert) == marker
    assert update == (marker, SOURCE_ID)
