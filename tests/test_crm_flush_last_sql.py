"""crm_flush_last against a REAL Postgres — the row /crm/health reads.

Skips without CRM_FLUSH_LAST_SQL_DSN. The db-parity job in pre-merge.yml sets it
and then FAILS if this file skipped — a skipped SQL proof proves nothing.

Why a real database: _record_flush never raises (a record must not be able to
fail the flush it describes), so SQL that Postgres refuses — an ON CONFLICT
target with no unique constraint, a bad cast — would degrade to
`last_flush: null` in production with nothing but a WARNING. The FakeDB in
test_crm_config_failure_never_burns_a_lead checks each statement's shape; only
Postgres checks that it runs.

Every table is built by the module's own _SCHEMA_SQL, never retyped, inside the
private schema crm_flush_last_t.
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

DSN = os.environ.get("CRM_FLUSH_LAST_SQL_DSN")
SCHEMA = "crm_flush_last_t"


@pytest.fixture()
def m(monkeypatch):
    if not DSN:
        pytest.skip("CRM_FLUSH_LAST_SQL_DSN not set")
    import routes.crm_reverse_etl as mod
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    monkeypatch.setattr(mod, "_conn", lambda: psycopg2.connect(
        DSN, options=f"-c TimeZone=UTC -c search_path={SCHEMA}"))
    monkeypatch.setattr(mod, "_return", lambda c, error=False: c.close())
    for k, v in dict(CRM_PROVIDER="stub", HUBSPOT_API_KEY="", SF_INSTANCE_URL="",
                     SF_ACCESS_TOKEN="", DRY_RUN=False, DISABLE=False,
                     DCHUB_ADMIN_KEY="adm-test", _SCHEMA_READY=False).items():
        monkeypatch.setattr(mod, k, v)
    monkeypatch.setenv("DCHUB_ROLE", "worker")
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "dchub-worker")
    monkeypatch.delenv("RAILWAY_REPLICA_ID", raising=False)
    c = mod._conn()
    mod._ensure_schema(c)
    assert mod._SCHEMA_READY, "the schema script did not run on Postgres"
    with c.cursor() as cur:
        cur.execute("""INSERT INTO crm_outbound_queue (event_type, lead_email, status)
                       VALUES ('paid_conversion', 'a@example.com', 'queued') ON CONFLICT DO NOTHING,
                              ('paid_conversion', 'b@example.com', 'queued_export'),
                              ('newsletter_signup', 'c@example.com', 'pushed')""")
    c.commit()
    c.close()
    yield mod
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    admin.close()


def _rows(m):
    c = m._conn()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT role, summary->>'skipped' FROM crm_flush_last ORDER BY role")
            return cur.fetchall()
    finally:
        c.close()


def test_a_skipped_flush_lands_a_row_the_reader_decodes(m):
    out = m.flush_outbound_queue(trigger="scheduler")
    assert (out["skipped"], out["recorded"], out["unsent"]) == (
        "destination_not_configured", True, 2), out
    rec = m._read_last_flushes()["worker"]
    assert rec["host"] == {"role": "worker", "service": "dchub-worker", "replica": None}
    assert rec["config"]["provider"] == "stub" and rec["trigger"] == "scheduler"
    assert 0 <= rec["age_hours"] < 0.1 and rec["ran_at"].endswith("+00:00"), rec
    assert m.flush_slot_status(rec) == "stalled: destination_not_configured"


def test_the_next_flush_overwrites_its_roles_row_and_a_new_role_adds_one(m, monkeypatch):
    """ON CONFLICT (role) needs the primary key — without it Postgres refuses the
    upsert and the recorder quietly returns None."""
    m.flush_outbound_queue(trigger="scheduler")
    monkeypatch.setattr(m, "DISABLE", True)
    assert m.flush_outbound_queue(trigger="scheduler")["recorded"] is True
    assert _rows(m) == [("worker", "disabled")]
    monkeypatch.setenv("DCHUB_ROLE", "web")
    m.flush_outbound_queue(trigger="admin")
    assert _rows(m) == [("web", "disabled"), ("worker", "disabled")]


def test_health_on_web_reads_the_workers_row_from_postgres(m, monkeypatch):
    m.flush_outbound_queue(trigger="scheduler")
    monkeypatch.setenv("DCHUB_ROLE", "web")
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "dchub-backend")
    monkeypatch.setattr(m, "CRM_PROVIDER", "hubspot")
    monkeypatch.setattr(m, "HUBSPOT_API_KEY", "eu1-legacy-key")
    app = flask.Flask(__name__)
    app.register_blueprint(m.crm_reverse_etl_bp)
    j = app.test_client().get("/api/v1/admin/crm/health",
                              headers={"X-Admin-Key": "adm-test"}).get_json()
    assert j["status_counts"] == {"queued": 1, "queued_export": 1, "pushed": 1}, j
    assert j["last_flush"]["host"]["service"] == "dchub-worker"
    assert j["flusher_config_mismatch"] is True and j["stalled"] is True, j
    assert "dchub-worker[worker]" in j["stalled_reason"]
