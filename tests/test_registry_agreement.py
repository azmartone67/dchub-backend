#!/usr/bin/env python3
"""tests/test_registry_agreement.py — two registries watching the same table must
not disagree about what "fresh" means.

NO NETWORK, NO DB. The three registries are parsed from source with `ast`/regex
and compared.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT. W1's third step was scoped as
"consolidate onto one registry". MEASUREMENT KILLED THAT SCOPE, and this file is
what replaced it. The three are not duplicates of each other:

  routes/infra_growth.py  _LAYERS   16 tables — a GROWTH board: row-count deltas
                                    plus a freshness column, tagged by cadence
  routes/data_freshness_radar.py
                          _DOMAINS  14 tables — a STALENESS SLA per domain, which
                                    self-heal escalates into the Brain
  routes/_freshness.py    QUERIES    8 names  — a HEARTBEAT PROBE SET emitting
                                    *_age_seconds, and it watches
                                    information_schema, users and
                                    db_health_snapshots. Those are health checks,
                                    not datasets. Folding it into a dataset
                                    registry is a category error.

Union 32, intersection ZERO. Only FOUR tables are watched by more than one
registry — and all four disagree, which is the real defect and is far smaller
than a merge:

    fiber_routes        _LAYERS created_at / 75d   vs  _DOMAINS updated_at / 60d
    gas_pipelines       _LAYERS created_at / 130d  vs  _DOMAINS updated_at / 60d
    substations         _LAYERS created_at / 10d   vs  _DOMAINS updated_at / 60d
    transmission_lines  _LAYERS created_at / 120d  vs  _DOMAINS updated_at / 60d

substations is the sharpest: a SIX-FOLD disagreement about when it counts as
stale. And created_at vs updated_at are not interchangeable — for a full-replace
lane created_at is rewritten every run and tracks refresh, while for an
incremental lane it tracks only NEW rows. Which is correct depends on the lane,
which is exactly what infra_growth's own module docstring documents.

★ THIS FILE CHANGES NO SEMANTICS. Picking a winner per table changes what
production monitoring measures and needs its own evidence per table. The four
existing conflicts are FROZEN below as a named backlog; the test fails when a
NEW one appears, when an existing one CHANGES shape, and when the count drops
without the baseline being updated — so fixing one is a deliberate act under
review rather than a silent edit.

Run standalone:   python3 tests/test_registry_agreement.py
Run under pytest: pytest tests/test_registry_agreement.py
"""
import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Frozen 2026-09-03, measured from source. Each entry is the DISAGREEMENT, not a
# blessing of it. Lower this dict as conflicts are reconciled — the test fails on
# a DECREASE too, on purpose: a baseline nobody must update stops describing
# anything (same shape as tests/test_admin_gate_fail_closed.py's _BASELINE).
_KNOWN_CONFLICTS = {
    "fiber_routes":       {"layers": ("created_at", "75"),  "domains": ("updated_at", 60.0)},
    "gas_pipelines":      {"layers": ("created_at", "130"), "domains": ("updated_at", 60.0)},
    "substations":        {"layers": ("created_at", "10"),  "domains": ("updated_at", 60.0)},
    "transmission_lines": {"layers": ("created_at", "120"), "domains": ("updated_at", 60.0)},
}


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def layers():
    """table -> (freshness column, max_stale_days as declared)."""
    src = _read("routes/infra_growth.py")
    rows = re.findall(r'\(\s*"([^"]+)",\s*"([^"]+)",\s*"([a-z]+)",\s*(\w+|None)\)',
                      re.search(r"_LAYERS\s*=\s*\[(.*?)\n\]", src, re.S).group(1))
    fresh = dict(re.findall(r'"([a-z_0-9]+)":\s+"([a-z_]+)"',
                 re.search(r"_FRESH_COL\s*=\s*\{(.*?)\n\}", src, re.S).group(1)))
    return {tbl: (fresh.get(label), stale) for label, tbl, _cad, stale in rows}


