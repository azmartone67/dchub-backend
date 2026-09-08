"""Growthfix wave (2026-07-24) — pins fixes 2-5 of the loop-audit fix-wave.

  2. competitor_gap_crawler: rotating sitemap window (the page cap used to
     re-read the SAME first 500 <loc>s every run → 15 zero-row runs while
     "green") + affirmative no_new_data beat.
  3. ingest_runs: no_new_data is an OK status, RESETS the consecutive_zero
     counter, and exempts the >=3-zero-row alarm; eia/osm workflows report it.
  5. growthfix master shell (#26): registered in main.py + cron_heartbeat,
     and its lane verdict never reads PASS when a critical check couldn't run.

CI-SAFETY: the unit-tests job installs ONLY pytest, so most tests here are
PURE (ast/string on source). competitor_gap_crawler is stdlib-only and is
imported directly; the shell needs flask and is importorskip-guarded.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IR = os.path.join(ROOT, "routes", "ingest_runs.py")
CG = os.path.join(ROOT, "routes", "competitor_gap_crawler.py")
GF = os.path.join(ROOT, "routes", "growthfix_master_shell.py")
CH = os.path.join(ROOT, "routes", "cron_heartbeat.py")
MAIN = os.path.join(ROOT, "main.py")
EIA = os.path.join(ROOT, ".github", "workflows", "eia-pricing-ingest.yml")
OSM = os.path.join(ROOT, ".github", "workflows", "osm-crawl.yml")


def _read(path):
    return open(path, encoding="utf-8").read()


def _set_literal(path, var_name):
    tree = ast.parse(_read(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == var_name
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{var_name} not found in {path}")


# ── 3 · ingest_runs no_new_data ──────────────────────────────────────

def test_ok_status_includes_no_new_data():
    ok = _set_literal(IR, "_OK_STATUS")
    assert "no_new_data" in ok and "no-new-data" in ok


def test_no_new_data_set_defined():
    nnd = _set_literal(IR, "_NO_NEW_DATA")
    assert nnd == {"no_new_data", "no-new-data"}


def test_beat_resets_counter_on_no_new_data():
    # LC6a: the sentinel moved out of the Flask handler into record_beat(), the one
    # shared upsert. Pin it THERE rather than anywhere in the file — record_beat
    # being the single consecutive_zero authority is exactly what the refactor buys,
    # so a future edit that reintroduces a second copy in the handler should fail.
    src = _read(IR)
    tree = ast.parse(src)
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "record_beat"]
    assert len(fns) == 1, "record_beat() must exist exactly once at module scope"
    body = ast.get_source_segment(src, fns[0]) or ""
    assert "in _NO_NEW_DATA and rows_sig == 0:" in body
    assert "rows_sig = 1" in body


def test_deadman_zero_row_alarm_exempts_no_new_data():
    src = _read(IR)
    assert 'if cz and cz >= 3 and (st or "").lower() not in _NO_NEW_DATA:' in src


def test_workflows_report_no_new_data():
    for path in (EIA, OSM):
        src = _read(path)
        assert "no_new_data" in src, f"{path} never reports no_new_data"
        assert '\\"status\\":\\"$STATUS\\"' in src, \
            f"{path} beat does not send the computed STATUS"


def test_no_new_data_is_earned_not_a_bare_zero_map():
    """2026-07-25 — no_new_data is an ASSERTION that zero was EXPECTED: it
    resets consecutive_zero and exempts the >=3-zero-row alarm. Mapping a bare
    rows==0 to it silences broken counters instead of fixing them. Both feeds
    had one: eia read total_records off an async 202 that has never carried it
    (0 forever, task never polled), and osm exited 0 on crawler errors. The
    status must come from a run that demonstrably completed and found nothing.
    """
    for path, zero_var in ((EIA, "COUNT"), (OSM, "RI")):
        src = _read(path)
        assert '[ "$%s" = "0" ] && STATUS="no_new_data"' % zero_var not in src, \
            f"{path} maps a bare rows==0 to no_new_data — it must be earned"
        assert "${BEAT_STATUS:-error}" in src, \
            f"{path} does not default to status=error when the run step " \
            "produced no outputs (silence must not read as healthy)"
        assert "if: always()" in src, \
            f"{path} beat step is skippable by a failed job — a skipped beat " \
            "is silence, and silence reads as healthy until the cadence expires"


# ── 2 · competitor gap rotating window ───────────────────────────────

def _sitemap_xml(n):
    locs = "".join(
        f"<url><loc>https://example.com/providers/op-{i}</loc></url>"
        for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + locs + "</urlset>")


# ── r-sweep-commit (2026-09-08) ─────────────────────────────────────
#
# competitor_gap_sweeps held ONE row, stamped 2026-07-29, while 1,744
# competitor_gap facility rows were ingested in the last 7 days. record_sweep
# never had a commit of its own: #1896 called it on the success path BEFORE
# persist_coverage_gaps, so its INSERT rode along on that function's commit;
# #1900 moved it into `finally`, which runs after every commit, and the call
# site closes the connection immediately afterwards.
#
# ★ The stub below models the ONE property that broke this — uncommitted work
#   is DISCARDED on close. A test that read the row back on the writing
#   connection would see it and pass, which is the bug itself.


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.pending.append(" ".join(str(sql).split()))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    """psycopg2 semantics, reduced to what this bug turns on."""

    def __init__(self, commit_raises=False):
        self.pending, self.committed = [], []
        self.commit_raises = commit_raises
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        if self.commit_raises:
            raise RuntimeError("connection already closed")
        self.committed.extend(self.pending)
        self.pending = []

    def rollback(self):
        self.pending = []

    def close(self):
        self.pending = []          # ★ uncommitted work is LOST
        self.closed = True


_SREC = {"parsed": 42, "true_gaps": 0, "gap_only": 0, "inserted": 0, "dup": 0,
         "status": 200, "error": None,
         "drops": {"dropped_existing": 40, "dropped_not_facility": 2}}
_P = {"locs_seen": 11859, "window_offset": 4321}


def _record(conn):
    import importlib
    cg = importlib.import_module("routes.competitor_gap_crawler")
    cg.record_sweep(conn, "cloudscene", _SREC, _P, window_size=500)


def test_the_sweep_row_survives_the_connection_close():
    """THE DEFECT. The call site records from `finally` and closes the
    connection on the next line, so a row that is merely INSERTed is gone."""
    conn = _FakeConn()
    _record(conn)
    conn.close()                       # exactly what the call site does next
    assert conn.committed, (
        "the sweep row was discarded on close — record_sweep did not commit, "
        "which is how competitor_gap_sweeps went 40 days without a row")
    assert any("competitor_gap_sweeps" in q for q in conn.committed), \
        conn.committed


def test_the_commit_lands_after_the_insert_not_before():
    """A FLOOR: a commit placed above the INSERT would leave the row pending
    and lose it just the same, so 'it calls commit' is not the assertion."""
    conn = _FakeConn()
    _record(conn)
    assert not conn.pending, f"work left uncommitted before close: {conn.pending}"
    assert len(conn.committed) >= 1, conn.committed


def test_a_failing_commit_still_never_costs_the_crawl():
    """The fail-soft contract the docstring promises is unchanged: a lost
    metric row is trivial, a lost crawl run is not."""
    conn = _FakeConn(commit_raises=True)
    _record(conn)                      # must not raise
    assert conn.committed == []


# ── r-placeholder-city-ingest (2026-09-07) ──────────────────────────
#
# Cloudscene buckets every facility it has no city for under a literal
# `/regional/` segment, per country. The slug parser deslugged that into the
# CITY "Regional" and wrote it as a place: 839 live rows, 35 countries, 839 of
# 839 with `regional` as the URL segment. Every URL below is a LIVE one.

_CLOUDSCENE_NO_CITY = (
    "https://cloudscene.com/data-center/united-kingdom/regional/"
    "custodian-data-centres-custodian-data-centre",
    "https://cloudscene.com/data-center/mexico/regional/hostdime-com-guadalajara",
    "https://cloudscene.com/data-center/india/regional/sify-sify-rabale-mumbai-dc-1-2",
)
# Cloudscene's REAL state-level labels use their own segment — 1,149 rows
# across 74 of them. Equality, never substring, is what keeps these.
_CLOUDSCENE_REAL = (
    ("https://cloudscene.com/data-center/united-states-of-america/"
     "connecticut-regional/tierpoint-waterbury", "Connecticut Regional", None),
    ("https://cloudscene.com/data-center/united-states-of-america/"
     "columbus-oh/cologix-col1", "Columbus", "OH"),
    ("https://cloudscene.com/data-center/japan/tokyo/"
     "ntt-communications-tokyo-no-6-data-center", "Tokyo", None),
)


def _parse_locs(urls):
    import importlib
    cg = importlib.import_module("routes.competitor_gap_crawler")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>")
    return cg.parse_competitor_sitemap(
        "cloudscene", "https://cloudscene.com/sitemap.xml",
        limit=50, _prefetched_text=xml)["parsed"]


def test_the_upstreams_no_city_bucket_is_written_as_null():
    """THE DEFECT, at the writer. tests/test_no_fabricated_facility_fields.py
    already states the rule: if the upstream did not say it, we publish NULL.
    It was enforced on country/status/power_mw and never on city."""
    rows = _parse_locs(_CLOUDSCENE_NO_CITY)
    assert len(rows) == len(_CLOUDSCENE_NO_CITY), rows
    for r in rows:
        assert r["city"] is None, r
    # FLOOR: the row is still INGESTED, with its country — nulling the city
    # must not drop a real facility, and the country is what keeps it
    # geocodable (_is_geocodable takes city OR state OR country).
    assert [r["country"] for r in rows] == ["GB", "MX", "IN"], rows
    assert all(r["name"] for r in rows), rows


def test_a_real_city_or_state_label_is_untouched():
    """The other half. 'Connecticut Regional' is one of 1,149 rows across 74
    real '<X> Regional' labels; a substring predicate would null every one."""
    rows = _parse_locs([u for u, _c, _s in _CLOUDSCENE_REAL])
    assert len(rows) == 3, rows
    for r, (_u, city, state) in zip(rows, _CLOUDSCENE_REAL):
        assert r["city"] == city, (r, city)
        assert r["state"] == state, (r, state)


def test_the_normalisation_runs_where_every_parser_converges():
    """Not inside the cloudscene parser. A per-parser copy is how the junk-slug
    predicate drifted into four disagreeing spellings (#4133), so this asserts
    the placeholder is cleared for a candidate that did NOT come from the
    cloudscene slug parser at all."""
    import importlib
    cg = importlib.import_module("routes.competitor_gap_crawler")
    src = open(CG, encoding="utf-8").read()
    body = src[src.index("def _parse_cloudscene_dc("):
               src.index("def parse_competitor_sitemap(")]
    assert "_is_placeholder_city" not in body, (
        "the placeholder test belongs at the convergence point, not inside "
        "one parser — a second copy is the drift this fix exists to end")
    conv = src[src.index("def parse_competitor_sitemap("):]
    assert "_is_placeholder_city(cand.get(\"city\"))" in conv, conv[:200]


def test_parser_offset_rotates_window():
    import importlib
    cg = importlib.import_module("routes.competitor_gap_crawler")
    xml = _sitemap_xml(10)
    out = cg.parse_competitor_sitemap(
        "testsrc", "https://example.com/sitemap.xml",
        limit=3, _prefetched_text=xml, offset=4)
    assert out["locs_seen"] == 10
    assert out["window_offset"] == 4
    urls = [c["source_url"] for c in out["parsed"]]
    assert urls == [f"https://example.com/providers/op-{i}" for i in (4, 5, 6)]
    # wrap: offset past the end comes back around, never an empty window
    out2 = cg.parse_competitor_sitemap(
        "testsrc", "https://example.com/sitemap.xml",
        limit=3, _prefetched_text=xml, offset=14)
    assert out2["window_offset"] == 4
    # offset=0 keeps the historical first-page behavior
    out3 = cg.parse_competitor_sitemap(
        "testsrc", "https://example.com/sitemap.xml",
        limit=3, _prefetched_text=xml, offset=0)
    assert [c["source_url"] for c in out3["parsed"]] == \
        [f"https://example.com/providers/op-{i}" for i in (0, 1, 2)]


def test_orchestrator_walks_window_daily():
    src = _read(CG)
    assert "tm_yday" in src, "no deterministic daily offset"
    assert "offset=_off" in src, "orchestrator never passes the offset"


def test_crawler_beats_no_new_data():
    src = _read(CG)
    assert '_status = "no_new_data"' in src


# ── 5 · growthfix master shell wiring + honesty ──────────────────────

def test_shell_registered_in_main():
    src = _read(MAIN)
    assert "growthfix_master_shell_bp" in src
    assert "register_blueprint(growthfix_master_shell_bp)" in src


def test_shell_cron_dispatch_registered():
    src = _read(CH)
    assert "growthfix_shell_daily" in src
    assert "/api/v1/admin/growthfix/master-tick" in src
    assert 'GROWTHFIX_SHELL_DISABLE") != "1"' in src


def test_shell_lane_verdict_honesty():
    pytest.importorskip("flask")
    from routes.growthfix_master_shell import _check, _lane_verdict
    # an indeterminate CRITICAL check must never render green
    assert _lane_verdict([_check("a", "a", None, "", critical=True)]) == "?"
    # any hard failure is FAIL
    assert _lane_verdict([_check("a", "a", True, "", critical=True),
                          _check("b", "b", False, "")]) == "FAIL"
    # all criticals affirmatively green (non-critical gauges may be "?")
    assert _lane_verdict([_check("a", "a", True, "", critical=True),
                          _check("b", "b", None, "")]) == "PASS"


def test_shell_sql_avoids_percent_trap():
    """★ psycopg2 trap: ANY literal percent in a statement executed without a
    params tuple attempts percent-substitution and 500s. The shell's SQL is
    literal-only, so its source must contain no percent-formatted SQL."""
    src = _read(GF)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "SELECT" in node.value.upper():
                assert "%" not in node.value, \
                    f"percent inside literal SQL: {node.value[:60]!r}"


def test_shell_age_days_accepts_text_timestamps():
    """★ house trap: coverage_gaps.created_at is TEXT — the shell's first
    live tick 500'd on '.tzinfo' of a str. _age_days must parse strings."""
    pytest.importorskip("flask")
    from routes.growthfix_master_shell import _age_days, _as_dt
    assert _age_days("2026-07-11 06:04:18.230977+00") is not None
    assert _age_days("2026-07-11T06:04:18Z") is not None
    assert _age_days("not a timestamp") is None
    assert _as_dt(None) is None


def test_shell_lane_crash_renders_indeterminate():
    pytest.importorskip("flask")
    from routes.growthfix_master_shell import _lane_verdict, _safe_lane

    def _boom(_c):
        raise AttributeError("'str' object has no attribute 'tzinfo'")
    checks = _safe_lane(_boom, None)
    assert _lane_verdict(checks) == "?"
    assert "lane crashed" in checks[0]["detail"]
