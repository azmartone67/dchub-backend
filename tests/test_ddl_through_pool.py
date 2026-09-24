"""The SKIP_DDL trap, turned from folklore into a red CI job (2026-08-04).

House rule: tests NEVER import main. This one loads
scripts/check_ddl_through_pool.py by path; that module imports only db_utils
(os/time/logging/random/re — no pool, no network), and nothing here runs at
module scope.

WHAT IS BEING GUARDED
=====================
`db_utils.PGCursorWrapper.execute()` returns early — silently, no log, no
raise — for any statement starting with a `_DDL_PREFIXES` entry, whenever
SKIP_DDL is set. It defaults to '1' and is absent from prod config. So a lazy
`CREATE TABLE IF NOT EXISTS` written against `db_utils.get_db()` / `safe_db()`
/ `try_get_db()` never creates anything, and the INSERT after it fails inside
whatever `except: pass` the caller wrapped its logging in.

That is not a hypothetical: it hid `mcp_sessions` for three months (#2196),
and free_tier_limiter, intelligence_engine, linkedin_posts_schema and
seo_promotion_engine each carry their own postmortem of the same bug in a
docstring. Twenty-five modules warning about a trap in prose is not a guard —
it is twenty-five people having independently walked into it.

★ THE ONE DISTINCTION THAT MAKES THIS CHECKABLE. `main.get_db` is NOT
`db_utils.get_db`. main.py:7613 imports the db_utils one, main.py:7625 rebinds
the name to `get_pg_connection()` — a raw psycopg2 connection with no wrapper
and no skip. 69 files import get_db from main. A check that conflated the two
would report ~60 false offences and be switched off within the week, so the
tests below pin the distinction in both directions.
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Frozen 2026-08-04 at 57, down from the 60 the scan first found — one fixed
# with the guard, two more once the live audit named which tables were really
# absent. 2026-09-18: 56, google_search_console.init_gsc_tables fixed. See
# scripts/ddl_through_pool_allowlist.txt — that list is a freeze, not an
# amnesty, so this is a CEILING: a new entry fails here even when the scanner
# itself is satisfied. There is no legitimate reason to add one, and every
# removal should ratchet this number down with it.
FROZEN_FUNCTIONS = 56


# Memo only. The scan walks ~1,270 files and four tests below need it; without
# this the module costs ~45s on its own. Populated lazily inside functions —
# nothing here executes at import.
_MEMO = {}


def _guard():
    if "mod" not in _MEMO:
        path = os.path.join(ROOT, "scripts", "check_ddl_through_pool.py")
        spec = importlib.util.spec_from_file_location("_ddl_guard", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MEMO["mod"] = mod
    return _MEMO["mod"]


def _scan():
    if "scan" not in _MEMO:
        _MEMO["scan"] = _guard().scan_tree(ROOT)
    return _MEMO["scan"]


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── the live tree ─────────────────────────────────────────────────────

def test_no_new_ddl_through_the_wrapper():
    """The whole point. A new lazy CREATE on a db_utils cursor fails here."""
    g = _guard()
    files, offences = _scan()
    allowed = g.load_allowlist(ROOT)
    new = sorted({f"{o['path']}::{o['function']}" for o in offences} - allowed)
    assert not new, (
        "DDL on a db_utils-wrapped cursor — SKIP_DDL drops it silently, so "
        "these tables are never created:\n  " + "\n  ".join(new) +
        "\n\nOpen a direct psycopg2 connection for the DDL (see "
        "routes/email_suppression._ensure_table) or move it to a migration. "
        "Do NOT add a line to scripts/ddl_through_pool_allowlist.txt.")


def test_the_scan_is_not_vacuous():
    """★ A guard whose file glob stopped matching exits 0 having checked
    nothing, which is the same silent-pass this exists to catch."""
    g = _guard()
    files, _ = _scan()
    assert files >= g.MIN_FILES, f"only {files} .py files scanned"


def test_the_allowlist_only_shrinks():
    g = _guard()
    allowed = g.load_allowlist(ROOT)
    assert len(allowed) <= FROZEN_FUNCTIONS, (
        f"{len(allowed)} allowlisted, frozen at {FROZEN_FUNCTIONS}. A new line "
        f"means shipping a table-create that does not create the table.")


def test_every_allowlisted_entry_still_matches_something():
    """A fixed entry should be deleted, not left to rot. This is the only
    direction in which the list is allowed to be wrong, and it is still worth
    saying out loud — a stale allowlist silently re-permits the offence if the
    function name is ever reused."""
    g = _guard()
    _, offences = _scan()
    keys = {f"{o['path']}::{o['function']}" for o in offences}
    stale = sorted(g.load_allowlist(ROOT) - keys)
    assert not stale, (
        "fixed — delete these lines from scripts/ddl_through_pool_allowlist"
        ".txt:\n  " + "\n  ".join(stale))


# ── the analyser, on synthetic sources ────────────────────────────────

def _offences(src):
    return _guard().scan_source(src, "x.py")


def test_a_pooled_create_table_is_caught():
    src = ("from db_utils import get_db\n"
           "def _ensure():\n"
           "    conn = get_db()\n"
           "    conn.cursor().execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    out = _offences(src)
    assert len(out) == 1 and out[0]["function"] == "_ensure"


def test_main_get_db_is_not_an_offence():
    """★ main.get_db returns a RAW pooled connection (main.py:7625). Its DDL
    really executes. 69 files do this; calling it an offence would make the
    guard useless on contact."""
    src = ("def _ensure():\n"
           "    from main import get_db\n"
           "    conn = get_db()\n"
           "    conn.cursor().execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    assert _offences(src) == []


def test_a_module_rebinding_the_name_shadows_the_import():
    """main.py itself: imports db_utils.get_db, then defines its own. Inside
    that module the name means the raw one."""
    src = ("from db_utils import get_db\n"
           "def get_db(*a, **k):\n"
           "    return get_pg_connection(*a, **k)\n"
           "def _ensure():\n"
           "    conn = get_db()\n"
           "    conn.cursor().execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    assert _offences(src) == []


def test_direct_psycopg2_is_not_an_offence():
    src = ("import psycopg2\n"
           "def _ensure():\n"
           "    c = psycopg2.connect('x')\n"
           "    c.autocommit = True\n"
           "    c.cursor().execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    assert _offences(src) == []


def test_a_local_helper_wrapping_psycopg2_is_resolved():
    """`_conn()` returning a direct connection is the dominant pattern in
    routes/ — a guard that missed it would fire on every correct module."""
    src = ("import psycopg2\n"
           "def _conn():\n"
           "    return psycopg2.connect('x')\n"
           "def _ensure():\n"
           "    with _conn() as c:\n"
           "        c.cursor().execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    assert _offences(src) == []


def test_a_local_helper_wrapping_get_db_is_resolved():
    """The mirror image, and a real one: free_tier_limiter._get_db()."""
    src = ("def _get_db():\n"
           "    from db_utils import get_db\n"
           "    return get_db()\n"
           "def _init_tables():\n"
           "    conn = _get_db()\n"
           "    conn.cursor().execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    out = _offences(src)
    assert [o["function"] for o in out] == ["_init_tables"]


def test_unwrapping_to_the_raw_cursor_is_recognised():
    """routes/paywall_hint_middleware's escape hatch: getattr(c, '_cur', c)
    reaches the psycopg2 cursor underneath, where DDL executes."""
    src = ("from db_utils import safe_db\n"
           "def _ensure():\n"
           "    with safe_db() as conn:\n"
           "        cur = getattr(conn.cursor(), '_cur', conn.cursor())\n"
           "        cur.execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    assert _offences(src) == []


def test_ddl_handed_to_safe_write_is_caught():
    """safe_write() opens its own wrapped cursor, so the caller cannot escape
    it — this is an offence with no connection variable in sight."""
    src = ("from db_utils import safe_write\n"
           "def _ensure():\n"
           "    safe_write(None, 'CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    out = _offences(src)
    assert len(out) == 1 and "safe_* helper" in out[0]["why"]


def test_a_handed_in_cursor_is_not_guessed_at():
    """No connection source in the function: unresolvable. Reporting it anyway
    is how a guard earns enough false positives to get deleted."""
    src = ("def _ensure(cur):\n"
           "    cur.execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    assert _offences(src) == []


def test_a_multi_statement_blob_is_split():
    src = ("from db_utils import get_db\n"
           "def _ensure():\n"
           "    get_db().cursor().executescript(\n"
           "        'CREATE TABLE a (id INT); SELECT 1; CREATE INDEX i ON a(id)')\n")
    assert len(_offences(src)) == 2


def test_plain_dml_is_left_alone():
    src = ("from db_utils import get_db\n"
           "def w():\n"
           "    get_db().cursor().execute('INSERT INTO t (id) VALUES (1) ON CONFLICT DO NOTHING')\n")
    assert _offences(src) == []


# ── the first casualty ────────────────────────────────────────────────

def test_the_upgrade_nudge_table_is_created_on_a_direct_connection():
    """★ The guard's first live catch, and it was not a cosmetic one.

    `routes/free_upgrade_nudge._ensure_schema` created `upgrade_nudge_log`
    through `safe_db()`, so it never ran. The damage is not a missing log: the
    candidate query filters on `NOT EXISTS (SELECT 1 FROM upgrade_nudge_log
    ...)`, which RAISES against a table that does not exist — so the free-tier
    upgrade nudge selected zero candidates and sent nothing, on a surface built
    specifically to convert free keys into paid ones.

    A June comment in that function already recorded "the missing table 500'd
    the preview while this except hid the reason" — the symptom was seen, the
    cause was not, and a print was added instead. Pinning it so the wrapper
    cannot creep back in."""
    src = _src("routes", "free_upgrade_nudge.py")
    head = src[src.index("def _ensure_schema"):]
    body = head[:head.index("def _key_hash")]
    # ★ Past the docstring before matching. The docstring NAMES safe_db as the
    # thing that broke it — searching raw text would fail on the explanation of
    # the fix, which is the same self-inflicted trap regression_lint documents
    # on itself.
    code = body[body.index('"""', body.index('"""') + 3) + 3:]
    assert "ddl_cursor" in code, "DDL must run on the direct blessed cursor"
    assert "safe_db" not in code, "safe_db SKIPs DDL — that is the whole bug"


# ── the blessed way out ───────────────────────────────────────────────

def test_db_utils_offers_one_marked_path_for_ddl():
    """★ Twenty-five modules each hand-rolled `psycopg2.connect` to escape the
    wrapper, because db_utils offered no alternative. A trap with no marked
    path around it gets walked into — which is the whole allowlist."""
    src = _src("db_utils.py")
    assert "def ddl_cursor" in src
    block = src[src.index("def ddl_cursor"):]
    block = block[:block.index("def safe_write")]
    assert "psycopg2.connect" in block, "its own connection, not the pool"
    assert "autocommit = True" in block
    assert "_get_pg_connection" not in block and "get_db" not in block


def test_ddl_cursor_refuses_rather_than_pretending():
    """★ No DATABASE_URL must RAISE. A helper that quietly did nothing would
    reproduce the exact bug it was written to end — DDL that reports success
    and creates no table."""
    import os as _os
    import db_utils
    saved = {k: _os.environ.pop(k, None)
             for k in ("DATABASE_URL", "NEON_DATABASE_URL")}
    try:
        raised = False
        try:
            with db_utils.ddl_cursor():
                pass
        except RuntimeError as e:
            raised = "refusing to pretend" in str(e)
        except Exception:
            raised = False
        assert raised, "a missing URL must raise, not no-op"
    finally:
        for k, v in saved.items():
            if v is not None:
                _os.environ[k] = v


def test_the_guard_recognises_the_blessed_path():
    """Otherwise the fix the guard recommends would itself fail the guard."""
    src = ("from db_utils import ddl_cursor\n"
           "def _ensure():\n"
           "    with ddl_cursor() as cur:\n"
           "        cur.execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    assert _offences(src) == []
    src2 = ("import db_utils\n"
            "def _ensure():\n"
            "    with db_utils.ddl_cursor() as cur:\n"
            "        cur.execute('CREATE TABLE IF NOT EXISTS t (id INT)')\n")
    assert _offences(src2) == []


def test_a_mixed_function_is_judged_per_cursor_not_per_function():
    """★ THE WAIVER THAT GREW EVERY TIME SOMEONE FIXED SOMETHING. The verdict
    used to read `if not s.ddl or direct or not pooled: continue`, and
    `direct` was function-WIDE: one ddl_cursor() anywhere in a function waived
    every CREATE in it, in either order. So half-fixing a function bought the
    other half permanent immunity — and half-fixing is exactly what happens
    when someone migrates the CREATE that broke and leaves its neighbours.

    Found 2026-09-18 by mutating google_search_console.init_gsc_tables: with
    all three CREATEs re-pooled the guard fired, with ONE re-pooled it stayed
    silent. A guard that only catches the whole-hog version of its own bug is
    most of the way to not being a guard.
    """
    mixed = ("from db_utils import get_db, ddl_cursor\n"
             "def f():\n"
             "    with ddl_cursor() as cur:\n"
             "        cur.execute('CREATE TABLE IF NOT EXISTS ok (id INT)')\n"
             "    bad = get_db().cursor()\n"
             "    bad.execute('CREATE TABLE IF NOT EXISTS oops (id INT)')\n")
    offs = _offences(mixed)
    assert [o["table"] for o in offs] == ["oops"], (
        f"expected exactly the pooled CREATE reported, got {offs}")

    # ...and the same function with the DDL in the other order, because the
    # original waiver was order-independent and a fix that is not would be a
    # coin flip.
    flipped = ("from db_utils import get_db, ddl_cursor\n"
               "def f():\n"
               "    bad = get_db().cursor()\n"
               "    bad.execute('CREATE TABLE IF NOT EXISTS oops (id INT)')\n"
               "    with ddl_cursor() as cur:\n"
               "        cur.execute('CREATE TABLE IF NOT EXISTS ok (id INT)')\n")
    assert [o["table"] for o in _offences(flipped)] == ["oops"]

    # The ddl_cursor half must still be silent on its own — a "fix" that
    # reports the blessed path too would just be the guard switched off.
    assert _offences("from db_utils import ddl_cursor\n"
                     "def f():\n"
                     "    with ddl_cursor() as cur:\n"
                     "        cur.execute('CREATE TABLE IF NOT EXISTS ok (id INT)')\n") == []


# ── the guard cannot drift from the wrapper ───────────────────────────

def test_the_prefix_list_is_imported_from_db_utils_not_copied():
    """★ A private copy would keep passing after someone adds a prefix to the
    wrapper — the guard would go on reporting green about a rule that had
    changed underneath it."""
    src = _src("scripts", "check_ddl_through_pool.py")
    assert "from db_utils import _DDL_PREFIXES" in src
    g = _guard()
    import db_utils
    assert tuple(g._DDL_PREFIXES) == tuple(db_utils._DDL_PREFIXES)
    assert g._PREFIX_SOURCE == "db_utils._DDL_PREFIXES"


def test_the_trap_still_exists_as_described():
    """If SKIP_DDL ever stops defaulting on, or the wrapper stops skipping,
    this whole guard is obsolete and should be deleted rather than left
    running. Pin the premise so that decision is forced, not forgotten."""
    src = _src("db_utils.py")
    assert "SKIP_DDL = os.environ.get('SKIP_DDL', '1') == '1'" in src
    assert "if _is_ddl(sql):" in src


def test_the_script_is_wired_into_ci():
    """★ A guard that only exists in the repo is not a guard. This is the
    check that #2196 needed and did not have."""
    wf = _src(".github", "workflows", "pre-merge.yml")
    assert "scripts/check_ddl_through_pool.py" in wf


