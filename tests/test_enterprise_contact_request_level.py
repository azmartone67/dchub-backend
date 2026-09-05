"""The enterprise lead form, exercised as a REQUEST — the coverage that was missing.

/api/v1/enterprise/contact is the public enterprise lead form. In one week it
shipped two production bugs, and neither was catchable by anything CI ran:

  1. r-inquiry-schema-fork — two incompatible `CREATE TABLE IF NOT EXISTS
     enterprise_inquiries` definitions meant this writer's INSERT named columns
     that did not exist. Live since 2026-06-30; every submission lost.
  2. r-restore-helpers — a refactor spliced out `_rate_limited` and
     `_relay_to_webhook`, and the endpoint answered
     `500 name '_rate_limited' is not defined` for every VALID submission.

Both are invisible to every gate the repo had: a missing column and a missing
function are not syntax errors, not import errors, and not changed lines near
the call site, so ast.parse, the module import and delta linting all pass. Bug
2 shipped with a green 15101-test suite.

★ THE REASON THE GAP EXISTED, and why this file is shaped the way it is: the
handler writes to a live table, so no test called it. But look at the ORDER the
handler does things —

    validate -> 400
    _rate_limited(src_ip) -> 429          <- bug 2 raised HERE
    _relay_to_webhook(payload)            <- no database
    _ensure_table() + INSERT -> 503       <- bug 1 raised HERE
    -> 200

— everything up to the INSERT runs with no database at all. So the first class
of test below needs NO Postgres and runs everywhere: drive a real request
through the real handler and assert the endpoint produces a HANDLED outcome,
never an unhandled 500. That alone would have caught bug 2 before it merged.

The second class needs a throwaway Postgres (DCHUB_PG_TEST_DSN, same opt-in as
tests/test_enterprise_inquiries_heal_on_postgres.py) and covers bug 1: the
success path, end to end, storing a row.

Imports routes/enterprise.py — a blueprint module with no side effects — never
main, per the green-main convention.
"""
import json
import os

import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

from routes.enterprise import enterprise_bp  # noqa: E402

VALID = {
    "org_name": "Acme Capacity Partners",
    "email": "buyer@example.com",
    "use_case": "evaluating 40MW of powered shell in ERCOT",
    "expected_volume": "10k",
    "company_url": "",          # honeypot, must be empty
}


def _client(dsn=None):
    app = Flask(__name__)
    app.register_blueprint(enterprise_bp)
    app.config["TESTING"] = True
    # The handler reads DATABASE_URL at call time via _conn().
    prev = os.environ.get("DATABASE_URL")
    if dsn:
        os.environ["DATABASE_URL"] = dsn
    else:
        os.environ.pop("DATABASE_URL", None)
    return app.test_client(), prev


def _restore(prev):
    if prev is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = prev


def _post(client, body):
    return client.post("/api/v1/enterprise/contact",
                       data=json.dumps(body),
                       content_type="application/json")


# ---------------------------------------------------------------------------
# CLASS 1 — no database required. Runs everywhere, every time.
# ---------------------------------------------------------------------------
@pytest.fixture()
def client_nodb():
    c, prev = _client(None)
    yield c
    _restore(prev)


def test_a_valid_submission_never_raises_an_unhandled_error(client_nodb):
    """★ THE ONE THAT WOULD HAVE CAUGHT THE 500.

    With no database the storage step must fail — that is expected and the
    handler turns it into a 503 naming the sales address. What must NEVER
    happen is an unhandled exception: that is what
    `name '_rate_limited' is not defined` was, and it reached production.

    Asserted as "not 500", not as "== 503", so this keeps its meaning if the
    storage-failure status is ever retuned."""
    r = _post(client_nodb, VALID)
    body = r.get_data(as_text=True)
    assert r.status_code != 500, (
        f"the handler raised instead of answering: {body[:300]}")
    assert "is not defined" not in body, (
        f"a NameError leaked into the response: {body[:300]}")
    assert r.status_code in (200, 429, 503), f"unexpected {r.status_code}: {body[:200]}"


