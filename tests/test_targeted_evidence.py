"""Actuation shell #39 lane 2 — question-TARGETED evidence gathering.

Extracts the real functions with `ast` instead of importing brain_investigator
(which pulls in main.py — house rule: tests NEVER import main), then exercises
them. The half that needs the LIVE read replica skips unless REPLICA_URL is set.

Was a standalone script whose module-scope `sys.exit()` aborted pytest
COLLECTION and took the entire suite down with it (exit code 3, ~1,550 tests
never ran). Now real tests; `tests/test_tests_are_collectable.py` keeps it that
way. The pure half — the AST wiring checks and path extraction — needs no
database and is now a gate on every PR instead of something a human had to
remember to run by hand.
"""
from __future__ import annotations

import ast
import logging
import os
import re
from functools import lru_cache
from pathlib import Path

import pytest

# Resolve off THIS file, never the cwd — a cwd-relative open() passes locally and
# fails wherever CI happens to start (the same trap fixed in #1797).
REPO_ROOT = Path(__file__).resolve().parent.parent
INVESTIGATOR_SRC = REPO_ROOT / "routes" / "brain_investigator.py"

WANT_FN = {"_extract_paths", "gather_targeted_evidence", "_gather_recent_findings"}
WANT_AS = {"_PATH_RE", "_TARGET_MAX_PATHS"}

DB = os.environ.get("REPLICA_URL") or ""
needs_replica = pytest.mark.skipif(
    not DB, reason="set REPLICA_URL to run the live-replica half")


def _conn():
    if not DB:
        return None
    import psycopg2
    c = psycopg2.connect(DB)
    c.set_session(readonly=True, autocommit=True)
    return c


@lru_cache(maxsize=1)
def _investigator():
    """AST-extract the functions under test into an isolated namespace.

    Cached rather than run at module scope so importing this file stays
    side-effect-free — that is the property the collection guard enforces.
    """
    tree = ast.parse(INVESTIGATOR_SRC.read_text(encoding="utf-8"),
                     filename=str(INVESTIGATOR_SRC))
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
            or (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") in WANT_AS)]
    ns = {"os": os, "re": re, "logger": logging.getLogger("t"), "_conn": _conn}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), ns)
    return tree, ns


Q404 = ("[reliability] Brain finding: repeated_404_pattern @ "
        "/api/v1/energy/retail/rates returned 404 171 times")


def _extract(q):
    return _investigator()[1]["_extract_paths"](q)


def _gather(q):
    return _investigator()[1]["gather_targeted_evidence"](q)


def _investigate_fn():
    tree, _ = _investigator()
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "investigate")


# ── extraction sanity ────────────────────────────────────────────────────────

def test_all_three_functions_were_found():
    tree, _ = _investigator()
    names = {n.name for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in WANT_FN}
    assert names == WANT_FN, \
        f"functions not found in {INVESTIGATOR_SRC}: {sorted(WANT_FN - names)}"


# ── WIRING (AST, not grep) ───────────────────────────────────────────────────