def domains():
    """table -> (first candidate timestamp column, SLA in days)."""
    src = _read("routes/data_freshness_radar.py")
    doms = ast.literal_eval(re.search(r"_DOMAINS\s*=\s*(\[.*?\n\])", src, re.S).group(1))
    return {t: (cols[0], sla / 24.0) for _n, tables, cols, sla in doms for t in tables}


def queries():
    """The heartbeat probe set — names only. NOT a dataset registry."""
    return set(re.findall(r"FROM\s+([a-z_][a-z0-9_]*)", _read("routes/_freshness.py")))


def _conflicts():
    L, D = layers(), domains()
    out = {}
    for t in sorted(set(L) & set(D)):
        lcol, lstale = L[t]
        dcol, dsla = D[t]
        differs = (lcol and lcol != dcol) or (
            lstale not in ("None", None) and abs(float(lstale) - dsla) > 1)
        if differs:
            out[t] = {"layers": (lcol, lstale), "domains": (dcol, dsla)}
    return out


# ── the ratchet ────────────────────────────────────────────────────────────

def test_no_new_registry_disagreement():
    """A table watched by two registries with two different meanings of fresh."""
    found = _conflicts()
    new = sorted(set(found) - set(_KNOWN_CONFLICTS))
    assert not new, (
        f"NEW registry disagreement on {new}. Two registries now watch these "
        f"tables with different freshness columns or materially different "
        f"staleness thresholds, so they will report different answers for the "
        f"same data. Reconcile them, or add the conflict to _KNOWN_CONFLICTS "
        f"with a reason. Found: "
        + "; ".join(f"{t}: _LAYERS={found[t]['layers']} vs _DOMAINS={found[t]['domains']}"
                    for t in new))


def test_known_conflicts_have_not_silently_changed_shape():
    """A frozen conflict that MOVED is a semantic change nobody reviewed."""
    found = _conflicts()
    for t, want in _KNOWN_CONFLICTS.items():
        if t not in found:
            continue
        assert found[t] == want, (
            f"{t}'s registry disagreement changed shape: expected {want}, got "
            f"{found[t]}. If this was deliberate, update _KNOWN_CONFLICTS in the "
            f"same commit so the change is visible in review.")


def test_resolved_conflicts_must_be_removed_from_the_baseline():
    """Fails on a DECREASE, so the backlog stays honest rather than aspirational."""
    stale = sorted(set(_KNOWN_CONFLICTS) - set(_conflicts()))
    assert not stale, (
        f"{stale} no longer disagree — good. Remove them from _KNOWN_CONFLICTS "
        f"so the number keeps describing reality.")


# ── non-vacuity: the parsers must actually be reading the registries ───────

def test_every_registry_parses_to_a_plausible_size():
    """If a parser silently returns {}, every assertion above passes forever."""
    L, D, Q = layers(), domains(), queries()
    assert len(L) >= 12, f"_LAYERS parsed to {len(L)} entries — the parser broke"
    assert len(D) >= 10, f"_DOMAINS parsed to {len(D)} entries — the parser broke"
    assert len(Q) >= 5, f"QUERIES parsed to {len(Q)} entries — the parser broke"


def test_the_registries_actually_overlap():
    """The whole comparison is vacuous if nothing is watched twice."""
    both = set(layers()) & set(domains())
    assert len(both) >= 4, (
        f"only {len(both)} table(s) watched by both _LAYERS and _DOMAINS — if "
        f"this dropped, either coverage was lost or a parser is misreading")


def test_the_probe_set_is_not_mistaken_for_a_dataset_registry():
    """_freshness.QUERIES watches health objects. If a future change starts
    treating it as a dataset registry, these names would become 'datasets'."""
    q = queries()
    assert {"information_schema", "users"} & q, (
        "QUERIES no longer probes health objects — if it became a dataset "
        "registry, the category distinction this file documents is gone and "
        "the consolidation question needs re-asking")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
