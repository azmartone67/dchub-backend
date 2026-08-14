"""PM Brief — tests on the real run path (2026-08-14).

Covers the contract that defines the tool:
  - a board that 500s renders UNMEASURED, and the brief still builds
  - an empty diff (first run) says FIRST RUN, not "all clear"
  - the escalation clock: 3 consecutive red snapshots -> escalates with age;
    board goes green -> leaves via WENT GREEN; the clock resets
  - dismissed items collapse but never vanish
  - ★ the no-write property is GUARDED, not just absent: the runtime guard
    raises before a foreign-table statement reaches the cursor, and the static
    scan fails if anyone adds a write path (mutation-tested — see the PR).

No DB, no network: readers are exercised by monkeypatching the module's single
HTTP entry point (_read_json), which is itself part of what is under test.
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from routes import pm_brief as pb  # noqa: E402


def _snap(lane_verdicts, date="2026-08-10"):
    """History row with one synthetic board holding the given lanes."""
    return {"date": date,
            "boards": {"freshness": {"status": "MEASURED", "reason": None,
                                     "lanes": dict(lane_verdicts),
                                     "headlines": {}}},
            "brief_md": ""}


def _today(lane_verdicts):
    return {"freshness": {"status": "MEASURED", "reason": None,
                          "lanes": dict(lane_verdicts), "headlines": {}}}


# ── three-valued reads ──────────────────────────────────────────────────────

def test_board_500_renders_unmeasured_and_brief_still_builds(monkeypatch):
    monkeypatch.setattr(pb, "_read_json", lambda *a, **k: (None, "HTTP 500"))
    board = pb._shell_reader("/api/v1/admin/freshness", "freshness")()
    assert board["status"] == "UNMEASURED"
    assert board["reason"] == "HTTP 500"
    assert board["lanes"] == {}  # never guessed
    brief = pb._build_brief({"freshness": board}, [], {}, "2026-08-14")
    assert "UNMEASURED" in brief
    assert "freshness — HTTP 500" in brief
    # an unreadable board must not surface as red OR green
    assert "freshness/" not in brief.split("## UNMEASURED")[0]


def test_reader_crash_is_unmeasured_not_a_verdict(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(pb, "_read_json", boom)
    boards = pb.collect_boards()  # the real run path run_collection() uses
    assert set(boards) == {b for b, _ in pb._BOARDS}  # none skipped silently
    for bid, board in boards.items():
        assert board["status"] == "UNMEASURED", bid
        assert board["reason"], bid  # always with a stated reason
        assert board["lanes"] == {}, bid  # never guessed


def test_malformed_board_body_is_unmeasured(monkeypatch):
    monkeypatch.setattr(pb, "_read_json",
                        lambda *a, **k: ({"unexpected": True}, None))
    board = pb._shell_reader("/api/v1/admin/adoption", "adoption")()
    assert board["status"] == "UNMEASURED"
    assert "lanes" in board["reason"] or board["reason"]


# ── first run ───────────────────────────────────────────────────────────────

def test_first_run_says_first_run_not_all_clear():
    brief = pb._build_brief(_today({"ingestion": pb.FAIL}), [], {},
                            "2026-08-14")
    assert "FIRST RUN" in brief
    assert "all clear" not in brief.lower()
    # day-one reds are shown as standing work orders with the clock at day 1
    assert "freshness/ingestion (day 1)" in brief


# ── the escalation clock ────────────────────────────────────────────────────

def test_three_consecutive_reds_escalate_with_age():
    hist = [_snap({"ingestion": pb.FAIL}, d)
            for d in ("2026-08-13", "2026-08-12", "2026-08-11")]
    brief = pb._build_brief(_today({"ingestion": pb.FAIL}), hist, {},
                            "2026-08-14")
    esc = brief.split("## ESCALATIONS")[1].split("##")[0]
    assert "freshness/ingestion — RED day 4" in esc


def test_two_days_red_does_not_escalate():
    hist = [_snap({"ingestion": pb.FAIL}, "2026-08-13")]
    brief = pb._build_brief(_today({"ingestion": pb.FAIL}), hist, {},
                            "2026-08-14")
    esc = brief.split("## ESCALATIONS")[1].split("##")[0]
    assert "freshness/ingestion" not in esc


def test_green_leaves_the_escalation_and_gets_credit():
    hist = [_snap({"ingestion": pb.FAIL}, d)
            for d in ("2026-08-13", "2026-08-12", "2026-08-11")]
    brief = pb._build_brief(_today({"ingestion": pb.PASS}), hist, {},
                            "2026-08-14")
    esc = brief.split("## ESCALATIONS")[1].split("##")[0]
    assert "freshness/ingestion" not in esc
    green = brief.split("## WENT GREEN")[1].split("##")[0]
    assert "freshness/ingestion" in green


def test_clock_resets_after_a_green_day():
    # red today, but yesterday was green: streak restarts at 1
    hist = [_snap({"ingestion": pb.PASS}, "2026-08-13"),
            _snap({"ingestion": pb.FAIL}, "2026-08-12"),
            _snap({"ingestion": pb.FAIL}, "2026-08-11")]
    assert pb._red_streak("freshness/ingestion",
                          pb._lane_map(_today({"ingestion": pb.FAIL})),
                          hist) == 1


def test_unmeasured_day_breaks_the_clock():
    # a day we could not read is not a day we measured red
    hist = [_snap({}, "2026-08-13"),  # lane absent that day
            _snap({"ingestion": pb.FAIL}, "2026-08-12")]
    assert pb._red_streak("freshness/ingestion",
                          pb._lane_map(_today({"ingestion": pb.FAIL})),
                          hist) == 1


def test_new_red_since_yesterday_is_listed():
    hist = [_snap({"ingestion": pb.PASS}, "2026-08-13")]
    brief = pb._build_brief(_today({"ingestion": pb.FAIL}), hist, {},
                            "2026-08-14")
    new = brief.split("## NEW REDS")[1].split("##")[0]
    assert "freshness/ingestion" in new


# ── dismissals ──────────────────────────────────────────────────────────────

def test_dismissed_items_collapse_but_never_vanish():
    dism = {"freshness/ingestion": {"reason": "vendor outage, accepted",
                                    "date": "2026-08-13"}}
    hist = [_snap({"ingestion": pb.FAIL}, d)
            for d in ("2026-08-13", "2026-08-12", "2026-08-11")]
    brief = pb._build_brief(_today({"ingestion": pb.FAIL}), hist, dism,
                            "2026-08-14")
    esc = brief.split("## ESCALATIONS")[1].split("##")[0]
    assert "freshness/ingestion" not in esc          # collapsed out of chase
    assert "## DISMISSED" in brief                   # ... but never vanished
    tail = brief.split("## DISMISSED")[1]
    assert "freshness/ingestion" in tail
    assert "vendor outage, accepted" in tail
    assert "2026-08-13" in tail


def test_brief_respects_line_cap():
    lanes = {f"lane_{i}": pb.FAIL for i in range(40)}
    brief = pb._build_brief(_today(lanes), [], {}, "2026-08-14")
    assert len(brief.splitlines()) <= pb._MAX_LINES


# ── ★ the no-write guard (runtime half) ─────────────────────────────────────

class _RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append(sql)


@pytest.mark.parametrize("sql", [
    "UPDATE market_power_scores SET verdict = 'PASS'",
    "INSERT INTO mcp_call_log (status) VALUES ('mpp_paid')",
    "DELETE FROM deals WHERE id = 1",
    "DROP TABLE discovered_facilities",
    "TRUNCATE dcpi_daily_snapshots",
    "ALTER TABLE pm_brief_snapshots_evil ADD COLUMN x INT",
    'UPDATE "public"."market_power_scores" SET verdict=1',
])
def test_guard_rejects_foreign_table_write(sql):
    cur = _RecordingCursor()
    with pytest.raises(pb.PMBriefWriteViolation):
        pb._guarded_execute(cur, sql)
    assert cur.calls == []  # the cursor never saw the statement


@pytest.mark.parametrize("sql", [
    "INSERT INTO pm_brief_snapshots (snapshot_date, boards) VALUES (1, 2)",
    "CREATE TABLE IF NOT EXISTS pm_brief_dismissals (item TEXT)",
    "SELECT snapshot_date FROM pm_brief_snapshots",
    "SELECT pg_try_advisory_xact_lock(814202602)",
])
def test_guard_allows_own_tables_and_reads(sql):
    cur = _RecordingCursor()
    pb._guarded_execute(cur, sql)
    assert cur.calls == [sql]


# ── ★ the no-write guard (static half) ──────────────────────────────────────
# These two tests are the mutation tripwire: ADD a write path to pm_brief.py
# (a raw cursor.execute outside _guarded_execute, a non-GET HTTP verb, or a
# write statement naming a board table) and one of them goes red.

def _module_source_and_tree():
    path = os.path.join(ROOT, "routes", "pm_brief.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    return src, ast.parse(src)


def test_static_all_db_executes_route_through_the_guard():
    src, tree = _module_source_and_tree()
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "execute"
                    and node.name != "_guarded_execute"):
                offenders.append((node.name, call.lineno))
    assert offenders == [], (
        f"raw .execute() outside _guarded_execute: {offenders} — every DB "
        f"statement must pass the no-write guard")


def test_static_no_http_verb_but_get():
    src, _ = _module_source_and_tree()
    bad = re.findall(r"\.\s*(post|put|delete|patch)\s*\(", src)
    assert bad == [], (
        f"pm_brief must be read-only over HTTP; found verbs: {bad}")


def test_static_every_write_literal_targets_pm_brief_tables():
    src, tree = _module_source_and_tree()
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = pb._WRITE_RE.match(node.value)
            if m:
                table = (m.group(2).strip().strip('"')
                         .split(".")[-1].lower())
                if table not in pb._ALLOWED_WRITE_TABLES:
                    offenders.append((node.lineno, table))
    assert offenders == [], (
        f"write statement(s) naming non-pm_brief tables: {offenders}")


def test_dismiss_is_owner_only_no_path_from_collector():
    """run_collection must have no code path into the dismissal writer — an
    item leaves the brief only via the board going green or the OWNER's
    explicit POST /dismiss."""
    src, tree = _module_source_and_tree()
    run_fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "run_collection")
    calls = {c.func.id for c in ast.walk(run_fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "pm_brief_dismiss" not in calls
    assert not any("dismiss" in c.lower() for c in calls
                   if c != "_load_dismissals"), calls
    # and nothing in the module ever DELETEs a dismissal (never vanish)
    assert not re.search(r"DELETE\s+FROM\s+pm_brief_dismissals", src,
                         re.IGNORECASE)


def test_lock_is_transaction_scoped_never_session_scoped():
    """★REGRESSION PIN (2026-08-14, live): pg_try_advisory_lock is SESSION
    scoped; behind the pooled endpoint with autocommit the unlock landed on a
    different backend, the lock leaked, and the next collection went green
    while writing nothing. The lock must be pg_try_advisory_xact_lock inside
    the explicit run_collection transaction, and a lock-busy skip must be
    ok:false so the cron cannot stay green on it."""
    _, tree = _module_source_and_tree()
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any("pg_try_advisory_xact_lock" in s for s in literals)
    session_locks = [s for s in literals
                     if re.search(r"pg_try_advisory_lock\s*\(", s)
                     or re.search(r"pg_advisory_unlock\s*\(", s)]
    assert session_locks == [], (
        "session-scoped advisory lock reintroduced — it leaks through the "
        "connection pooler and turns the daily collection into a no-op: "
        f"{session_locks}")
