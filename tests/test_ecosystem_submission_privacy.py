"""
tests/test_ecosystem_submission_privacy.py — the ecosystem directory's public
reads must not hand back submitter PII (2026-08-26).

WHAT WENT WRONG. `ecosystem_routes.py` served `ecosystem_companies` rows to the
public through three GET routes, each building its payload the same way:

    company = dict(row)          # every column, including contact_email
    ...
    return jsonify({'companies': companies, ...})

`SELECT *` plus `dict(row)` means the response shape is the TABLE shape: every
column is public by default, and any column added later is public the day it is
added. `contact_email` and `submitted_by` are written by POST /api/ecosystem
straight from the submission form. No submitted row existed yet when this was
found -- all 66 approved rows are seeded, with contact_email NULL -- so nothing
leaked. The first real submission would have been the leak.

Worse, `?status=pending` was an ungated query parameter on a public route, so
the entire unreviewed submission queue (name, website, contact) was listable by
anyone who guessed the value.

Second defect, same file: the admin gate read

    admin_key = os.environ.get('ADMIN_API_KEY', 'dc-hub-admin-2024')

The fallback literal is published in this repo. On any process whose env lacks
ADMIN_API_KEY, that literal IS the admin key and approve/feature are open to
anyone who can read the source. The web process has ADMIN_API_KEY set (the
literal returns 403 in production), so this was latent, not live -- but a
default that turns a misconfigured box into an open one is not a gate.

WHAT THIS LOCKS. serialize_company() is the single exit for a row, PRIVATE_FIELDS
never survive it without admin, is_admin_request() returns False when the env var
is missing, and the three public GET handlers must keep going through the
serializer -- asserted on the AST, so re-introducing a hand-rolled `dict(row)`
in any of them fails here rather than in production.
"""
import ast
import pathlib

import pytest

flask = pytest.importorskip("flask")
pytest.importorskip("psycopg2")

import ecosystem_routes as er

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "ecosystem_routes.py"

# A row as psycopg2 hands it back: every column of ecosystem_companies.
ROW = {
    "id": "eka-sunucu-1f2e3d",
    "name": "Eka Sunucu",
    "description": "Turkish hosting provider",
    "category": "Managed Services",
    "website": "https://www.ekasunucu.com/",
    "markets": '["Turkey", "Germany"]',
    "services": '["VPS", "Dedicated"]',
    "ai_keywords": "not json",
    "contact_email": "someone@ekasunucu.com",
    "submitted_by": "someone@ekasunucu.com",
    "status": "pending",
}


def _app():
    return flask.Flask(__name__)


# ── the serializer ────────────────────────────────────────────────────────────

def test_public_serialization_drops_every_private_field():
    company = er.serialize_company(ROW)
    for field in er.PRIVATE_FIELDS:
        assert field not in company, f"{field} survived into a public payload"
    # and it is not merely renamed or nested somewhere in the payload
    assert "someone@ekasunucu.com" not in repr(company)


def test_private_fields_are_the_ones_that_carry_pii():
    assert "contact_email" in er.PRIVATE_FIELDS
    assert "submitted_by" in er.PRIVATE_FIELDS


def test_admin_serialization_keeps_them():
    company = er.serialize_company(ROW, include_private=True)
    assert company["contact_email"] == "someone@ekasunucu.com"
    assert company["submitted_by"] == "someone@ekasunucu.com"


def test_serializer_still_decodes_json_columns_and_tolerates_junk():
    company = er.serialize_company(ROW)
    assert company["markets"] == ["Turkey", "Germany"]
    assert company["services"] == ["VPS", "Dedicated"]
    # a column that is not JSON must pass through, not raise
    assert company["ai_keywords"] == "not json"


def test_serializer_does_not_mutate_the_row_it_was_given():
    row = dict(ROW)
    er.serialize_company(row)
    assert row["contact_email"] == "someone@ekasunucu.com"


# ── the admin gate ────────────────────────────────────────────────────────────

