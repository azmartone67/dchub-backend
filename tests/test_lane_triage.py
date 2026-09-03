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
    assert classify("loop_control", "surface_canon")[0] == "build"
    assert is_code_actionable("loop_control", "surface_canon") is True


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
    got = split_lanes([("loop_control", "surface_canon"),
                       ("loop_control", "agent_identity"),
                       ("loop_control", "no_such_lane")])
    assert [x[1] for x in got["code_actionable"]] == ["surface_canon"]
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
    # ★ ASYMMETRIC ON PURPOSE. This fixture used to make both counts 1, so
    # swapping code_actionable and not_code was undetectable — an adversarial
    # review confirmed the swap passed the whole suite. Three failing lanes,
    # two classes, unequal counts.
    t3 = triage_feed("agent_pay-shell-daily",
                     "lanes: demand=FAIL reachability=FAIL rail_health=FAIL")
    assert t3["code_actionable_count"] == 2   # reachability, rail_health (build)
    assert t3["not_code_count"] == 1          # demand (commercial)
    assert t["code_actionable_count"] == 1    # counter_canon
    assert t["not_code_count"] == 1


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



def test_a_non_shell_feed_is_flagged_and_not_a_lane_gap():
    from routes.lane_triage import triage_feed
    t = triage_feed("iso-intl", ISO_INTL_NOTE)
    assert t["not_a_shell"] is True
    assert t["shell"] is None
    assert t["lanes_named"] is False          # it has no lanes to name
    assert "no lanes" in t["note"]


def test_a_real_shell_is_not_flagged_as_non_shell():
    """★ THE GREEN DIRECTION. Marking everything not_a_shell would empty the
    gap count entirely and hide the three shells that really do hide names."""
    from routes.lane_triage import triage_feed
    t = triage_feed("webmcp-shell-daily", "lanes 2/4 pass")
    assert t["not_a_shell"] is False
    assert t["lanes_named"] is False          # this one IS a real gap
    assert "not WHICH" in t["note"]


def test_the_rollup_excludes_non_shell_feeds_from_the_gap_count():
    """The live payload's own shape: three shells hiding names + one ISO feed.
    The gap count must be 3."""
    from routes.lane_triage import triage_feed
    rows = [("agentic-loop-shell-daily", "PASS 2 FAIL 2 ? 0"),
            ("growth-funnel-shell-daily", "3 failing / 1 unknown of 4 lanes"),
            ("webmcp-shell-daily", "lanes 2/4 pass"),
            ("iso-intl", ISO_INTL_NOTE)]
    tri = [triage_feed(f, n) for f, n in rows]
    gap = sum(1 for t in tri
              if not t.get("lanes_named")
              and not t.get("triage_skipped")
              and not t.get("not_a_shell"))
    assert gap == 3, f"expected 3 real gaps, got {gap}"


ISO_INTL_NOTE = ("failed: OCCTO (mix: hokkaido:month_not_published_yet:202609 "
                 "(day 3 JST; monthly file absent upstream, URL template "
                 "unchanged; raw=http_404))")


# ══ BEHAVIOURAL REPLACEMENTS FOR THE SUBSTRING GUARDS ══════════════════
# ★ An adversarial review (2026-09-03) demonstrated that the four guards this
# block replaces were substring matches on source text, and that with all of
# them in place the ENTIRE triage wiring could be switched off with 42/42
# passing. Every assertion below executes the real code path instead.

def _spy_beat(monkeypatch, stem):
    """Run a shell's real tick and capture the note it actually beats."""
    m = __import__(f"routes.{stem}_master_shell", fromlist=["x"])
    box = {}
    monkeypatch.setattr(m, "_beat_ledger",
                        lambda *a, **k: box.setdefault("note", a[0]))
    m._run_tick(beat=True)
    return box.get("note"), m


@pytest.mark.parametrize("stem", ["growth_funnel", "webmcp"])
def test_the_shell_actually_beats_a_parseable_named_note(monkeypatch, stem):
    """★ REAL PATH. Drives _run_tick(beat=True) with no DB — every lane comes
    back unmeasured or failing, which is exactly the hostile case — and reads
    the note the shell really hands the ledger."""
    from routes.lane_triage import parse_failing_lanes
    note, _ = _spy_beat(monkeypatch, stem)
    assert note, f"{stem} beat no note"
    assert note.startswith("lanes: "), f"{stem} beat a bespoke format: {note!r}"
    _lanes, named = parse_failing_lanes(note)
    assert named is True, f"the board cannot read {stem}'s note: {note!r}"
    for lane_id in _shell_lane_ids(stem):
        assert f"{lane_id}=" in note, f"{stem} omitted lane {lane_id}: {note!r}"


