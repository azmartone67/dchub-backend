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
    assert "freshness/ingestion (red day 1)" in brief


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
    # red today, but yesterday was MEASURED green: streak restarts at 1.
    # A measured PASS is the ONLY thing that resets the clock (Lens A3/B4).
    hist = [_snap({"ingestion": pb.PASS}, "2026-08-13"),
            _snap({"ingestion": pb.FAIL}, "2026-08-12"),
            _snap({"ingestion": pb.FAIL}, "2026-08-11")]
    assert pb._red_streak("freshness/ingestion",
                          pb._lane_map(_today({"ingestion": pb.FAIL})),
                          hist, "2026-08-14") == (1, 0)


def test_unmeasured_day_holds_the_clock_neither_extends_nor_resets():
    # ★ Lens A3 DESIGN DECISION (supersedes the old "breaks the clock" pin):
    # a day we could not read is not a day we measured red (must NOT extend),
    # and it must NOT reset either — the old reset let a flaky reader
    # permanently prevent escalation (Lens B4). It HOLDS, counted as a gap.
    hist = [_snap({}, "2026-08-13"),  # lane absent that day: UNMEASURED
            _snap({"ingestion": pb.FAIL}, "2026-08-12")]
    assert pb._red_streak("freshness/ingestion",
                          pb._lane_map(_today({"ingestion": pb.FAIL})),
                          hist, "2026-08-14") == (2, 1)


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


# ═════════ the three defeated lenses (2026-08-14) — one test per defect ══════

def _boards_multi(freshness_lanes, adoption_lanes=None, rbd=None,
                  lane_meta=None):
    b = {"freshness": {"status": "MEASURED", "reason": None,
                       "lanes": dict(freshness_lanes), "headlines": {}}}
    if adoption_lanes is not None:
        b["adoption"] = {"status": "MEASURED", "reason": None,
                         "lanes": dict(adoption_lanes),
                         "red_by_design": list(rbd or []),
                         "lane_meta": dict(lane_meta or {}),
                         "headlines": {}}
    return b


# ── Lens A1: somebody chases the PM ─────────────────────────────────────────

def test_a1_pm_brief_workflow_registered_in_deadman_watch():
    """pm-brief-daily.yml must sit in the deadman WORKFLOWS registry with a
    cadence the watcher can actually keep green (mutation: remove the entry
    or set an impossible cadence -> red)."""
    import importlib.util
    path = os.path.join(ROOT, "tools", "deadman", "watch.py")
    spec = importlib.util.spec_from_file_location("deadman_watch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "pm-brief-daily.yml" in mod.WORKFLOWS, (
        "pm-brief-daily.yml absent from the deadman registry — a dead cron "
        "(GH 60-day auto-disable, deleted secret) would alarm nothing")
    cad = mod.WORKFLOWS["pm-brief-daily.yml"]
    assert cad >= 24, "daily loop: cadence must cover a full day"
    assert 2 * cad >= mod.WATCH_INTERVAL_H * mod.WATCH_MARGIN, (
        "cadence tighter than the watcher can keep green (false-red on drift)")


