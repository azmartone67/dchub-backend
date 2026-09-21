"""The claim door's unused-key cap, against a REAL Postgres.

Skips without CLAIM_CAP_SQL_DSN. The db-parity job in pre-merge.yml sets it and
then FAILS if this file skipped — a skipped SQL proof proves nothing.

WHY A DATABASE. The stub cursor in tests/test_claim_key_no_ip_handback.py hands
its primed rows to any cap query, whatever the WHERE clause says. Which keys
count as "unused" is decided entirely in that WHERE clause, so only Postgres
can say what the cap counts.

WHAT IT PINS
  · a key used over REST is USED: only MCP writes last_used_at, so before this
    the cap counted REST-only agents on a shared address as idle and merged
    them onto one key;
  · keys nobody used still hit the cap — the control that proves the cap is
    live here at all, rather than failing open on a bad query;
  · a verified partner workspace is never handed another workspace's key, and a
    workspace's key is never handed to another name from the partner egress.

Everything that decides an outcome is production code:
  claim_key        the real handler, via the AST harness in
                   tests/test_claim_key_no_ip_handback.py, with a real pool
  REST use         real requests through routes/api_usage_tracker's hooks and its
                   real _flush(); the rows the cap reads are the rows the tracker
                   writes, never an INSERT typed here
  tables           from their writers' DDL, as tests/test_usage_tracker_self_serve_sql.py
All of it lives in a private schema, dropped afterwards.
"""
import os
from urllib.parse import quote

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

import routes.api_usage_tracker as tracker  # noqa: E402
from tests.test_claim_key_no_ip_handback import PARTNER_IP, _run  # noqa: E402
from tests.test_install_first_use_sql import _dev_keys_ddl, _meter_ddl  # noqa: E402
from tests.test_usage_tracker_self_serve_sql import _api_keys_ddl  # noqa: E402

DSN = os.environ.get("CLAIM_CAP_SQL_DSN")
SCHEMA = "claim_unused_cap_t"

REST_IP = "198.51.100.7"    # three agents that each used their key over REST
IDLE_IP = "198.51.100.8"    # three agents that never used theirs
REST_AGENTS = ["rest-agent-1", "rest-agent-2", "rest-agent-3"]
IDLE_AGENTS = ["idle-agent-1", "idle-agent-2", "idle-agent-3"]
# From the partner egress, in this order: three IP-metered names, then three
# workspaces — so every workspace key is NEWER than every IP-metered key.
EGRESS_OTHERS = ["egress-other-1", "egress-other-2", "egress-other-3"]
WORKSPACES = ["anythingmcp/ws1", "anythingmcp/ws2", "anythingmcp/ws3"]


def _scoped_dsn():
    return DSN + ("&" if "?" in DSN else "?") + "options=" + quote("-c search_path=" + SCHEMA)


_REAL_CONNECT = psycopg2.connect   # captured before any test patches it


class _Conn:
    """One pooled checkout: commit on a clean exit, roll back on an error."""
    def __enter__(self):
        self.c = _REAL_CONNECT(_scoped_dsn())
        return self.c

    def __exit__(self, exc_type, *a):
        try:
            (self.c.rollback if exc_type else self.c.commit)()
        finally:
            self.c.close()
        return False


class _PgPool:
    def connection(self):
        return _Conn()


def _rows(sql, args=()):
    with _Conn() as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _claim(client_name, ip):
    j, status, _ = _run(body={"client_name": client_name}, cur=None, ip=ip,
                        pool=_PgPool())
    assert status == 200, j
    return j


def _key_count():
    return _rows("SELECT COUNT(*) FROM mcp_dev_keys")[0][0]