# ── the two reachable MISSING tables, fixed 2026-08-04 ────────────────

def test_async_task_results_is_created_on_a_direct_cursor():
    """★ The cross-replica poll fallback had nothing to read. The CREATE sat
    inline in _eia_task_persist on a db_utils.get_db() cursor, so the table was
    never made and every INSERT failed into `logger.debug("eia task persist
    skipped")`. Confirmed absent by the live boot audit before this fix."""
    src = _src("main.py")
    fn = src[src.index("def _ensure_async_task_results"):]
    fn = fn[:fn.index("def _eia_task_persist")]
    assert "ddl_cursor" in fn
    assert "async_task_results" in fn
    # and the handler no longer carries its own CREATE
    persist = src[src.index("def _eia_task_persist"):]
    persist = persist[:persist.index("def _eia_task_lookup")]
    assert "CREATE TABLE" not in persist.upper()
    assert "_ensure_async_task_results()" in persist


def test_daily_anomalies_is_created_on_a_direct_cursor():
    """★ /api/v1/observability/anomalies returned its empty fallback on every
    call since it was written — an empty anomaly list reads exactly like a
    healthy system, which is why nobody noticed."""
    src = _src("routes", "observability_routes.py")
    fn = src[src.index("def _ensure_daily_anomalies"):]
    fn = fn[:fn.index("def anomalies")]
    assert "ddl_cursor" in fn and "daily_anomalies" in fn
    handler = src[src.index("def anomalies():"):]
    handler = handler[:handler.index("@observability_bp", 10)] \
        if "@observability_bp" in handler[10:] else handler
    assert "CREATE TABLE" not in handler.upper()


