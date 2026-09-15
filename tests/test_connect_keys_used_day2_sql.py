#!/usr/bin/env python3
"""The /connect return read on GET /api/v1/connect/stats, against a REAL
Postgres: keys_used_day2, keys_used_by_day and keys_refused_now.

The read joins the key a /connect page minted (connect_landing_views
.key_minted_for) to mcp_call_log.api_key and asks, per key, for an MCP call on
or after the UTC calendar day that begins day 2, 4 and 7. It also says how
/api/v1/keys/validate answers each key now, from auto_trial_keys and
mcp_dev_keys. The day boundaries under a non-UTC session, the event-type
filter, one key on two page views, the 30-day window, the index the join has to
ride and the statement timeout only mean something on a real Postgres.

Page views go through the real _record_view, keys through the real
POST /api/v1/connect/mint-update, and the numbers come back through the real
GET. Only what those endpoints cannot produce is written by hand: a mint time
on a chosen UTC day, the call rows the MCP track endpoint writes, and the key
rows the trial minter and the bind mirror write. Every table is created from
the DDL the repo ships. The endpoint's connections run in Pacific/Kiritimati,
14 hours ahead of UTC.

keys_used_day2 (_seed):
  chatgpt   k1   minted 23:59 UTC, called 00:00:30 the next UTC day   counts
                 (the same day at +14)
            k3   later-day rows: a page event and a bulk REST row     out
            k4   later-day call whose status maps to no event_type    counts
            k5   later-day call that hit the paywall                  counts
            k6   never called                                         out
            and one view that minted nothing
  gemini    k2   minted 09:00 UTC, called 11:00 the same UTC day      out
                 (the next day at +14)
  opencode  k11  called at exactly 00:00:00 UTC the next day          counts
  cursor    k7   on two views, called between their mint days         counts,
                 from the EARLIER mint
            k8   on two views, called after both                      counts ONCE
            k9   minted and called, but viewed 40 days ago            out
  cline     k10  minted and called on the current UTC day             out
  claude-desktop  one view that minted nothing

keys_used_by_day (_seed_by_day). Day N of a key minted D UTC days ago has
begun when D >= N - 1:
  chatgpt  D=8  b1  called on day 2                              used 2
                b2  called at 00:00:00 UTC, as day 4 begins      used 2, 4
                b3  called at 23:59:59 UTC on day 3              used 2
                b4  called at 00:00:00 UTC, as day 7 begins      used 2, 4, 7
                b5  a page event on day 7                        used none
                b6  minted 23:59, called 00:00:30 the next day   used 2
                    (the same day at +14)
  gemini   D=5  g1  called today, its day 6      begun 2, 4      used 2, 4
           D=6  g2  never called                 begun 2, 4, 7   used none
           D=3  g3  called on day 3              begun 2, 4      used 2
           D=0  g4  a call row stamped tomorrow  begun none      used none

keys_refused_now (_refusal_cases): one key per client, the client named for the
answer the validator gives it. The test then asks the real validate_trial_key
about the same rows.

The database tests skip without CONNECT_STATS_SQL_DSN. The db-parity job in
pre-merge.yml sets it and then FAILS if this file skipped. Owns and recreates
only connect_landing_views, mcp_call_log, auto_trial_keys and mcp_dev_keys.
"""
import ast
import functools
import io
import os
import pathlib
import re
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.auto_trial as auto_trial  # noqa: E402
import routes.mcp_connect as mc  # noqa: E402

DSN = os.environ.get("CONNECT_STATS_SQL_DSN")
FAR_ZONE = "Pacific/Kiritimati"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) connect-day2-sql-test"

# (UTC days before today, UTC time of day)
K1_MINT, K1_CALL = (5, "23:59:00"), (4, "00:00:30")
K2_MINT, K2_CALL = (5, "09:00:00"), (5, "11:00:00")
K11_MINT, K11_CALL = (6, "10:00:00"), (5, "00:00:00")
B6_MINT, B6_CALL = (8, "23:59:00"), (7, "00:00:30")
LABELS = ("k1", "k2", "k3", "k4", "k5", "k6", "k7", "k8", "k9", "k10", "k11",
          "b1", "b2", "b3", "b4", "b5", "b6", "g1", "g2", "g3", "g4")

# What validate_trial_key returns, named the way keys_refused_now names it.
VALIDATOR_REASON = {"unknown_trial_key": "unknown_key", "not_trial_prefix": "unknown_key",
                    "expired": "expired", "bind_email_required": "bind_email_required"}


