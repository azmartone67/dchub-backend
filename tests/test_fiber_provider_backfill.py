"""HIFLD owner sentinel in fiber_routes.PROVIDER (2026-08-16).

#2726 scrubbed the sentinel out of `name` and deliberately scoped itself to
display names. The sibling column was left dirty: 1,150 rows hold the literal
'NOT AVAILABLE' in `provider`, which is served publicly as `carrier` on
/api/v1/fiber/routes AND is filterable —

    GET /api/v1/fiber/routes?carrier=NOT%20AVAILABLE   ->   total: 1150

so unlike the name half this one is a wrong ANSWER, not just a wrong label.

★WHAT THESE TESTS ARE FOR. The repair is one line of behaviour and three lines
of risk, so they pin the risks:

  1. DELEGATION. The sentinel family must be read from
     infrastructure_discovery.hifld_owner, never re-listed here — a second copy
     drifts silently the day a sentinel is added to one of them.
     test_the_family_is_delegated_not_reimplemented proves it by feeding a
     sentinel that exists ONLY in a stub module; a hardcoded list fails it.
  2. SCOPE. A blank provider is not a sentinel. Mapping '' -> 'Unknown' would
     invent data for rows this backfill was never about.
  3. UNIQUE(name, provider) is live on fiber_routes (twice). Collapsing two
     spellings into one can collide, and two CANDIDATES can collide with each
     other in a way the scan's SQL snapshot cannot see.

Per repo convention, tests never import main.py; the functions under test are
pulled out of the source with ast and executed against stubs.
"""
import ast
import os
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "fiber_name_quality.py"