def test_a1_run_collection_posts_ingest_beat_after_commit():
    """The collector must post its OWN liveness beat (feed
    pm-brief-collection) — and only AFTER the commit, so a beat never
    vouches for a write that did not land. Mutation: drop the _beat_ledger
    call from run_collection -> red."""
    src, tree = _module_source_and_tree()
    run_fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "run_collection")
    calls = [c for c in ast.walk(run_fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id == "_beat_ledger"]
    assert calls, "run_collection never beats the deadman ledger (Lens A1)"
    commit_line = min(c.end_lineno for c in ast.walk(run_fn)
                      if isinstance(c, ast.Call)
                      and isinstance(c.func, ast.Attribute)
                      and c.func.attr == "commit")
    assert all(c.lineno > commit_line for c in calls), (
        "beat must land AFTER the commit — a pre-commit beat reports "
        "liveness for a collection that may not have persisted")


def test_a1_beat_posts_to_ingest_runs_with_producer_feed_name(monkeypatch):
    """Exercise _beat_ledger on the real path: it must POST the
    pm-brief-collection feed to the ingest-runs beat URL, and a beat failure
    must never raise (fail-open)."""
    sent = {}

    class _FakeResp:
        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["method"] = req.get_method()
        sent["body"] = json.loads(req.data.decode())
        return _FakeResp()

    import json
    monkeypatch.setattr(pb._urlreq, "urlopen", fake_urlopen)
    pb._beat_ledger("snapshot 2026-08-14")
    assert sent["url"].endswith("/api/v1/admin/ingest-runs/beat")
    assert sent["method"] == "POST"
    assert sent["body"]["feed"] == "pm-brief-collection"
    assert sent["body"]["cadence_hours"] >= 24

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(pb._urlreq, "urlopen", boom)
    pb._beat_ledger("must not raise")  # fail-open


def test_a1_post_capability_exists_only_inside_the_beat():
    """★ guard EXTENSION, not weakening: the module's single POST lives in
    _beat_ledger and can only hit the ingest-runs beat URL. Add any other
    method="POST" / urlopen and this goes red."""
    src, tree = _module_source_and_tree()
    beat_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_beat_ledger")
    beat_src = ast.get_source_segment(src, beat_fn)
    assert src.count('method="POST"') == 1
    assert beat_src.count('method="POST"') == 1
    assert src.count("urlopen") == beat_src.count("urlopen") >= 1
    assert "/api/v1/admin/ingest-runs/beat" in beat_src
    # and the beat writes no board: it names only its own feed
    assert '"feed": "pm-brief-collection"' in beat_src


# ── Lens A2: a week-old brief must not read as "latest" ─────────────────────

def test_a2_stale_snapshot_gets_banner_fresh_does_not():
    import datetime as dt
    today = dt.date(2026, 8, 14)
    age, banner = pb._staleness_banner("2026-08-07", today=today)
    assert age == 7
    assert "STALE" in banner and "7 days old" in banner
    assert "collector may be dead" in banner
    # <= 1 day old is fresh: yesterday's snapshot before today's cron is fine
    for fresh in ("2026-08-14", "2026-08-13"):
        age, banner = pb._staleness_banner(fresh, today=today)
        assert banner == "", fresh
    # unparseable date must fail LOUD (banner), never render as fresh
    age, banner = pb._staleness_banner("not-a-date")
    assert "STALE" in banner


def test_a2_latest_route_wires_the_banner_and_json_fields():
    """Mutation: serve row[2] without the banner, or drop stale/age from the
    JSON branch -> red. Static on the route because exercising it needs a
    live DB; _staleness_banner itself is tested above on real inputs."""
    src, tree = _module_source_and_tree()
    route_fn = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "pm_brief_latest")
    seg = ast.get_source_segment(src, route_fn)
    assert "_staleness_banner(" in seg, "latest route never checks age"
    assert "stale=" in seg and "snapshot_age_days=" in seg, (
        "?format=json must carry the same staleness fields")
    assert re.search(r"banner\s*\+", seg), (
        "markdown response must be banner-prefixed")


# ── Lens A3 + B4: gaps HOLD the streak (extend -> lie; reset -> unchaseable) ─

def test_a3_missing_snapshot_day_holds_the_streak_and_brief_says_gap():
    # snapshots 08-10/08-11/08-13 red + today red; 08-12 never measured.
    # The defective code said "RED day 4" under a "consecutive MEASURED days"
    # header. Correct: 4 MEASURED red days, 1 gap — and the brief SAYS so.
    hist = [_snap({"ingestion": pb.FAIL}, "2026-08-13"),
            _snap({"ingestion": pb.FAIL}, "2026-08-11"),
            _snap({"ingestion": pb.FAIL}, "2026-08-10")]
    assert pb._red_streak("freshness/ingestion",
                          pb._lane_map(_today({"ingestion": pb.FAIL})),
                          hist, "2026-08-14") == (4, 1)
    brief = pb._build_brief(_today({"ingestion": pb.FAIL}), hist, {},
                            "2026-08-14")
    esc = brief.split("## ESCALATIONS")[1].split("##")[0]
    assert "RED 4 measured days (1 gap)" in esc
    # and the header itself no longer promises "consecutive"
    assert "measured days" in brief.split("\n## ESCALATIONS")[1].split("\n")[0]