def _key(label):
    """Built, and not key-shaped: scripts/check_no_leaked_credentials.py."""
    return "connect-day2-" + label


def _trial_key(label):
    """Shaped like the keys a /connect page mints, and built at runtime."""
    return "dch_trial_" + "connect-" + label


def _day(eligible, used):
    return {"eligible": eligible, "used": used}


# ── the shipped DDL ─────────────────────────────────────────────────────────

def _schema_repair_group(title):
    """The statements routes/schema_repair.py ships under one group title."""
    tree = ast.parse((ROOT / "routes" / "schema_repair.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Tuple) and len(node.elts) == 2
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == title):
            return [ast.literal_eval(s) for s in node.elts[1].elts]
    raise AssertionError("routes/schema_repair.py has no %r group" % title)


def _shipped_sql(rel, table):
    """The statements in a shipped .sql file that create, alter or index `table`."""
    text = "\n".join(ln for ln in (ROOT / rel).read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("--"))
    wanted = re.compile(r"(CREATE TABLE IF NOT EXISTS|ALTER TABLE"
                        r"|CREATE INDEX IF NOT EXISTS \w+\s+ON)\s+%s\b" % table, re.I)
    return [s.strip() for s in text.split(";") if wanted.match(s.strip())]


@functools.lru_cache(maxsize=1)
def _pool_wrapper_def():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body
             if isinstance(n, ast.ClassDef) and n.name == "_PoolConnWrapper"]
    assert len(found) == 1, len(found)
    return found[0]


def _pool_wrapper(returned):
    """main._PoolConnWrapper, the object main.get_db() lends, compiled out of
    main.py (main cannot be imported here). return_pg_connection is the one
    global it calls: this stand-in records the state the pool would get the
    connection back in, then closes it."""
    def return_pg_connection(wrapper):
        raw = wrapper.raw
        returned.append({"autocommit": raw.autocommit,
                         "in_transaction": raw.info.transaction_status != 0})
        raw.close()

    ns = {"return_pg_connection": return_pg_connection}
    exec(compile(ast.Module(body=[_pool_wrapper_def()], type_ignores=[]),
                 "main.py", "exec"), ns)
    return ns["_PoolConnWrapper"]


@pytest.fixture
def db(monkeypatch):
    if not DSN:
        pytest.skip("CONNECT_STATS_SQL_DSN not set")
    import flask
    import psycopg2
    import psycopg2.extras

    views_ddl = _schema_repair_group("connect_landing_views table")
    calls_ddl = (_shipped_sql("migration_001_api_keys.sql", "mcp_call_log")
                 + _shipped_sql("migrations/2026-05-25_funnel_instrumentation.sql",
                                "mcp_call_log"))
    dev_keys_ddl = _shipped_sql("dchub-mcp-v2.1/migration_001_api_keys.sql", "mcp_dev_keys")
    admin = psycopg2.connect(DSN, options="-c TimeZone=UTC")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS connect_landing_views, mcp_call_log,"
                    " auto_trial_keys, mcp_dev_keys CASCADE")
        for stmt in views_ddl + calls_ddl + dev_keys_ddl:
            cur.execute(stmt)
    # auto_trial_keys comes from the function that ships it. Production runs it
    # once per process, so the latch is lifted here and put back afterwards.
    monkeypatch.setattr(auto_trial, "_SCHEMA_READY", False)
    auto_trial._ensure_schema(admin)
    with admin.cursor() as cur:
        cur.execute("SELECT to_regclass('auto_trial_keys') IS NOT NULL")
        assert cur.fetchone()[0], "auto_trial._ensure_schema created no auto_trial_keys"

    sent = []

    class Recording(psycopg2.extras.LoggingConnection):
        """A real psycopg2 connection that keeps each statement as it was sent."""
        def filter(self, msg, curs):
            sent.append(msg.decode() if isinstance(msg, bytes) else msg)
            return None

    def connect():
        c = psycopg2.connect(DSN, options="-c TimeZone=" + FAR_ZONE,
                             connection_factory=Recording)
        c.initialize(io.StringIO())
        return c

    def trial_conn():
        """What auto_trial._conn lends validate_trial_key: autocommit, on this database."""
        c = psycopg2.connect(DSN)
        c.autocommit = True
        return c

    monkeypatch.setattr(mc, "_get_db", connect)
    monkeypatch.setattr(auto_trial, "_conn", trial_conn)
    app = flask.Flask("connect-keys-used-day2-sql")
    app.register_blueprint(mc.mcp_connect_bp)
    try:
        yield {"admin": admin, "app": app, "client": app.test_client(),
               "connect": connect, "sent": sent,
               "ddl": {"views": views_ddl, "calls": calls_ddl, "dev_keys": dev_keys_ddl}}
    finally:
        admin.close()


