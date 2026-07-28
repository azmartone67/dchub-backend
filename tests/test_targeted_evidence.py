"""Actuation shell #39 lane 2 — question-TARGETED evidence gathering.

Extracts the real functions with `ast` instead of importing brain_investigator
(which pulls in main.py — house rule: tests NEVER import main), then asserts
against them. The live-replica half runs only with REPLICA_URL set.

WHY THIS IS PYTEST FUNCTIONS AND NOT A SCRIPT
---------------------------------------------
It shipped as a standalone script: a module body that printed a report and ended
in a bare module-scope `sys.exit(1 if fail else 0)`. It lives in tests/ under a
`test_` prefix, so pytest imported it AT COLLECTION TIME, the body ran, and the
SystemExit tore down the whole session:

    INTERNALERROR> File "tests/test_targeted_evidence.py", line 61, in <module>
    INTERNALERROR>   sys.exit(1 if fail else 0)
    INTERNALERROR> SystemExit: 0
    Process completed with exit code 3

Exit 3 means ZERO tests ran -- not "some tests failed". Every test in the repo
was dead for the window this was on main. This is the second time: #1797 fixed
byte-identical damage in tests/test_climate_intel_cache.py the same day.

If you want a script, put it anywhere that is not tests/ or drop the `test_`
prefix. Nothing under tests/ may execute work or exit at module scope.

Every assertion from the script is preserved; the live half now skips cleanly
instead of exiting, and the source path resolves from __file__ rather than the
cwd (the same script assumed it was run from the repo root).
"""
import ast
import functools
import logging
import os
import pathlib
import re

import pytest

# Resolve from the test file, never the cwd -- the suite is invoked from both
# the repo root and from tests/, and the original `open('routes/...')` only
# worked from the root.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_PATH = REPO_ROOT / "routes" / "brain_investigator.py"

WANT_FN = {"_extract_paths", "gather_targeted_evidence", "_gather_recent_findings"}
WANT_AS = {"_PATH_RE", "_TARGET_MAX_PATHS"}

REPLICA_URL = os.environ.get("REPLICA_URL") or ""

# The live half talks to the read replica. Skip, never exit -- an exit at module
# scope is what killed collection in the first place.
requires_replica = pytest.mark.skipif(
    not REPLICA_URL,
    reason="set REPLICA_URL to run the live-replica half",
)

Q404 = ("[reliability] Brain finding: repeated_404_pattern @ "
        "/api/v1/energy/retail/rates returned 404 171 times")


def _conn():
    """Read-only connection to the replica, or None when unconfigured."""
    if not REPLICA_URL:
        return None
    import psycopg2
    c = psycopg2.connect(REPLICA_URL)
    c.set_session(readonly=True, autocommit=True)
    return c


@functools.lru_cache(maxsize=1)
def _extracted():
    """AST-extract the real functions out of brain_investigator.py.

    Runs inside the tests rather than at module scope: a raise during import is
    a COLLECTION error that takes the whole run down instead of failing one
    test -- see the module docstring.

    Returns (globals_dict, module_tree).
    """
    assert SRC_PATH.is_file(), "{p} not found".format(p=SRC_PATH)
    src = SRC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign)
            and getattr(n.targets[0], "id", "") in WANT_AS)
    ]
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert found == WANT_FN, (
        "AST extraction did not find every function it needs: missing {m}. "
        "A helper that is not extracted raises NameError into the enclosing "
        "`except Exception` and the path goes silently untested (the #1797 "
        "_ci_rkey trap).".format(m=sorted(WANT_FN - found))
    )

    g = {"os": os, "re": re, "logger": logging.getLogger("t"), "_conn": _conn}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    return g, tree


def _gather():
    return _extracted()[0]["gather_targeted_evidence"]


def _extract():
    return _extracted()[0]["_extract_paths"]


# ── wiring: investigate() actually calls it (AST, not grep) ──────────────────