def test_gsc_tables_are_created_on_a_direct_cursor():
    """★ All THREE of init_gsc_tables' CREATEs sat on a db_utils.get_db()
    cursor, so none of them ever ran — the 2026-09-18 05:21:14Z boot log
    carried DDL-DROPPED for gsc_sitemap_submissions once per worker.

    The production tables exist anyway (checked 2026-09-18: all three present,
    consecutive low OIDs, 2 rows in gsc_sitemap_submissions from 2026-06-15
    and 2026-07-02), created by a deploy predating the wrapper's DDL skip —
    the case the allowlist header calls out. So this is not a live outage; it
    is a module that cannot rebuild its own schema, which matters the moment
    the database is a fresh one. tests/test_gsc_tables_created_on_postgres.py
    is that case, against a real Postgres.

    Asserted through the REAL scanner rather than a substring: a partial
    regression — one of the three re-pooled — still fails here, which a
    `"ddl_cursor" in fn` check would not catch.
    """
    src = _src("google_search_console.py")
    offences = [o for o in _guard().scan_source(src, "google_search_console.py")
                if o["function"] == "init_gsc_tables"]
    assert offences == [], (
        "init_gsc_tables is back on a pooled cursor — these CREATEs will not "
        f"run: {[o['sql'][:60] for o in offences]}")

    # ...and the DDL is still THERE. An empty function would also score zero
    # offences, which is the other way this test could pass for free.
    fn = src[src.index("def init_gsc_tables"):src.index("def get_access_token")]
    body = fn[fn.index("from db_utils import ddl_cursor"):]
    for table in ("gsc_index_requests", "gsc_crawl_errors",
                  "gsc_sitemap_submissions"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in body, table
    assert "with ddl_cursor() as cur:" in body


def test_gsc_init_cannot_be_re_frozen_into_the_allowlist():
    """★ The allowlist is keyed `path::function` and nothing compares the
    statement COUNT, so re-adding this one line would re-freeze all three
    CREATEs at once and turn the scanner green on a live regression. The
    freeze is meant to shrink; this pins that this entry never comes back."""
    listed = _src("scripts", "ddl_through_pool_allowlist.txt")
    entries = [ln.split("#", 1)[0].strip() for ln in listed.splitlines()]
    assert "google_search_console.py::init_gsc_tables" not in entries, (
        "init_gsc_tables was re-added to the allowlist. That is the move the "
        "list's own header forbids — fix the DDL, do not re-freeze it.")


def test_the_dead_modules_stay_dead_and_say_why():
    """★ Three of the five MISSING tables belong to unreachable code. Creating
    them would add empty tables nobody reads. The dispositions live in the
    allowlist so the next reader does not re-derive them — and so nobody
    'fixes' free_tier_limiter believing quotas are unenforced, when the live
    gate is a different module entirely."""
    txt = _src("scripts", "ddl_through_pool_allowlist.txt")
    for entry in ("ai_agent_discovery.py::init_tracking_db",
                  "free_tier_limiter.py::_init_tables",
                  "self_learning_discovery.py::SelfLearningDiscovery._init_db"):
        assert entry in txt, f"{entry} must stay frozen"
    assert "ai_discovery_routes.py instead" in txt
    assert "does NOT mean quotas are unenforced" in txt
    assert "never provisioned" in txt