# ── writes ──────────────────────────────────────────────────────────────────

def _at(db, when):
    """(UTC days before today, "HH:MM:SS") as a UTC timestamptz literal."""
    days, clock = when
    with db["admin"].cursor() as cur:
        cur.execute("SELECT ((date_trunc('day', NOW() AT TIME ZONE 'UTC')"
                    " - make_interval(days => %s) + %s::interval)"
                    " AT TIME ZONE 'UTC')::text", (days, clock))
        return cur.fetchone()[0]


def _view(db, page):
    with db["app"].test_request_context("/connect/" + page, headers={"User-Agent": UA}):
        view_id = mc._record_view(page)
    assert view_id, page
    return view_id


def _mint(db, page, key, when):
    """A view, its key through the real mint-update, then dated by hand: the
    endpoint can only stamp NOW()."""
    view_id = _view(db, page)
    r = db["client"].post("/api/v1/connect/mint-update",
                          json={"view_id": view_id, "api_key": key})
    assert (r.status_code, r.get_json()) == (200, {"ok": True}), r.get_json()
    at = _at(db, when)
    with db["admin"].cursor() as cur:
        cur.execute("UPDATE connect_landing_views SET key_minted_at = %s, viewed_at = %s"
                    " WHERE id = %s AND key_minted_for = %s", (at, at, view_id, key))
        assert cur.rowcount == 1, (page, key)


def _call(db, key, when, event_type="tool_call", status="ok"):
    """One call row, as the MCP track endpoint stores it."""
    with db["admin"].cursor() as cur:
        cur.execute("INSERT INTO mcp_call_log"
                    " (timestamp, tool, platform, api_key, status, event_type)"
                    " VALUES (%s, 'get_market_intel', 'mcp', %s, %s, %s)",
                    (_at(db, when), key, status, event_type))


def _trial_row(db, key, call_count=0, expired=False, signed_up_email=None,
               operator_email=None):
    """The auto_trial_keys row mint_trial_for_request writes, in the state a case needs."""
    with db["admin"].cursor() as cur:
        cur.execute("INSERT INTO auto_trial_keys"
                    " (api_key, expires_at, call_count, signed_up_email, operator_email)"
                    " VALUES (%s, NOW() + %s::interval, %s, %s, %s)",
                    (key, "-1 hour" if expired else "5 days", call_count,
                     signed_up_email, operator_email))


def _dev_row(db, key, status):
    """The mcp_dev_keys row a bind mirrors a trial key into
    (auto_trial._mirror_trial_to_mcp_dev_keys), with the status a case needs."""
    with db["admin"].cursor() as cur:
        cur.execute("INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, status)"
                    " VALUES (%s, %s, 'ops@example.com', 'free', %s)", (key, key, status))


def _seed(db):
    """The keys_used_day2 table in the module docstring."""
    _mint(db, "chatgpt", _key("k1"), K1_MINT)
    _call(db, _key("k1"), K1_CALL)
    _mint(db, "chatgpt", _key("k3"), (5, "12:00:00"))
    _call(db, _key("k3"), (3, "12:00:00"), event_type="key_first_use")
    _call(db, _key("k3"), (3, "12:00:00"), event_type="bulk:free")
    _mint(db, "chatgpt", _key("k4"), (5, "12:00:00"))
    _call(db, _key("k4"), (2, "12:00:00"), event_type=None, status="trial_cap_exceeded")
    _mint(db, "chatgpt", _key("k5"), (5, "12:00:00"))
    _call(db, _key("k5"), (4, "12:00:00"), event_type="paywall_block",
          status="blocked_paid_only")
    _mint(db, "chatgpt", _key("k6"), (5, "12:00:00"))
    _view(db, "chatgpt")
    _mint(db, "gemini", _key("k2"), K2_MINT)
    _call(db, _key("k2"), K2_CALL)
    _mint(db, "opencode", _key("k11"), K11_MINT)
    _call(db, _key("k11"), K11_CALL)
    _mint(db, "cursor", _key("k7"), (20, "12:00:00"))
    _mint(db, "cursor", _key("k7"), (18, "12:00:00"))
    _call(db, _key("k7"), (19, "12:00:00"), event_type="tool_error", status="error")
    _mint(db, "cursor", _key("k8"), (20, "12:00:00"))
    _mint(db, "cursor", _key("k8"), (19, "12:00:00"))
    _call(db, _key("k8"), (17, "12:00:00"))
    _mint(db, "cursor", _key("k9"), (40, "12:00:00"))
    _call(db, _key("k9"), (39, "12:00:00"))
    _mint(db, "cline", _key("k10"), (0, "00:00:01"))
    _call(db, _key("k10"), (0, "00:00:02"))
    _view(db, "claude-desktop")