def test_admin_gate_fails_closed_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    with _app().test_request_context(headers={"X-API-Key": "dc-hub-admin-2024"}):
        assert er.is_admin_request() is False, (
            "the repo's published default literal must never authenticate"
        )


def test_admin_gate_fails_closed_for_empty_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "")
    with _app().test_request_context(headers={"X-API-Key": ""}):
        assert er.is_admin_request() is False


def test_admin_gate_accepts_the_configured_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    with _app().test_request_context(headers={"X-API-Key": "s3cret"}):
        assert er.is_admin_request() is True
    with _app().test_request_context("/?api_key=s3cret"):
        assert er.is_admin_request() is True


def test_admin_gate_rejects_a_wrong_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    with _app().test_request_context(headers={"X-API-Key": "nope"}):
        assert er.is_admin_request() is False
    with _app().test_request_context():
        assert er.is_admin_request() is False


def test_no_route_reads_the_published_default_literal():
    assert "dc-hub-admin-2024" not in SOURCE.read_text(encoding="utf-8")


# ── the shape, on the AST ─────────────────────────────────────────────────────

PUBLIC_READ_HANDLERS = ("list_companies", "get_company", "search_companies")


def _handler(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from ecosystem_routes.py -- rename or removal")


@pytest.mark.parametrize("name", PUBLIC_READ_HANDLERS)
def test_cacheable_handlers_never_ask_for_private_fields(name):
    """include_private on a cacheable route is the leak, not the fix."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    source = ast.unparse(_handler(tree, name))
    assert "include_private" not in source, (
        f"{name} serves an edge-cached path and must never request private "
        f"fields -- the KV cache key is auth-stripped, so one admin response "
        f"is served to every caller"
    )


@pytest.mark.parametrize("name", PUBLIC_READ_HANDLERS)
def test_public_read_handlers_go_through_the_serializer(name):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    fn = _handler(tree, name)
    calls = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "serialize_company" in calls, (
        f"{name} builds its payload without serialize_company(); a hand-rolled "
        f"dict(row) republishes every column, which is how contact_email leaked"
    )
    assert "dict" not in calls, (
        f"{name} calls dict() directly -- rows must exit through serialize_company"
    )


# ── the route, exercised ──────────────────────────────────────────────────────
#
# These drive the real handler through a request. An earlier version of this file
# asserted that the strings "is_admin_request()" and "403" appeared in the
# function body; `if False and status != 'approved'` passed that test with the
# gate disabled. Source text is not behaviour.


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self._last = ""

    def execute(self, query, params=None):
        self._last = query

    def fetchone(self):
        if "COUNT(" in self._last:
            return [len(self._rows)]
        return dict(self._rows[0]) if self._rows else None

    rowcount = 1

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False
        self.committed = False

    def cursor(self):
        return _FakeCursor(self._rows)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _client():
    app = _app()
    app.register_blueprint(er.ecosystem_bp)
    return app.test_client()


def test_pending_queue_is_refused_and_never_reaches_the_database(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")

    def _boom():
        raise AssertionError(
            "list_companies queried the database on an unauthorised pending listing"
        )

    monkeypatch.setattr(er, "get_db", _boom)
    response = _client().get("/api/ecosystem?status=pending")
    assert response.status_code == 403
    assert response.get_json()["success"] is False


def test_the_cacheable_route_refuses_pending_even_for_an_admin(monkeypatch):
    """The edge caches /api/ecosystem and its KV key is auth-stripped.

    A privileged 200 on this path would be stored and replayed to anonymous
    callers, so the route has no admin mode at all -- the key must not open it.
    """
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")

    def _boom():
        raise AssertionError("the cacheable route queried the database for pending rows")

    monkeypatch.setattr(er, "get_db", _boom)
    response = _client().get("/api/ecosystem?status=pending", headers={"X-API-Key": "s3cret"})
    assert response.status_code == 403


def test_admin_reads_the_queue_on_the_force_origin_path(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    monkeypatch.setattr(er, "get_db", lambda: _FakeConn([dict(ROW)]))
    response = _client().get("/api/ecosystem/pending", headers={"X-API-Key": "s3cret"})
    assert response.status_code == 200
    company = response.get_json()["companies"][0]
    assert company["contact_email"] == "someone@ekasunucu.com"


def test_the_queue_is_never_stored_by_a_cache(monkeypatch):
    """Every response from the admin path, including its refusals."""
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    monkeypatch.setattr(er, "get_db", lambda: _FakeConn([dict(ROW)]))
    client = _client()
    for headers in ({"X-API-Key": "s3cret"}, {}):
        response = client.get("/api/ecosystem/pending", headers=headers)
        assert "no-store" in response.headers.get("Cache-Control", ""), (
            f"queue response with headers={headers} is cacheable: "
            f"{response.headers.get('Cache-Control')!r}"
        )


def test_the_queue_is_refused_without_a_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")

    def _boom():
        raise AssertionError("list_pending queried the database unauthenticated")

    monkeypatch.setattr(er, "get_db", _boom)
    assert _client().get("/api/ecosystem/pending").status_code == 403
    assert _client().get("/api/ecosystem/pending",
                         headers={"X-API-Key": "wrong"}).status_code == 403


def test_pending_is_not_read_as_a_company_id(monkeypatch):
    """Werkzeug must route the static rule ahead of /api/ecosystem/<company_id>."""
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    monkeypatch.setattr(er, "get_db", lambda: _FakeConn([dict(ROW)]))
    response = _client().get("/api/ecosystem/pending", headers={"X-API-Key": "s3cret"})
    body = response.get_json()
    assert "companies" in body, f"fell through to get_company: {body}"
    assert body.get("status") == "pending"


@pytest.mark.parametrize("method,path", [
    ("post", "/api/ecosystem/some-id/reject"),
    ("post", "/api/ecosystem/some-id/delete"),
    ("post", "/api/ecosystem/some-id/approve"),
    ("post", "/api/ecosystem/some-id/feature"),
])
def test_every_mutating_admin_route_is_refused_without_a_key(monkeypatch, method, path):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")

    def _boom():
        raise AssertionError(f"{method.upper()} {path} reached the database unauthenticated")

    monkeypatch.setattr(er, "get_db", _boom)
    assert getattr(_client(), method)(path).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("post", "/api/ecosystem/some-id/reject"),
    ("post", "/api/ecosystem/some-id/delete"),
])
def test_reject_and_delete_work_for_an_admin(monkeypatch, method, path):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    conn = _FakeConn([dict(ROW)])
    monkeypatch.setattr(er, "get_db", lambda: conn)
    response = getattr(_client(), method)(path, headers={"X-API-Key": "s3cret"})
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert conn.committed, f"{method.upper()} {path} returned success without committing"


def test_public_listing_returns_rows_without_contacts(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    monkeypatch.setattr(er, "get_db", lambda: _FakeConn([dict(ROW)]))
    response = _client().get("/api/ecosystem")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    company = response.get_json()["companies"][0]
    assert company["name"] == "Eka Sunucu"
    for field in er.PRIVATE_FIELDS:
        assert field not in company
    assert "someone@ekasunucu.com" not in body


def test_single_company_read_is_scrubbed_even_for_an_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    monkeypatch.setattr(er, "get_db", lambda: _FakeConn([dict(ROW)]))

    class _OneRow(_FakeConn):
        def cursor(self):
            cursor = _FakeCursor(self._rows)
            cursor.fetchone = lambda: dict(ROW)
            return cursor

    monkeypatch.setattr(er, "get_db", lambda: _OneRow([dict(ROW)]))
    for headers in ({}, {"X-API-Key": "s3cret"}):
        response = _client().get("/api/ecosystem/eka-sunucu-1f2e3d", headers=headers)
        assert response.status_code == 200
        assert "someone@ekasunucu.com" not in response.get_data(as_text=True), (
            f"single-company read leaked a contact with headers={headers}"
        )
