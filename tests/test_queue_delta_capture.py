"""WS6 — interconnection-queue transition capture must never fabricate a withdrawal.

The write path has no withdrawal signal to begin with: the loader is a pure
UPSERT with no DELETE (load_interconnect_queue_live.py:602-619) and 5 of the 7
parsers write a hardcoded queue_status='active', so a departed project keeps a
row reading 'active' forever. Capture infers the exit from ABSENCE, which is
exactly the inference that goes wrong loudly:

  - a header-drift partial parse (40 of 2,000 PJM rows) would publish ~1,960
    fabricated withdrawals unless the short-feed floor blocks it;
  - the first run would read the ENTIRE pre-existing ghost backlog as
    withdrawals detected today unless the seed rule blocks it;
  - nothing deletes the row, so a real disappearance would re-emit every single
    day unless the already-reported gate blocks it.

These tests pin all three plus the fail-closed timestamp comparison. The diff is
pulled out with `ast` and executed against stubs — tests never import main, and
routes/iso_queue_ingest.py imports flask at module scope.

pytest functions only; nothing runs (or exits) at module scope.
"""
import ast
import datetime
import pathlib

SRC = (pathlib.Path(__file__).resolve().parent.parent
       / "routes" / "iso_queue_ingest.py")

_WANT_FN = ("_qd_mw", "_qd_status", "_qd_ge", "_qd_diff")
_WANT_CONST = ("_QD_FLOOR", "_QD_SEEN_SLACK_S")
_UTC = datetime.timezone.utc


def _load():
    """ast-extract the pure diff helpers. Asserts the parse actually produced
    them AND that every free name they close over resolves — an empty extraction
    passes every assertion below while testing nothing, and a missing free name
    is either a NameError at runtime or, worse, silently untested."""
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert tree.body, "ast.parse produced an empty module — nothing was tested"
    ns = {"datetime": datetime}
    got_fn, got_const = set(), set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in _WANT_CONST:
                    exec(compile(ast.Module(body=[node], type_ignores=[]),
                                 str(SRC), "exec"), ns)
                    got_const.add(t.id)
        elif isinstance(node, ast.FunctionDef) and node.name in _WANT_FN:
            exec(compile(ast.get_source_segment(src, node), str(SRC), "exec"), ns)
            got_fn.add(node.name)
    assert got_fn == set(_WANT_FN), "missing from iso_queue_ingest.py: {}".format(
        sorted(set(_WANT_FN) - got_fn))
    assert got_const == set(_WANT_CONST), "missing consts: {}".format(
        sorted(set(_WANT_CONST) - got_const))
    # every free name _qd_diff reaches for must exist in the namespace we built
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_qd_diff"][0]
    for n in ast.walk(body):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id.startswith("_qd") or n.id.startswith("_QD"):
                assert n.id in ns, "_qd_diff references {} which is not extracted".format(n.id)
    return ns


def _row(qid, name, mw, status, iso="PJM"):
    """load_interconnect_queue_live._row shape (:117-134)."""
    return (qid, name, iso, None, None, None, mw, status, None, None,
            None, None, "test")


def test_extraction_is_not_vacuous():
    ns = _load()
    assert ns["_qd_diff"]({}, {"A": _row("A", "a", 1.0, "active")},
                          None, {}, True, False), "MUST-FAIL control: the seed " \
        "branch produced no events, so every other assertion here is vacuous"


def test_seed_run_never_emits_additions_or_withdrawals():
    """Day 1 the table is ALREADY full and full of ghosts. Emitting 'add' for
    every parsed row, or 'disappeared' for every un-refreshed row, is the
    backfill-stamp-read-as-an-event-spike trap."""
    ns = _load()
    d = datetime.datetime(2026, 7, 27, 5, 55, tzinfo=_UTC)
    roster = {"OLD": (3.0, "active", d, "Ghost")}
    feed = {"A": _row("A", "Alpha", 10.0, "active")}
    ev = ns["_qd_diff"](roster, feed, None, {}, True, False)
    assert [e[1] for e in ev] == ["seed"], ev


def test_short_feed_blocks_withdrawals_but_not_additions():
    """emit_disappear=False is what the ~0.6 floor buys. Adds stay safe."""
    ns = _load()
    assert 0 < ns["_QD_FLOOR"] < 1, "the short-feed floor must be a real fraction"
    d = datetime.datetime(2026, 7, 27, 5, 55, tzinfo=_UTC)
    roster = {"GONE": (500.0, "active", d, "Big")}
    feed = {"NEW": _row("NEW", "New", 10.0, "active")}
    ev = ns["_qd_diff"](roster, feed, d, {}, False, False)
    assert [e[1] for e in ev] == ["appeared_in_feed"], ev
    ev = ns["_qd_diff"](roster, feed, d, {}, False, True)
    assert sorted(e[1] for e in ev) == ["appeared_in_feed", "disappeared_from_feed"], ev