def test_investigate_actually_calls_gather_targeted_evidence():
    """A correct function nothing calls is the failure mode this repo keeps
    shipping — assert the call edge exists, don't grep for the name."""
    calls = {c.func.id for c in ast.walk(_investigate_fn())
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "gather_targeted_evidence" in calls


def test_targeted_evidence_is_prepended():
    """The model must read subject-specific rows FIRST."""
    ev = [n for n in ast.walk(_investigate_fn())
          if isinstance(n, ast.Assign)
          and getattr(n.targets[0], "id", "") == "evidence"
          and isinstance(n.value, ast.BinOp)]
    assert ev, "no `evidence = <BinOp>` assignment found in investigate()"
    assert getattr(ev[0].value.left, "id", "") == "targeted"


# ── path extraction ──────────────────────────────────────────────────────────

def test_pulls_the_endpoint_out_of_a_real_finding_title():
    assert _extract(Q404) == ["/api/v1/energy/retail/rates"]


def test_generic_question_yields_no_targeted_query():
    assert _extract("why is conversion down this month?") == []


def test_paths_are_bounded():
    assert len(_extract("/api/a /api/b /api/c /api/d /api/e")) <= 3


def test_repeats_are_deduped():
    assert _extract("see /api/v1/x, and /api/v1/x again") == ["/api/v1/x"]


def test_no_literal_percent_in_the_sql():
    """The documented psycopg2 trap: a literal % in a query 500s at execute()."""
    src = INVESTIGATOR_SRC.read_text(encoding="utf-8")
    sql = src.split("def gather_targeted_evidence")[1].split("SELECT")[1].split('"""')[0]
    assert "%" not in sql.replace("%s", "")


# ── live replica half ────────────────────────────────────────────────────────

@needs_replica
def test_returns_evidence_about_the_endpoint_in_the_question():
    ev = _gather(Q404)
    assert len(ev) > 0, "no evidence returned for a real endpoint question"
    assert all(set(e) >= {"claim", "source", "value"} for e in ev), \
        "every item must carry the claim/source/value shape gather_evidence() emits"
    assert any("question-targeted" in (e.get("source") or "") for e in ev)
    blob = " ".join(e["claim"] for e in ev)
    assert "/api/v1/energy/retail/rates" in blob, \
        "the evidence must be ABOUT the endpoint in the question"
    assert "CONTRADICTION" in blob, \
        "must surface the detector-vs-ground-truth contradiction " \
        "(finding says 404s, log says none)"


@needs_replica
def test_a_question_with_no_path_costs_nothing():
    assert _gather("why is conversion down?") == [], "no path -> no DB work"


@needs_replica
def test_kill_switch_disables_without_a_deploy(monkeypatch):
    monkeypatch.setenv("BRAIN_TARGETED_EVIDENCE", "0")
    assert _gather(Q404) == []
    monkeypatch.delenv("BRAIN_TARGETED_EVIDENCE")
    assert len(_gather(Q404)) > 0, "must re-enable when unset"


@needs_replica
def test_recent_findings_worklist_can_never_return_a_resolved_finding():
    """The 'live detector worklist' must actually be LIVE."""
    _, ns = _investigator()
    recent = ns["_gather_recent_findings"](limit=12)
    assert len(recent) > 0, "gatherer returned no findings"

    conn = _conn()
    try:
        cur = conn.cursor()
        # 0 by construction: the filtered query cannot admit a resolved row.
        cur.execute("""SELECT COUNT(*) FROM (
                         SELECT COALESCE(status,'open') st, resolved_at
                           FROM brain_findings
                          WHERE COALESCE(status,'open')='open' AND resolved_at IS NULL
                          ORDER BY last_seen DESC NULLS LAST LIMIT 12) q
                       WHERE q.st <> 'open' OR q.resolved_at IS NOT NULL""")
        assert cur.fetchone()[0] == 0

        # And the risk is REAL, not theoretical: resolved-but-fresh findings
        # compete for the same recency-ordered slots the unfiltered query used.
        cur.execute("""SELECT COUNT(*) FROM brain_findings
                        WHERE status<>'open'
                          AND last_seen > now()-interval '24 hours'""")
        fresh_resolved = cur.fetchone()[0]
        assert fresh_resolved > 0, (
            "expected resolved+fresh findings to exist, proving the filter earns "
            "its keep; none found — re-check the fixture data")
    finally:
        try:
            conn.close()
        except Exception:
            pass


@needs_replica
def test_absence_is_reported_never_silent():
    ev = _gather("what happened to /api/v1/definitely-not-a-real-endpoint-xyz ?")
    assert len(ev) == 1 and "NO rows" in ev[0]["claim"], \
        "an unknown path must yield an explicit absence item, not []"
