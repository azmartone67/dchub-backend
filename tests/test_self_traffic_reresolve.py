"""One-time re-resolution of rows latched FALSE (2026-09-20).

self_traffic was two-state until today: "no mcp_call_log evidence YET" was
written as False, indistinguishable from "evidence exists and says external".
The backfill only revisits NULL, so those rows are stranded — our own traffic,
published as unconverted demand, permanently.

The endpoint promotes FALSE -> TRUE and only where mcp_call_log now shows a
'dchub-%' platform for that session. The three properties that make a mass
UPDATE on production telemetry safe to run are pinned here.

CI-SAFETY: source/AST-level plus pglast; no network, no DB.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "schema_repair.py")


def _norm(s):
    return " ".join((s or "").split())


@pytest.fixture(scope="module")
def src():
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def fn(src):
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "funnel_self_traffic_reresolve"):
            return node
    pytest.fail("funnel_self_traffic_reresolve not found")


@pytest.fixture(scope="module")
def candidates(src):
    i = src.index("_RERESOLVE_CANDIDATES = \"\"\"")
    return src[i:src.index('"""', i + 28)]


# ── it must converge ─────────────────────────────────────────────────

def test_the_exists_filter_is_inside_the_limit(candidates, src):
    """THE subtle one. Selecting the newest N *FALSE* rows and filtering by
    EXISTS afterwards re-picks the same unpromotable rows on every call and
    never converges — `promoted` would sit at 0 while `remaining` never moved.
    With EXISTS inside, every selected row IS promoted, so each pass strictly
    shrinks the FALSE set."""
    assert "EXISTS" in candidates, "no evidence predicate in the candidate set"
    assert "LIMIT" not in candidates, (
        "the shared candidate block must not carry its own LIMIT — the caller "
        "appends ORDER BY/LIMIT so the bound applies AFTER the EXISTS")
    composed = _norm(candidates + " ORDER BY s2.created_at DESC LIMIT %s")
    assert composed.index("EXISTS") < composed.index("LIMIT")


def test_promotion_is_monotonic(src, fn):
    """It may only write TRUE. A write of FALSE here would re-strand rows, and
    a real external caller can never qualify: the sole entry condition is
    evidence that the session WAS ours."""
    body = ast.get_source_segment(src, fn) or ""
    assert "SET self_traffic = TRUE" in _norm(body)
    assert "self_traffic = FALSE" not in _norm(body).replace(
        "s2.self_traffic IS FALSE", "")


def test_candidate_set_reads_only_false_rows(candidates):
    """IS FALSE, not = FALSE and not `IS NOT TRUE`: NULL rows are the heal's
    job, and sweeping them here would race the insert-time resolver."""
    assert "self_traffic IS FALSE" in _norm(candidates)


# ── it must not write unless asked ───────────────────────────────────

def test_dry_run_is_the_default_and_cannot_write(src, fn):
    """A mass UPDATE on production telemetry defaults to reporting. The UPDATE
    and the commit must live INSIDE the confirm branch — pinned structurally,
    because 'confirm' appearing in the function proves nothing about where the
    write sits."""
    confirms = [n for n in ast.walk(fn)
                if isinstance(n, ast.If) and "confirm" in ast.dump(n.test)]
    assert confirms, "no `if confirm:` gate around the write"
    guarded = "\n".join(ast.dump(s) for n in confirms for s in n.body)
    assert "UPDATE mcp_upgrade_signals" in guarded, (
        "the UPDATE is not inside the confirm branch — a dry run writes")
    assert "commit" in guarded
    whole = ast.dump(fn)
    assert whole.count("UPDATE mcp_upgrade_signals") == 1, (
        "more than one UPDATE in this function; only the guarded one may exist")


def test_confirm_requires_an_explicit_truthy_value(src, fn):
    body = ast.get_source_segment(src, fn) or ""
    assert re.search(r'request\.args\.get\("confirm"', body)
    assert '"1", "true", "yes"' in body


# ── a ceiling must not read as a total ───────────────────────────────

def test_remaining_is_capped_and_says_so(src, fn):
    """A bounded count reported as a total stops moving at the cap and looks
    exactly like convergence. The flag is what distinguishes 'ten thousand
    left' from 'at least ten thousand left'."""
    body = _norm(ast.get_source_segment(src, fn) or "")
    assert '"remaining_capped"' in body
    assert "min(n, _REMAINING_CAP)" in body


def test_done_is_computed_from_the_uncapped_count(src, fn):
    """`done` must come from n, not from the capped value — otherwise a capped
    read could report done while candidates remain."""
    body = _norm(ast.get_source_segment(src, fn) or "")
    assert 'out["done"] = (n == 0)' in body


def test_the_cap_query_asks_for_one_more_than_the_cap(src, fn):
    """LIMIT _REMAINING_CAP would make 'exactly at the cap' and 'over the cap'
    indistinguishable, so remaining_capped could never be True."""
    body = _norm(ast.get_source_segment(src, fn) or "")
    assert "_REMAINING_CAP + 1" in body


# ── and the SQL has to be SQL ────────────────────────────────────────

def test_both_composed_statements_parse_as_postgres(candidates):
    pglast = pytest.importorskip("pglast")
    cand = candidates.split('"""')[-1] if '"""' in candidates else candidates
    cand = cand.replace("%s", "'90 days'").replace("%%", "%")
    upd = ("UPDATE mcp_upgrade_signals s SET self_traffic = TRUE "
           " WHERE s.id IN (SELECT s2.id " + cand
           + " ORDER BY s2.created_at DESC LIMIT 5000)")
    cnt = "SELECT COUNT(*) FROM (SELECT 1 " + cand + " LIMIT 10001) t"
    for label, q in (("update", upd), ("count", cnt)):
        try:
            pglast.parse_sql(q)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{label} statement does not parse: {exc}")


def test_it_is_not_wired_into_the_repair_sweep(src):
    """POST /schema/repair already runs ~14s against a 15s edge timeout. A
    table-wide UPDATE added to SCHEMA_STATEMENTS would push it over and roll
    the whole repair back."""
    i = src.index("SCHEMA_STATEMENTS")
    sweep = src[i:src.index("def schema_repair", i)]
    assert "_RERESOLVE_CANDIDATES" not in sweep
    assert "self_traffic IS FALSE" not in sweep
