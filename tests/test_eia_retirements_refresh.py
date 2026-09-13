"""generator_retirements refreshes on a clock, and the refresh cannot lie (2026-09-12).

WHAT WAS MEASURED. eia_retirements.py said "monthly via POST
/api/jobs/eia-retirements … external cron caller". No workflow, scheduler slot or
job registry ever called that route, so the table held a single load from the
module's creation day: on 2026-09-12 /api/v1/retirement-headroom served the
2026-04 filing and served-table-freshness read the table frozen at 63 days.

WHAT THIS PINS, with no network and no database
(tests/test_eia_retirements_refresh_sql.py runs the transaction on a real
Postgres):
  1. the slot exists, fires once a day under CRAWLER_SCHEDULE=once, and its
     runner raises when a refresh fails, so the slot does not beat success;
  2. refresh_due opens on an empty table, a newer EIA period or a 28-day-old
     refresh — and on nothing else;
  3. a fetch that came back short is refused before a prune can run against it;
  4. normalize drops rows no refresh could ever update, and keeps a filed
     retirement it cannot parse out of the prune;
  5. the outcome beat reports failures and stays silent on nights nothing was
     due.
"""
import ast
import datetime as dt
import inspect
import logging
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import eia_retirements as er  # noqa: E402  (stdlib-only at import)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 12, 22, 0, tzinfo=UTC)
KEY = "EIA-KEY-NEVER-IN-A-URL"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _module_value(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"no module-level {name} — the extractor can no longer see it")


def _runner_node():
    src = _read("crawler_scheduler.py")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_eia_retirements":
            return src, node
    raise AssertionError("crawler_scheduler.py no longer defines _run_eia_retirements")


# ── 1. the slot ──────────────────────────────────────────────────────────
def test_the_refresh_has_a_daily_slot_that_fires_under_once_mode():
    """CRAWLER_SCHEDULE=once is deployed on dchub-worker, so only hour1 ever
    fires. A (0, 12) pair would silently lose its second leg."""
    tree = ast.parse(_read("crawler_scheduler.py"))
    rows = [r for r in ast.literal_eval(_module_value(tree, "SCHEDULE"))
            if r[2] == "eia_retirements"]
    assert len(rows) == 1, rows
    hour1, hour2, _name, handler = rows[0]
    assert hour1 == hour2, rows[0]
    assert handler == "_run_eia_retirements"
    runners = _module_value(tree, "_RUNNERS")
    registered = {k.value: v.id for k, v in zip(runners.keys, runners.values)}
    assert registered.get("eia_retirements") == "_run_eia_retirements", (
        "a SCHEDULE name missing from _RUNNERS is silently skipped every night")


def _exec_runner(monkeypatch, result):
    src, node = _runner_node()
    calls = []

    def run_if_due():
        calls.append("run_if_due")
        return result

    fake = types.ModuleType("eia_retirements")
    fake.run_if_due = run_if_due
    monkeypatch.setitem(sys.modules, "eia_retirements", fake)
    ns = {"logger": logging.getLogger("test_eia_retirements_refresh")}
    exec(ast.get_source_segment(src, node), ns)
    return ns["_run_eia_retirements"], calls


def test_a_failed_refresh_raises_so_the_slot_does_not_beat_success(monkeypatch):
    run, calls = _exec_runner(monkeypatch, {
        "ok": False, "error": "EIA 2026-07: 5000 of 10007 generator rows arrived"})
    with pytest.raises(RuntimeError, match="5000 of 10007"):
        run()
    assert calls == ["run_if_due"]


def test_a_night_with_nothing_due_returns_without_raising(monkeypatch):
    run, calls = _exec_runner(monkeypatch, {"ok": True, "due": False,
                                            "reason": "not due"})
    assert run()["due"] is False
    assert calls == ["run_if_due"]


def test_the_runner_calls_the_gated_entry_the_module_really_defines():
    """Binds the fake above to the real module: every attribute the runner
    reaches for must be a function eia_retirements.py defines, and the slot
    must take the GATED path — the forced backfill would re-pull EIA nightly."""
    _src, node = _runner_node()
    attrs = {n.attr for n in ast.walk(node)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
             and n.value.id == "eia_retirements"}
    defs = {n.name for n in ast.parse(_read("eia_retirements.py")).body
            if isinstance(n, ast.FunctionDef)}
    assert attrs == {"run_if_due"}, attrs
    assert attrs <= defs, attrs - defs


def test_the_manual_route_still_forces_a_refresh():
    assert inspect.signature(er.run_eia_retirements_ingest).parameters["force"].default is True
    src = _read("routes/jobs_routes.py")
    handler = next(n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.FunctionDef) and n.name == "job_eia_retirements")
    targets = [kw.value.id for n in ast.walk(handler) if isinstance(n, ast.Call)
               for kw in n.keywords
               if kw.arg == "target" and isinstance(kw.value, ast.Name)]
    assert targets == ["run_eia_retirements_ingest"], targets