def test_pre_existing_backlog_is_not_a_withdrawal_detected_today():
    """A row whose loaded_at predates the last parse_ok run was already gone
    before capture existed — it has unknown age and must stay silent."""
    ns = _load()
    old = datetime.datetime(2026, 7, 20, 5, 55, tzinfo=_UTC)
    new = datetime.datetime(2026, 7, 27, 5, 55, tzinfo=_UTC)
    roster = {"BACKLOG": (3.0, "active", old, "Ghost"),
              "FRESH": (4.0, "active", new, "Real")}
    ev = ns["_qd_diff"](roster, {}, new, {}, False, True)
    assert [(e[0], e[1]) for e in ev] == [("FRESH", "disappeared_from_feed")], ev


def test_disappearance_is_reported_once_not_every_day():
    """Nothing deletes the row, so the naive version re-emits forever."""
    ns = _load()
    d = datetime.datetime(2026, 7, 27, 5, 55, tzinfo=_UTC)
    roster = {"GONE": (3.0, "active", d, "Ghost")}
    reported = {"GONE": d + datetime.timedelta(hours=1)}
    assert ns["_qd_diff"](roster, {}, d, reported, False, True) == []
    # but a project that came BACK and left again is a new event
    stale = {"GONE": d - datetime.timedelta(days=7)}
    ev = ns["_qd_diff"](roster, {}, d, stale, False, True)
    assert [e[1] for e in ev] == ["disappeared_from_feed"], ev


def test_no_ledger_baseline_means_no_withdrawal():
    """prev_feed_seen_at=None (every prior run failed) must not be read as
    'everything vanished'."""
    ns = _load()
    d = datetime.datetime(2026, 7, 27, 5, 55, tzinfo=_UTC)
    roster = {"GONE": (3.0, "active", d, "Ghost")}
    assert ns["_qd_diff"](roster, {}, None, {}, False, True) == []


def test_timestamp_comparison_failure_fails_closed():
    """LIVE schema != repo DDL: if loaded_at is a naive TIMESTAMP, comparing it
    to an aware TIMESTAMPTZ raises. That must suppress the withdrawal, not
    publish it."""
    ns = _load()
    aware = datetime.datetime(2026, 7, 27, 5, 55, tzinfo=_UTC)
    naive = datetime.datetime(2026, 7, 27, 5, 55)
    assert ns["_qd_ge"](naive, aware) is None
    roster = {"GONE": (3.0, "active", naive, "Ghost")}
    assert ns["_qd_diff"](roster, {}, aware, {}, False, True) == []


def test_capacity_and_status_transitions():
    ns = _load()
    d = datetime.datetime(2026, 7, 27, 5, 55, tzinfo=_UTC)
    roster = {"A": (100.0, "active", d, "Alpha"),
              "B": (50.0, "active", d, "Bravo"),
              "C": (10.0, "active", d, "Char")}
    feed = {"A": _row("A", "Alpha", 320.0, "active"),
            "B": _row("B", "Bravo", 50.0, "Suspended"),
            "C": _row("C", "Char", 10.005, "active")}
    ev = ns["_qd_diff"](roster, feed, d, {}, False, True)
    by = {(e[0], e[1]): e for e in ev}
    assert ("A", "capacity_changed") in by and by[("A", "capacity_changed")][3] == 100.0
    assert by[("A", "capacity_changed")][4] == 320.0
    assert ("B", "status_changed") in by and by[("B", "status_changed")][6] == "suspended"
    assert not any(e[0] == "C" for e in ev), "0.005 MW of float jitter is not a change"


def test_null_to_value_capacity_counts_as_a_change():
    ns = _load()
    d = datetime.datetime(2026, 7, 27, 5, 55, tzinfo=_UTC)
    ev = ns["_qd_diff"]({"A": (None, "active", d, "Alpha")},
                        {"A": _row("A", "Alpha", 10.0, "active")}, d, {}, False, True)
    assert [e[1] for e in ev] == ["capacity_changed"], ev


def test_capture_writes_nothing_to_interconnect_queue():
    """ADDITIVE contract: the capture block must never write the queue table."""
    code = "\n".join(ln for ln in SRC.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))
    start = code.find("_QD_SCHEMA = ")
    end = code.find("def ingest_projects")
    assert start != -1 and end > start, "WS6 capture block not found"
    block = code[start:end]
    for verb in ("UPDATE interconnect_queue ", "DELETE FROM interconnect_queue",
                 "INSERT INTO interconnect_queue ", "TRUNCATE", "DROP TABLE",
                 "ALTER TABLE interconnect_queue "):
        assert verb not in block, "capture must not {} — it is additive".format(verb)
    assert "INSERT INTO interconnect_queue_events" in block
    assert "INSERT INTO interconnect_queue_runs" in block
