"""The re-resolve pass (Shell #36 lane 5a) stays wired and stays one-way.

in_facilities is stamped at INSERT and refreshed only when an entity is
re-mentioned, so an entity whose matching facility arrives LATER sits
unresolved forever — lane 5a's "resolver blind spot" regrew 0 → 9 between
2026-07-27 and 08-16 exactly this way. The fix is a bounded FALSE→TRUE
re-resolve pass on the scan cadence (routes/news_entity_extraction.py).

These guards pin the three properties that make the pass safe to keep:
  1. it RUNS — _scan calls it, so no human is in the loop;
  2. it is ONE-WAY — no UPDATE it issues may set in_facilities = FALSE
     (ground truth from the facilities tables is never overruled here);
  3. it uses the scan's own matcher (_already_known), so the lane and the
     resolver cannot drift apart.

All static/AST — CI runs with no DATABASE_URL, and a DB-gated check would
skip green while proving nothing. Every helper asserts it FOUND its target
first: an empty parse satisfies every "not in".
"""

import ast
import os

SRC = os.path.join(os.path.dirname(__file__), "..",
                   "routes", "news_entity_extraction.py")


def _tree():
    with open(SRC, encoding="utf-8") as f:
        return ast.parse(f.read())


def _func(tree, name):
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == name]
    assert fns, f"{name} not found in {SRC}"
    return fns[0]


def _calls(fn, callee):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == callee]


def test_scan_runs_the_reresolve_pass():
    scan = _func(_tree(), "_scan")
    assert _calls(scan, "_reresolve_unmatched"), (
        "_scan no longer calls _reresolve_unmatched — the lane 5a blind spot "
        "regrows silently until the next manual audit")


def test_reresolve_only_ever_sets_in_facilities_true():
    fn = _func(_tree(), "_reresolve_unmatched")
    updates = [" ".join(n.value.split()) for n in ast.walk(fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "UPDATE news_discovered_entities" in n.value]
    assert updates, "no UPDATE statement found inside _reresolve_unmatched"
    for u in updates:
        assert "in_facilities = TRUE" in u, u
        assert "in_facilities = FALSE" not in u, (
            "the re-resolve pass may never UN-resolve a row: " + u)


def test_reresolve_uses_the_scans_own_matcher():
    fn = _func(_tree(), "_reresolve_unmatched")
    assert _calls(fn, "_already_known"), (
        "_reresolve_unmatched must resolve via _already_known — a private "
        "variant is exactly how the lane and the resolver drift apart")


def test_reresolve_endpoint_is_admin_gated():
    ep = _func(_tree(), "ner_reresolve")
    assert _calls(ep, "_admin_ok"), (
        "/api/v1/admin/news-ner/re-resolve lost its _admin_ok() gate")