# ── 2. the gate ──────────────────────────────────────────────────────────
def test_an_empty_table_is_due():
    assert er.refresh_due("2026-07", None, None, NOW)[0] is True


def test_a_newer_eia_period_is_due_the_same_night():
    due, why = er.refresh_due("2026-07", "2026-06", NOW - dt.timedelta(hours=20), NOW)
    assert due is True and "2026-07" in why


def test_periods_compare_across_a_year_and_a_two_digit_month():
    assert er.refresh_due("2026-10", "2026-09", NOW - dt.timedelta(days=1), NOW)[0] is True
    assert er.refresh_due("2027-01", "2026-12", NOW - dt.timedelta(days=1), NOW)[0] is True


def test_the_same_period_refreshed_recently_is_not_due():
    due, why = er.refresh_due("2026-06", "2026-06",
                              NOW - dt.timedelta(days=er.REFRESH_MAX_AGE_DAYS, seconds=-60), NOW)
    assert due is False and why.startswith("not due")


def test_the_same_period_is_due_again_at_the_max_age():
    assert er.refresh_due("2026-06", "2026-06",
                          NOW - dt.timedelta(days=er.REFRESH_MAX_AGE_DAYS), NOW)[0] is True


def test_an_older_eia_period_does_not_open_the_gate():
    assert er.refresh_due("2026-05", "2026-06", NOW - dt.timedelta(days=3), NOW)[0] is False


def test_an_unreachable_eia_leaves_the_age_rule_in_force():
    assert er.refresh_due(None, "2026-06", NOW - dt.timedelta(days=3), NOW)[0] is False
    assert er.refresh_due(None, "2026-06", NOW - dt.timedelta(days=40), NOW)[0] is True


# ── 3. the fetch ─────────────────────────────────────────────────────────
def _pager(total, pages):
    calls = []

    def get_json(url, timeout=None, tries=None, headers=None):
        calls.append((url, headers))
        i = len(calls) - 1
        return {"response": {"total": str(total),
                             "data": pages[i] if i < len(pages) else []}}
    return get_json, calls


def _gen(i, month="2027-06"):
    return {"plantid": str(1000 + i), "generatorid": "G1",
            "planned-retirement-year-month": month}


def test_a_complete_fetch_keeps_only_filed_retirements_and_sends_the_key_as_a_header():
    page1 = [_gen(i, "2027-06" if i % 2 else None) for i in range(er.PAGE)]
    page2 = [_gen(er.PAGE + i) for i in range(10)]
    get_json, calls = _pager(er.PAGE + 10, [page1, page2])
    rows = er.fetch_planned_retirements(KEY, "2026-07", get_json=get_json)
    assert len(calls) == 2
    assert len(rows) == er.PAGE // 2 + 10
    assert all(KEY not in url for url, _ in calls)
    assert all(headers == {"X-Api-Key": KEY} for _, headers in calls)


def test_a_page_that_never_arrives_refuses_the_whole_refresh():
    get_json, _ = _pager(er.PAGE * 2 + 7, [[_gen(i) for i in range(er.PAGE)], []])
    with pytest.raises(er.RefreshRefused, match="5000 of 10007"):
        er.fetch_planned_retirements(KEY, "2026-07", get_json=get_json)


def test_a_response_reporting_no_generators_is_refused():
    get_json, _ = _pager(0, [[]])
    with pytest.raises(er.RefreshRefused):
        er.fetch_planned_retirements(KEY, "2026-07", get_json=get_json)


# ── 4. normalize ─────────────────────────────────────────────────────────
def test_rows_no_refresh_could_ever_update_are_dropped():
    rows = [
        {"plantid": "", "generatorid": "1", "planned-retirement-year-month": "2027-01"},
        {"plantid": "77", "generatorid": "  ", "planned-retirement-year-month": "2027-01"},
        {"plantid": "x7", "generatorid": "2", "planned-retirement-year-month": "2027-01"},
        {"plantid": "78", "generatorid": "3", "planned-retirement-year-month": ""},
    ]
    records, asserted, stats = er.normalize(rows, "2026-07")
    assert records == [] and asserted == set()
    assert stats["no_key"] == 3


def test_an_unparseable_month_stays_out_of_the_prune_and_is_not_written():
    rows = [{"plantid": "5", "generatorid": "A", "planned-retirement-year-month": "2027-13"}]
    records, asserted, stats = er.normalize(rows, "2026-07")
    assert records == []
    assert asserted == {(5, "A")}
    assert stats["bad_month"] == 1


