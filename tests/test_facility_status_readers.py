"""Reader-side guards for the canonical facility-status vocabulary.

tests/test_facility_status_canon.py (PR #2047) pinned the WRITE boundary: every
ingest path routes status through util.facility_status.canon_status. This file
pins the READ boundary, which is what actually blocked the canon backfill
(backfill_facility_status_canon.py, PR #2054).

THE BUG CLASS
-------------
Five queries used the *status literal* as a proxy for "not one of the 10,435
zero-MW shell rows" — either by admitting `LOWER(status)='operational'` or by
excluding `COALESCE(status,'') <> 'active'`. The canon backfill rewrites
'active' -> 'Operational', which collapses both cohorts onto the SAME literal,
so every one of those predicates silently changes meaning:

  * routes/mcp_tier1_tools.py  the inverse trap — shells flood IN, dragging
                               COALESCE(AVG(power_mw),0) down
  * main.py /compare           exact lowercase match -> 12 rows today, 0 after
  * routes/radar.py            \\
  * routes/ai_capacity_index.py > count-based exclusions -> shells flood IN
  * routes/market_intel_preview.py /

The durable fix is to stop asking `status` to answer a dedup question. Lifecycle
comes from util/status_taxonomy (which owns every spelling of both cohorts, so
it reads identically before and after the backfill) and the shells are excluded
by the #1539 canonical fleet filter, COALESCE(is_duplicate,0)=0.

WHY A SOURCE FENCE *AND* A SIMULATION
-------------------------------------
The source fence below is a grep with an AST assert on the extraction — it
catches a literal coming back. But a comment satisfies grep, so the real proof
is test_predicates_are_invariant_under_the_backfill(): it runs each predicate
against the live table twice, once with the backfill SIMULATED in SQL, and
asserts the row set does not move. That is behaviour, not spelling.
"""
import ast
import os
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The predicates backfill_facility_status_canon.py refuses to run against.
BLOCKERS = ("<> 'active'", "LOWER(status) = 'operational'", "status = 'operational'")

READERS = (
    "routes/mcp_tier1_tools.py",
    "routes/radar.py",
    "routes/ai_capacity_index.py",
    "routes/market_intel_preview.py",
    "main.py",
)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _sql_strings(src, rel):
    """Every string constant EXCEPT docstrings — the same extraction the
    backfill tool's scanner uses, so this test fails on exactly what would
    re-block the backfill.

    Asserts the parse produced constants: an empty extraction would make every
    assertion below vacuously true (the silent-green trap).
    """
    tree = ast.parse(src)
    doc = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)) and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            doc.add(id(body[0].value))
    out = [(n.value, n.lineno) for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and id(n) not in doc]
    assert out, f"ast extraction produced no string constants for {rel}"
    return out


def test_no_reader_carries_a_backfill_blocking_literal():
    """The fence. Note it must hold for SQL *comments* too: a `--` comment sits
    inside the query string constant, so narrating a removed predicate there is
    indistinguishable from a live one and re-arms the block (this is exactly how
    the first attempt at this fix still failed the scanner)."""
    hits = []
    for rel in READERS:
        for text, lineno in _sql_strings(_read(rel), rel):
            for lit in BLOCKERS:
                if lit in text:
                    hits.append(f"{rel}:{lineno}: {lit}")
    assert not hits, (
        "status-literal predicate back in a reader — this re-blocks "
        "backfill_facility_status_canon.py:\n  " + "\n  ".join(hits))


def test_readers_route_through_the_shared_taxonomy_and_fleet_filter():
    tier1 = _read("routes/mcp_tier1_tools.py")
    assert "from util.status_taxonomy import operational_sql" in tier1
    assert "{operational_sql()}" in tier1, "tier1 stopped using the taxonomy"
    assert "COALESCE(is_duplicate, 0) = 0" in tier1, "tier1 lost the #1539 fleet filter"

    m = _read("main.py")
    assert "from util.status_taxonomy import operational_sql as _osql" in m
    assert "{_op_sql}" in m and "{_pipe_sql}" in m, (
        "/compare counters stopped using the taxonomy")

    for rel in ("routes/radar.py", "routes/ai_capacity_index.py",
                "routes/market_intel_preview.py"):
        assert "is_duplicate" in _read(rel), f"{rel} lost the #1539 fleet filter"


def test_taxonomy_owns_both_spellings_of_the_backfilled_cohort():
    """The property that makes the readers backfill-proof: the pre- and
    post-backfill spellings are BOTH operational, so the predicate cannot move."""
    from util.status_taxonomy import classify
    assert classify("active") == "operational"
    assert classify("Operational") == "operational"
    assert classify("operational") == "operational"


# ── behavioural proof (needs a DB; skipped without one) ────────────────────

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))


@pytest.mark.skipif(not _DB, reason="no database URL in the environment")
def test_predicates_are_invariant_under_the_backfill():
    """Run each predicate twice against the live table — once as-is, once with
    the backfill simulated in SQL — and assert the counts do not move.

    This is the assertion the source fence cannot make. A comment satisfies a
    grep; only the row count proves the predicate survived.
    """
    import psycopg2
    from util.status_taxonomy import operational_sql

    conn = psycopg2.connect(_DB, connect_timeout=20)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()

    # The backfill: 'active' and 'operational' both become 'Operational'.
    POST = ("CASE WHEN status IN ('active','operational') THEN 'Operational' "
            "ELSE status END")
    FLEET = "COALESCE(is_duplicate,0) = 0"

    def count(pred):
        cur.execute(f"SELECT COUNT(*) FROM discovered_facilities WHERE {pred}")
        return cur.fetchone()[0]

    now = operational_sql()
    post = operational_sql().replace("status", f"({POST})")

    n_now, n_post = count(f"{now} AND {FLEET}"), count(f"{post} AND {FLEET}")
    assert n_now == n_post, (
        f"the operational predicate MOVED under the backfill: {n_now} -> {n_post}")
    assert n_now > 0, "predicate matched nothing — the assertion above is vacuous"

    # Control: the OLD predicate must genuinely move, or this test proves nothing.
    old_now = count("LOWER(status) = 'operational'")
    old_post = count(f"LOWER({POST}) = 'operational'")
    assert old_now != old_post, (
        "CONTROL FAILED: the old literal predicate did not move under the "
        "simulated backfill, so this test cannot detect the bug it guards")

    conn.close()