def _seed_by_day(db):
    """The keys_used_by_day table in the module docstring."""
    for label, call in (("b1", (7, "12:00:00")), ("b2", (5, "00:00:00")),
                        ("b3", (6, "23:59:59")), ("b4", (2, "00:00:00"))):
        _mint(db, "chatgpt", _key(label), (8, "12:00:00"))
        _call(db, _key(label), call)
    _mint(db, "chatgpt", _key("b5"), (8, "12:00:00"))
    _call(db, _key("b5"), (2, "12:00:00"), event_type="key_first_use")
    _mint(db, "chatgpt", _key("b6"), B6_MINT)
    _call(db, _key("b6"), B6_CALL)
    _mint(db, "gemini", _key("g1"), (5, "12:00:00"))
    _call(db, _key("g1"), (0, "00:00:05"))
    _mint(db, "gemini", _key("g2"), (6, "00:00:00"))
    _mint(db, "gemini", _key("g3"), (3, "12:00:00"))
    _call(db, _key("g3"), (1, "12:00:00"))
    _mint(db, "gemini", _key("g4"), (0, "00:00:01"))
    _call(db, _key("g4"), (-1, "12:00:00"))


def _refusal_cases(free):
    """(client, key, auto_trial_keys row or None, mcp_dev_keys status or None,
    the answer). `free` is the validator's allowance of free unbound calls."""
    t = _trial_key
    return (
        ("serves-fresh", t("fresh"), {"call_count": 0}, None, None),
        ("serves-last-free-call", t("last-free-call"), {"call_count": free - 1}, None, None),
        ("serves-bound", t("bound"),
         {"call_count": free + 40, "operator_email": "ops@example.com"}, None, None),
        ("serves-dev-key", t("dev-key"), None, "active", None),
        ("serves-expired-bound-mirror", t("expired-bound-mirror"),
         {"call_count": free, "expired": True, "signed_up_email": "ops@example.com"},
         "active", None),
        ("refused-free-calls-used", t("free-calls-used"), {"call_count": free}, None,
         "bind_email_required"),
        ("refused-blank-emails", t("blank-emails"),
         {"call_count": free, "signed_up_email": "", "operator_email": ""}, None,
         "bind_email_required"),
        ("refused-revoked-dev-key", t("revoked-dev-key"), {"call_count": free}, "revoked",
         "bind_email_required"),
        ("refused-expired", t("expired"), {"call_count": 0, "expired": True}, None,
         "expired"),
        ("refused-expired-and-used", t("expired-and-used"),
         {"call_count": free, "expired": True}, None, "expired"),
        ("refused-unknown", t("unknown"), None, None, "unknown_key"),
        ("refused-not-trial-shaped", "connect-refusal-not-trial-shaped",
         {"call_count": 0}, None, "unknown_key"),
    )


def _seed_refusals(db, cases):
    for client, key, trial, dev_status, _ in cases:
        _mint(db, client, key, (3, "12:00:00"))
        if trial is not None:
            _trial_row(db, key, **trial)
        if dev_status is not None:
            _dev_row(db, key, dev_status)


# ── reads ───────────────────────────────────────────────────────────────────

def _stats(db):
    r = db["client"].get("/api/v1/connect/stats")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r


def _by_client(body):
    return {r["client"]: (r["views_30d"], r["keys_minted"], r["keys_used_day2"])
            for r in body["by_client"]}


def _returns(body):
    return {r["client"]: (r["keys_used_by_day"], r["keys_refused_now"])
            for r in body["by_client"]}


def _answer(refused):
    """The one reason a one-key client's keys_refused_now names, or None."""
    named = [r for r in mc.KEYS_REFUSED_REASONS if refused[r]]
    assert sorted(refused) == sorted(mc.KEYS_REFUSED_REASONS + ("total",)), refused
    assert refused["total"] == sum(refused[r] for r in mc.KEYS_REFUSED_REASONS), refused
    assert len(named) <= 1 and all(refused[r] <= 1 for r in mc.KEYS_REFUSED_REASONS), refused
    return named[0] if named else None


