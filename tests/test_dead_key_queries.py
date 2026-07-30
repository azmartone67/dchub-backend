"""Dead-query regression fence — 2026-07-30.

Two api-key lookups queried columns their tables have NEVER had, threw
on every call, and were swallowed by bare `except: pass` — the same
class as the flask_mcp_endpoints.validate_key leg fixed by PR #1943:

  - free_tier_limiter._get_current_user_id: `api_keys.key_value` (live
    schema: key_hash + is_active INTEGER), PLUS sqlite-style
    `c.execute(...).fetchone()` chaining (PGCursorWrapper.execute
    returns None), PLUS dict-indexing a tuple row. Net effect: callers
    presenting only an X-API-Key got 401 "authentication_required"
    from @limit_land_power_search / @limit_api_requests.
  - routes.gating_routes._tier_from_api_key fallback:
    `mcp_dev_keys.key_value OR id::text` — mcp_dev_keys has neither
    column (live schema: api_key/tier/status, no id at all), so the
    fallback always reported 'anonymous'.

Extended 2026-07-30 (second PR) for the rest of free_tier_limiter.py —
the four remaining execute-chains plus a `result.rowcount` read off
execute()'s None return:

  - _get_usage / _increment_usage: chained + dict-indexed, and the
    fallback INSERT declared ON CONFLICT (user_id), which is not a
    unique key on free_tier_usage (real key: user_id, usage_type,
    usage_month) — Postgres rejects the statement. Live DB has NO
    free_tier_usage table at all (db_utils skips DDL), so reads must
    fail-open to 0.
  - _get_user_tier: also selected users.stripe_subscription_id +
    stripe_subscription_status — neither exists live (real names:
    stripe_customer_id, subscription_status). Paying pro/enterprise
    users were always told 'free'.
  - the two /api/admin/usage closures: selected users.is_admin (no such
    column; live `role` has no 'admin' value either) — now gate on
    DCHUB_ADMIN_KEY instead of the users table.

Three layers here:
  1. Functional stub tests that drive the REAL functions against a
     cursor that behaves like db_utils.PGCursorWrapper (execute
     returns None, rows are tuples) and assert the emitted SQL.
  2. A repo-wide AST sweep: no SQL string may reference the dead
     columns on these tables again (allowlist below documents the
     two known stragglers pending their own PRs).
  3. An AST guard that the two fixed functions never regress to
     `execute(...).fetchone()` chaining.

No test here imports main.py. Runs under the pre-merge install line:
pytest requests flask pyyaml psycopg2-binary psycopg[binary] Unidecode.
"""
import ast
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── stub DB plumbing (mimics db_utils.PGConnectionWrapper contract) ──

