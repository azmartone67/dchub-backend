"""Guards for routes/gsc_performance.py — the SEO time series.

Extracted with `ast` and run against stubs, per the repo rule: no test imports
main.py in-process.

The properties worth guarding are the ones whose failure is SILENT:

  * a partial-grain failure must NOT report success (the "green board, dead
    lane" pattern this whole audit kept finding),
  * the upsert must be idempotent on (date, dimension, dim_value), or a daily
    re-fetch of the trailing window duplicates every row,
  * `dim_value` must never be NULL for the site grain, or duplicate site rows
    accumulate under a NULL-tolerant unique key,
  * the read route must distinguish "not ingested" from "no traffic".
"""

import ast
import logging
import pathlib
import re

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "routes" / "gsc_performance.py")
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)


def _const(name):
    for node in TREE.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node
    return None


def _func(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


# ── schema shape ─────────────────────────────────────────────────────

def test_primary_key_makes_reingest_idempotent():
    """(date, dimension, dim_value) is what lets the daily trailing-window
    re-fetch update in place. Lose it and every run duplicates the window."""
    ddl = _const("_DDL")
    assert ddl is not None, "_DDL missing"
    sql = ast.literal_eval(ddl.value)
    norm = " ".join(sql.split()).lower()
    assert "primary key (date, dimension, dim_value)" in norm


def test_dim_value_is_not_nullable():
    """The site grain stores '' rather than NULL. NULL is not equal to NULL in
    a unique index, so a nullable dim_value would let one site row per ingest
    accumulate forever without ever conflicting."""
    sql = ast.literal_eval(_const("_DDL").value)
    norm = " ".join(sql.split()).lower()
    assert "dim_value" in norm
    m = re.search(r"dim_value\s+text\s+not null\s+default\s+''", norm)
    assert m, "dim_value must be NOT NULL DEFAULT ''"


def test_upsert_is_on_conflict_do_update_not_do_nothing():
    """DO NOTHING would freeze the first (undercounted) read of a day forever —
    the exact bug the trailing window exists to avoid."""
    sql = ast.literal_eval(_const("_UPSERT").value)
    norm = " ".join(sql.split()).lower()
    assert "on conflict (date, dimension, dim_value) do update" in norm
    for col in ("clicks", "impressions", "ctr", "position"):
        assert f"{col} = excluded.{col}" in norm, f"{col} not refreshed on conflict"


def test_index_covers_the_read_pattern():
    sql = ast.literal_eval(_const("_DDL_INDEX").value)
    norm = " ".join(sql.split()).lower()
    assert "(dimension, date desc)" in norm


# ── honesty of the writer's own verdict ──────────────────────────────

def test_partial_grain_failure_is_not_reported_as_success():
    """success must be derived from the error set, never hardcoded true.

    A run where the `query` grain 500s but `site` succeeds is a FAILED run. The
    audit found this exact shape repeatedly: a shell reporting green while one
    of its lanes was dead."""
    fn = _func("ingest_daily_performance")
    assert fn is not None
    src = ast.get_source_segment(TEXT, fn)
    assert '"success": not errors' in src, \
        "success must be `not errors`, not a constant"
    # and there must be no literal success:True in this function
    assert '"success": True' not in src


def test_missing_token_fails_closed():
    fn = _func("ingest_daily_performance")
    src = ast.get_source_segment(TEXT, fn)
    assert '"success": False' in src.split("_ensure_table")[0], \
        "a missing token must return success:false before doing any work"


# ── known repo traps are accounted for ───────────────────────────────

def test_ddl_uses_the_raw_cursor():
    """safe_db SKIPs DDL — documented four times over in this repo. A CREATE
    through the wrapper is silently surrendered and the first INSERT then fails
    on a table that was never made."""
    fn = _func("_ensure_table")
    src = ast.get_source_segment(TEXT, fn)
    assert 'getattr(c, "_cur", c)' in src, "_ensure_table must use the raw cursor"


def test_insert_uses_the_raw_cursor():
    """The wrapper probes SELECT lastval() after an INSERT without RETURNING.
    This table has no sequence, so lastval() is undefined, PG errors, and the
    open transaction aborts — taking the next chunk with it."""
    fn = _func("ingest_daily_performance")
    src = ast.get_source_segment(TEXT, fn)
    assert 'getattr(c, "_cur", c)' in src


def test_writes_are_chunked():
    """A single VALUES list of 500 days x 500 queries would be one enormous
    statement; chunking keeps each round trip bounded."""
    fn = _func("ingest_daily_performance")
    src = ast.get_source_segment(TEXT, fn)
    assert "range(0, len(payload), 500)" in src


# ── the read route refuses to let absence read as zero ───────────────

def test_read_route_declares_coverage():
    """An empty series means NOT INGESTED, not no-traffic. Without this block a
    caller — including the brain — would read a flat zero line as a finding."""
    fn = _func("read_performance")
    src = ast.get_source_segment(TEXT, fn)
    assert '"coverage"' in src
    assert "NOT INGESTED" in src
    assert '"oldest"' in src and '"newest"' in src


def test_read_route_weights_position_by_impressions():
    """A flat AVG(position) across days lets a single 1-impression day at rank 3
    outvote 10,000 impressions at rank 40, which would invert the ranking this
    route exists to produce."""
    fn = _func("read_performance")
    src = ast.get_source_segment(TEXT, fn)
    assert "SUM(position * impressions) / SUM(impressions)" in src


def test_read_route_validates_dimension():
    fn = _func("read_performance")
    src = ast.get_source_segment(TEXT, fn)
    assert '("site", "query", "page")' in src
    assert "400" in src


def test_the_blueprint_actually_registers():
    """★ THE TEST THAT WAS MISSING, and it cost a silent production 404.

    The first version of this file asserted only that the string
    "require_internal_or_admin" appeared in admin_ingest's decorator list. It
    did — as `@require_internal_or_admin`. But that name is a PREDICATE,
    `require_internal_or_admin(req) -> bool`, not a decorator. Applied with @,
    it received the function, returned False, and the route tried to register
    the bool `False` as a view. Flask needs `__name__` on a view, so
    registration raised

        'bool' object has no attribute '__name__'

    main.py's try/except logged and swallowed it. The whole blueprint failed to
    register — BOTH routes 404'd in production, including the public read route
    — while every structural test in this file stayed green.

    A grep over decorator names cannot see that. Registering the blueprint
    against a real Flask app can, so do that."""
    flask = pytest.importorskip("flask")
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from routes.gsc_performance import register_gsc_performance_routes

    app = flask.Flask(__name__)
    register_gsc_performance_routes(app)          # must not raise
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert "/api/v1/seo/performance" in rules
    assert "/api/v1/admin/gsc/performance/ingest" in rules


def test_admin_ingest_rejects_an_unauthenticated_caller():
    """Behavioural, not structural: hit the route with no credential and
    require a 401. The previous name-based check passed while the route did not
    exist at all."""
    flask = pytest.importorskip("flask")
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from routes.gsc_performance import register_gsc_performance_routes

    app = flask.Flask(__name__)
    register_gsc_performance_routes(app)
    with app.test_client() as cl:
        r = cl.post("/api/v1/admin/gsc/performance/ingest?days=5")
    assert r.status_code == 401, \
        f"unauthenticated ingest returned {r.status_code}, expected 401"


def test_the_predicate_is_never_used_as_a_decorator():
    """Pin the specific misuse so it cannot come back anywhere in this module."""
    for fn_name in ("admin_ingest", "read_performance"):
        fn = _func(fn_name)
        for d in fn.decorator_list:
            nm = (getattr(d, "id", None)
                  or getattr(d, "attr", None)
                  or getattr(getattr(d, "func", None), "id", None))
            assert nm != "require_internal_or_admin", (
                f"{fn_name} uses require_internal_or_admin as a decorator; it "
                f"is a predicate — call it inside the view instead")
    assert "if not require_internal_or_admin(request)" in TEXT, \
        "the ingest route must still check the credential"


def test_admin_ingest_clamps_days():
    """?days=999999 would ask GSC for a window it will refuse and hold a worker
    for the full timeout."""
    fn = _func("admin_ingest")
    src = ast.get_source_segment(TEXT, fn)
    assert "min(days, 480)" in src


# ── the daily cron exists and is wired to this route ─────────────────

def test_cron_workflow_exists_and_targets_this_route():
    """A writer with no caller is the failure mode the audit found in four
    retired producer classes. Prove the schedule exists."""
    wf = (pathlib.Path(__file__).resolve().parents[1]
          / ".github" / "workflows" / "gsc-performance-ingest.yml")
    assert wf.exists(), "daily ingest workflow missing"
    text = wf.read_text()
    assert "/api/v1/admin/gsc/performance/ingest" in text
    assert re.search(r"cron:\s*'[^']+'", text), "no schedule"
    # and it must verify from the OUTSIDE, not trust the writer's own report
    assert "/api/v1/seo/performance" in text, \
        "workflow must read the series back to prove the write landed"


def test_cron_does_not_collide_with_an_existing_schedule():
    """The heal board already reports six pairs of workflows firing on the exact
    same minute. Do not add a seventh."""
    wfdir = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
    mine = (wfdir / "gsc-performance-ingest.yml").read_text()
    m = re.search(r"cron:\s*'([^']+)'", mine)
    assert m
    my_cron = m.group(1).strip()

    clashes = []
    for f in wfdir.glob("*.yml"):
        if f.name == "gsc-performance-ingest.yml":
            continue
        try:
            body = f.read_text()
        except Exception:
            continue
        for c in re.findall(r"cron:\s*'([^']+)'", body):
            if c.strip() == my_cron:
                clashes.append(f.name)
    assert not clashes, f"cron '{my_cron}' already used by {clashes}"


def test_blueprint_is_registered_in_main():
    """A blueprint that is never registered serves nothing — and the route
    would 404 while every test here still passed."""
    main = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "register_gsc_performance_routes" in main


# ── rowLimit: Google's ceiling is 25,000, and it 400s rather than truncating ──

def test_row_limit_is_capped_at_googles_ceiling():
    """The 2026-08-31 480-day seed lost BOTH the query and page grains to

        "'240000' is not a valid row limit value"

    because the per-day limit was multiplied by the window length and sent as a
    single rowLimit. Google rejects an oversized rowLimit outright — it does not
    truncate — so the whole grain is lost, not shortened."""
    src = ast.get_source_segment(TEXT, _func("_query_gsc"))
    assert "_GSC_MAX_ROW_LIMIT" in src
    assert "min(want, _GSC_MAX_ROW_LIMIT)" in src, \
        "the per-call rowLimit must be clamped to Google's ceiling"
    node = _const("_GSC_MAX_ROW_LIMIT")
    assert node is not None and ast.literal_eval(node.value) == 25000


def test_more_than_one_page_is_fetched_by_pagination():
    """Clamping alone would silently drop everything past 25,000. The rows have
    to be paged with startRow, as refresh_proven_pages already does."""
    src = ast.get_source_segment(TEXT, _func("_query_gsc"))
    assert '"startRow": start_row' in src
    assert "start_row += len(batch)" in src
    assert "if len(batch) < per_call:" in src, \
        "a short page is the end-of-data signal; without it this loops forever"


def test_the_grain_limit_is_bounded_not_unbounded():
    """A 16-month seed must not page indefinitely."""
    src = ast.get_source_segment(TEXT, _func("ingest_daily_performance"))
    assert "min(_wanted, _SEED_ROW_CEILING)" in src


def test_a_truncation_is_reported_not_silent():
    """A cap nobody is told about reads as complete coverage — the exact
    failure this module's `coverage` block exists to refuse."""
    src = ast.get_source_segment(TEXT, _func("ingest_daily_performance"))
    assert '"rows_capped"' in src
    assert "_wanted > _SEED_ROW_CEILING" in src, \
        "rows_capped must be None when the ceiling did NOT bite"


# ── F4 (2026-09-02): the ingest also refreshes seo_proven_pages ──────────
# MEASURED 2026-09-02: /api/gsc/proven -> last_refreshed 2026-08-24 06:45:03,
# 21,672 rows, qualifying_at_threshold 7,292. No workflow, scheduler or route
# called POST /api/gsc/proven/refresh (grep of .github/workflows, main.py,
# routes/) — yet sitemap admission (#2946) reads that table, so the admission
# list froze on 08-24 while the sitemap rebuilt every 4h. The daily ingest,
# which already holds a GSC token, now carries the refresh; a refresh failure
# is an ingest failure by this module's partial-failure rule.

def test_ingest_calls_refresh_proven_pages():
    fn = _func("ingest_daily_performance")
    assert fn is not None
    called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
              for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "refresh_proven_pages" in called, (
        "ingest_daily_performance no longer refreshes seo_proven_pages — the "
        "sitemap admission table has no other caller")
    assert re.search(r"from google_search_console import \(?[^)]*refresh_proven_pages", TEXT), (
        "refresh_proven_pages must come from google_search_console (one integration)")


def _exec_ingest(monkey_refresh):
    """Run ingest_daily_performance against stubs: GSC returns nothing, the DB
    is never touched, and refresh_proven_pages is `monkey_refresh`."""
    fn = _func("ingest_daily_performance")
    from datetime import datetime, timedelta
    ns = {
        "datetime": datetime, "timedelta": timedelta,
        "DEFAULT_WINDOW_DAYS": 5, "DEFAULT_ROW_LIMIT": 500,
        "_GSC_MAX_ROW_LIMIT": 25000, "_SEED_ROW_CEILING": 100000,
        "_UPSERT": "x", "_ensure_table": lambda: None,
        "_query_gsc": lambda *a, **k: ([], None),
        "get_db": lambda: pytest.fail("no grain had rows — the DB must not be opened"),
        "refresh_proven_pages": monkey_refresh,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["ingest_daily_performance"]


def test_a_proven_refresh_failure_is_an_ingest_failure():
    calls = []

    def _bad(token, **kw):
        calls.append(token)
        return {"success": False, "error": "HTTP 403: quota"}

    out = _exec_ingest(_bad)("tok")
    assert calls == ["tok"], "the ingest's token must be reused, not re-minted"
    assert out["success"] is False
    assert "quota" in out["errors"]["proven_pages"]
    assert out["proven_pages"]["success"] is False


def test_a_raising_proven_refresh_is_an_ingest_failure_not_a_crash():
    def _boom(token, **kw):
        raise RuntimeError("neon down")

    out = _exec_ingest(_boom)("tok")
    assert out["success"] is False and "neon down" in out["errors"]["proven_pages"]


def test_a_healthy_proven_refresh_is_reported_and_keeps_success():
    out = _exec_ingest(lambda token, **kw: {"success": True, "upserted": 7})("tok")
    assert out["success"] is True and out["proven_pages"]["upserted"] == 7


def test_the_refresh_can_be_switched_off_explicitly_only():
    ingest = _exec_ingest(lambda token, **kw: pytest.fail("must not refresh when refresh_proven=False"))
    out = ingest("tok", refresh_proven=False)
    assert out["success"] is True and out["proven_pages"] is None


# ── the trailing window must actually be read (2026-09-19) ──────────────
#
# The budget is `row_limit * days`, requested as ONE top-K list over the whole
# (date,page) grid. GSC fills that list from the window START, so a budget
# smaller than the window's real row count does not thin the window evenly —
# it drops the NEWEST days entirely. Measured live: `days=62` wrote 50,000 rows
# and populated 07-19..08-01, then nothing. The daily `days=5 x 500 = 2,500`
# against ~3,400 pages/day refreshed only the oldest day of its own window, so
# the 72 h self-correction DEFAULT_WINDOW_DAYS exists for never ran, from the
# writer's first run (2026-08-31) to 2026-09-19.

# Measured OUTSIDE this repo: per-day distinct page rows from GSC,
# 2026-07-12..2026-09-14. Peak day 2026-08-11 = 4,098. This is an external
# floor, not a mirror of the constant — if the shipped default falls under it
# the trailing window silently stops covering its own newest days again.
MEASURED_PEAK_PAGE_ROWS_PER_DAY = 4098


def _shipped_env_default_int(const_name):
    """The literal default this module SHIPS, read from its own source.

    ★ NOT the value `_exec_ingest` injects. These guards exist to check that the
    shipped default covers the shipped window; a harness supplying its own
    numbers would pass whatever main.py binds — be#4722's lesson, a harness
    strictly more capable than the module runs a different program.
    """
    node = _const(const_name)
    assert node is not None, f"{const_name} is not a module-level assignment"
    call = node.value                        # int(os.environ.get("X", "500"))
    assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "int"
    inner = call.args[0]                     # os.environ.get("X", "500")
    assert isinstance(inner, ast.Call) and getattr(inner.func, "attr", None) == "get"
    return int(ast.literal_eval(inner.args[1]))


def test_the_shipped_row_limit_covers_a_measured_peak_day():
    limit = _shipped_env_default_int("DEFAULT_ROW_LIMIT")
    assert limit >= MEASURED_PEAK_PAGE_ROWS_PER_DAY, (
        f"DEFAULT_ROW_LIMIT={limit} is below the measured peak of "
        f"{MEASURED_PEAK_PAGE_ROWS_PER_DAY} page rows in a single day, so "
        f"`row_limit * days` cannot cover the trailing window and GSC will "
        f"drop the newest days of it.")


class _FakeGSC:
    """`_query_gsc`, faithful on the one property that caused the defect.

    The real API returns the window's rows as ONE list and truncates it to the
    requested limit, and the surviving prefix fills from the window START. That
    is reproduced here. It never returns more rows than it was asked for, and
    never rows outside [start, end] — a fake more generous than the API would
    hide exactly the bug this guards.
    """

    def __init__(self, rows_per_day):
        self.rows_per_day = rows_per_day
        self.asked = []

    def __call__(self, token, start, end, dims, row_limit, max_pages=20):
        from datetime import date, timedelta as _td
        day, last, out = date.fromisoformat(start), date.fromisoformat(end), []
        while day <= last:
            n = 1 if dims == ["date"] else self.rows_per_day
            for i in range(n):
                keys = ([day.isoformat()] if dims == ["date"]
                        else [day.isoformat(), f"https://dchub.cloud/p/{i}"])
                out.append({"keys": keys, "clicks": 0, "impressions": 1,
                            "ctr": 0.0, "position": 9.0})
            day += _td(days=1)
        self.asked.append((tuple(dims), row_limit, len(out)))
        return out[:row_limit], None


class _RecordingDB:
    """Records the parameters execute() was actually handed.

    ★ It also checks the SQL it is given: a fake cursor that accepts any string
    and reports rows written would pass while the caller built nonsense.
    """

    def __init__(self):
        self.params, self.commits = [], 0

    def cursor(self):
        return self

    def execute(self, sql, params):
        groups = sql.count("(%s,%s,%s,%s,%s,%s,%s)")
        assert groups == len(params) // 7, (
            f"placeholder groups ({groups}) do not match the parameters "
            f"supplied ({len(params)} = {len(params)/7:.1f} rows)")
        self.params.extend(params)

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def rollback(self):
        pass

    def days_written(self):
        return {p for p in self.params[0::7]}


def _run_ingest(rows_per_day, days=None, row_limit=None):
    """Execute the real `ingest_daily_performance` with the SHIPPED defaults."""
    fn = _func("ingest_daily_performance")
    from datetime import datetime, timedelta
    db, gsc = _RecordingDB(), _FakeGSC(rows_per_day)
    ns = {
        "datetime": datetime, "timedelta": timedelta,
        "DEFAULT_WINDOW_DAYS": _shipped_env_default_int("DEFAULT_WINDOW_DAYS"),
        "DEFAULT_ROW_LIMIT": _shipped_env_default_int("DEFAULT_ROW_LIMIT"),
        "_GSC_MAX_ROW_LIMIT": 25000, "_SEED_ROW_CEILING": 100000,
        "_UPSERT": ("INSERT INTO gsc_daily_performance VALUES {values} "
                    "ON CONFLICT DO NOTHING"),
        "_ensure_table": lambda: None,
        "_query_gsc": gsc,
        "get_db": lambda: db,
        "refresh_proven_pages": lambda token, **kw: {"success": True},
        "logger": logging.getLogger("t"),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    kwargs = {}
    if days is not None:
        kwargs["days"] = days
    if row_limit is not None:
        kwargs["row_limit"] = row_limit
    return ns["ingest_daily_performance"]("tok", **kwargs), db, gsc


def test_the_default_daily_run_writes_every_day_of_its_window():
    """The whole point of a trailing window: every day in it gets re-read."""
    out, db, _ = _run_ingest(rows_per_day=MEASURED_PEAK_PAGE_ROWS_PER_DAY)
    from datetime import datetime, timedelta
    end = datetime.utcnow().date()
    window_days = _shipped_env_default_int("DEFAULT_WINDOW_DAYS")
    expected = {(end - timedelta(days=i)).isoformat()
                for i in range(window_days + 1)}
    missing = expected - db.days_written()
    assert not missing, (
        f"{len(missing)} of {len(expected)} days in the trailing window were "
        f"never written: {sorted(missing)}. GSC fills the row budget from the "
        f"window START, so a budget short of the window drops its NEWEST days "
        f"— the days a trailing re-read exists to correct.")
    assert out["success"] is True, out.get("errors")
    assert out["window_truncated"] is None


def test_a_budget_truncated_window_is_an_error_not_a_silent_partial():
    """`rows_written` looks healthy on a truncated fetch — 50,000 rows that
    covered 14 days of a 62-day window. Only an explicit signal catches it."""
    out, _, _ = _run_ingest(rows_per_day=MEASURED_PEAK_PAGE_ROWS_PER_DAY,
                            days=5, row_limit=500)
    assert out["success"] is False, (
        "a fetch that stopped on its own budget did not read the window, and "
        "must not report success")
    assert "window_truncated" in out["errors"]
    assert out["window_truncated"], "the truncated grains must be named"
    assert set(out["window_truncated"]) >= {"page", "query"}


def test_a_seed_ceiling_truncation_is_reported_but_not_an_error():
    """A 480-day seed is capped by design; it must stay green.

    ★ rows_per_day must be big enough to ACTUALLY hit _SEED_ROW_CEILING —
    481 x 250 = 120,250 against a 100,000 ceiling. At 50/day nothing truncates
    and the assertion below is vacuous: it passed even with `_ceiling_bit`
    hard-coded False, which is the mutation that would redden every seed.
    """
    out, _, _ = _run_ingest(rows_per_day=250, days=480, row_limit=5000)
    assert out["rows_capped"] is not None
    assert out["window_truncated"], (
        "this guard is vacuous unless the ceiling actually bit")
    assert "window_truncated" not in (out["errors"] or {})
    assert out["success"] is True, out.get("errors")
