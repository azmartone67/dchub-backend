"""Graph Master Shell #49 pins (2026-08-02).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

The shell's whole claim is that the EDGES are missing while the nodes are fine.
Two properties have to hold for that claim to stay worth reading:

  · the declared graph must keep POINTING AT SOMETHING — an edge naming a loop
    that no probe emits is checking nothing, and it will read exactly like a
    real edge on the board.
  · a lane must never read PASS when it could not check. A false green here is
    the same failure the shell exists to expose, one level up.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── the declared graph ────────────────────────────────────────────────

def test_five_lanes_with_stable_ids():
    src = _src("routes", "graph_master_shell.py")
    ids = re.findall(r'\("([a-z_]+)", "\d+ · ', src)
    assert ids == ["loop_edges", "causal_edges", "typed_nodes", "identity",
                   "orchestrator"], ids


def test_every_edge_is_fully_specified():
    from routes.graph_master_shell import LOOP_EDGES
    assert LOOP_EDGES, "the shell's premise is a declared graph; it is empty"
    for e in LOOP_EDGES:
        for k in ("producer", "consumer", "kind", "evidence", "marker", "basis"):
            assert k in e, f"edge {e.get('producer')}->{e.get('consumer')} lacks {k}"
        assert e["evidence"] in ("code", "declared"), e["evidence"]
        # A basis is the difference between an edge and a guess someone typed.
        assert len(e["basis"]) > 40, f"edge {e['producer']}->{e['consumer']} has no real basis"
        if e["evidence"] == "code":
            assert e["marker"], "a code-evidenced edge must carry a re-checkable marker"


def test_every_edge_endpoint_names_a_real_loop():
    """THE DRIFT GUARD, running for real. An edge pointing at a renamed or
    deleted loop is dead weight that still renders like a live check."""
    from routes.graph_master_shell import LOOP_EDGES
    sys_src = _src("routes", "system_loops.py")
    known = set(re.findall(r'"name":\s*"([a-z0-9_]+)"', sys_src))
    assert known, "could not extract any loop names from system_loops.py"
    for e in LOOP_EDGES:
        assert e["producer"] in known, f"unknown producer {e['producer']}"
        assert e["consumer"] in known, f"unknown consumer {e['consumer']}"


def test_code_evidenced_edges_actually_reproduce():
    """The mcp_traffic -> brain_learn edge is asserted because
    _probe_brain_learn reads mcp_tool_calls to decide idle-vs-dead. If that
    read is refactored away the edge is a lie — fail here, loudly, rather than
    let the board keep showing it as proven."""
    from routes.graph_master_shell import LOOP_EDGES, _edge_evidenced
    sys_src = _src("routes", "system_loops.py")
    coded = [e for e in LOOP_EDGES if e["evidence"] == "code"]
    assert coded, "no edge is code-evidenced — the marker mechanism is untested"
    for e in coded:
        assert _edge_evidenced(e, sys_src), (
            f"{e['producer']}->{e['consumer']} no longer reproduces against "
            f"routes/system_loops.py — re-verify the dependency or drop the edge")


def test_marker_cannot_leak_past_its_function():
    """The marker is anchored to the function body. A bare `.*?` would match an
    mcp_tool_calls read in ANY later probe and keep reporting the edge as
    proven after the real dependency was removed."""
    from routes.graph_master_shell import LOOP_EDGES, _edge_evidenced
    e = next(x for x in LOOP_EDGES if x["evidence"] == "code")
    # Same source, but the probe's own body emptied out: must stop reproducing.
    gutted = "def _probe_brain_learn(cur):\n    return {}\n\ndef _later(cur):\n    mcp_tool_calls\n"
    assert _edge_evidenced(e, gutted) is False


def test_declared_only_edges_are_not_reported_as_proven():
    from routes.graph_master_shell import LOOP_EDGES, _edge_evidenced
    sys_src = _src("routes", "system_loops.py")
    for e in LOOP_EDGES:
        if e["evidence"] == "declared":
            assert _edge_evidenced(e, sys_src) is False


def test_edge_evidence_is_false_when_source_is_unreadable():
    from routes.graph_master_shell import LOOP_EDGES, _edge_evidenced
    e = next(x for x in LOOP_EDGES if x["evidence"] == "code")
    assert _edge_evidenced(e, None) is False


def test_finding_edge_kinds_are_a_closed_vocabulary():
    """L14's writer and the selector's reader must not invent two different
    vocabularies for the same edge."""
    from routes.graph_master_shell import FINDING_EDGE_KINDS
    assert FINDING_EDGE_KINDS
    for k in FINDING_EDGE_KINDS:
        assert re.fullmatch(r"[a-z_]+", k), k


# ── the honesty rule ──────────────────────────────────────────────────

def test_undecided_lane_is_none_not_pass():
    from routes.graph_master_shell import _verdict
    assert _verdict([]) is None
    assert _verdict([{"pass": None}]) is None
    # One unmeasured check must not be able to drag a lane green.
    assert _verdict([{"pass": True}, {"pass": None}]) is True
    assert _verdict([{"pass": True}, {"pass": False}]) is False


def test_missing_column_is_distinguishable_from_absent_column():
    """_has_column returns None when information_schema itself is unreadable.
    'the column is missing' and 'I could not look' are different answers and
    only one of them is a finding."""
    from routes.graph_master_shell import _has_column

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("no db")

        def fetchone(self):
            raise RuntimeError("no db")

    assert _has_column(_Boom(), "brain_findings", "count_kind") is None


def test_tool_column_is_introspected_not_guessed():
    """The tree uses `tool` in ~163 places and `tool_name` in one. A hardcoded
    guess has a real chance of being wrong on the deployed schema, so the lane
    asks the schema and degrades to '?' when it cannot."""
    from routes.graph_master_shell import _call_column

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("no db")

        def fetchone(self):
            raise RuntimeError("no db")

    assert _call_column(_Boom()) is None
    src = _src("routes", "graph_master_shell.py")
    assert "information_schema.columns" in src


def test_stale_input_check_never_passes_unmeasured():
    """The payoff check. If the probes cannot run, the lane must say so — a
    false green on 'no loop is alive on stale input' is precisely the failure
    this shell was built to end."""
    src = _src("routes", "graph_master_shell.py")
    m = re.search(r'if statuses is None:(.{0,400})', src, re.S)
    assert m, "the unmeasured branch of the stale-input check is gone"
    assert "None," in m.group(1)
    assert "UNMEASURED" in m.group(1)


# ── read-only / safety ────────────────────────────────────────────────

def test_shell_writes_nothing():
    """READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing."""
    src = _src("routes", "graph_master_shell.py")
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    upper = body.upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE",
                 "ALTER TABLE", "DROP TABLE"):
        assert verb not in upper, f"shell #49 is read-only but contains {verb}"


def test_admin_gate_and_kill_switch():
    src = _src("routes", "graph_master_shell.py")
    assert "GRAPH_SHELL_DISABLE" in src
    assert "DCHUB_ADMIN_KEY" in src and "DCHUB_INTERNAL_KEY" in src
    # Every route must be behind the gate.
    routes = re.findall(r"@graph_master_shell_bp\.route\(([^)]+)\)", src)
    assert len(routes) == 3, routes


def test_no_literal_percent_in_sql():
    """★psycopg2 runs %-interpolation even against an empty tuple, so a single
    unescaped % raises IndexError, _one() swallows it, and the check reports
    UNMEASURED against a query that would have worked. This cost the
    white-glove shell a round-trip; do not re-learn it here."""
    src = _src("routes", "graph_master_shell.py")
    for m in re.finditer(r"(SELECT|HAVING)[^\"']*?%(?!s|%|\()", src):
        raise AssertionError(f"bare % in SQL near: {m.group(0)[:80]}")


def test_every_lane_degrades_to_unmeasured_without_a_db():
    """Exercise every lane against a cursor that answers nothing. No lane may
    raise, and no lane may invent a PASS out of a query it could not run —
    the shell has to be safe to look at when Neon is down, which is exactly
    when someone will open it."""
    from routes import graph_master_shell as g

    class _NullCur:
        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    cur = _NullCur()
    db_lanes = [g._lane_loop_edges, g._lane_causal_edges,
                g._lane_typed_nodes, g._lane_identity]
    for lane in db_lanes:
        checks = lane(cur)
        assert checks, f"{lane.__name__} returned no checks"
        for ch in checks:
            assert set(ch) >= {"id", "name", "pass", "detail"}, ch
        # Anything that can only be answered by the DB must be '?', not green.
        for ch in checks:
            if ch["id"] in ("no_stale_input", "edge_coverage", "identity_agrees",
                            "enumeration_split", "edge_store", "identity_store",
                            "count_kind_column"):
                assert ch["pass"] is not True, (
                    f"{ch['id']} reported PASS against a dead DB — a lane must "
                    f"never read PASS when it could not check")


def test_orchestrator_lane_needs_no_db_and_reads_the_real_source():
    from routes.graph_master_shell import _lane_orchestrator
    checks = _lane_orchestrator()
    ids = [c["id"] for c in checks]
    assert ids == ["orchestrator_declared", "orchestrator_parallel",
                   "orchestrator_resumable"], ids
    # Tier 3's four independent probes are now declared nodes; the rest of the
    # tick is still inline _call() lines, so this stays RED and names how many.
    assert checks[0]["pass"] is False, (
        "every step is declared now — update shell #49 lane 5 rather than "
        "leaving a stale FAIL on the board")
    assert re.search(r"\d+ declared node", checks[0]["detail"])
    assert re.search(r"\d+ still\s+inline", checks[0]["detail"])


def test_parallel_lane_is_not_green_just_because_the_runner_exists(monkeypatch):
    """★Building the fan-out is not the same as running it. A green here while
    BRAIN_MASTER_PARALLEL is unset would claim wall-clock nobody is saving."""
    from routes.graph_master_shell import _lane_orchestrator
    monkeypatch.delenv("BRAIN_MASTER_PARALLEL", raising=False)
    ch = next(c for c in _lane_orchestrator() if c["id"] == "orchestrator_parallel")
    assert ch["pass"] is False
    assert "DORMANT" in ch["detail"]
    monkeypatch.setenv("BRAIN_MASTER_PARALLEL", "1")
    ch = next(c for c in _lane_orchestrator() if c["id"] == "orchestrator_parallel")
    assert ch["pass"] is True


# ── lane 1's actuator: the edge set applied to the public board ───────

def _loops(**status):
    return [{"name": n, "status": s} for n, s in status.items()]


def test_consumer_alive_on_dead_producer_is_flagged():
    """THE payoff. dcpi_recompute recomputes on schedule whether or not
    iso_extract fed it anything — before the edge set, that board was green."""
    from routes.system_loops import apply_loop_edges, count_alive_on_stale_input
    out = apply_loop_edges(_loops(iso_extract="dead", dcpi_recompute="alive"))
    dcpi = next(l for l in out if l["name"] == "dcpi_recompute")
    assert dcpi["input_status"] == "stale"
    assert dcpi["stale_inputs"][0]["producer"] == "iso_extract"
    assert count_alive_on_stale_input(out) == 1


def test_healthy_producer_leaves_the_consumer_ok():
    from routes.system_loops import apply_loop_edges, count_alive_on_stale_input
    out = apply_loop_edges(_loops(iso_extract="alive", dcpi_recompute="alive"))
    assert next(l for l in out if l["name"] == "dcpi_recompute")["input_status"] == "ok"
    assert count_alive_on_stale_input(out) == 0


def test_status_is_never_overwritten():
    """★CONTRACT PIN. babysit_loops() reads `status not in {alive, idle}` as
    'fire the refresh hook'. If a consumer's status became
    alive_on_stale_input, the babysitter would POST /api/v1/dcpi/recompute
    against dead ISO input — rewriting the same stale answer with a fresh
    timestamp, making the board look healed while the data got no better.
    The consumer is not what needs healing; its producer is."""
    from routes.system_loops import apply_loop_edges
    out = apply_loop_edges(_loops(iso_extract="dead", dcpi_recompute="alive"))
    assert [l["status"] for l in out] == ["dead", "alive"]


def test_unknown_producer_is_not_reported_as_ok():
    """A producer this survey did not probe is an unanswered question.
    Calling it healthy input recreates the false green the edges exist for."""
    from routes.system_loops import apply_loop_edges
    out = apply_loop_edges(_loops(dcpi_recompute="alive"))
    assert next(l for l in out if l["name"] == "dcpi_recompute")["input_status"] == "unknown"


def test_loop_with_no_declared_input_says_so():
    from routes.system_loops import apply_loop_edges
    out = apply_loop_edges(_loops(testimonial_ingest="alive"))
    assert out[0]["input_status"] == "no_declared_input"


def test_babysitter_heals_the_producer_first():
    """Firing dcpi_recompute before iso_extract recomputes against the stale
    input and burns the run. Probe order gave no such guarantee — it was
    whatever order the list literal happened to be written in."""
    from routes.system_loops import order_producers_first
    ordered = [l["name"] for l in order_producers_first(
        _loops(dcpi_recompute="dead", iso_extract="dead"))]
    assert ordered.index("iso_extract") < ordered.index("dcpi_recompute")


def test_producer_ordering_has_a_kill_switch(monkeypatch):
    from routes.system_loops import order_producers_first
    monkeypatch.setenv("LOOP_EDGE_ORDER_DISABLE", "1")
    given = _loops(dcpi_recompute="dead", iso_extract="dead")
    assert [l["name"] for l in order_producers_first(given)] == \
        ["dcpi_recompute", "iso_extract"]


def test_ordering_terminates_on_a_cycle(monkeypatch):
    """Bounded relaxation: a cycle must stop improving, not spin."""
    from routes import system_loops as sl
    monkeypatch.setattr(sl, "_declared_edges", lambda: (
        {"producer": "a", "consumer": "b"}, {"producer": "b", "consumer": "a"}))
    out = sl.order_producers_first(_loops(a="dead", b="dead"))
    assert {l["name"] for l in out} == {"a", "b"}


def test_board_is_unchanged_when_the_edge_set_is_unavailable(monkeypatch):
    """The edge set lives in a diagnostic module. An import failure there
    must never be able to alter — let alone break — a health endpoint."""
    from routes import system_loops as sl
    monkeypatch.setattr(sl, "_declared_edges", lambda: ())
    given = _loops(iso_extract="dead", dcpi_recompute="alive")
    out = sl.apply_loop_edges(given)
    assert all("input_status" not in l for l in out)
    assert sl.count_alive_on_stale_input(out) == 0


def test_public_survey_emits_the_derived_count():
    src = _src("routes", "system_loops.py")
    assert '"alive_on_stale_input": on_stale_input' in src
    assert "loops = apply_loop_edges(loops)" in src


def test_shell_lane_now_sees_the_wiring():
    """Shell #49 lane 1 shipped RED on board_consumes_edges by design. With
    the wiring landed it must go green — otherwise the board is carrying a
    stale FAIL, which is the green-by-silence failure in reverse."""
    from routes.graph_master_shell import _lane_loop_edges

    class _NullCur:
        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return None

    ch = next(c for c in _lane_loop_edges(_NullCur())
              if c["id"] == "board_consumes_edges")
    assert ch["pass"] is True


def test_blueprint_is_registered_in_main():
    src = _src("main.py")
    assert "from routes.graph_master_shell import graph_master_shell_bp" in src
    assert "app.register_blueprint(graph_master_shell_bp)" in src
