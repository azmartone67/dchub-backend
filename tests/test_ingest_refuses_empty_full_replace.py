#!/usr/bin/env python3
"""tests/test_ingest_refuses_empty_full_replace.py — a feed that returns nothing
must never be allowed to empty the table it feeds.

NO NETWORK, NO DB. The real handlers run inside a Flask test request context with
`psycopg2.connect` replaced by a spy that FAILS if it is called at all.

WHAT WENT WRONG (2026-09-02). Every full-replace ingest endpoint writes
DELETE-then-INSERT inside one transaction. That is the right shape — it is why a
weekly replace has no window where the layer is empty. But it means an empty row
list is indistinguishable from a legitimate wipe:

    DELETE FROM gas_pipelines WHERE source = %s   -- runs
    (no rows to insert)                           -- inserts nothing
    return jsonify(ok=True, ...)                  -- reports success

routes/transmission_ingest.py has refused this since it was written. The gas
endpoint was copied from it WITHOUT the guard, so one upstream response that
parses to zero features would silently drop 32,851 rows and report ok=true.

★ THE TEST IS "THE DATABASE WAS NEVER TOUCHED", not "the status code was 400".
A guard that returns 400 *after* opening the transaction would pass a
status-code assertion and still have deleted the rows. The spy is the point.

★ Both endpoints are checked, and any future one should be added here. The
defect was not that someone wrote a bad endpoint — it was that someone copied a
good one and dropped a line.

Run standalone:   python3 tests/test_ingest_refuses_empty_full_replace.py
Run under pytest: pytest tests/test_ingest_refuses_empty_full_replace.py
"""
import gzip
import json
import os
import sys

import pytest

flask = pytest.importorskip("flask")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ADMIN_KEY = "test-admin-key"


class _ConnectWasCalled(AssertionError):
    pass


def _spy(*_a, **_kw):
    raise _ConnectWasCalled(
        "psycopg2.connect was called on an EMPTY payload — the DELETE in the "
        "full-replace transaction would have run and emptied the table")


def _post_empty(module, handler_name, path):
    """POST a gzipped {"rows": []} at the real handler with the DB spied."""
    app = flask.Flask(__name__)
    real_connect = module.psycopg2.connect
    module.psycopg2.connect = _spy
    try:
        body = gzip.compress(json.dumps({"rows": []}).encode())
        with app.test_request_context(
            path, method="POST", data=body,
            headers={"X-Admin-Key": ADMIN_KEY,
                     "Content-Type": "application/json",
                     "Content-Encoding": "gzip"},
        ):
            with app.app_context():
                return getattr(module, handler_name)()
    finally:
        module.psycopg2.connect = real_connect


def _status(result):
    return result[1] if isinstance(result, tuple) else 200


def _payload(result):
    resp = result[0] if isinstance(result, tuple) else result
    return resp.get_json() if hasattr(resp, "get_json") else resp


@pytest.fixture(autouse=True)
def _admin_key_set(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")


def test_gas_ingest_refuses_an_empty_full_replace():
    """THE REGRESSION. This endpoint had no guard."""
    import routes.gas_pipeline_ingest as gas
    result = _post_empty(gas, "ingest_gas_pipelines", "/api/v1/admin/ingest/gas-pipelines")
    assert _status(result) == 400, (
        "empty payload was accepted (%s) — the DELETE would have run" % (_payload(result),))
    body = _payload(result)
    assert body.get("ok") is False
    assert "empty" in (body.get("error") or "").lower()


def test_transmission_ingest_refuses_an_empty_full_replace():
    """The sibling that already had the guard — pinned so it cannot be lost."""
    import routes.transmission_ingest as tx
    handler = next(n for n in ("ingest_transmission_lines", "ingest_transmission")
                   if hasattr(tx, n))
    result = _post_empty(tx, handler, "/api/v1/admin/ingest/transmission-lines")
    assert _status(result) == 400, "transmission lost its empty-replace guard"


def test_the_guard_runs_before_any_connection_is_opened():
    """Explicit statement of the property the spy enforces.

    A guard placed after psycopg2.connect() would satisfy a status-code check
    and still have deleted the rows, so this asserts the ORDER: on an empty
    payload the spy must never fire, which is what makes the 400 meaningful.
    """
    import routes.gas_pipeline_ingest as gas
    try:
        result = _post_empty(gas, "ingest_gas_pipelines", "/api/v1/admin/ingest/gas-pipelines")
    except _ConnectWasCalled as exc:
        raise AssertionError(str(exc))
    assert _status(result) == 400


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