def _validator_answer(key):
    """The real validate_trial_key on this database, named as keys_refused_now names it."""
    ok, reason = auto_trial.validate_trial_key(key)
    return None if ok else VALIDATOR_REASON[reason]


def _pooled(db, monkeypatch, autocommit, returned):
    wrapper = _pool_wrapper(returned)

    def lend():
        raw = db["connect"]()
        raw.autocommit = autocommit
        return wrapper(raw)

    monkeypatch.setattr(mc, "_get_db", lend)


def _hold_call_log(ready, release):
    """ACCESS EXCLUSIVE on mcp_call_log from another session, until released."""
    import psycopg2
    c = psycopg2.connect(DSN)
    try:
        with c.cursor() as cur:
            cur.execute("LOCK TABLE mcp_call_log IN ACCESS EXCLUSIVE MODE")
            ready.set()
            release.wait(30)
        c.rollback()
    finally:
        c.close()


# ── the fixture is what it claims ───────────────────────────────────────────

def test_every_table_comes_from_the_ddl_the_repo_ships(db):
    views, calls, dev_keys = db["ddl"]["views"], db["ddl"]["calls"], db["ddl"]["dev_keys"]
    with db["admin"].cursor() as cur:
        cur.execute("SELECT i.relname FROM pg_index x"
                    " JOIN pg_class i ON i.oid = x.indexrelid"
                    " JOIN pg_class t ON t.oid = x.indrelid"
                    " WHERE x.indisprimary"
                    " AND t.relname IN ('auto_trial_keys', 'mcp_dev_keys')"
                    " ORDER BY 1")
        primary_keys = [r[0] for r in cur.fetchall()]
    seen = {
        "views_table": sum(s.startswith("CREATE TABLE IF NOT EXISTS connect_landing_views")
                           for s in views),
        "calls_table": sum(s.startswith("CREATE TABLE IF NOT EXISTS mcp_call_log")
                           for s in calls),
        "api_key_index": sum("idx_mcp_log_apikey" in s for s in calls),
        "event_type_column": sum(bool(re.search(r"ADD COLUMN IF NOT EXISTS event_type\b", s))
                                 for s in calls),
        "dev_keys_table": sum(s.startswith("CREATE TABLE IF NOT EXISTS mcp_dev_keys")
                              for s in dev_keys),
        "key_tables_primary_keys": primary_keys,
    }
    assert seen == {"views_table": 1, "calls_table": 1, "api_key_index": 1,
                    "event_type_column": 1, "dev_keys_table": 1,
                    "key_tables_primary_keys": ["auto_trial_keys_pkey",
                                                "mcp_dev_keys_pkey"]}, seen


def test_the_far_zone_splits_the_boundary_keys_the_way_the_tables_say(db):
    """k1, k2 and b6 only test the UTC boundary if the endpoint's session zone
    disagrees with UTC about their dates: k1 and b6 cross midnight in UTC and not
    at +14, k2 the other way round. k11's call sits exactly on a UTC midnight."""
    pairs = {"k1": (K1_MINT, K1_CALL), "k2": (K2_MINT, K2_CALL),
             "k11": (K11_MINT, K11_CALL), "b6": (B6_MINT, B6_CALL)}
    c = db["connect"]()
    try:
        with c.cursor() as cur:
            cur.execute("SHOW TimeZone")
            seen = {"zone": cur.fetchone()[0]}
            for label, (mint, call) in pairs.items():
                cur.execute(
                    "SELECT (%(m)s::timestamptz AT TIME ZONE 'UTC')::date"
                    "       < (%(c)s::timestamptz AT TIME ZONE 'UTC')::date,"
                    "       %(m)s::timestamptz::date < %(c)s::timestamptz::date,"
                    "       (%(c)s::timestamptz AT TIME ZONE 'UTC')::time = '00:00:00'",
                    {"m": _at(db, mint), "c": _at(db, call)})
                seen[label] = cur.fetchone()
    finally:
        c.close()
    assert seen == {
        "zone": FAR_ZONE,
        "k1": (True, False, False),
        "k2": (False, True, False),
        "k11": (True, False, True),
        "b6": (True, False, False),
    }, seen


# ── what the endpoint publishes ─────────────────────────────────────────────

