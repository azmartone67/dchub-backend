"""tests/test_loop_source_nodes.py — a root is not a gap (2026-08-12).

`no_declared_input` conflated two OPPOSITE states:

  · this loop is a ROOT — its producer is outside the board (external MCP
    clients, public HN/Reddit, a GitHub Actions cron). Nothing to do, ever.
  · this loop HAS an upstream and nobody wrote the edge. A real gap.

Lane 4 of shell #63 counted both as missing coverage, so 3 of 7 loops sat
permanently amber on a board where no amount of work could clear them. A
permanent amber is ignored exactly as fast as a permanent green — which is the
same disease as lane 2's frozen 85%, and both are fixed in this change.

Guards:
  (1) FABRICATED EDGE — a source loop is "fixed" by inventing an edge to some
      loop that merely runs nearby. That lie would then be trusted exactly as
      much as a proven edge (see the "declared" tier warning in LOOP_EDGES).
  (2) CONFLATION RETURN — external_source collapses back into
      no_declared_input, or lane 4 stops distinguishing them.
  (3) SILENT-BLIND — a source loop stops being reported at all, so a genuinely
      undeclared edge hides among the roots.
  (4) EVIDENCE-FREE — a source entry with no basis, i.e. an assertion nobody
      can check.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_loop_source_nodes.py -v
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _graph():
    return importlib.import_module("routes.graph_master_shell")


def _loops_mod():
    return importlib.import_module("routes.system_loops")


def _sources():
    return _graph().LOOP_SOURCE_PRODUCERS


def test_the_three_known_roots_are_typed():
    """These are the exact loops that read no_declared_input on 2026-08-12."""
    named = {s["loop"] for s in _sources()}
    assert {"mcp_traffic", "testimonial_ingest", "iso_extract"} <= named


@pytest.mark.parametrize("field", ["loop", "producer", "evidence", "basis"])
def test_every_source_carries_checkable_provenance(field):
    """★An assertion nobody can check is how the 'declared' edge tier goes bad.
    A source claim needs a named external producer and a basis someone can
    verify against the code."""
    for s in _sources():
        assert str(s.get(field) or "").strip(), \
            "source entry %r is missing %s" % (s.get("loop"), field)
    if field == "basis":
        for s in _sources():
            assert len(s["basis"]) > 60, \
                "source %r has a basis too short to verify" % s["loop"]


def test_a_source_is_never_also_an_edge_consumer():
    """★THE LIE THIS GUARDS. 'Fixing' a root by inventing an upstream would put
    a false edge in a graph the work-selector ranks on."""
    g = _graph()
    consumers = {e.get("consumer") for e in g.LOOP_EDGES}
    for s in _sources():
        assert s["loop"] not in consumers, (
            "%s is typed as an external SOURCE and also declared as an edge "
            "consumer — one of the two is a fabrication" % s["loop"])


def test_apply_loop_edges_separates_roots_from_gaps():
    m = _loops_mod()
    loops = [
        {"name": "mcp_traffic", "status": "alive"},        # typed source
        {"name": "dcpi_recompute", "status": "alive"},     # has a declared edge
        {"name": "iso_extract", "status": "alive"},        # typed source
        {"name": "totally_new_loop", "status": "alive"},   # genuine gap
    ]
    out = {l["name"]: l for l in m.apply_loop_edges(loops)}
    assert out["mcp_traffic"]["input_status"] == "external_source"
    assert out["mcp_traffic"].get("source_producer"), \
        "a source must name its external producer"
    assert out["iso_extract"]["input_status"] == "external_source"
    assert out["totally_new_loop"]["input_status"] == "no_declared_input", \
        "a genuinely undeclared loop must NOT be absorbed into the source set"


def test_green_on_stale_detection_still_works():
    """★The fix must not blind the board. dcpi_recompute consumes iso_extract;
    if the producer goes stale the consumer must still be flagged."""
    m = _loops_mod()
    loops = [
        {"name": "iso_extract", "status": "stale"},
        {"name": "dcpi_recompute", "status": "alive"},
    ]
    out = m.apply_loop_edges(loops)
    by = {l["name"]: l for l in out}
    assert by["dcpi_recompute"]["input_status"] == "stale"
    assert m.count_alive_on_stale_input(out) == 1


def test_a_source_that_goes_stale_does_not_silence_its_consumers():
    """iso_extract is a source AND a producer for dcpi_recompute. Typing it as
    a root must not stop it propagating staleness downstream."""
    m = _loops_mod()
    out = m.apply_loop_edges([
        {"name": "iso_extract", "status": "dead"},
        {"name": "dcpi_recompute", "status": "alive"},
    ])
    by = {l["name"]: l for l in out}
    assert by["iso_extract"]["input_status"] == "external_source"
    assert by["dcpi_recompute"]["input_status"] == "stale"


def test_lane4_reports_sources_separately_from_gaps():
    src = (_ROOT / "routes" / "context_integrity_master_shell.py").read_text(
        encoding="utf-8")
    assert '"external_source"' in src, \
        "lane 4 no longer distinguishes a root from an undeclared edge"
    # ★Substring chosen to survive the line-continuation the message is written
    # across — the first cut asserted a phrase split by a string concat and
    # failed on correct code, which is a guard lying in the safe direction but
    # lying nonetheless.
    assert "external sources" in src, \
        "lane 4 stopped reporting how many loops are sources"
    assert 'input_status") == "no_declared_input"' in src, \
        "lane 4 stopped counting genuinely undeclared edges"