def test_the_whole_handler_body_is_reached_not_just_validation(client_nodb):
    """Guards the guard. A 400 short-circuits before _rate_limited and before
    the storage block, so a test that only ever produced 400s would pass
    against a handler whose entire body was broken — which is exactly the
    shape of the bug that shipped."""
    r = _post(client_nodb, VALID)
    assert r.status_code != 400, (
        "the valid payload was rejected by validation, so this file never "
        "exercises the code past it — fix VALID, do not relax this")
    payload = r.get_json() or {}
    # storage_failed is the honest no-database outcome and proves control
    # reached the INSERT block, i.e. past _rate_limited and _relay_to_webhook.
    assert payload.get("error") == "storage_failed" or r.status_code == 200


@pytest.mark.parametrize("missing", ["org_name", "email", "use_case", "expected_volume"])
def test_each_required_field_is_enforced(client_nodb, missing):
    body = dict(VALID)
    body[missing] = ""
    r = _post(client_nodb, body)
    assert r.status_code == 400
    assert missing in (r.get_json() or {}).get("errors", {})


def test_the_honeypot_swallows_bots_without_storing(client_nodb):
    """A filled company_url returns 200 and stores nothing — and must not
    reach the storage path (which would 503 here, revealing the trap)."""
    r = _post(client_nodb, {**VALID, "company_url": "http://spam.example"})
    assert r.status_code == 200
    assert (r.get_json() or {}).get("ok") is True


def test_an_invalid_email_is_rejected(client_nodb):
    r = _post(client_nodb, {**VALID, "email": "not-an-email"})
    assert r.status_code == 400


def test_an_unknown_volume_is_rejected(client_nodb):
    r = _post(client_nodb, {**VALID, "expected_volume": "42k"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# CLASS 2 — the success path, against a real Postgres. Opt-in.
# ---------------------------------------------------------------------------
DSN = os.environ.get("DCHUB_PG_TEST_DSN", "")
if DSN and any(x in DSN.lower() for x in ("neon", "azure", "amazonaws", "railway", "prod")):
    raise RuntimeError(
        "DCHUB_PG_TEST_DSN looks like a real database; this file writes rows")

pg = pytest.mark.skipif(
    not DSN, reason="set DCHUB_PG_TEST_DSN to a throwaway Postgres to run")


@pytest.fixture()
def client_pg():
    psycopg2 = pytest.importorskip("psycopg2")
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS enterprise_inquiries CASCADE")
    c.close()
    cl, prev = _client(DSN)
    yield cl
    _restore(prev)


def _rows():
    """Stored leads, or [] if the table was never created.

    The honeypot and validation paths return BEFORE _ensure_table(), so on
    those the table genuinely does not exist — which is the strongest possible
    form of "nothing was stored", not a condition to work around. Treating
    UndefinedTable as a hard error would make the two negative tests below fail
    for a reason that is the assertion succeeding."""
    import psycopg2
    c = psycopg2.connect(DSN)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("SELECT org_name, email, use_case, expected_volume, "
                        "status FROM enterprise_inquiries")
            return cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        return []
    finally:
        c.close()


@pg
def test_a_valid_submission_is_stored(client_pg):
    """★ THE ONE THAT WOULD HAVE CAUGHT THE SCHEMA FORK. The table does not
    exist when this starts, so the handler must create it via the shared
    schema and then insert into it."""
    r = _post(client_pg, VALID)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert (r.get_json() or {}).get("ok") is True
    rows = _rows()
    assert len(rows) == 1, f"expected exactly one stored lead, got {len(rows)}"
    org, email, use_case, vol, status = rows[0]
    assert org == VALID["org_name"] and email == VALID["email"]
    assert vol == VALID["expected_volume"]
    assert status == "new", f"status defaulted to {status!r}, not 'new'"


@pg
def test_the_honeypot_stores_nothing(client_pg):
    _post(client_pg, {**VALID, "company_url": "http://spam.example"})
    assert _rows() == []


@pg
def test_a_rejected_submission_stores_nothing(client_pg):
    _post(client_pg, {**VALID, "email": "nope"})
    assert _rows() == []