def test_keys_used_day2_counts_distinct_keys_called_on_a_later_utc_day(db):
    _seed(db)
    r = _stats(db)
    body = r.get_json()
    text = r.get_data(as_text=True)
    seen = {
        "ok": body["ok"],
        "status": body["keys_used_day2_status"],
        "rows": _by_client(body),
        "day2_is_used_on_day_2": {row["client"]: row["keys_used_day2"]
                                  == row["keys_used_by_day"]["2"]["used"]
                                  for row in body["by_client"]},
        "basis": body["keys_used_day2_basis"] == mc.KEYS_USED_DAY2_BASIS,
        "keys_in_the_public_body": [k for k in LABELS if _key(k) in text],
    }
    assert seen == {
        "ok": True,
        "status": "measured",
        "rows": {
            "chatgpt": (6, 5, 3),
            "gemini": (1, 1, 0),
            "opencode": (1, 1, 1),
            "cursor": (4, 4, 2),
            "cline": (1, 1, 0),
            "claude-desktop": (1, 0, 0),
        },
        "day2_is_used_on_day_2": {client: True for client in (
            "chatgpt", "gemini", "opencode", "cursor", "cline", "claude-desktop")},
        "basis": True,
        "keys_in_the_public_body": [],
    }, seen


def test_keys_used_by_day_counts_keys_whose_day_began_and_those_called_on_or_after_it(db):
    _seed_by_day(db)
    r = _stats(db)
    body = r.get_json()
    text = r.get_data(as_text=True)
    seen = {
        "status": body["keys_used_day2_status"],
        "by_day": {client: by_day for client, (by_day, _) in _returns(body).items()},
        "day2": {client: day2 for client, (_, _, day2) in _by_client(body).items()},
        "basis": body["keys_used_by_day_basis"] == mc.KEYS_USED_BY_DAY_BASIS,
        "keys_in_the_public_body": [k for k in LABELS if _key(k) in text],
    }
    assert seen == {
        "status": "measured",
        "by_day": {
            "chatgpt": {"2": _day(6, 5), "4": _day(6, 2), "7": _day(6, 1)},
            "gemini": {"2": _day(3, 2), "4": _day(3, 1), "7": _day(1, 0)},
        },
        "day2": {"chatgpt": 5, "gemini": 2},
        "basis": True,
        "keys_in_the_public_body": [],
    }, seen


def test_keys_refused_now_is_the_validators_own_answer_for_each_key(db):
    """★ The classification restates the validator's rules in SQL, so it is held
    to the validator itself. After the read, each key without an active
    mcp_dev_keys row goes through the real routes.auto_trial.validate_trial_key
    on the same rows. A key with an active row never reaches that function:
    test_an_active_mcp_dev_keys_row_never_reaches_the_trial_validator."""
    cases = _refusal_cases(auto_trial.TRIAL_FREE_CALLS_UNBOUND)
    _seed_refusals(db, cases)
    r = _stats(db)
    body = r.get_json()
    text = r.get_data(as_text=True)
    published = {row["client"]: row["keys_refused_now"] for row in body["by_client"]}
    seen = {
        "status": body["keys_used_day2_status"],
        "endpoint": {client: _answer(published[client]) for client, *_ in cases},
        "validator": {client: None if dev_status == "active" else _validator_answer(key)
                      for client, key, _, dev_status, _ in cases},
        "basis": body["keys_refused_now_basis"] == mc.KEYS_REFUSED_NOW_BASIS,
        "keys_in_the_public_body": [client for client, key, *_ in cases if key in text],
    }
    expected = {client: answer for client, *_, answer in cases}
    assert seen == {"status": "measured", "endpoint": expected, "validator": expected,
                    "basis": True, "keys_in_the_public_body": []}, seen


def test_the_free_call_allowance_is_the_one_the_validator_reads(db, monkeypatch):
    """★ TRIAL_FREE_CALLS_UNBOUND is tuned without a deploy. At an allowance of
    3, a trial with 3 unbound calls is refused and one with 2 is served, by the
    validator and by keys_refused_now alike. A 10 copied into the read would
    call both served."""
    monkeypatch.setattr(auto_trial, "TRIAL_FREE_CALLS_UNBOUND", 3)
    at_allowance, under = _trial_key("tuned-at-allowance"), _trial_key("tuned-under")
    _mint(db, "cursor", at_allowance, (3, "12:00:00"))
    _trial_row(db, at_allowance, call_count=3)
    _mint(db, "cline", under, (3, "12:00:00"))
    _trial_row(db, under, call_count=2)
    body = _stats(db).get_json()
    published = {row["client"]: row["keys_refused_now"] for row in body["by_client"]}
    seen = {
        "endpoint": {client: _answer(published[client]) for client in ("cursor", "cline")},
        "validator": {"cursor": _validator_answer(at_allowance),
                      "cline": _validator_answer(under)},
    }
    assert seen == {"endpoint": {"cursor": "bind_email_required", "cline": None},
                    "validator": {"cursor": "bind_email_required", "cline": None}}, seen


