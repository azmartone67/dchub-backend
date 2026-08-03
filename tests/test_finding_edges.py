"""#49 lane 2: the causal graph, persisted and USED (2026-08-02).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly, and nothing runs at module scope.

brain_layer14_causal already did the expensive part — join related findings,
ask Claude for the root cause — and then discarded the graph, so rank_work()
kept treating five symptoms of one cause as five independent pieces of work.
These pins hold the store honest (a stale edge is worse than no edge) and hold
the ranker to its documented safety property (defer, never drop).
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── normalisation: the join key that exists on both sides ─────────────

def test_normalize_strips_the_things_that_make_one_finding_look_like_two():
    from routes.brain_finding_edges import normalize_key
    a = normalize_key("Brain finding: cron_silently_dead @ /api/jobs/site-baseline")
    b = normalize_key("cron_silently_dead (seen x477455)")
    assert a == b == "cron_silently_dead"


def test_empty_key_is_never_a_wildcard():
    """A blank key that matched everything would collapse the whole agenda
    into one group — the loudest possible version of this bug."""
    from routes.brain_finding_edges import normalize_key, root_of
    assert normalize_key(None) == ""
    assert normalize_key("   ") == ""
    assert root_of({"issue": ""}, {"": ("some_root", 0.9)}) == ""


def test_confidence_mapping():
    from routes.brain_finding_edges import confidence_of
    assert confidence_of("high") > confidence_of("medium") > confidence_of("low")
    assert confidence_of("nonsense") == 0.5
    assert confidence_of(0.42) == 0.42


# ── chain → edges (pure) ──────────────────────────────────────────────

def test_a_chain_becomes_symptom_edges():
    from routes.brain_finding_edges import edges_from_chain
    rows = edges_from_chain({
        "title": "ISO ingestion dead, not a stale-data problem",
        "symptoms": ["freshness.facilities over SLA",
                     "scheduler.facility_discovery runs=0"],
        "confidence": "high"})
    assert len(rows) == 2
    assert {r["kind"] for r in rows} == {"symptom_of"}
    assert all(r["cause_key"] == rows[0]["cause_key"] for r in rows)
    assert rows[0]["confidence"] == 0.9


def test_a_chain_of_one_is_not_a_chain():
    """A single-symptom 'chain' is a finding restating itself. Collapsing on it
    merges nothing while still consuming a root slot."""
    from routes.brain_finding_edges import edges_from_chain
    assert edges_from_chain({"title": "x", "symptoms": ["only one"]}) == []
    assert edges_from_chain({"title": "x", "symptoms": []}) == []
    assert edges_from_chain({"title": "", "symptoms": ["a", "b"]}) == []
    assert edges_from_chain(None) == []


def test_self_loops_and_duplicates_are_dropped():
    """A symptom that normalises to the root would make the root its own child
    and drop it out of its own group."""
    from routes.brain_finding_edges import edges_from_chain
    rows = edges_from_chain({
        "title": "paywall fires but users don't redeem",
        "symptoms": ["paywall fires but users don't redeem",
                     "funnel.conversions_30d flat",
                     "funnel.conversions_30d flat"],
        "confidence": "medium"})
    assert len(rows) == 1
    assert rows[0]["effect_key"] == "funnel.conversions_30d flat"


def test_writer_replaces_a_chain_rather_than_appending():
    """A re-analysis that drops a symptom must not leave the old edge behind —
    a stale edge collapses two findings nobody believes are related any more,
    which is worse than having no edge."""
    src = _src("routes", "brain_finding_edges.py")
    assert "DELETE FROM brain_finding_edges " in src
    assert re.search(r"DELETE FROM brain_finding_edges\s+\"\s*\n\s*\"WHERE cause_key",
                     src) or "WHERE cause_key = %s" in src


def test_write_chains_never_raises_without_a_db(monkeypatch):
    """L14's analyze path must not start failing because a diagnostic store is
    unavailable."""
    from routes import brain_finding_edges as e
    monkeypatch.setattr(e, "_conn", lambda: None)
    rep = e.write_chains([{"title": "t", "symptoms": ["a", "b"]}])
    assert rep["error"] == "no_database" and rep["written"] == 0
    assert e.write_chains(None)["written"] == 0
    assert e.write_chains([])["written"] == 0


# ── root lookup ───────────────────────────────────────────────────────

def test_root_of_matches_exactly_and_by_contained_key():
    from routes.brain_finding_edges import root_of
    rmap = {"scheduler.facility_discovery runs=0": ("iso ingestion dead", 0.9)}
    assert root_of({"issue": "scheduler.facility_discovery runs=0"}, rmap) \
        == "iso ingestion dead"
    assert root_of(
        {"claim": "Brain finding: scheduler.facility_discovery runs=0 @ /x"},
        rmap) == "iso ingestion dead"
    assert root_of({"issue": "something unrelated entirely"}, rmap) == ""


def test_short_keys_cannot_swallow_unrelated_findings():
    """A two-word key like 'slow api' matching by substring would drag every
    finding whose text contains it into one bogus group."""
    from routes.brain_finding_edges import root_of
    assert root_of({"issue": "the api is slow today"},
                   {"slow": ("root", 0.9)}) == ""


def test_no_edges_means_no_root():
    from routes.brain_finding_edges import root_of
    assert root_of({"issue": "anything"}, {}) == ""
    assert root_of(None, {"a": ("b", 0.5)}) == ""


# ── the ranking consequence ───────────────────────────────────────────

def _patch_map(monkeypatch, mapping):
    import routes.brain_finding_edges as e
    monkeypatch.setattr(e, "load_root_map", lambda: mapping)


def test_siblings_are_deferred_behind_unrelated_work(monkeypatch):
    """The payoff. Before this, a run could spend its whole per-run budget
    opening draft PRs against four symptoms of one cause — none of which fixes
    it — and then record four failures against the fix-class."""
    from routes.brain_work_selector import collapse_to_roots
    _patch_map(monkeypatch, {
        "symptom a": ("one broken thing", 0.9),
        "symptom b": ("one broken thing", 0.9),
        "symptom c": ("one broken thing", 0.9)})
    out = collapse_to_roots([{"issue": "symptom a"}, {"issue": "symptom b"},
                             {"issue": "unrelated work"}, {"issue": "symptom c"}])
    names = [c["issue"] for c in out]
    assert names[0] == "symptom a", "the highest-ranked member must lead"
    assert names.index("unrelated work") < names.index("symptom b")
    assert names.index("unrelated work") < names.index("symptom c")


def test_collapse_is_a_permutation_never_a_filter(monkeypatch):
    """★THE SAFETY PROPERTY. rank_work is documented rank-only: the caller
    slices the front under MAX_DRAFT_PRS_PER_RUN, so a dropped candidate is
    lost work, not deferred work."""
    from routes.brain_work_selector import collapse_to_roots
    _patch_map(monkeypatch, {f"s{i}": ("root", 0.9) for i in range(5)})
    given = [{"issue": f"s{i}"} for i in range(5)] + [{"issue": "other"}]
    out = collapse_to_roots(given)
    assert len(out) == len(given)
    assert sorted(c["issue"] for c in out) == sorted(c["issue"] for c in given)


def test_deferred_siblings_say_why(monkeypatch):
    from routes.brain_work_selector import collapse_to_roots
    _patch_map(monkeypatch, {"a": ("root", 0.9), "b": ("root", 0.9)})
    out = collapse_to_roots([{"issue": "a"}, {"issue": "b"}])
    assert out[0]["_root_role"] == "representative"
    assert out[1]["_root_role"] == "deferred_sibling"
    assert "root cause" in out[1]["_rationale"]["deferred_because"]


def test_no_edges_leaves_the_order_exactly_as_it_was(monkeypatch):
    """Degrades to today's flat behaviour — the whole change is inert until
    L14 has actually written a chain."""
    from routes.brain_work_selector import collapse_to_roots
    _patch_map(monkeypatch, {})
    given = [{"issue": "a"}, {"issue": "b"}, {"issue": "c"}]
    assert [c["issue"] for c in collapse_to_roots(given)] == ["a", "b", "c"]


def test_a_broken_edge_store_cannot_reorder_anything(monkeypatch):
    import routes.brain_finding_edges as e
    from routes.brain_work_selector import collapse_to_roots

    def _boom():
        raise RuntimeError("db on fire")

    monkeypatch.setattr(e, "load_root_map", _boom)
    given = [{"issue": "a"}, {"issue": "b"}]
    assert [c["issue"] for c in collapse_to_roots(given)] == ["a", "b"]


def test_rank_work_still_returns_a_permutation(monkeypatch):
    """End to end through the real entry point."""
    from routes.brain_work_selector import rank_work
    _patch_map(monkeypatch, {"a": ("root", 0.9), "b": ("root", 0.9)})
    given = [{"issue": "a", "count": 2}, {"issue": "b", "count": 9},
             {"issue": "z", "count": 1}]
    out = rank_work(given)
    assert len(out) == 3
    assert sorted(c["issue"] for c in out) == ["a", "b", "z"]


def test_collapse_handles_empty_and_singleton():
    from routes.brain_work_selector import collapse_to_roots
    assert collapse_to_roots([]) == []
    assert len(collapse_to_roots([{"issue": "a"}])) == 1


# ── the wiring ────────────────────────────────────────────────────────

def test_l14_persists_its_chains():
    src = _src("routes", "brain_layer14_causal.py")
    assert "from routes.brain_finding_edges import write_chains" in src
    assert 'write_chains(analysis.get("causal_chains"))' in src


def test_edge_write_cannot_fail_a_successful_analysis():
    """The edge write sits after _persist_analysis inside its own try — a
    diagnostic store must never turn a good Claude call into a failed one."""
    src = _src("routes", "brain_layer14_causal.py")
    lines = src.splitlines()
    at = next(i for i, ln in enumerate(lines) if "write_chains(analysis" in ln)
    before = "\n".join(lines[max(0, at - 4):at])
    after = "\n".join(lines[at:at + 20])
    assert "try:" in before, "the edge write is not inside a try"
    assert "except Exception" in after, "the edge write has no except arm"


def test_shell_lane_2_sees_the_store_and_both_consumers():
    """Shell #49 lane 2 shipped RED on l14_persists and selector_reads_edges.
    Both must now be green — a stale FAIL is green-by-silence in reverse."""
    from routes.graph_master_shell import _lane_causal_edges

    class _NullCur:
        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return None

    checks = {c["id"]: c for c in _lane_causal_edges(_NullCur())}
    assert checks["l14_persists"]["pass"] is True
    assert checks["selector_reads_edges"]["pass"] is True


# ── #49 lane 5: the tick's nodes, declared ────────────────────────────

def test_tier3_nodes_are_declared_with_dependencies():
    from routes.brain_master_orchestrator import ORCHESTRATOR_NODES_T3
    assert len(ORCHESTRATOR_NODES_T3) == 4
    for n in ORCHESTRATOR_NODES_T3:
        assert n["method"] in ("GET", "POST")
        assert n["path"].startswith("/api/")
        assert "depends_on" in n, "a node without depends_on is not a node"
    steps = [n["step"] for n in ORCHESTRATOR_NODES_T3]
    assert len(set(steps)) == 4, "duplicate step names collide in the report"


def test_runner_preserves_declaration_order(monkeypatch):
    """The report must read identically whether or not the fan-out is on — a
    diff there should mean the system changed, not that the scheduler did."""
    from routes import brain_master_orchestrator as o
    monkeypatch.setattr(o, "_call",
                        lambda m, p, **k: {"ok": True, "http": 200, "ms": 1})
    monkeypatch.setenv("BRAIN_MASTER_PARALLEL", "1")
    par = [s["step"] for s in o._run_nodes(o.ORCHESTRATOR_NODES_T3)]
    monkeypatch.delenv("BRAIN_MASTER_PARALLEL")
    ser = [s["step"] for s in o._run_nodes(o.ORCHESTRATOR_NODES_T3)]
    assert par == ser == [n["step"] for n in o.ORCHESTRATOR_NODES_T3]


def test_runner_is_dormant_by_default(monkeypatch):
    """The 07-03 outage was thread-pool starvation from this very tick, and
    every step is a self-HTTP call served by that same pool."""
    from routes import brain_master_orchestrator as o
    monkeypatch.delenv("BRAIN_MASTER_PARALLEL", raising=False)
    assert o._parallel_enabled() is False
    monkeypatch.setenv("BRAIN_MASTER_PARALLEL", "1")
    assert o._parallel_enabled() is True


def test_parallel_width_is_bounded(monkeypatch):
    from routes import brain_master_orchestrator as o
    monkeypatch.setenv("BRAIN_MASTER_PARALLEL_MAX", "999")
    assert o._parallel_width() <= 6
    monkeypatch.setenv("BRAIN_MASTER_PARALLEL_MAX", "0")
    assert o._parallel_width() >= 1
    monkeypatch.setenv("BRAIN_MASTER_PARALLEL_MAX", "nonsense")
    assert o._parallel_width() == 3


def test_a_node_whose_dependency_failed_is_skipped_not_run(monkeypatch):
    """Running a node against a missing prerequisite produces a confident
    wrong answer, which is worse than an honest skip."""
    from routes import brain_master_orchestrator as o
    monkeypatch.setattr(o, "_call", lambda m, p, **k: {"ok": False, "error": "boom"})
    nodes = ({"step": "a", "method": "GET", "path": "/api/a", "depends_on": ()},
             {"step": "b", "method": "GET", "path": "/api/b", "depends_on": ("a",)})
    out = {s["step"]: s for s in o._run_nodes(nodes)}
    assert out["a"]["ok"] is False
    assert out["b"].get("skipped") is True
    assert "unmet_dependency" in out["b"]["error"]


def test_runner_terminates_on_an_impossible_dependency(monkeypatch):
    from routes import brain_master_orchestrator as o
    monkeypatch.setattr(o, "_call", lambda m, p, **k: {"ok": True, "ms": 1})
    nodes = ({"step": "a", "method": "GET", "path": "/api/a",
              "depends_on": ("nonexistent",)},)
    out = o._run_nodes(nodes)
    assert out and out[0].get("skipped") is True


def test_orchestrator_uses_the_declared_nodes():
    src = _src("routes", "brain_master_orchestrator.py")
    assert "_run_nodes(ORCHESTRATOR_NODES_T3)" in src
    # The four inline tier-3 _call lines they replaced must be gone.
    assert '_call("GET", "/api/v1/brain/lifecycle/audit")' not in src


def test_edge_insert_is_conflict_safe():
    """regression-lint's insert-no-on-conflict rule, pinned at the source. Two
    workers can analyse at once, and an edge is identified by (cause, effect) —
    a bare INSERT would double-write it. Bare DO NOTHING (no conflict target)
    on purpose: it stays valid whether or not the unique index built."""
    src = _src("routes", "brain_finding_edges.py")
    i = src.index("INSERT INTO brain_finding_edges")
    assert "ON CONFLICT DO NOTHING" in src[i:i + 400]


def test_a_failed_unique_index_degrades_the_store_but_does_not_disable_it():
    """A unique index can legitimately fail to build on a table that predates
    it. Losing the index is a degraded store; losing the store is an outage."""
    from routes.brain_finding_edges import ensure_schema

    class _Cur:
        def execute(self, sql, args=None):
            if "UNIQUE INDEX" in sql:
                raise RuntimeError("duplicate key values violate uniqueness")

    assert ensure_schema(_Cur()) is True


def test_a_failed_create_table_disables_the_store():
    from routes.brain_finding_edges import ensure_schema

    class _Cur:
        def execute(self, sql, args=None):
            raise RuntimeError("no permission")

    assert ensure_schema(_Cur()) is False