def test_agentic_loop_beat_note_is_named_and_parseable():
    """agentic_loop builds its note in a Flask route, so _beat_note() was
    extracted to make the real string reachable."""
    from routes.agentic_loop_master_shell import _beat_note
    from routes.lane_triage import parse_failing_lanes
    out = {"summary": {"PASS": 2, "FAIL": 2, "?": 0},
           "lanes": [{"name": "graduation", "verdict": "PASS"},
                     {"name": "human_queues", "verdict": "FAIL"},
                     {"name": "learn", "verdict": "FAIL"},
                     {"name": "detectors_with_fix", "verdict": "PASS"}]}
    note = _beat_note(out)
    assert note.startswith("lanes: ")
    assert parse_failing_lanes(note) == (["human_queues", "learn"], True)


def test_webmcp_writes_a_crashed_lane_as_question_mark_not_fail(monkeypatch):
    """★ THE PRODUCTION BUG THIS REPLACES A SUBSTRING GUARD FOR.

    webmcp's `pass` is a BOOLEAN and cannot say "unmeasured": with every check
    undecided, `lane_pass` is False — the same value as a real failure. The
    beat wrote that as `=FAIL`, so the board triaged a crashed probe as a
    code-actionable `build` defect on a tick that measured nothing.

    Driven through the shell's OWN crash path: force one lane to raise, which
    is exactly the `except` branch that emits pass=None."""
    import routes.webmcp_master_shell as m
    from routes.lane_triage import parse_failing_lanes, classify

    def boom(_c):
        raise RuntimeError("probe timed out")
    monkeypatch.setattr(m, "_lane_headers", boom)
    monkeypatch.setattr(m, "_LANES",
                        [(k, l, (boom if k == "headers" else f), a)
                         for k, l, f, a in m._LANES])
    note, _ = _spy_beat(monkeypatch, "webmcp")
    assert "headers=?" in note, (
        f"a crashed lane must be unmeasured, not failed; note was {note!r}")
    failing, named = parse_failing_lanes(note)
    assert named is True
    assert "headers" not in failing, (
        "the crashed lane reached the board as a failure, and "
        f"classify() would call it {classify('webmcp', 'headers')[0]}")


# ── the rollup, now that it is a reachable function ───────────────────
def _tri(**kw):
    base = {"lanes_named": True, "code_actionable_count": 0,
            "not_code_count": 0, "unclassified_count": 0}
    base.update(kw)
    return base


def test_the_rollup_sums_are_not_interchangeable():
    """★ Asymmetric by construction: swapping code_actionable and not_code in
    rollup_triage must fail. An adversarial review showed the swap passed
    while the arithmetic was inline in deadman() and unreachable."""
    from routes.lane_triage import rollup_triage
    r = rollup_triage([_tri(code_actionable_count=5, not_code_count=2,
                            unclassified_count=1)])
    assert r["code_actionable"] == 5
    assert r["not_code"] == 2
    assert r["unclassified"] == 1


def test_the_rollup_excludes_non_shells_but_counts_real_gaps():
    """★ THE FIX UNDER REVIEW, driven rather than grepped. Three shells hiding
    their lane names plus one ISO data feed must roll up to 3, not 4."""
    from routes.lane_triage import rollup_triage, triage_feed
    rows = [triage_feed("agentic-loop-shell-daily", "PASS 2 FAIL 2 ? 0"),
            triage_feed("growth-funnel-shell-daily", "3 failing of 4 lanes"),
            triage_feed("webmcp-shell-daily", "lanes 2/4 pass"),
            triage_feed("iso-intl", "failed: OCCTO (mix: hokkaido:...)"),
            triage_feed("audit-closure-shell-daily", "p0_incidents=FAIL")]
    assert rollup_triage(rows)["red_lanes_unnamed"] == 3


def test_the_rollup_survives_junk_without_inventing_numbers():
    from routes.lane_triage import rollup_triage
    assert rollup_triage([])["code_actionable"] == 0
    assert rollup_triage(None)["red_lanes_unnamed"] == 0
    assert rollup_triage([None, "x", 7])["not_code"] == 0


def test_the_formatter_applies_no_bound_and_does_not_claim_one():
    """★ The docstring used to promise a 280-char bound it never applied, and
    attributed the cap to record_beat, which has none (it lives in the HTTP
    beat() handler). Pin the honest contract: no bound, and no claim of one."""
    from routes.lane_triage import format_lane_verdicts
    import routes.lane_triage as _lt
    note = format_lane_verdicts([(f"lane_{i:03d}", "FAIL") for i in range(40)])
    assert len(note) > 280, "if a bound is ever added, update this guard"
    doc = format_lane_verdicts.__doc__ or ""
    assert "CONTRACT: this applies NO length bound" in doc, (
        "the docstring must state the honest contract; it previously promised "
        "a 280-char bound it never applied, attributed to a record_beat cap "
        "that does not exist")


# ── anti-rot, by import rather than by substring ──────────────────────
_LANES_SHELLS = {"growth_funnel", "webmcp", "agentic_loop"}


