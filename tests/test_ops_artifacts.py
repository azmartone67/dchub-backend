"""ops_artifacts — the write is gated, the names are an allowlist, and a
missing artifact is an absence rather than a zero.

★ THE CASE THAT MATTERS MOST is the last one. A store that answers a
  never-published name with `{}` hands a dashboard something that parses, and
  a dashboard that parses it renders confident zeroes for data it never
  received. That failure is invisible precisely when it is worst — right after
  a publisher breaks. So "not published" must be a 404 that names the workflow
  which should have written it.
"""
import json

import pytest

from routes import ops_artifacts as oa


class _Cur:
    def __init__(self, rows=None): self.rows, self.executed = rows or [], []
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows
    def close(self): pass


class _Conn:
    def __init__(self, rows=None): self.cur = _Cur(rows); self.committed = False
    def cursor(self): return self.cur
    def commit(self): self.committed = True
    def close(self): pass


@pytest.fixture
def app(monkeypatch):
    from flask import Flask
    a = Flask(__name__)
    a.register_blueprint(oa.ops_artifacts_bp)
    monkeypatch.setattr(oa, "_get_db", lambda: _Conn())
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    return a


URL = "/api/v1/ops/artifact/growth"


def test_write_without_a_key_is_401(app):
    r = app.test_client().post(URL, data=json.dumps({"a": 1}))
    assert r.status_code == 401, r.data


def test_write_with_a_wrong_key_is_401(app):
    r = app.test_client().post(URL, data=json.dumps({"a": 1}),
                               headers={"X-Admin-Key": "not-the-key"})
    assert r.status_code == 401, r.data


def test_write_with_the_admin_key_is_accepted(app):
    r = app.test_client().post(URL, data=json.dumps({"a": 1}),
                               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200, r.data
    assert r.get_json()["ok"] is True


def test_a_name_outside_the_allowlist_is_rejected_even_when_authorised(app):
    r = app.test_client().post("/api/v1/ops/artifact/anything-i-like",
                               data=json.dumps({"a": 1}),
                               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 400, r.data
    assert r.get_json()["error"] == "unknown_artifact"


def test_oversized_body_is_rejected(app):
    big = json.dumps({"pad": "x" * (oa.MAX_BYTES + 10)})
    r = app.test_client().post(URL, data=big,
                               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 413, r.status_code


def test_non_json_body_is_rejected(app):
    r = app.test_client().post(URL, data="not json at all",
                               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_json"


def test_scalar_top_level_is_rejected(app):
    r = app.test_client().post(URL, data="42",
                               headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_shape"


def test_reading_an_unpublished_artifact_is_404_not_an_empty_object(app):
    """★ The one that keeps a dashboard from rendering zeroes it never got."""
    r = app.test_client().get(URL)
    assert r.status_code == 404, r.status_code
    body = r.get_json()
    assert body["error"] == "not_published_yet"
    assert body["publisher"] == "stats-snapshot.yml"
    # It must not be mistakable for data.
    assert body != {}
    assert "body" not in body


def test_a_published_artifact_comes_back_verbatim_with_freshness(app, monkeypatch):
    import datetime as dt
    when = dt.datetime(2026, 9, 5, 1, 30, tzinfo=dt.timezone.utc)
    payload = {"pushes": 3, "nested": {"ok": True}}
    monkeypatch.setattr(oa, "_get_db",
                        lambda: _Conn([(payload, 41, when)]))
    r = app.test_client().get(URL)
    assert r.status_code == 200
    # Verbatim: a page that fetched the file can fetch this instead, unchanged.
    assert r.get_json() == payload
    assert r.headers["X-Artifact-Updated-At"].startswith("2026-09-05T01:30")
    assert r.headers["X-Artifact-Was"] == "data/growth.json"


def test_reading_an_unknown_name_lists_the_known_ones(app):
    r = app.test_client().get("/api/v1/ops/artifact/nope")
    assert r.status_code == 404
    assert "growth" in r.get_json()["known"]


def test_every_allowlisted_name_declares_what_it_was_and_who_writes_it():
    assert oa.ARTIFACTS, "an empty allowlist would make every write a 400 — vacuous"
    for name, entry in oa.ARTIFACTS.items():
        was, publisher = entry
        assert was and publisher, name
        assert publisher.endswith(".yml"), f"{name}: publisher should name a workflow"
        assert not name.startswith("/") and "/" not in name, name