def _load(names):
    """Execute only the named module-level defs/assigns from the repair module."""
    tree = ast.parse(SRC.read_text())
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            keep.append(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    keep.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            keep.append(node)
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<extract>", "exec"), ns)
    return ns


@pytest.fixture()
def repair():
    ns = _load({"repair_provider", "_hifld_owner", "_unknown_owner"})
    return ns["repair_provider"]


# ── the repair itself ────────────────────────────────────────────────────────

def test_the_exact_live_row_shape_is_repaired():
    """1,150 rows, all source='hifld', all this literal (measured 2026-08-16)."""
    assert _load({"repair_provider", "_hifld_owner", "_unknown_owner"})[
        "repair_provider"]("NOT AVAILABLE") == "Unknown"


def test_sentinel_is_caught_case_insensitively(repair):
    # The live data carries it in caps; a case-sensitive check would miss it.
    for bad in ("NOT AVAILABLE", "not available", "Not Available", "  N/A  ",
                "no data", "-999999", "NULL"):
        assert repair(bad) == "Unknown", bad


def test_real_carriers_come_back_byte_identical(repair):
    for good in ("Zayo", "Lumen", "Dominion", "Cogent", "AT&T",
                 "Unknown"):          # already repaired -> no second write
        assert repair(good) == good


def test_a_real_carrier_is_not_whitespace_trimmed(repair):
    """hifld_owner strips; this backfill does not. Only a mapping to the
    fallback counts as a repair, so a padded REAL name is left alone rather
    than quietly rewritten by a lane that was authorised to fix sentinels."""
    assert repair("  Zayo  ") == "  Zayo  "


def test_blank_and_null_are_never_given_an_invented_value(repair):
    """★SCOPE. hifld_owner('') returns the fallback — correct at INGEST, where
    something must be written. Here it would fabricate a carrier for a row that
    was never part of this defect."""
    for blank in (None, "", "   "):
        assert repair(blank) is blank or repair(blank) == blank
        assert repair(blank) != "Unknown"


def test_repair_is_idempotent(repair):
    once = repair("NOT AVAILABLE")
    assert repair(once) == once


# ── ★ the delegation guard ───────────────────────────────────────────────────

def test_the_family_is_delegated_not_reimplemented(repair, monkeypatch):
    """Feed a sentinel that exists ONLY in a stub infrastructure_discovery.

    If repair_provider carries its own copy of the string list, 'ACME-SENTINEL'
    is unknown to it and comes back unchanged — this fails. It can only pass by
    actually calling through to the ingest's definition.
    """
    stub = types.ModuleType("infrastructure_discovery")
    stub._HIFLD_NULL_STRINGS = frozenset({"", "acme-sentinel"})

    def hifld_owner(owner, operator=None):
        for cand in (owner, operator):
            t = str(cand or "").strip()
            if t and t.lower() not in stub._HIFLD_NULL_STRINGS:
                return t
        return "NO PARTY NAMED"      # a fallback the real module never returns

    stub.hifld_owner = hifld_owner
    monkeypatch.setitem(sys.modules, "infrastructure_discovery", stub)

    assert repair("ACME-SENTINEL") == "NO PARTY NAMED"
    # ...and the real module's sentinel is NOT special-cased locally: under this
    # stub 'NOT AVAILABLE' is an ordinary party name and must survive.
    assert repair("NOT AVAILABLE") == "NOT AVAILABLE"


def test_every_upstream_sentinel_maps_to_the_upstream_fallback(repair):
    """Whatever the ingest considers a sentinel today, the backfill repairs."""
    from infrastructure_discovery import _HIFLD_NULL_STRINGS, hifld_owner
    for s in _HIFLD_NULL_STRINGS:
        if s and s.strip():
            assert repair(s) == hifld_owner(""), s


def test_the_sql_prefilter_reads_the_shared_set_and_drops_blanks():
    from infrastructure_discovery import _HIFLD_NULL_STRINGS
    vals = _load({"_sentinel_values"})["_sentinel_values"]()
    assert set(vals) == {s for s in _HIFLD_NULL_STRINGS if s and s.strip()}
    assert "" not in vals
    assert vals == sorted(vals)          # stable param for the = ANY(%s)


# ── ★ UNIQUE(name, provider) ────────────────────────────────────────────────

def _defer():
    return _load({"_defer_intra_set_collisions"})["_defer_intra_set_collisions"]


def test_two_candidates_repairing_to_the_same_key_do_not_both_get_written():
    """The scan's `EXISTS` reads the pre-UPDATE snapshot, so it sees a row that
    ALREADY holds 'Unknown' but never a sibling candidate about to become it.
    Two spellings on one name ('NOT AVAILABLE' + 'N/A') are exactly that case:
    both are legal under UNIQUE(name, provider) today, and both repair to one
    key. Writing both violates the constraint and fails the whole chunk."""
    keep, deferred = _defer()([
        {"id": 1, "name": "Unknown 500kV Line - Ashburn", "from": "NOT AVAILABLE", "to": "Unknown"},
        {"id": 2, "name": "Unknown 500kV Line - Ashburn", "from": "N/A", "to": "Unknown"},
        {"id": 3, "name": "Unknown 230kV Line - Dallas", "from": "NOT AVAILABLE", "to": "Unknown"},
    ], [], "name")
    assert [r["id"] for r in keep] == [1, 3]      # lowest id of the group wins
    assert [r["id"] for r in deferred] == [2]


def test_distinct_names_are_never_deferred():
    keep, deferred = _defer()(
        [{"id": i, "name": "Line %d" % i, "to": "Unknown"} for i in range(5)], [], "name")
    assert len(keep) == 5 and deferred == []


def test_sql_detected_collisions_are_carried_through_not_dropped():
    """A deferred row must stay COUNTABLE — 'skipped 3' is a report, silently
    losing 3 is the bug this repo keeps finding."""
    pre = [{"id": 9, "name": "Line A", "to": "Unknown"}]
    keep, deferred = _defer()([{"id": 1, "name": "Line B", "to": "Unknown"}], pre, "name")
    assert keep and [r["id"] for r in deferred] == [9]


# ── ★ what makes undo total ─────────────────────────────────────────────────

def _fn_source(name):
    tree = ast.parse(SRC.read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, "%s not found in routes/fiber_name_quality.py" % name
    return ast.get_source_segment(SRC.read_text(), fn)


def test_apply_writes_the_undo_slot_under_coalesce():
    """★The property the name lane documents and this lane inherits: re-running
    apply must never launder a REPAIRED provider into the "original" slot. A
    bare `provider_orig = provider` on a second run would do exactly that and
    make undo restore the repair instead of the original."""
    src = _fn_source("fiber_providers_apply")
    assert "COALESCE(f.provider_orig, f.provider)" in src


def test_apply_is_read_only_without_confirm():
    src = _fn_source("fiber_providers_apply")
    i_guard = src.index('request.args.get("confirm")')
    i_write = src.index("UPDATE fiber_routes")
    assert i_guard < i_write, "the confirm gate must precede the write"
    assert 'dry_run=True' in src


def test_undo_restores_from_the_provider_slot_and_clears_it():
    src = _fn_source("fiber_providers_undo")
    assert "provider = provider_orig" in src
    assert "provider_orig = NULL" in src
    assert "WHERE provider_orig IS NOT NULL" in src


def test_both_undo_columns_are_created_together():
    src = _fn_source("_ensure_columns")
    for col in ("name_orig", "name_fixed_at", "provider_orig", "provider_fixed_at"):
        assert "ADD COLUMN IF NOT EXISTS %s" % col in src, col


def test_the_three_provider_routes_exist_and_are_admin_gated():
    tree = ast.parse(SRC.read_text())
    routes = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name.startswith("fiber_providers_"):
            paths = [d.args[0].value for d in n.decorator_list
                     if isinstance(d, ast.Call) and d.args
                     and isinstance(d.args[0], ast.Constant)]
            routes[n.name] = paths
            assert "_admin_ok" in ast.dump(n), "%s is not admin-gated" % n.name
    assert set(routes) == {"fiber_providers_analyze", "fiber_providers_apply",
                           "fiber_providers_undo"}
    assert routes["fiber_providers_analyze"] == ["/api/v1/admin/fiber-providers/analyze"]


# ── ★★ the scan must not re-select its own output (2026-08-16) ──────────────
# The lane repairs ONTO hifld_owner('') == 'Unknown', and 'unknown' is ALSO a
# member of _HIFLD_NULL_STRINGS — so the prefilter selects the lane's own
# output. Python discarded those rows (repair_provider returns them unchanged),
# but only after the correlated EXISTS had run once per row against the
# fallback bucket of fiber_routes_name_provider_key, whose LEADING column is
# `name`. Both factors are the size of that bucket, so the scan was QUADRATIC
# in the number of rows already repaired. Measured on prod 2026-08-16 with
# EXPLAIN (ANALYZE, BUFFERS) over the 1,200 such rows:
#
#     before   1,200 index searches   2,822,677 shared hits   6,158ms
#     after        0 index searches      23,077 shared hits      85ms
#
# to return `fixable: 0` either way. /api/v1/admin/fiber-providers/ has no
# ROUTE_TIMEOUTS entry in worker.js, so at ~2,400 rows the old form breaks the
# edge's 15s DEFAULT on an EMPTY result set.

def _scan_provider_sql():
    """The literal SQL _scan_providers hands to execute(), from the source."""
    fn = next(n for n in ast.walk(ast.parse(SRC.read_text()))
              if isinstance(n, ast.FunctionDef) and n.name == "_scan_providers")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "execute"]
    assert len(calls) == 1, "_scan_providers must issue exactly one statement"
    sql, args = calls[0].args
    # the query is constants + the optional LIMIT tail; fold the constants only
    parts = []

    def walk(node):
        if isinstance(node, ast.Constant):
            parts.append(node.value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            walk(node.left)
            walk(node.right)
    walk(sql)
    return "".join(p for p in parts if isinstance(p, str)), args


def test_the_prefilter_excludes_rows_already_at_the_fallback():
    """★The one-line fix. Without it the scan re-reads every row it has ever
    repaired and pays a per-row index probe to conclude nothing."""
    sql, _ = _scan_provider_sql()
    assert "f.provider IS DISTINCT FROM %s" in sql, (
        "the prefilter must drop rows that already hold the fallback — "
        "they are the lane's own output and repair_provider skips them anyway")


def test_the_exclusion_is_byte_exact_not_normalised():
    """★A row spelled 'unknown' or '  Unknown  ' IS a genuine repair: it
    normalises to the fallback but is not byte-equal to it, and repair_provider
    rewrites it. Wrapping the exclusion in lower()/btrim() would silently drop
    those rows from the lane — a correctness regression wearing a perf fix."""
    sql, _ = _scan_provider_sql()
    i = sql.index("f.provider IS DISTINCT FROM %s")
    clause = sql[max(0, i - 40):i + 30]
    for norm in ("lower(", "btrim(", "upper(", "trim("):
        assert norm not in clause, (
            "the exclusion must compare f.provider byte-exactly; %r in %r "
            "would drop a spelling that only NORMALISES to the fallback"
            % (norm, clause))


def test_the_excluded_value_is_derived_from_the_ingest_not_hardcoded():
    """Same delegation rule the rest of the module follows: the fallback is
    asked for, never spelled. A literal here re-opens the drift this file's
    delegation guard exists to close."""
    sql, args = _scan_provider_sql()
    assert isinstance(args, ast.Tuple)
    passed = [ast.unparse(a) for a in args.elts]
    assert passed == ["fallback", "_sentinel_values()", "fallback"], passed
    assert sql.count("%s") == len(passed)
    fn = next(n for n in ast.walk(ast.parse(SRC.read_text()))
              if isinstance(n, ast.FunctionDef) and n.name == "_scan_providers")
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body   # prose may SAY it
    code = "\n".join(ast.unparse(n) for n in body)
    assert "fallback = _unknown_owner()" in code
    assert "Unknown" not in code, "the fallback is spelled, not asked for"


def test_a_spelling_that_merely_normalises_to_the_fallback_is_still_repaired(repair):
    """The behaviour the exclusion must NOT break — the Python side of the
    invariant the live mirror test checks against the SQL."""
    for near in ("unknown", "UNKNOWN", "  Unknown  ", "Unknown "):
        assert repair(near) == "Unknown", near
        assert repair(near) != near, near
    # ...and the exact fallback is the ONLY selected spelling that is a no-op.
    assert repair("Unknown") == "Unknown"


def test_the_name_lane_is_not_symmetric_and_must_not_be_changed_to_match():
    """★The comment this asymmetry deserves, pinned. repair_name's output does
    not satisfy the name lane's own ILIKE prefilter, so a repaired row leaves
    the selection set on its own — the name lane has nothing to exclude, and
    adding an exclusion there would be a change with no defect behind it."""
    ns = _load({"repair_name", "_VOLT_RE", "_OWNER_RE", "_SENTINEL_ARGS"})
    repaired = ns["repair_name"]("NOT AVAILABLE -999999kV Line - Columbus [c1]")
    assert repaired == "Unknown Line - Columbus [c1]"
    # the two ILIKE patterns, applied as Postgres would apply them
    assert not repaired.upper().startswith("NOT AVAILABLE ")
    assert "-999999KV" not in repaired.upper()
    assert ns["_SENTINEL_ARGS"] == ("%-999999kV%", "NOT AVAILABLE %")
    # and the name lane's scan therefore carries no such exclusion
    assert "IS DISTINCT FROM" not in _fn_source("_scan")


# ── ★ live parity: the SQL prefilter vs repair_provider, on a real mirror ────
# Verified against a `CREATE TEMP TABLE fiber_routes (LIKE public.fiber_routes
# INCLUDING ALL)` mirror rather than live rows: pg_temp shadows public for the
# unqualified name the module uses, the mirror carries the real UNIQUE indexes,
# and the whole thing is rolled back. Nothing in public is written or locked
# beyond the AccessShareLock that LIKE takes.

# ★FIBER_PARITY_DSN FIRST, and it is not a synonym for DATABASE_URL. 58 test
# files gate on DATABASE_URL, so setting that on the unit-tests job to wake this
# one test would wake all of them against an empty database and take a REQUIRED
# check down. The dedicated name lets CI enable exactly this test.
_DSN = (os.environ.get("FIBER_PARITY_DSN")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL"))

# ★A SKIP IS NOT A PASS. Without this, a CI job whose Postgres service is
# renamed, removed, or fails to come up still reports green — the job runs, the
# test skips, and nothing says so. FIBER_PARITY_REQUIRE=1 turns "no DSN" from a
# skip into a failure, so the job can only be green by actually connecting.
_REQUIRE = os.environ.get("FIBER_PARITY_REQUIRE") == "1"

# ★Bootstrap is OPT-IN and CI-only. `LIKE public.fiber_routes INCLUDING ALL`
# needs the table to exist and a throwaway CI Postgres is empty. Gating creation
# behind an explicit flag means pointing this at a real database can never
# create anything there — on prod the table exists and this is a no-op anyway.
# It also runs INSIDE the test's transaction, so it is rolled back with
# everything else and leaves no state between runs.
_BOOTSTRAP = os.environ.get("FIBER_PARITY_BOOTSTRAP") == "1"

# The minimum shape of public.fiber_routes this test depends on. Deliberately
# NOT the full prod table: the parity claim is about `provider` and the unique
# key, and a fixture mirroring all ~30 columns would drift without telling us.
# ★UNIQUE(name, provider) is NOT in the repo's CREATE TABLE — it exists only on
# the live DB (twice: fiber_routes_name_provider_key and _unique). It is stated
# here because the collision path the scan reports depends on it, and
# _assert_fixture_is_faithful below fails if it ever goes missing.
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS public.fiber_routes (
    id       INTEGER PRIMARY KEY,
    name     TEXT,
    provider TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS fiber_routes_name_provider_key
    ON public.fiber_routes (name, provider);
"""


def _connect(dsn):
    """psycopg2.connect honouring the DSN's own sslmode.

    The old call hardcoded sslmode='require', which is right for Neon and fatal
    for a CI service container that speaks no TLS. A DSN that states its mode
    wins; anything else still defaults to require, so a prod DSN cannot be
    downgraded by omission.
    """
    import psycopg2
    kw = {"connect_timeout": 15}
    if "sslmode=" not in (dsn or ""):
        kw["sslmode"] = "require"
    return psycopg2.connect(dsn, **kw)


def _assert_fixture_is_faithful(cur):
    """The two properties the parity assertions actually rest on.

    ★Without this, a bootstrap that quietly lost the unique index would still go
    green — the scan would report `collisions: []` because nothing CAN collide,
    not because nothing does, and the test would be asserting against a weaker
    table than prod carries.
    """
    cur.execute("SELECT to_regclass('public.fiber_routes')")
    assert cur.fetchone()[0] is not None, (
        "public.fiber_routes is missing — set FIBER_PARITY_BOOTSTRAP=1 for a "
        "throwaway database, or point FIBER_PARITY_DSN at one that has it")
    cur.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'fiber_routes' "
        "  AND indexdef LIKE '%UNIQUE%'")
    defs = [d for (d,) in cur.fetchall()]
    assert any("(name, provider)" in d for d in defs), (
        "no UNIQUE(name, provider) on public.fiber_routes — the mirror would "
        "carry no collision constraint and `collisions: []` would prove "
        "nothing. Found: %s" % defs)

# provider spelling -> is it a candidate the lane should select?
# The authority is repair_provider; this table just names the interesting cases.
_MIRROR_ROWS = [
    (1, "Unknown Line - Ashburn [a1]", "Unknown"),        # AT the fallback
    (2, "Unknown Line - Ashburn [a2]", "unknown"),        # normalises to it
    (3, "Unknown Line - Ashburn [a3]", "  Unknown  "),    # normalises to it
    (4, "Unknown Line - Ashburn [a4]", "NOT AVAILABLE"),  # the live shape
    (5, "Unknown Line - Ashburn [a5]", "N/A"),
    (6, "Unknown Line - Ashburn [a6]", "Zayo"),           # a real carrier
    (7, "Unknown Line - Ashburn [a7]", None),             # never invented for
    (8, "Unknown Line - Ashburn [a8]", ""),
    (9, "Unknown Line - Ashburn [a9]", "   "),
]


class _RecordingCursor:
    """Delegates to a real cursor, keeping the (sql, args) actually issued.

    ★The scan's RESULT cannot distinguish the fix from the defect — that is
    what "behaviour-preserving" means, and it is why the parity assertion below
    passes against the pre-fix code too. The perf property lives one level
    down, in the statement: the rows the SQL SELECTS are the rows that pay the
    correlated EXISTS. So the test replays what the scan asked for.
    """

    def __init__(self, cur, log):
        self._cur, self._log = cur, log

    def execute(self, sql, args=None):
        self._log.append((sql, args))
        return self._cur.execute(sql, args)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)


class _SessionConn:
    """Hands _scan_providers the open transaction and refuses to close it."""

    def __init__(self, conn, log):
        self._conn, self._log = conn, log

    def cursor(self, *a, **k):
        return _RecordingCursor(self._conn.cursor(*a, **k), self._log)

    def close(self):
        pass


@pytest.mark.skipif(not _DSN and not _REQUIRE,
                    reason="no FIBER_PARITY_DSN/DATABASE_URL — LIVE PREFILTER "
                           "PARITY UNPROVEN (static+pure guards above ran)")
def test_live_mirror_prefilter_selects_exactly_what_repair_provider_changes(caplog):
    """★The invariant, end to end: the SQL prefilter and repair_provider must
    agree on what a candidate is. The scan's SQL is the shipped string — the
    real _scan_providers runs against the mirror — so a drift between the two
    fails here rather than showing up as 4s of wasted DB time in production."""
    # ★Fails rather than skips when CI asked for this test and no DSN arrived —
    # otherwise a broken service container is indistinguishable from a pass.
    assert _DSN, ("FIBER_PARITY_REQUIRE=1 but no DSN — this test would have "
                  "silently skipped and the job would still be green")
    ns = _load({"_scan_providers", "_conn", "repair_provider", "_hifld_owner",
                "_unknown_owner", "_sentinel_values",
                "_defer_intra_set_collisions", "logger"})
    conn = _connect(_DSN)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '3s'")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            if _BOOTSTRAP:
                # Throwaway CI database only — unreachable with the flag unset.
                cur.execute(_BOOTSTRAP_SQL)
            _assert_fixture_is_faithful(cur)
            cur.execute("CREATE TEMP TABLE fiber_routes "
                        "(LIKE public.fiber_routes INCLUDING ALL)")
            # ★prove the shadow BEFORE writing anything: if the unqualified name
            # still resolved to public, every INSERT below would hit prod.
            cur.execute("SELECT n.nspname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.oid = 'fiber_routes'::regclass")
            schema = cur.fetchone()[0]
            assert schema.startswith("pg_temp"), (
                "'fiber_routes' resolves to %s, not a temp schema — refusing to "
                "write" % schema)
            cur.executemany(
                "INSERT INTO fiber_routes (id, name, provider) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                _MIRROR_ROWS)

            # the pre-fix prefilter, to prove the exclusion is load-bearing
            cur.execute("SELECT f.id FROM fiber_routes f WHERE "
                        "btrim(lower(coalesce(f.provider, ''))) = ANY(%s) "
                        "ORDER BY f.id", (ns["_sentinel_values"](),))
            before = [r[0] for r in cur.fetchall()]
            assert 1 in before, (
                "the row already AT the fallback was not selected even by the "
                "OLD prefilter — this fixture no longer reproduces the defect")

            issued = []
            ns["_conn"] = lambda: _SessionConn(conn, issued)
            scanned = ns["_scan_providers"]()

            assert scanned is not None, "scan failed: %s" % caplog.text
            fixable, collisions = scanned
            got = sorted(r["id"] for r in fixable)
            want = sorted(i for i, _n, p in _MIRROR_ROWS
                          if ns["repair_provider"](p) != p)
            assert got == want, (
                "scan returned %s, repair_provider changes %s — the prefilter "
                "and the repair disagree about what a candidate is" % (got, want))
            assert got == [2, 3, 4, 5], got   # near-misses 'unknown' /
            assert collisions == []           # '  Unknown  ' are kept
            assert all(r["to"] == "Unknown" for r in fixable)

            # ★THE PERF HALF, which the result above cannot see. Replay the
            # statement the scan actually issued: the fallback row must never
            # reach Python at all. Every row the SELECT returns pays one
            # correlated EXISTS against the fallback bucket of
            # fiber_routes_name_provider_key, so a row discarded afterwards in
            # Python has already cost a full index probe — 1,200 of them, 6.2s,
            # on prod 2026-08-16.
            assert len(issued) == 1, issued
            sql, args = issued[0]
            cur.execute(sql, args)
            selected = sorted(r[0] for r in cur.fetchall())
            assert selected == want, (
                "the SELECT returns %s but only %s are repairable — the scan is "
                "re-selecting its own output and paying an index probe per row "
                "to discard it" % (selected, want))
            assert 1 not in selected
            assert 1 in before      # ...and it did before the exclusion
    finally:
        conn.rollback()
        conn.close()
