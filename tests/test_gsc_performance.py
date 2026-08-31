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


def test_admin_ingest_is_auth_gated():
    fn = _func("admin_ingest")
    assert fn is not None
    names = []
    for d in fn.decorator_list:
        if isinstance(d, ast.Name):
            names.append(d.id)
        elif isinstance(d, ast.Attribute):
            names.append(d.attr)
        elif isinstance(d, ast.Call):
            f = d.func
            names.append(getattr(f, "id", getattr(f, "attr", "")))
    assert "require_internal_or_admin" in names, \
        "the ingest route must not be public"


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
