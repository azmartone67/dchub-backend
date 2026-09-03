"""The lane triage registry must stay bonded to the shells and to DEFERRED.

★ WHAT THIS PROTECTS. routes/lane_triage.py answers one question per lane:
can an ENGINEER clear this red, or not? Measured 2026-09-02, of the 10 red
`*-shell-daily` feeds, lanes like loop_control/agent_identity ("no one caller
is >40pct" — chain-hire is 66.8%) and agent_pay/demand ("a REAL agent has ever
asked to pay") are CORRECT and unclearable by code. A board that is mostly
unclearable red trains everyone to scroll past all of it, which is how
failover-canary sat red while the DR mirror drifted 112 commits behind.

★ THE TWO WAYS THIS REGISTRY ROTS, both guarded here:
  1. A lane is renamed or deleted in its shell and its entry lingers,
     describing a lane that no longer exists.
  2. Someone types a class that is not in the vocabulary; with a plain dict
     literal that silently invents a sixth class.

★ AND THE BOND. The vocabulary was PROMOTED from
audit_closure_master_shell.DEFERRED (79 findings classified since 2026-08),
not invented. test_deferred_classes_are_a_subset keeps the two from drifting
into two vocabularies for one concept — which is the drift mechanism this
whole session was chasing.

Behavioural except where noted: the existence check reads the shell sources,
because that IS the coupling being asserted.
"""
import ast
import io
import os
import re

import pytest

