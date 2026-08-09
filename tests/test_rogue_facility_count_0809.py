"""The rogue 20,132 must not come back to an agent-facing facility count.

2026-08-09: /api/agents/intelligence-index published
`data_summary.facilities = 20132` — COUNT(*) over the LEGACY `facilities`
table, a different table from the canonical fleet, undeduplicated, every
lifecycle status. It reconciled with no published basis (canon "17,200+",
facilities_distinct 17,294, facilities_records 25,024) and it is quoted
verbatim by the MCP get_intelligence_index tool.

These guards fail if any agent-facing handler goes back to counting the legacy
table, or publishes a facility figure with no stated basis. Every guard asserts
its target was actually located first — a guard that silently finds nothing and
passes is worse than no guard.
"""

import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# COUNT(*) FROM facilities — the legacy table. `(?!_)` so discovered_facilities
# and facilities_* tables never match.
LEGACY_COUNT = re.compile(r"COUNT\(\s*\*\s*\)\s+FROM\s+facilities\b(?!_)", re.I)


def _handler_node(relpath, fname):
    """(node, full_source) for one function, or (None, None)."""
    path = os.path.join(ROOT, relpath)
    src = open(path, encoding="utf-8", errors="ignore").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fname:
            return node, src
    return None, None


def _handler_source(relpath, fname):
    """Source text of one function, or None if it is not there."""
    node, src = _handler_node(relpath, fname)
    if node is None:
        return None
    seg = ast.get_source_segment(src, node)
    return seg if seg and seg.strip() else None


def _handler_sql(relpath, fname):
    """Every string LITERAL inside one function, docstring excluded.

    Matching raw source would hit `#` comments — the comment describing this
    very defect quotes the rogue query, and an earlier draft of this guard went
    red on its own prose. SQL only ever lives in string literals, so match
    those and nothing else.
    """
    node, _src = _handler_node(relpath, fname)
    if node is None:
        return None
    doc = ast.get_docstring(node, clean=False)
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if doc is not None and sub.value == doc:
                continue
            out.append(sub.value)
    return out


# ── the regex itself must be able to fire ────────────────────────────────────

def test_legacy_pattern_actually_matches_the_rogue_query():
    """Anti-vacuity: prove LEGACY_COUNT matches the query that caused this."""
    assert LEGACY_COUNT.search("SELECT COUNT(*) FROM facilities")
    assert LEGACY_COUNT.search('c.execute("SELECT COUNT(*) FROM facilities")')
    # and does NOT match the canonical table
    assert not LEGACY_COUNT.search("SELECT COUNT(*) FROM discovered_facilities")


# ── agent-facing handlers must not count the legacy table ────────────────────

AGENT_HANDLERS = [
    ("main.py", "api_agents_intelligence_index"),
    ("moltbook_integration.py", "agent_stats"),
]


@pytest.mark.parametrize("relpath,fname", AGENT_HANDLERS)
def test_agent_handler_does_not_count_legacy_facilities_table(relpath, fname):
    sql = _handler_sql(relpath, fname)
    assert sql, (
        f"{relpath}::{fname} not found, or contains no string literals — the "
        f"guard would pass against nothing. If the handler was renamed, update "
        f"AGENT_HANDLERS.")
    hits = [q for q in sql if LEGACY_COUNT.search(q)]
    assert not hits, (
        f"{relpath}::{fname} counts the LEGACY `facilities` table ({hits}). "
        f"That is the 20,132 defect: a different table from the canonical "
        f"fleet, undeduplicated. Use util.facility_canon_count.")


@pytest.mark.parametrize("relpath,fname", AGENT_HANDLERS)
def test_agent_handler_reads_the_canonical_definition(relpath, fname):
    body = _handler_source(relpath, fname)
    assert body, f"{relpath}::{fname} not found — guard vacuous"
    assert "canonical_facility_count" in body, (
        f"{relpath}::{fname} must read the count via "
        f"util.facility_canon_count.canonical_facility_count, so there is ONE "
        f"definition of the platform facility count.")


def test_intelligence_index_publishes_a_basis_string():
    """A number without a stated basis is not publishable."""
    body = _handler_source("main.py", "api_agents_intelligence_index")
    assert body, "api_agents_intelligence_index not found — guard vacuous"
    assert "'facilities_basis'" in body or '"facilities_basis"' in body, (
        "data_summary.facilities must ship a facilities_basis beside it.")


def test_stats_canonical_total_facilities_aliases_the_distinct_count():
    """/api/v1/stats/canonical must not contradict its own citeable field."""
    body = _handler_source("routes/facilities_by_dims.py", "stats_canonical")
    assert body, "stats_canonical not found — guard vacuous"
    assert 'stats["total_facilities"] = stats["facilities_distinct"]' in body, (
        "total_facilities must alias facilities_distinct. It was COUNT(*) over "
        "the legacy table (20,132) sitting beside facilities_distinct (17,294) "
        "on the endpoint whose stated purpose is making surfaces agree.")
    assert "legacy_facilities_table_rows" in body, (
        "the legacy figure must remain reachable under a name that says which "
        "table it counts — restated, not silently deleted.")


# ── the shared definition behaves ────────────────────────────────────────────

class _FakeCursor:
    """Records SQL; returns a canned row, or raises if `boom` is set."""

    def __init__(self, row=(17294,), boom=False):
        self.sql = []
        self._row = row
        self._boom = boom

    def execute(self, q, *a):
        self.sql.append(q)
        if self._boom:
            raise RuntimeError("statement timeout")

    def fetchone(self):
        return self._row


def test_canonical_count_queries_the_canonical_table_not_the_legacy_one():
    from util.facility_canon_count import canonical_facility_count

    cur = _FakeCursor(row=(17294,))
    assert canonical_facility_count(cur) == 17294
    assert len(cur.sql) == 1, "expected exactly one query"
    q = cur.sql[0]
    assert "discovered_facilities" in q, f"must count the canonical fleet, got: {q}"
    assert "COUNT(DISTINCT canonical_slug)" in q, f"must count distinct buildings, got: {q}"
    assert not LEGACY_COUNT.search(q), f"must not count the legacy table, got: {q}"


def test_canonical_count_returns_none_and_never_a_fallback_number():
    """An unmeasurable count is unknown, not a plausible constant."""
    from util.facility_canon_count import canonical_facility_count

    assert canonical_facility_count(_FakeCursor(boom=True)) is None
    # a zero row is also 'unknown', never published as 0 facilities
    assert canonical_facility_count(_FakeCursor(row=(0,))) is None


def test_no_count_is_hardcoded_in_the_shared_module():
    """The module defines SQL and prose, never a baked-in figure."""
    import util.facility_canon_count as m

    for name in ("CANON_SQL", "RECORDS_SQL", "LEGACY_SQL"):
        assert isinstance(getattr(m, name), str) and getattr(m, name).strip()
    # no module-level int/float constants — a number baked into code goes stale
    numeric = [k for k, v in vars(m).items()
               if not k.startswith("__") and isinstance(v, (int, float))
               and not isinstance(v, bool)]
    assert not numeric, f"hardcoded numeric constants in the count module: {numeric}"


def test_basis_strings_describe_their_own_query():
    from util.facility_canon_count import CANON_BASIS, LEGACY_BASIS, RECORDS_BASIS

    assert "canonical_slug" in CANON_BASIS and "discovered_facilities" in CANON_BASIS
    assert "SOURCE RECORDS" in RECORDS_BASIS
    assert "legacy" in LEGACY_BASIS.lower()
    # the legacy basis must warn, not merely describe
    assert "never publish" in LEGACY_BASIS.lower()