@pytest.fixture(scope="module")
def world():
    """Mint every key through the real handler, then use the REST agents' keys
    through the real tracker."""
    if not DSN:
        pytest.skip("CLAIM_CAP_SQL_DSN not set")
    admin = _REAL_CONNECT(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(_dev_keys_ddl())
        # The migration's tier CHECK predates the 'identified' tier every claim
        # is minted with; production accepts it, so the constraint goes.
        cur.execute("ALTER TABLE mcp_dev_keys DROP CONSTRAINT IF EXISTS mcp_dev_keys_tier_check")
        cur.execute(tracker._SCHEMA)
        cur.execute(_meter_ddl())
        cur.execute(_api_keys_ddl())

    saved = (tracker._ensure_schema, tracker._ensure_flusher_running,
             os.environ.get("DATABASE_URL"))
    tracker._ensure_schema = lambda: None
    tracker._ensure_flusher_running = lambda: None
    os.environ["DATABASE_URL"] = _scoped_dsn()
    try:
        keys = {}
        for name, ip in ([(n, REST_IP) for n in REST_AGENTS]
                         + [(n, IDLE_IP) for n in IDLE_AGENTS]
                         + [(n, PARTNER_IP) for n in EGRESS_OTHERS + WORKSPACES]):
            j = _claim(name, ip)
            assert not j.get("reused"), (name, j)       # every one a fresh mint
            keys[name] = j["api_key"]

        app = flask.Flask(__name__)
        app.add_url_rule("/api/v1/facilities", "facilities", lambda: "ok")
        tracker.install_tracker(app)
        with tracker._BUFFER_LOCK:
            tracker._BUFFER.clear()
        client = app.test_client()
        for name in REST_AGENTS:
            r = client.get("/api/v1/facilities", headers={"X-API-Key": keys[name]})
            assert r.status_code == 200
        flushed = tracker._flush()
        yield {"keys": keys, "flushed": flushed}
    finally:
        tracker._ensure_schema, tracker._ensure_flusher_running, db_url = saved
        if db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = db_url
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        admin.close()


def test_the_rest_use_is_the_trackers_own_rows(world):
    """Positive control for the writer: exactly the REST agents' keys are in
    api_endpoint_log, under the prefix the tracker stores."""
    assert world["flushed"].get("flushed") == len(REST_AGENTS), world["flushed"]
    n = tracker.STORED_PREFIX_LEN
    held = {r[0] for r in _rows("SELECT DISTINCT api_key_prefix FROM api_endpoint_log")}
    assert held == {world["keys"][a][:n] for a in REST_AGENTS}


def test_keys_used_over_rest_do_not_count_as_unused(world):
    """Three agents used their keys over REST only, so none has last_used_at.
    A fourth agent on the same address gets a key of its own."""
    assert _rows("SELECT COUNT(*) FROM mcp_dev_keys WHERE metadata->>'ip' = %s "
                 "AND last_used_at IS NOT NULL", (REST_IP,)) == [(0,)]
    before = _key_count()
    j = _claim("rest-agent-4", REST_IP)
    assert j["api_key"] not in {world["keys"][a] for a in REST_AGENTS}, (
        "an agent was handed another agent's REST-used key — REST use still "
        "reads as unused")
    assert j.get("gate") != "unused_key_cap"
    assert _key_count() == before + 1


def test_keys_nobody_used_still_hit_the_cap(world):
    """The control: the same harness, the same handler, and the cap fires —
    so the test above passed on the REST rows, not on a cap that failed open.
    Other addresses' REST rows are in the table and must not lift this one."""
    before = _key_count()
    j = _claim("idle-agent-4", IDLE_IP)
    assert j.get("gate") == "unused_key_cap", j
    assert j["api_key"] == world["keys"]["idle-agent-3"], "not the NEWEST unused key"
    assert _key_count() == before, "over the cap must reuse, not mint"


def test_a_fourth_partner_workspace_gets_its_own_key(world):
    before = _key_count()
    j = _claim("anythingmcp/ws4", PARTNER_IP)
    handed = set(world["keys"].values())
    assert j["api_key"] not in handed, (
        "a verified partner workspace was handed another caller's key")
    assert j.get("gate") != "unused_key_cap"
    assert _key_count() == before + 1
    assert _rows("SELECT metadata->>'client_name', metadata->>'meter_scope' "
                 "FROM mcp_dev_keys WHERE api_key = %s", (j["api_key"],)) == [
        ("anythingmcp/ws4", "partner:anythingmcp/")]


def test_a_workspace_reclaim_returns_its_own_key(world):
    """What the cap would have been scoped to is already answered by the
    (client_name, ip) dedupe."""
    j = _claim("anythingmcp/ws1", PARTNER_IP)
    assert j.get("reused") is True
    assert j["api_key"] == world["keys"]["anythingmcp/ws1"]
    assert j.get("gate") != "unused_key_cap"


def test_a_workspace_key_is_never_handed_to_another_name_from_the_egress(world):
    """Another name from the partner egress is metered by IP and still capped —
    but on the IP-metered keys only. The workspace keys are newer, so a cap that
    counted them would hand this caller anythingmcp/ws3's key."""
    before = _key_count()
    j = _claim("egress-other-4", PARTNER_IP)
    assert j.get("gate") == "unused_key_cap", j
    assert j["api_key"] == world["keys"]["egress-other-3"], (
        "the cap handed out a key that is not the newest IP-metered unused key")
    assert j["api_key"] not in {world["keys"][w] for w in WORKSPACES}
    assert _key_count() == before