from routes.lane_triage import (
    CODE_ACTIONABLE,
    LANE_CLASSES,
    LANE_TRIAGE,
    UNCLASSIFIED_SHELLS,
    classify,
    is_code_actionable,
    split_lanes,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES = os.path.join(ROOT, "routes")


def _shell_source(shell: str) -> str:
    p = os.path.join(ROUTES, f"{shell}_master_shell.py")
    assert os.path.isfile(p), f"no shell file for {shell!r}: {p}"
    return io.open(p, encoding="utf-8").read()


# ── the vocabulary ────────────────────────────────────────────────────
def test_every_class_used_is_in_the_vocabulary():
    """Catches the typo that would silently invent a sixth class."""
    bad = {k: v[0] for k, v in LANE_TRIAGE.items() if v[0] not in LANE_CLASSES}
    assert not bad, f"unknown class(es): {bad}"


def test_code_actionable_is_a_subset_of_the_vocabulary():
    assert CODE_ACTIONABLE <= set(LANE_CLASSES)


def test_commercial_is_not_code_actionable():
    """★ The whole point. If 'commercial' ever becomes code-actionable the
    registry stops distinguishing anything."""
    assert "commercial" not in CODE_ACTIONABLE
    assert "owner-flag" not in CODE_ACTIONABLE


def test_every_entry_carries_evidence_not_just_a_class():
    """A class with no `why` is an assertion nobody can check."""
    thin = [k for k, v in LANE_TRIAGE.items() if len(v[1].strip()) < 40]
    assert not thin, f"entries with no real rationale: {thin}"


# ── the bond to audit_closure's existing vocabulary ───────────────────
def test_deferred_classes_are_a_subset_of_the_vocabulary():
    """★ THE ANTI-DRIFT BOND. audit_closure.DEFERRED has classified 79
    findings with these words since 2026-08. If it grows a class this module
    does not know, we have two vocabularies for one concept — exactly the
    drift this registry exists to end."""
    src = _shell_source("audit_closure")
    used = set(re.findall(r'"SH52-\d+":\s*\("([a-z-]+)"', src))
    assert used, "found no DEFERRED classes — the regex has rotted"
    unknown = used - set(LANE_CLASSES)
    assert not unknown, (
        f"audit_closure.DEFERRED uses class(es) {sorted(unknown)} that "
        f"lane_triage.LANE_CLASSES does not define")


# ── the bond to the shells themselves ─────────────────────────────────
def test_every_classified_lane_still_exists_in_its_shell():
    """★ ANTI-ROT. A renamed or deleted lane must FAIL here, not linger as an
    entry describing something that is gone. Accepts either convention the
    shells use: a lane id string, or a `_lane_<id>` function."""
    missing = []
    for shell, lane in LANE_TRIAGE:
        src = _shell_source(shell)
        by_id = f'"{lane}"' in src
        by_fn = re.search(rf"def _lane_{re.escape(lane)}\b", src) is not None
        if not (by_id or by_fn):
            missing.append(f"{shell}/{lane}")
    assert not missing, f"classified lanes absent from their shell: {missing}"


def test_the_shell_scan_actually_found_lanes():
    """Guards the guard: if _shell_source started returning '' every
    assertion above would pass vacuously."""
    assert len(LANE_TRIAGE) >= 30
    assert len(_shell_source("loop_control")) > 10_000


def test_unclassified_shells_are_named_with_a_reason():
    """Silence about a shell must be deliberate and explained."""
    assert "audit_closure" in UNCLASSIFIED_SHELLS
    assert len(UNCLASSIFIED_SHELLS["audit_closure"]) > 30
    assert not any(s == "audit_closure" for s, _ in LANE_TRIAGE), \
        "audit_closure is declared unclassified but has entries"


# ── behaviour ─────────────────────────────────────────────────────────
def test_a_known_signal_lane_is_not_code_actionable():
    klass, why = classify("loop_control", "agent_identity")
    assert klass == "commercial"
    assert is_code_actionable("loop_control", "agent_identity") is False
    assert "66.8" in why


def test_a_known_defect_lane_is_code_actionable():
    """★ THE GREEN DIRECTION. Without this, classifying EVERYTHING as
    not-actionable would satisfy the rest of this file."""
    assert classify("agent_pay", "pricing")[0] == "build"
    assert is_code_actionable("agent_pay", "pricing") is True


def test_a_miscalibrated_check_is_code_actionable_but_not_a_system_defect():
    """`instrument` red is cleared by fixing the CHECK — still engineering."""
    assert classify("loop_control", "counter_canon")[0] == "instrument"
    assert is_code_actionable("loop_control", "counter_canon") is True


def test_an_unclassified_lane_is_none_never_false():
    """★ None is 'unknown'. Returning False would quietly assert that an
    unclassified lane is somebody else's problem."""
    assert classify("loop_control", "no_such_lane") is None
    assert is_code_actionable("loop_control", "no_such_lane") is None


def test_split_lanes_partitions_all_three_ways():
    got = split_lanes([("agent_pay", "pricing"),
                       ("loop_control", "agent_identity"),
                       ("loop_control", "no_such_lane")])
    assert [x[1] for x in got["code_actionable"]] == ["pricing"]
    assert [x[1] for x in got["not_code"]] == ["agent_identity"]
    assert [x[1] for x in got["unclassified"]] == ["no_such_lane"]


def test_split_lanes_handles_empty_input():
    got = split_lanes([])
    assert got == {"code_actionable": [], "not_code": [], "unclassified": []}


def test_the_miscalibrated_lane_is_still_named():
    """A red whose CHECK is wrong, not whose system is broken. Reclassifying
    one of these must be a deliberate edit — which is why this assertion
    exists and why the 2026-09-03 correction had to come here first.

    ★ loop_control/counter_canon KEEPS this classification: measured live
    2026-09-03 01:45Z it still fails on a grep ("28 file(s) query DISTINCT
    agent_id across 1233 scanned") while its own detail concedes "a grep hit
    is not proof two counters DISAGREE". The check measures the wrong thing.

    ★ loop_flywheel/cron LOST it, deliberately. Its stated reason was that
    "dead-man board clear" reads the aggregate board and so is red whenever
    any shell is red, including itself. D2 (2026-09-02) had already fixed
    exactly that the same day this table was written: measured live
    2026-09-03 01:45Z the check reads "202 feeds, 0 overdue" and PASSES. The
    lane fails on its OTHER check, cron_dupes — the WAVE 4 order to retire
    ~314 overlapping jobs, which is an engineer's work, not a broken meter.
    Bonded by tests/test_triage_reasons_match_the_failing_check.py.
    """
    assert classify("loop_control", "counter_canon")[0] == "instrument"
    assert "grep" in classify("loop_control", "counter_canon")[1]

    klass, why = classify("loop_flywheel", "cron")
    assert klass == "build"
    assert "itself" not in why.split("replaces", 1)[0], (
        "the circularity claim was retired on 2026-09-03 — it must not come "
        "back without a fresh live measurement")


# ── reading the board's free-text notes ───────────────────────────────
# Verbatim from GET /api/v1/ops/deadman on 2026-09-02. Fixtures are the real
# strings, not idealised ones — three of these shells name nothing.
REAL_NOTES = {
    "agent-pay-shell-daily":
        "lanes: demand=FAIL reachability=FAIL pricing=PASS rail_health=FAIL "
        "metric_integrity=PASS",
    "loop-control-shell-daily":
        "lanes: cron_liveness=PASS count_semantics=PASS triage_wired=PASS "
        "surface_canon=PASS writer_discipline=PASS agent_identity=FAIL "
        "counter_canon=FAIL relay_two_artifact=PASS",
    "relay-closure-shell-daily":
        "lanes: 5 | reds: B/relay_demand_verdict,C/mint_attributability",
    "loop-flywheel-shell-daily":
        "lanes: infra=? edge=PASS failover=PASS identity=PASS rag=PASS "
        "mcp=PASS ai_doors=PASS inventory=PASS cron=FAIL",
    # ★ these three COUNT failures without NAMING them
    "growth-funnel-shell-daily": "3 failing / 1 unknown of 4 lanes",
    "webmcp-shell-daily": "lanes 2/4 pass",
    "agentic-loop-shell-daily": "PASS 2 FAIL 2 ? 0 | filed 0 | rate 0.602",
}


def test_feed_names_map_to_shell_module_stems():
    from routes.lane_triage import feed_to_shell
    assert feed_to_shell("agent-pay-shell-daily") == "agent_pay"
    assert feed_to_shell("loop-control-shell-daily") == "loop_control"
    assert feed_to_shell("relay-closure-shell-daily") == "relay_closure"
    assert feed_to_shell("iso-intl") is None
    assert feed_to_shell("") is None


def test_it_parses_the_equals_fail_format():
    from routes.lane_triage import parse_failing_lanes
    lanes, named = parse_failing_lanes(REAL_NOTES["agent-pay-shell-daily"])
    assert named is True
    assert lanes == ["demand", "reachability", "rail_health"]


def test_it_parses_the_reds_list_format():
    from routes.lane_triage import parse_failing_lanes
    lanes, named = parse_failing_lanes(REAL_NOTES["relay-closure-shell-daily"])
    assert named is True
    assert lanes == ["B/relay_demand_verdict", "C/mint_attributability"]


def test_a_question_mark_lane_is_not_read_as_a_failure():
    """loop_flywheel's note carries `infra=?`. Unknown is not FAIL."""
    from routes.lane_triage import parse_failing_lanes
    lanes, _ = parse_failing_lanes(REAL_NOTES["loop-flywheel-shell-daily"])
    assert lanes == ["cron"]


def test_all_pass_is_named_with_no_failures():
    """★ ([], True) — the note named its lanes and none failed."""
    from routes.lane_triage import parse_failing_lanes
    assert parse_failing_lanes("lanes: a=PASS b=PASS") == ([], True)


def test_an_unreadable_note_is_false_never_an_empty_pass():
    """★★ THE LOAD-BEARING DISTINCTION. ([], False) means we could not read
    the note; ([], True) means we read it and nothing failed. Collapsing them
    turns a blind spot into a clean bill of health — the same error as
    asserting no_new_data without evidence."""
    from routes.lane_triage import parse_failing_lanes
    for feed in ("growth-funnel-shell-daily", "webmcp-shell-daily",
                 "agentic-loop-shell-daily"):
        lanes, named = parse_failing_lanes(REAL_NOTES[feed])
        assert (lanes, named) == ([], False), f"{feed} parsed as readable"
    assert parse_failing_lanes("") == ([], False)


def test_triage_feed_classifies_a_real_board_row():
    from routes.lane_triage import triage_feed
    t = triage_feed("loop-control-shell-daily",
                    REAL_NOTES["loop-control-shell-daily"])
    assert t["shell"] == "loop_control" and t["lanes_named"] is True
    got = {x["lane"]: x["class"] for x in t["failing_lanes"]}
    assert got == {"agent_identity": "commercial", "counter_canon": "instrument"}
    assert t["code_actionable_count"] == 1   # counter_canon (fix the CHECK)
    assert t["not_code_count"] == 1          # agent_identity (fix demand)


def test_triage_feed_reports_an_unnamed_shell_as_a_gap_not_a_zero():
    from routes.lane_triage import triage_feed
    t = triage_feed("webmcp-shell-daily", REAL_NOTES["webmcp-shell-daily"])
    assert t["lanes_named"] is False
    assert t["failing_lanes"] == []
    assert "not WHICH" in t["note"]


def test_a_skipped_meta_shell_is_flagged_not_silently_empty():
    """audit_closure names its lanes fine; we decline to class them twice.
    Empty here must not read as 'nothing failed'."""
    from routes.lane_triage import triage_feed
    t = triage_feed("audit-closure-shell-daily", "p0_incidents=FAIL secrets=PASS")
    assert t.get("triage_skipped") is True
    assert t["failing_lanes"] == []


def test_triage_feed_never_raises_on_junk():
    from routes.lane_triage import triage_feed
    for feed, note in (("x-shell-daily", None), ("", ""), ("iso-intl", "x=FAIL")):
        assert isinstance(triage_feed(feed, note), dict)


# ── the wiring ────────────────────────────────────────────────────────
def test_the_board_routes_red_feeds_through_triage_and_publishes_a_rollup():
    """STRUCTURAL. deadman() opens a live psycopg2 connection, so driving it
    here would test a stub. The coupling asserted is exactly the wiring: red
    feeds get a triage block and the response carries the rollup."""
    src = io.open(os.path.join(ROUTES, "ingest_runs.py"), encoding="utf-8").read()
    assert "from routes.lane_triage import triage_feed" in src
    assert 'rec["triage"] = triage_feed(' in src
    assert "red_triage=red_triage" in src
    assert "red_lanes_unnamed" in src


# ── the three shells that counted failures without naming them ────────
# Until 2026-09-02 these wrote "PASS 2 FAIL 2", "3 failing / 1 unknown of 4
# lanes" and "lanes 2/4 pass". All three say HOW MANY broke and none says
# WHICH, so /ops/deadman could not triage them at all — published as
# red_lanes_unnamed=3. Each now writes through format_lane_verdicts, which
# lives beside parse_failing_lanes so a writer cannot drift from its reader.

def _shell_lane_ids(stem):
    """The lane ids each shell will actually emit — read from the live
    _LANES, not from a regex of the source."""
    mod = __import__(f"routes.{stem}_master_shell", fromlist=["_LANES"])
    lanes = mod._LANES
    if stem == "growth_funnel":      # ("1 · attribution", fn)
        return [fn.__name__[len("_lane_"):] for _label, fn in lanes]
    if stem == "webmcp":             # (key, label, fn, actuator)
        return [t[0] for t in lanes]
    if stem == "agentic_loop":       # (key, name, headline, fn)
        return [t[1] for t in lanes]
    raise AssertionError(stem)


@pytest.mark.parametrize("stem", ["growth_funnel", "webmcp", "agentic_loop"])
def test_every_lane_these_shells_emit_is_classified(stem):
    """★ THE COUPLING. If a lane is renamed in its shell the board starts
    emitting an id the registry has never seen, and the triage silently
    degrades to `unclassified` instead of failing loudly. Catch it here."""
    from routes.lane_triage import classify
    ids = _shell_lane_ids(stem)
    assert ids, f"{stem} exposed no lane ids"
    unknown = [i for i in ids if classify(stem, i) is None]
    assert not unknown, f"{stem} emits unclassified lane ids: {unknown}"


@pytest.mark.parametrize("stem", ["growth_funnel", "webmcp", "agentic_loop"])
def test_the_note_each_shell_writes_round_trips_back_to_named_lanes(stem):
    """★ END TO END, both directions. Format the shell's real lane ids, then
    read them back the way the board does."""
    from routes.lane_triage import format_lane_verdicts, parse_failing_lanes
    ids = _shell_lane_ids(stem)
    note = format_lane_verdicts([(i, "FAIL") for i in ids])
    lanes, named = parse_failing_lanes(note)
    assert named is True
    assert lanes == ids
    assert len(note) <= 280, f"{stem} note is {len(note)} chars, cap is 280"


def test_the_formatter_emits_fail_as_fail():
    """★ THE GREEN DIRECTION. A formatter that wrote everything as PASS would
    satisfy the round-trip test above by returning an empty failure list."""
    from routes.lane_triage import format_lane_verdicts, parse_failing_lanes
    note = format_lane_verdicts([("a", "PASS"), ("b", "FAIL")])
    assert note == "lanes: a=PASS b=FAIL"
    assert parse_failing_lanes(note) == (["b"], True)


def test_an_unmeasured_lane_is_never_written_as_a_failure():
    """`?` is not FAIL. Writing an unmeasured lane as failed would invent a
    defect; writing it as PASS would hide one."""
    from routes.lane_triage import format_lane_verdicts, parse_failing_lanes
    note = format_lane_verdicts([("a", "?"), ("b", None), ("c", "weird")])
    assert note == "lanes: a=? b=? c=?"
    assert parse_failing_lanes(note) == ([], True)


def test_the_formatter_is_empty_for_no_lanes():
    from routes.lane_triage import format_lane_verdicts
    assert format_lane_verdicts([]) == ""
    assert format_lane_verdicts(None) == ""


@pytest.mark.parametrize("stem", ["growth_funnel", "webmcp", "agentic_loop"])
def test_each_shell_beats_through_the_shared_formatter(stem):
    """STRUCTURAL: these beat paths need a live DB and a tick, so the
    coupling asserted is that the shared writer is the one being called —
    a fourth bespoke note format is exactly what regressed here."""
    src = io.open(os.path.join(ROUTES, f"{stem}_master_shell.py"),
                  encoding="utf-8").read()
    assert "from routes.lane_triage import format_lane_verdicts" in src, stem
    assert "format_lane_verdicts(" in src, stem