def test_b4_unmeasured_day_does_not_reset_escalation():
    # 3 measured reds around an unmeasured day must still escalate: the old
    # reset meant a flaky reader could permanently prevent escalation.
    hist = [_snap({}, "2026-08-13"),                     # unmeasured: HOLD
            _snap({"ingestion": pb.FAIL}, "2026-08-12"),
            _snap({"ingestion": pb.FAIL}, "2026-08-11")]
    n, gaps = pb._red_streak("freshness/ingestion",
                             pb._lane_map(_today({"ingestion": pb.FAIL})),
                             hist, "2026-08-14")
    assert (n, gaps) == (3, 1)
    brief = pb._build_brief(_today({"ingestion": pb.FAIL}), hist, {},
                            "2026-08-14")
    esc = brief.split("## ESCALATIONS")[1].split("##")[0]
    assert "freshness/ingestion" in esc, (
        "3 measured red days must escalate even across a gap")


def test_a3_interior_gaps_only_and_pass_still_resets():
    # a measured PASS older than the gap still resets; trailing unmeasured
    # days beyond the oldest measured red are not counted as gaps
    hist = [_snap({}, "2026-08-13"),
            _snap({"ingestion": pb.PASS}, "2026-08-12"),
            _snap({"ingestion": pb.FAIL}, "2026-08-11")]
    assert pb._red_streak("freshness/ingestion",
                          pb._lane_map(_today({"ingestion": pb.FAIL})),
                          hist, "2026-08-14") == (1, 0)


# ── Lens B1: capped sections must count what they cut ───────────────────────

def test_b1_escalations_overflow_is_marked_never_silent():
    lanes = {f"lane_{i:02d}": pb.FAIL for i in range(8)}
    hist = [_snap(lanes, d)
            for d in ("2026-08-13", "2026-08-12", "2026-08-11")]
    brief = pb._build_brief(_today(lanes), hist, {}, "2026-08-14")
    esc = brief.split("## ESCALATIONS")[1].split("##")[0]
    assert "+2 more (see ?format=json)" in esc, (
        "8 escalated, cap 6: the 2 cut must be COUNTED, not invisible")


def test_b1_new_reds_overflow_is_marked():
    lanes = {f"lane_{i:02d}": pb.FAIL for i in range(11)}
    hist = [_snap({k: pb.PASS for k in lanes}, "2026-08-13")]
    brief = pb._build_brief(_today(lanes), hist, {}, "2026-08-14")
    new = brief.split("## NEW REDS")[1].split("##")[0]
    assert "+3 more (see ?format=json)" in new


# ── Lens B2: the brief is a complete red inventory ──────────────────────────

def test_b2_every_red_lane_appears_in_standing_reds_with_age():
    # 10 day-2 reds: not new, not escalated, at most 3 in the top slots —
    # the defect was 7 of these appearing NOWHERE. Every one must be in
    # STANDING REDS with its age.
    lanes = {f"lane_{i:02d}": pb.FAIL for i in range(10)}
    hist = [_snap(lanes, "2026-08-13")]
    brief = pb._build_brief(_today(lanes), hist, {}, "2026-08-14")
    standing = brief.split("## STANDING REDS")[1].split("\n## ")[0]
    for k in lanes:
        assert f"freshness/{k} — red day 2" in standing, k


# ── Lens B3: dismissals — all in JSON, counted in markdown ──────────────────

def test_b3_dismissals_past_five_are_counted_in_markdown():
    dism = {f"freshness/lane_{i}": {"reason": f"accepted {i}",
                                    "date": "2026-08-10"} for i in range(9)}
    brief = pb._build_brief(_today({"ok_lane": pb.PASS}), [], dism,
                            "2026-08-14")
    tail = brief.split("## DISMISSED")[1]
    assert "+4 more (see ?format=json)" in tail, (
        '"collapsed but never vanish" must stay true past 5')


def test_b3_json_branch_always_carries_all_dismissals():
    """Mutation: drop dismissals= from the format=json response -> red."""
    src, tree = _module_source_and_tree()
    route_fn = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "pm_brief_latest")
    seg = ast.get_source_segment(src, route_fn)
    assert "dismissals=_load_dismissals(cur)" in seg, (
        "?format=json must always carry ALL dismissals (Lens B3)")