def test_investigate_calls_gather_targeted_evidence():
    """A correct function nothing calls is the failure mode this repo keeps
    shipping."""
    _, tree = _extracted()
    inv = next(n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "investigate")
    calls = {c.func.id for c in ast.walk(inv)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "gather_targeted_evidence" in calls, (
        "investigate() never calls gather_targeted_evidence -- the gatherer "
        "would be dead code."
    )


def test_targeted_evidence_is_prepended():
    """The model must read subject-specific rows FIRST."""
    _, tree = _extracted()
    inv = next(n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "investigate")
    assigns = [n for n in ast.walk(inv) if isinstance(n, ast.Assign)]
    ev = [n for n in assigns
          if getattr(n.targets[0], "id", "") == "evidence"
          and isinstance(n.value, ast.BinOp)]
    assert ev and getattr(ev[0].value.left, "id", "") == "targeted", (
        "targeted evidence is not prepended to the evidence list"
    )


# ── path extraction (pure, no DB) ───────────────────────────────────────────

def test_pulls_the_endpoint_out_of_a_real_finding_title():
    got = _extract()(Q404)
    assert got == ["/api/v1/energy/retail/rates"], got


def test_generic_question_yields_no_path():
    assert _extract()("why is conversion down this month?") == [], (
        "a generic question must not trigger a targeted query"
    )


def test_paths_are_bounded():
    many = _extract()("/api/a /api/b /api/c /api/d /api/e")
    assert len(many) <= 3, "expected <=3 paths, got {n}".format(n=len(many))


def test_repeated_paths_are_deduped():
    assert _extract()("see /api/v1/x, and /api/v1/x again") == ["/api/v1/x"]


def test_no_literal_percent_in_the_sql():
    """The documented psycopg2 trap: a literal % in the SQL 500s."""
    src = SRC_PATH.read_text(encoding="utf-8")
    sql = (src.split("def gather_targeted_evidence")[1]
              .split("SELECT")[1]
              .split('"""')[0]
              .replace("%s", ""))
    assert "%" not in sql, (
        "literal % left in the targeted-evidence SQL -- psycopg2 treats it as a "
        "placeholder and the query 500s"
    )


# ── live evidence against the real replica ──────────────────────────────────

@requires_replica
def test_returns_evidence_for_a_real_endpoint_question():
    ev = _gather()(Q404)
    assert len(ev) > 0, "no evidence returned for a real endpoint question"
    assert all(set(e) >= {"claim", "source", "value"} for e in ev), (
        "every item must have the claim/source/value shape gather_evidence() emits"
    )
    assert any("question-targeted" in (e.get("source") or "") for e in ev), (
        "items are not labelled question-targeted"
    )
    blob = " ".join(e["claim"] for e in ev)
    assert "/api/v1/energy/retail/rates" in blob, (
        "the evidence is not ABOUT the endpoint in the question"
    )
    assert "CONTRADICTION" in blob, (
        "does not surface the detector-vs-ground-truth contradiction "
        "(finding says 404s, log says none)"
    )


@requires_replica
def test_a_question_with_no_path_costs_nothing():
    assert _gather()("why is conversion down?") == [], (
        "no path must mean no DB work and an empty list"
    )


@requires_replica
def test_kill_switch(monkeypatch):
    """BRAIN_TARGETED_EVIDENCE=0 disables it with no deploy.

    monkeypatch restores the environment even if the assert fails -- the script
    version popped the var by hand and leaked it on failure.
    """
    gather = _gather()
    monkeypatch.setenv("BRAIN_TARGETED_EVIDENCE", "0")
    assert gather(Q404) == [], "kill switch did not disable the gatherer"
    monkeypatch.delenv("BRAIN_TARGETED_EVIDENCE")
    assert len(gather(Q404)) > 0, "did not re-enable when the var is unset"


@requires_replica
def test_recent_findings_worklist_is_live_and_open_only():
    """The 'live detector worklist' must be LIVE -- and never serve a resolved
    finding."""
    g, _ = _extracted()
    recent = g["_gather_recent_findings"](limit=12)
    assert len(recent) > 0, "gatherer returned no findings at all"

    conn = _conn()
    try:
        cur = conn.cursor()
        # Behavioural, not a source grep: re-run the filter against the DB and
        # prove it cannot admit a non-open row.
        cur.execute("""SELECT COUNT(*) FROM (
                         SELECT COALESCE(status,'open') st, resolved_at
                         FROM brain_findings
                         WHERE COALESCE(status,'open')='open'
                           AND resolved_at IS NULL
                         ORDER BY last_seen DESC NULLS LAST LIMIT 12) q
                       WHERE q.st <> 'open' OR q.resolved_at IS NOT NULL""")
        assert cur.fetchone()[0] == 0, (
            "the filtered query returned a resolved finding -- it is 0 by "
            "construction, so the filter is wrong"
        )

        cur.execute("""SELECT COUNT(*) FROM brain_findings
                       WHERE status<>'open'
                         AND last_seen > now()-interval '24 hours'""")
        fresh_resolved = cur.fetchone()[0]
        assert fresh_resolved > 0, (
            "the risk this filter guards is supposed to be REAL, not "
            "theoretical: expected resolved findings with last_seen<24h "
            "competing for the same recency-ordered slots, found none"
        )
    finally:
        conn.close()


@requires_replica
def test_absence_is_reported_never_silent():
    ev = _gather()("what happened to /api/v1/definitely-not-a-real-endpoint-xyz ?")
    assert len(ev) == 1 and "NO rows" in ev[0]["claim"], (
        "an unknown path must yield an explicit absence item, not []"
    )