def test_every_classified_lane_resolves_in_its_live_shell():
    """★ REPLACES a source-substring check. For the three shells that expose
    _LANES, assert MEMBERSHIP in the live tuple — that catches a lane retired
    from _LANES with its function left behind, which the substring version
    could not. For the rest, resolve a live `_lane_<id>` callable, which still
    catches a deleted or renamed lane. Residual gap, stated rather than
    implied: a non-_LANES shell that stops CALLING a lane it still defines
    would pass."""
    unresolved = []
    for shell, lane in LANE_TRIAGE:
        if shell in _LANES_SHELLS:
            if lane not in _shell_lane_ids(shell):
                unresolved.append(f"{shell}/{lane} (not in live _LANES)")
            continue
        mod = __import__(f"routes.{shell}_master_shell", fromlist=["x"])
        from routes.lane_triage import LANE_FN_ALIASES
        cands = [LANE_FN_ALIASES.get((shell, lane)), f"_lane_{lane}"]
        if not any(c and callable(getattr(mod, c, None)) for c in cands):
            unresolved.append(f"{shell}/{lane} (no live _lane_* callable)")
    assert not unresolved, f"classified lanes that no longer resolve: {unresolved}"


# ══ END TO END: the board endpoint itself ══════════════════════════════
# ★ An adversarial review's headline finding was that the ENTIRE triage
# wiring could be switched off with the whole suite green, because the only
# guard on it was a substring match against deadman()'s source. deadman()
# opens a psycopg2 connection, which is why the substring shortcut was taken
# — but the connection is reachable through the module's own attributes, so
# the real handler CAN be driven. This is that test.

def _drive_deadman(monkeypatch, rows):
    """Call the real deadman() handler over a stubbed ledger."""
    import types
    import routes.ingest_runs as ir
    from flask import Flask

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return rows

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self, *a, **k): return _Cur()
        def commit(self): pass

    monkeypatch.setattr(ir, "_dsn", lambda: "postgres://stub")
    monkeypatch.setattr(ir, "_ensure", lambda cur: None)
    monkeypatch.setattr(ir, "psycopg2",
                        types.SimpleNamespace(connect=lambda *a, **k: _Conn()))
    app = Flask(__name__)
    with app.test_request_context("/api/v1/ops/deadman"):
        import json as _json
        return _json.loads(ir.deadman().get_data(as_text=True))


def _rows():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    return [
        # a real shell with one commercial and one instrument lane failing
        ("loop-control-shell-daily", now, "lanes_failing", 1, None, 24.0, 0,
         "lanes: agent_identity=FAIL counter_canon=FAIL"),
        # a shell that hides which lanes failed
        ("webmcp-shell-daily", now, "lanes_failing", 1, None, 24.0, 0,
         "lanes 2/4 pass"),
        # NOT a shell — must not count as a hidden-lane gap
        ("iso-intl", now, "degraded", 321, None, 1.0, 0,
         "failed: OCCTO (mix: hokkaido:month_not_published_yet:202609)"),
        # a healthy feed — must get no triage block at all
        ("newsroom-auto", now, "success", 5, None, 4.5, 0, "fine"),
    ]


def test_the_board_publishes_triage_for_red_feeds_only(monkeypatch):
    d = _drive_deadman(monkeypatch, _rows())
    by = {f["feed"]: f for f in d["feeds"]}
    assert "triage" in by["loop-control-shell-daily"]
    assert "triage" not in by["newsroom-auto"], "a healthy feed was triaged"


def test_the_board_classifies_each_failing_lane_end_to_end(monkeypatch):
    """★ THE WIRING GUARD. Switching triage off in deadman() must fail HERE,
    not merely change a substring that nothing executes."""
    d = _drive_deadman(monkeypatch, _rows())
    lanes = {x["lane"]: x for x in
             next(f for f in d["feeds"]
                  if f["feed"] == "loop-control-shell-daily")["triage"]["failing_lanes"]}
    assert lanes["agent_identity"]["class"] == "commercial"
    assert lanes["agent_identity"]["code_actionable"] is False
    assert lanes["counter_canon"]["code_actionable"] is True


def test_the_board_rollup_is_asymmetric_and_excludes_non_shells(monkeypatch):
    """One code-actionable lane, one not, one shell hiding names, one ISO feed
    that is not a shell. Every number distinct, so a swap cannot hide."""
    rt = _drive_deadman(monkeypatch, _rows())["red_triage"]
    assert rt["code_actionable"] == 1     # counter_canon
    assert rt["not_code"] == 1            # agent_identity
    assert rt["unclassified"] == 0
    assert rt["red_lanes_unnamed"] == 1   # webmcp only — NOT iso-intl


def test_the_board_never_5xxs_when_triage_raises(monkeypatch):
    """Fail-soft must stay soft: a broken triage may not take the board down."""
    import routes.lane_triage as lt
    def boom(*a, **k):
        raise RuntimeError("triage exploded")
    monkeypatch.setattr(lt, "triage_feed", boom)
    d = _drive_deadman(monkeypatch, _rows())
    assert d["ok"] is True
    assert d["red_count"] >= 1