def test_the_statement_the_endpoint_sends_rides_the_key_indexes(db):
    """★ Production planned the day-2 statement this read replaced as a Nested
    Loop Semi Join over a Bitmap Index Scan on idx_mcp_log_apikey (EXPLAIN,
    2026-09-14). A function around m.api_key, or around the key column of either
    key table, would leave a scan of a whole table behind a public route.
    EXPLAIN the text the endpoint SENT, with sequential scans priced out, and
    read which scan reaches each table."""
    _seed(db)
    _seed_refusals(db, _refusal_cases(auto_trial.TRIAL_FREE_CALLS_UNBOUND))
    assert _stats(db).get_json()["keys_used_day2_status"] == "measured"
    sent = [q for q in db["sent"] if "AS used_days" in q]
    assert len(sent) == 1, sent
    with db["admin"].cursor() as cur:
        cur.execute("ANALYZE connect_landing_views; ANALYZE mcp_call_log;"
                    " ANALYZE auto_trial_keys; ANALYZE mcp_dev_keys")
        cur.execute("SET enable_seqscan = off")
        try:
            cur.execute("EXPLAIN " + sent[0])
            plan = "\n".join(r[0] for r in cur.fetchall())
        finally:
            cur.execute("RESET enable_seqscan")
    seen = {
        "seq_scans": sorted(set(re.findall(
            r"Seq Scan on (mcp_call_log|auto_trial_keys|mcp_dev_keys)\b", plan))),
        "api_key_index_scan": bool(re.search(
            r"(Bitmap Index Scan on|Index Scan using|Index Only Scan using)"
            r" idx_mcp_log_apikey\b", plan)),
    }
    assert seen == {"seq_scans": [], "api_key_index_scan": True}, plan


DROP_ONE = {
    "mcp_call_log": "DROP TABLE mcp_call_log CASCADE",
    "auto_trial_keys": "DROP TABLE auto_trial_keys CASCADE",
    "mcp_dev_keys": "DROP TABLE mcp_dev_keys CASCADE",
}


@pytest.mark.parametrize("table", sorted(DROP_ONE))
def test_a_read_that_cannot_run_is_null_and_says_why(db, table):
    """Fail-soft: with any table the read joins gone, the stats still serve,
    keys_used_day2, keys_used_by_day and keys_refused_now are null for every
    client, never 0, and the status says the query failed."""
    _mint(db, "cursor", _key("gone"), (3, "12:00:00"))
    with db["admin"].cursor() as cur:
        cur.execute(DROP_ONE[table])
    body = _stats(db).get_json()
    seen = {"ok": body["ok"], "status": body["keys_used_day2_status"],
            "rows": _by_client(body), "returns": _returns(body)}
    assert seen == {"ok": True, "status": "query_failed",
                    "rows": {"cursor": (1, 1, None)},
                    "returns": {"cursor": (None, None)}}, seen


@pytest.mark.parametrize("autocommit", [False, True], ids=["pool-default", "autocommit-on"])
def test_a_held_call_log_times_the_read_out_and_the_stats_still_serve(
        db, monkeypatch, autocommit):
    """★ Another session holds mcp_call_log. The read must give up inside its
    statement_timeout, come back null with status 'timeout', and leave the
    page's other numbers standing. Without an effective timeout it waits the
    lock out (30s here) and reads 'measured'; on an autocommit connection a SET
    LOCAL outside a transaction is exactly that. Run through the pool's real
    connection wrapper, which must get the connection back as it lent it."""
    _mint(db, "chatgpt", _key("held"), (3, "12:00:00"))
    returned = []
    _pooled(db, monkeypatch, autocommit, returned)
    ready, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_call_log, args=(ready, release))
    holder.start()
    try:
        assert ready.wait(10), "the lock holder never took its lock"
        t0 = time.monotonic()
        body = _stats(db).get_json()
        elapsed = time.monotonic() - t0
    finally:
        release.set()
        holder.join()
    seen = {
        "status": body["keys_used_day2_status"],
        "rows": _by_client(body),
        "returns": _returns(body),
        "inside_the_budget": elapsed < mc.KEYS_RETURN_TIMEOUT_MS / 1000.0 + 2,
        "returned": returned,
    }
    assert seen == {
        "status": "timeout",
        "rows": {"chatgpt": (1, 1, None)},
        "returns": {"chatgpt": (None, None)},
        "inside_the_budget": True,
        "returned": [{"autocommit": autocommit, "in_transaction": False}],
    }, (seen, elapsed)