def test_the_last_row_for_a_generator_wins_as_it_did_row_by_row():
    rows = [
        {"plantid": "5", "generatorid": "A", "planned-retirement-year-month": "2027-01",
         "nameplate-capacity-mw": "10"},
        {"plantid": "5", "generatorid": "A", "planned-retirement-year-month": "2028-02",
         "nameplate-capacity-mw": ".9"},
    ]
    records, asserted, stats = er.normalize(rows, "2026-07")
    assert len(records) == 1 and asserted == {(5, "A")}
    assert stats["duplicates"] == 1
    assert records[0][7] == 0.9 and records[0][11] == "2028-02-01"


def test_a_record_lines_up_with_the_upsert_column_list():
    """A record whose arity or order drifted from the INSERT would fail every
    refresh on its first page, in production, at 00:00 UTC."""
    cols = re.search(r"INSERT INTO generator_retirements\s*\((.*?)\)",
                     er._UPSERT_SQL, re.S).group(1)
    names = [c.strip() for c in cols.split(",")]
    records, _, _ = er.normalize([{
        "plantid": "5", "generatorid": "A", "planned-retirement-year-month": "2027-01",
        "plantName": "Plant", "stateid": "TX", "county": "Harris",
        "latitude": "29.7", "longitude": "-95.3", "nameplate-capacity-mw": "650",
        "technology": "Conventional Steam Coal", "prime_mover_code": "ST",
        "balancing_authority_code": "ERCO"}], "2026-07")
    assert len(records[0]) == len(names) == 14
    rec = dict(zip(names, records[0]))
    assert rec == {
        "eia_plant_id": 5, "generator_id": "A", "plant_name": "Plant", "state": "TX",
        "county": "Harris", "lat": 29.7, "lng": -95.3, "capacity_mw": 650.0,
        "fuel_category": "Conventional Steam Coal", "prime_mover": "ST",
        "ba_code": "ERCO", "retirement_date": "2027-01-01",
        "status": "planned_retirement", "source_month": "2026-07"}


# ── 5. the outcome beat ──────────────────────────────────────────────────
def _recorder():
    beats = []
    return beats, (lambda feed, **kw: beats.append(dict(kw, feed=feed)))


def test_the_outcome_feed_uses_the_watchers_monthly_cadence():
    workflows = ast.literal_eval(_module_value(ast.parse(_read("tools/deadman/watch.py")),
                                               "WORKFLOWS"))
    assert er.CADENCE_HOURS == workflows["gem-refresh.yml"] == \
        workflows["planned-generators-ingest.yml"]


def test_missing_config_is_an_error_outcome_not_a_quiet_no_op(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    beats, beat = _recorder()
    out = er.run_eia_retirements_ingest(beat=beat)
    assert out == {"ok": False, "error": "missing_config"}
    assert [(b["feed"], b["status"], b["cad"]) for b in beats] == \
        [(er.FEED, "error", er.CADENCE_HOURS)]


def test_a_night_with_nothing_due_fetches_nothing_and_beats_nothing(monkeypatch):
    monkeypatch.setattr(er, "_latest_period", lambda key: "2026-07")
    monkeypatch.setattr(er, "_read_state", lambda url: ("2026-07", NOW - dt.timedelta(days=2)))
    monkeypatch.setattr(er, "fetch_planned_retirements",
                        lambda *a, **k: pytest.fail("fetched on a night nothing was due"))
    beats, beat = _recorder()
    out = er.run_eia_retirements_ingest("postgres://unused", KEY, force=False,
                                        beat=beat, now=NOW)
    assert out["ok"] is True and out["due"] is False
    assert beats == []


def test_an_eia_blip_on_a_night_nothing_was_due_is_not_a_failure(monkeypatch):
    def unreachable(key):
        raise OSError("name resolution failed")
    monkeypatch.setattr(er, "_latest_period", unreachable)
    monkeypatch.setattr(er, "_read_state", lambda url: ("2026-07", NOW - dt.timedelta(days=2)))
    beats, beat = _recorder()
    out = er.run_eia_retirements_ingest("postgres://unused", KEY, force=False,
                                        beat=beat, now=NOW)
    assert out["ok"] is True and out["due"] is False
    assert "lookup failed" in out["reason"]
    assert beats == []


def test_an_unreachable_eia_when_a_refresh_is_due_is_an_error(monkeypatch):
    def unreachable(key):
        raise OSError("name resolution failed")
    monkeypatch.setattr(er, "_latest_period", unreachable)
    monkeypatch.setattr(er, "_read_state", lambda url: ("2026-06", NOW - dt.timedelta(days=40)))
    beats, beat = _recorder()
    out = er.run_eia_retirements_ingest("postgres://unused", KEY, force=False,
                                        beat=beat, now=NOW)
    assert out["ok"] is False and "lookup failed" in out["error"]
    assert [b["status"] for b in beats] == ["error"]