# ── Lens C1: top three is value-ranked, not alphabetical ────────────────────

def test_c1_red_by_design_deprioritized_diagnosable_ranked_first():
    # Alphabetically, adoption/* wins; by value, the diagnosable freshness
    # lane (escalated, critical failing check) must take slot 1 and the
    # red_by_design adoption lanes must sit in WORK ORDERS, not the slots.
    boards = _boards_multi(
        {"zz_diagnosable": pb.FAIL},
        adoption_lanes={"aa_conversion": pb.FAIL, "ab_identity": pb.FAIL},
        rbd=["aa_conversion", "ab_identity"],
        lane_meta={"aa_conversion": {"critical_fail": False,
                                     "failing_checks": 3},
                   "ab_identity": {"critical_fail": False,
                                   "failing_checks": 2}})
    hist = [{"date": d, "boards": boards, "brief_md": ""}
            for d in ("2026-08-13", "2026-08-12", "2026-08-11")]
    brief = pb._build_brief(boards, hist, {}, "2026-08-14")
    top = brief.split("## TOP THREE")[1].split("\n## ")[0]
    slot1 = next(ln for ln in top.splitlines() if ln.startswith("1."))
    assert "freshness/zz_diagnosable" in slot1, (
        f"alphabetical ranking is back — slot 1 was: {slot1}")
    assert "adoption/aa_conversion" not in top
    assert "adoption/ab_identity" not in top
    wo = brief.split("## WORK ORDERS")[1].split("\n## ")[0]
    assert "adoption/aa_conversion" in wo and "adoption/ab_identity" in wo


def test_c1_critical_failing_check_outranks_noncritical_same_age():
    boards = _boards_multi(
        {}, adoption_lanes={"aa_noncrit": pb.FAIL, "zz_crit": pb.FAIL},
        rbd=[],
        lane_meta={"aa_noncrit": {"critical_fail": False,
                                  "failing_checks": 1},
                   "zz_crit": {"critical_fail": True, "failing_checks": 1}})
    brief = pb._build_brief(boards, [{"date": "2026-08-13", "boards": boards,
                                      "brief_md": ""}], {}, "2026-08-14")
    top = brief.split("## TOP THREE")[1].split("\n## ")[0]
    slot1 = next(ln for ln in top.splitlines() if ln.startswith("1."))
    assert "zz_crit" in slot1, (
        f"critical failing check must outrank non-critical: {slot1}")


def test_c1_identical_start_commands_collapse_into_one_slot():
    # three same-board reds share the generic "GET .../admin/freshness"
    # start command — they must occupy ONE slot naming all lanes, freeing
    # slots for other boards' actions
    boards = _boards_multi({"a_red": pb.FAIL, "b_red": pb.FAIL,
                            "c_red": pb.FAIL})
    brief = pb._build_brief(boards, [{"date": "2026-08-13", "boards": boards,
                                      "brief_md": ""}], {}, "2026-08-14")
    top = brief.split("## TOP THREE")[1].split("\n## ")[0]
    slot_lines = [ln for ln in top.splitlines()
                  if re.match(r"^\d\.", ln)]
    starts = [ln.split("— start:")[1].strip() for ln in slot_lines
              if "— start:" in ln]
    assert len(starts) == len(set(starts)), (
        f"two slots share a start command: {starts}")
    slot1 = slot_lines[0]
    assert "also covers:" in slot1
    assert "freshness/b_red" in slot1 and "freshness/c_red" in slot1


# ── Lens C2: standing items age from known_since, never "(day 1)" ───────────

def test_c2_seeded_standing_items_age_from_known_since():
    brief = pb._build_brief(_today({"ok": pb.PASS}), [], {}, "2026-08-14")
    top = brief.split("## TOP THREE")[1].split("\n## ")[0]
    # standing/hive-submission is seeded known_since 2026-08-08 -> day 7
    hive = next(ln for ln in top.splitlines() if "Hive" in ln)
    assert "known since 2026-08-08, day 7" in hive, hive
    assert "(day 1)" not in hive
    # every _KNOWN_STANDING entry must carry a known_since date
    for key, entry in pb._KNOWN_STANDING.items():
        assert len(entry) >= 4 and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", entry[3]), (
            f"{key} has no known_since date (Lens C2)")