@pytest.mark.parametrize("autocommit", [False, True], ids=["pool-default", "autocommit-on"])
def test_the_pool_gets_the_connection_back_as_it_lent_it(db, monkeypatch, autocommit):
    """SET LOCAL needs a transaction, so the read turns autocommit off for itself.
    The connection then goes back to a shared pool: autocommit as it was lent,
    and no transaction left open."""
    _mint(db, "cursor", _key("lent"), (3, "12:00:00"))
    _call(db, _key("lent"), (2, "12:00:00"))
    returned = []
    _pooled(db, monkeypatch, autocommit, returned)
    body = _stats(db).get_json()
    seen = {"status": body["keys_used_day2_status"], "rows": _by_client(body),
            "returned": returned}
    assert seen == {
        "status": "measured",
        "rows": {"cursor": (1, 1, 1)},
        "returned": [{"autocommit": autocommit, "in_transaction": False}],
    }, seen


# ── what counts as an MCP call is read out of the writers ───────────────────

def test_the_counted_event_types_are_the_ones_the_track_endpoint_writes():
    tree = ast.parse((ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8"))
    track = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == "track_tool_call"]
    assert len(track) == 1, len(track)
    maps = [n for n in ast.walk(track[0]) if isinstance(n, ast.Dict)
            and "blocked_paid_only" in [getattr(k, "value", None) for k in n.keys]]
    assert len(maps) == 1, len(maps)
    assert sorted(v.value for v in maps[0].values) == sorted(mc.MCP_CALL_EVENT_TYPES)


def test_the_rows_seeded_as_not_calls_are_what_the_page_and_bulk_writers_store():
    """k3's later-day rows carry the event_type values the two non-MCP writers
    of mcp_call_log store, read out of those writers."""
    page = ast.parse((ROOT / "routes" / "onboarding_page.py").read_text(encoding="utf-8"))
    page_inserts = [n.value for n in ast.walk(page)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and "INSERT INTO mcp_call_log" in n.value]
    bulk = ast.parse((ROOT / "routes" / "market_brief.py").read_text(encoding="utf-8"))
    logger_fn = [n for n in ast.walk(bulk)
                 if isinstance(n, ast.FunctionDef) and n.name == "_bulk_log_call"]
    assert len(logger_fn) == 1, len(logger_fn)
    bulk_prefixes = [n.values[0].value for n in ast.walk(logger_fn[0])
                     if isinstance(n, ast.JoinedStr) and n.values
                     and isinstance(n.values[0], ast.Constant)]
    seen = {
        "page_event_written": any("'key_first_use'" in s for s in page_inserts),
        "bulk_prefix_written": "bulk:" in bulk_prefixes,
        "neither_counts": not ({"key_first_use", "bulk:free"} & set(mc.MCP_CALL_EVENT_TYPES)),
    }
    assert seen == {"page_event_written": True, "bulk_prefix_written": True,
                    "neither_counts": True}, seen


# ── what keys_refused_now assumes about the validator ───────────────────────

def test_an_active_mcp_dev_keys_row_never_reaches_the_trial_validator():
    """keys_refused_now serves every key with an active mcp_dev_keys row. That is
    validate_key's order: it calls validate_trial_key only when that row is
    missing or not active, and it refuses an active row only through its
    unbound-key gate, which needs a dch_live_ key. A /connect page mints
    dch_trial_ keys."""
    tree = ast.parse((ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8"))
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "validate_key"]
    assert len(fn) == 1, len(fn)
    trial_calls = [n for n in ast.walk(fn[0]) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == "validate_trial_key"]
    not_active = [n for n in ast.walk(fn[0]) if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "not row or row[3] != 'active'"]
    inside = [c for guard in not_active for stmt in guard.body
              for c in ast.walk(stmt) if c in trial_calls]
    unbound_gate = [ast.unparse(n.value) for n in ast.walk(fn[0])
                    if isinstance(n, ast.Assign)
                    and [getattr(t, "id", None) for t in n.targets] == ["_unbound_live"]]
    seen = {
        "trial_validator_calls": len(trial_calls),
        "inside_the_not_active_branch": len(inside),
        "unbound_gate_needs_dch_live": ["api_key.startswith('dch_live_')" in g
                                        for g in unbound_gate],
    }
    assert seen == {"trial_validator_calls": 1, "inside_the_not_active_branch": 1,
                    "unbound_gate_needs_dch_live": [True]}, seen