class _StubCursor:
    """Like db_utils.PGCursorWrapper: execute() returns None (NOT the
    cursor — chaining .fetchone() off it must AttributeError), rows
    are plain tuples (NOT dicts)."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = []  # (sql, params) pairs, for assertions

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return None

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        pass


class _StubConn:
    def __init__(self, rows):
        self.cur = _StubCursor(rows)

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ── 1a. free_tier_limiter API-key lookup, happy path ─────────────────

def test_free_tier_limiter_api_key_lookup_resolves_user():
    from flask import Flask
    import free_tier_limiter as ftl

    conn = _StubConn(rows=[('user-42',)])
    orig = ftl._get_db
    ftl._get_db = lambda: conn
    try:
        app = Flask('dead_key_queries_test')
        with app.test_request_context(
                '/api/v1/anything', headers={'X-API-Key': 'dch_live_stubkey'}):
            uid = ftl._get_current_user_id()
    finally:
        ftl._get_db = orig

    assert uid == 'user-42', (
        f"_get_current_user_id returned {uid!r} — the API-key leg is dead "
        "again (execute-chaining, dict-indexed tuple row, or thrown SQL)")

    assert len(conn.cur.executed) == 1
    sql, params = conn.cur.executed[0]
    assert 'key_hash IN (%s, %s)' in sql, f"expected dual key_hash lookup, got: {sql}"
    assert 'key_value' not in sql, "api_keys.key_value does not exist (live schema: key_hash)"
    assert 'is_active IS NULL OR is_active = 1' in sql, (
        "is_active is INTEGER and nullable — must tolerate NULL, compare = 1")
    # Dual-hash convention (free_tier_gate._user_from_api_key, PR #1943):
    # standard keys store sha256(key) in key_hash; partner/admin keys
    # store the RAW key string.
    assert params == (
        hashlib.sha256(b'dch_live_stubkey').hexdigest(), 'dch_live_stubkey')


# ── 1b. unknown key falls through to None (no throw, no 'None' str) ──

def test_free_tier_limiter_api_key_lookup_unknown_key_is_none():
    from flask import Flask
    import free_tier_limiter as ftl

    conn = _StubConn(rows=[])  # no matching row
    orig = ftl._get_db
    ftl._get_db = lambda: conn
    try:
        app = Flask('dead_key_queries_test')
        with app.test_request_context(
                '/api/v1/anything', headers={'X-API-Key': 'dch_live_nosuch'}):
            uid = ftl._get_current_user_id()
    finally:
        ftl._get_db = orig

    assert uid is None, f"unknown key must yield None, got {uid!r}"


# ── 1c. gating_routes mcp_dev_keys fallback ──────────────────────────

def test_gating_routes_dev_key_fallback_resolves_tier():
    """Force the fallback: poison mcp_upgrade_gate so the primary
    `from mcp_upgrade_gate import validate_key_tier` raises, and plant
    a fake `psycopg` module (tried before psycopg2) whose connect()
    hands back the stub. Restores sys.modules either way."""
    import types

    fake_psycopg = types.ModuleType('psycopg')
    conn = _StubConn(rows=[('pro',)])
    fake_psycopg.connect = lambda dsn: conn

    saved = {}
    for name in ('mcp_upgrade_gate', 'psycopg'):
        saved[name] = sys.modules.get(name)
    sys.modules['mcp_upgrade_gate'] = None  # import of it now raises
    sys.modules['psycopg'] = fake_psycopg
    os_environ_orig = os.environ.get('NEON_DATABASE_URL')
    os.environ['NEON_DATABASE_URL'] = 'postgresql://stub/stub'
    try:
        import routes.gating_routes as gr
        tier = gr._tier_from_api_key('dch_live_stubkey')
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        if os_environ_orig is None:
            os.environ.pop('NEON_DATABASE_URL', None)
        else:
            os.environ['NEON_DATABASE_URL'] = os_environ_orig

    assert tier == 'pro', (
        f"fallback returned {tier!r} — the mcp_dev_keys lookup is dead again")
    assert len(conn.cur.executed) == 1
    sql, params = conn.cur.executed[0]
    assert 'api_key = %s' in sql, f"mcp_dev_keys keys on api_key, got: {sql}"
    assert 'key_value' not in sql, "mcp_dev_keys.key_value does not exist"
    assert 'id::text' not in sql, "mcp_dev_keys has no id column at all"
    assert "status = 'active'" in sql, (
        "match the primary path (mcp_upgrade_gate.validate_key_tier): "
        "active keys only")
    assert params == ('dch_live_stubkey',)


# ── 1d. usage read: tuple row, no chaining, fail-open on DB error ────

def test_get_usage_reads_tuple_row():
    import free_tier_limiter as ftl

    conn = _StubConn(rows=[(7,)])
    orig = ftl._get_db
    ftl._get_db = lambda: conn
    try:
        n = ftl._get_usage('user-42', 'api_request')
    finally:
        ftl._get_db = orig

    assert n == 7, (
        f"_get_usage returned {n!r} — the read is dead again "
        "(execute-chaining or dict-indexed tuple row)")
    assert len(conn.cur.executed) == 1
    sql, params = conn.cur.executed[0]
    assert 'free_tier_usage' in sql and 'usage_count' in sql
    assert params[0] == 'user-42' and params[1] == 'api_request'


def test_get_usage_fails_open_to_zero():
    """The live DB has NO free_tier_usage table (db_utils skips DDL, and
    nothing else creates it — verified 2026-07-30). A thrown read must
    degrade to 0, not propagate a 500 into every gated endpoint."""
    import free_tier_limiter as ftl

    class _ThrowingConn(_StubConn):
        def cursor(self, *a, **k):
            raise RuntimeError('relation "free_tier_usage" does not exist')

    orig = ftl._get_db
    ftl._get_db = lambda: _ThrowingConn(rows=[])
    try:
        n = ftl._get_usage('user-42', 'api_request')
    finally:
        ftl._get_db = orig

    assert n == 0, f"missing-table read must fail-open to 0, got {n!r}"


# ── 1e. tier lookup: live columns only, paid plans honored ───────────

def test_get_user_tier_resolves_paid_plans():
    """users.plan is the live tier field. The dead query also selected
    stripe_subscription_id + stripe_subscription_status (live names:
    stripe_customer_id, subscription_status) — every lookup threw and
    paying users were treated as free."""
    import free_tier_limiter as ftl

    for plan, want in [('pro', 'pro'), ('enterprise', 'enterprise'),
                       ('developer', 'developer'), ('founding', 'founding'),
                       ('professional', 'pro'), ('ent', 'enterprise'),
                       # starter/team/research_seed entries landed in #1951
                       ('starter', 'starter'), ('team', 'team'),
                       ('research_seed', 'research_seed'),
                       ('legacy_gold', 'free'),  # unknown plan → free limits
                       (None, 'free')]:
        conn = _StubConn(rows=[(plan,)])
        orig = ftl._get_db
        ftl._get_db = lambda: conn
        try:
            got = ftl._get_user_tier('user-42')
        finally:
            ftl._get_db = orig

        assert got == want, f"plan={plan!r}: got {got!r}, want {want!r}"
        assert got in ftl.TIER_LIMITS, (
            "tier must be a TIER_LIMITS key — check_api_limit indexes "
            "TIER_LIMITS[tier] when building 429 headers")
        sql, _params = conn.cur.executed[0]
        for dead in ('stripe_subscription_status', 'stripe_subscription_id',
                     'is_admin'):
            assert dead not in sql, f"users.{dead} does not exist live"


def test_get_user_tier_unknown_user_is_free():
    import free_tier_limiter as ftl

    conn = _StubConn(rows=[])
    orig = ftl._get_db
    ftl._get_db = lambda: conn
    try:
        got = ftl._get_user_tier('nobody@nowhere.invalid')
    finally:
        ftl._get_db = orig
    assert got == 'free'


# ── 1f. increment: single upsert on the table's real unique key ──────

def test_increment_usage_upserts_on_composite_key():
    import free_tier_limiter as ftl

    conn = _StubConn(rows=[])
    orig = ftl._get_db
    ftl._get_db = lambda: conn
    try:
        ftl._increment_usage('user-42', 'api_request')
    finally:
        ftl._get_db = orig

    assert len(conn.cur.executed) == 1, (
        "one statement expected — the old two-step UPDATE/INSERT read "
        ".rowcount off execute()'s None return")
    sql, params = conn.cur.executed[0]
    assert 'ON CONFLICT (user_id, usage_type, usage_month)' in sql, (
        "upsert must key on the table's UNIQUE(user_id, usage_type, "
        "usage_month) — ON CONFLICT (user_id) is not a unique key and "
        "Postgres rejects the whole statement")
    assert 'usage_count = free_tier_usage.usage_count + 1' in sql
    assert params[0] == 'user-42' and params[1] == 'api_request'


# ── 1g. admin endpoints gate on DCHUB_ADMIN_KEY, not users.is_admin ──

def test_admin_usage_endpoints_gate_on_admin_key():
    """The admin closures selected users.is_admin — no such column live,
    and live `role` has no 'admin' value, so a role-based repoint would
    deny forever. They now gate on DCHUB_ADMIN_KEY."""
    from flask import Flask
    import free_tier_limiter as ftl

    # Rows consumed in order: init's to_regclass verify, then for the
    # authorized GET: tier lookup, land-power usage, api usage.
    conn = _StubConn(rows=[('free_tier_usage',), ('pro',), (2,), (9,)])
    orig = ftl._get_db
    ftl._get_db = lambda: conn
    env_orig = os.environ.get('DCHUB_ADMIN_KEY')
    os.environ['DCHUB_ADMIN_KEY'] = 'stub-admin-key'
    try:
        app = Flask('dead_key_queries_admin_test')
        ftl.init_free_tier_limiter(app)
        client = app.test_client()

        denied = client.get('/api/admin/usage/user-42')
        assert denied.status_code == 403

        ok = client.get('/api/admin/usage/user-42',
                        headers={'X-Admin-Key': 'stub-admin-key'})
        assert ok.status_code == 200
        body = ok.get_json()
        assert body['tier'] == 'pro'
        assert body['land_power']['searches_used'] == 2
        assert body['api']['requests_used'] == 9

        reset = client.post('/api/admin/usage/user-42/reset',
                            headers={'X-Admin-Key': 'stub-admin-key'})
        assert reset.status_code == 200
        assert reset.get_json()['success'] is True
    finally:
        ftl._get_db = orig
        if env_orig is None:
            os.environ.pop('DCHUB_ADMIN_KEY', None)
        else:
            os.environ['DCHUB_ADMIN_KEY'] = env_orig

    executed_sql = [sql for sql, _ in conn.cur.executed]
    assert any('DELETE FROM free_tier_usage' in s for s in executed_sql)
    assert not any('is_admin' in s for s in executed_sql), (
        "users.is_admin does not exist — admin auth is DCHUB_ADMIN_KEY")


# ── 2. repo-wide sweep: the dead columns must not reappear in SQL ────

# Known stragglers, each pending its own PR. An entry here only mutes
# THAT file — delete the entry when its fix merges so the fence
# re-arms for the file. Empty as of 2026-07-30: the last entry
# (add_performance_indexes.py's dead CREATE INDEX on api_keys(key_value))
# was removed when that index entry was dropped — key_hash is already
# uniquely indexed live.
_SWEEP_ALLOW = {}

# (table, dead-fragment REGEX) pairs: a single string literal mentioning
# the table AND matching the fragment is a dead query. Comments never
# trip this (AST sees only real string constants); adjacent-literal SQL
# is folded into one constant at parse time, so split queries are still
# caught. The id::text lookbehind exempts casts of OTHER columns that
# merely end in "id" — developer_id::text (main.py /me resolver) and
# u.id::text (brain_investigator) are legitimate.
_DEAD_PAIRS = [
    ('api_keys', r'\bkey_value\b'),
    ('api_keys', r'\brevoked_at\b'),
    ('mcp_dev_keys', r'\bkey_value\b'),
    ('mcp_dev_keys', r'(?<![\w.])id::text'),
    # users has subscription_status / stripe_customer_id — the stripe_-
    # prefixed *_subscription_status name never existed (2026-07-30 sweep:
    # free_tier_limiter was the only file using it). users.is_admin and
    # users.stripe_subscription_id are NOT fenced here: those tokens
    # appear across many files in non-users contexts, unaudited.
    ('users', r'\bstripe_subscription_status\b'),
]

_SKIP_DIRS = {'tests', 'node_modules', '__pycache__', 'venv', '.venv'}


def _iter_py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith('.py'):
                yield os.path.join(dirpath, fn)


def _string_constants(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):  # f-string literal parts
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    yield node.lineno, part.value


def test_no_dead_key_columns_in_sql():
    violations = []
    for path in _iter_py_files():
        rel = os.path.relpath(path, ROOT)
        if rel in _SWEEP_ALLOW:
            continue
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                tree = ast.parse(f.read())
        except SyntaxError:
            continue  # parseability is syntax-check's job, not ours
        for lineno, s in _string_constants(tree):
            low = s.lower()
            for table, dead_re in _DEAD_PAIRS:
                if table in low and re.search(dead_re, low):
                    violations.append(f"{rel}:{lineno}: string references "
                                      f"{table} + /{dead_re}/")
    assert not violations, (
        "Dead-column queries reintroduced (these tables do not have these "
        "columns — see tests/test_dead_key_queries.py docstring):\n"
        + "\n".join(sorted(set(violations))))


# ── 3. the fixed functions must not regress to execute-chaining ──────

def _chained_fetches_in(path, func_name):
    with open(os.path.join(ROOT, path), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in ('fetchone', 'fetchall')
                        and isinstance(sub.func.value, ast.Call)
                        and isinstance(sub.func.value.func, ast.Attribute)
                        and sub.func.value.func.attr == 'execute'):
                    hits.append(f"{path}:{sub.lineno}")
            return hits
    raise AssertionError(f"{func_name} not found in {path} — if it was "
                         "renamed, update this fence rather than deleting it")


def _execute_value_uses_in_file(path):
    """Whole-file scan for ANY use of execute()'s return value — not just
    .fetchone() chains. PGCursorWrapper.execute returns None, so
    `c.execute(...).anything` AttributeErrors and
    `result = c.execute(...)` assigns None (the free_tier_limiter
    _increment_usage bug read `.rowcount` off it via such an assign)."""
    with open(os.path.join(ROOT, path), encoding='utf-8') as f:
        tree = ast.parse(f.read())

    def _is_execute_call(n):
        return (isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in ('execute', 'executemany'))

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_execute_call(node.value):
            hits.append(f"{path}:{node.lineno}: .{node.attr} chained off execute()")
        elif (isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                and _is_execute_call(getattr(node, 'value', None))):
            hits.append(f"{path}:{node.lineno}: execute() return value assigned")
    return hits


def test_fixed_lookups_do_not_chain_execute():
    """db_utils.PGCursorWrapper.execute returns None, so any use of its
    return value is dead under Postgres. free_tier_limiter.py is fully
    clean as of 2026-07-30 (the last four chains + a rowcount-off-execute
    fixed), so it gets the whole-file scan; gating_routes.py stays scoped
    to its one fixed function — the rest of that file is unaudited."""
    bad = (_execute_value_uses_in_file('free_tier_limiter.py')
           + _chained_fetches_in(os.path.join('routes', 'gating_routes.py'),
                                 '_tier_from_api_key'))
    assert not bad, ("execute() return value used — dead under "
                     "PGCursorWrapper (returns None): " + ", ".join(bad))
