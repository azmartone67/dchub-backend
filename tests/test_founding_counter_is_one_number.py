"""One founding counter, every surface (2026-09-02, findings 2 + 3).

MEASURED 2026-09-02T00:23Z: /api/founding-members and
/api/v1/founding-customers/count served 18 of 25 (edge + origin) while
/api/v1/founding-spots served a hardcoded {remaining:47,total:50} — and
dashboard.html read the latter. Separately, the $99 founding price — the
SKU that actually sells (10 of 14 active external subs) — was absent from
canonical/mcp_facts.json, the file every agent-facing manifest is checked
against.

What "founding" COUNTS (today: the first 25 paid customers of ANY plan) is
the owner's decision and is deliberately NOT changed here; these tests pin
that every surface reads the one counter, whatever it counts.

House rule: never import main — main.py is read as source/AST.
"""
import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _founding_spots_fn():
    tree = ast.parse(_src("main.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "founding_spots":
            return node
    raise AssertionError("main.py: founding_spots() not found")


# ── finding 2 · /api/v1/founding-spots ───────────────────────────────

def test_founding_spots_is_an_alias_of_the_one_counter():
    fn = _founding_spots_fn()
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "founding_status" in calls, "founding_spots must read founding_status()"
    literal_ints = {n.value for n in ast.walk(fn)
                    if isinstance(n, ast.Constant) and isinstance(n.value, int)
                    and not isinstance(n.value, bool)}
    assert not (literal_ints & {47, 50}), \
        f"hardcoded founding numbers are back: {sorted(literal_ints)}"
    seg = ast.get_source_segment(_src("main.py"), fn)
    assert "'total': st['cap']" in seg and "'remaining': st['remaining']" in seg


def test_an_unreadable_counter_says_so_instead_of_a_number():
    seg = ast.get_source_segment(_src("main.py"), _founding_spots_fn())
    assert "503" in seg and "'remaining': None" in seg


def test_the_counter_itself_reads_the_cohort_table(monkeypatch):
    """founding_status is what the alias now serves — pin its arithmetic."""
    from routes import founding_customers as fc

    class _Cur:
        def __init__(self, n): self.n = n
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            assert "FROM founding_customers" in sql
        def fetchone(self): return (self.n,)

    class _Conn:
        def __init__(self, n): self.n = n
        def cursor(self): return _Cur(self.n)
        def close(self): pass

    monkeypatch.setattr(fc, "FOUNDING_CAP", 25)
    monkeypatch.setattr(fc, "_get_db", lambda: _Conn(18))
    st = fc.founding_status()
    # r-price-collapse (2026-09-05): the cohort is still counted from the same
    # table — which is what this test is about — but the PROGRAMME is retired,
    # so program_active is False and `retired` joined the shape. Kept exact
    # (that is what catches a stray key) rather than relaxed to a subset.
    assert st == {"claimed": 18, "cap": 25, "remaining": 7,
                  "program_active": False, "retired": True}
    monkeypatch.setattr(fc, "_get_db", lambda: _Conn(25))
    assert fc.founding_status()["program_active"] is False


def test_dashboard_reads_both_numbers_from_the_counter():
    src = _src("dashboard.html")
    i = src.index("Founding member pricing")
    block = src[i:i + 900]
    assert 'id="founding-total"' in block and "d.total" in block
    assert ">47<" not in block and "of 50 spots" not in block


# ── finding 3 · mcp_facts pricing carries founding while open ────────

@pytest.fixture()
def exporter(monkeypatch):
    mfe = pytest.importorskip("mcp_facts_export")
    phrases = {"facilities": "15,300+", "countries": "170+",
               "markets": "300+", "deals": "1,600+"}
    state = {"counter": {"program_active": True, "remaining": 7, "claimed": 18,
                         "total": 25, "price": 99}}

    def fake_get(path):
        if path == "/api/v1/canon/phrases":
            return phrases
        if path == mfe.FOUNDING_COUNTER_PATH:
            return state["counter"]
        raise AssertionError(f"unexpected fetch {path}")

    monkeypatch.setattr(mfe, "_get_json", fake_get)
    monkeypatch.setattr(mfe, "_live_numbers", lambda: {})
    monkeypatch.setattr(mfe.c, "get_canonical_stats", lambda: {})
    monkeypatch.setattr(mfe.c, "grid_coverage_phrase", lambda k: "7 ISOs")
    return mfe, state


def test_facts_carry_the_founding_price_while_the_programme_is_open(exporter):
    mfe, _ = exporter
    import tier_registry
    pricing = mfe.build()["pricing_usd_month"]
    assert pricing["founding"] == tier_registry.price("founding") == 99
    for k in mfe.PRICE_TIERS:
        assert k in pricing


@pytest.mark.parametrize("counter", [
    {"program_active": False, "remaining": 0},
    {"program_active": True, "remaining": 0},
    {"program_active": False, "remaining": 3},
])
def test_facts_drop_founding_when_the_programme_is_closed(exporter, counter):
    """★ The point of reading the counter: a sold-out price is not for sale."""
    mfe, state = exporter
    state["counter"] = counter
    pricing = mfe.build()["pricing_usd_month"]
    assert "founding" not in pricing, pricing


@pytest.mark.parametrize("counter", [{}, {"program_active": "yes", "remaining": 7},
                                     {"program_active": True, "remaining": "7"}])
def test_facts_refuse_to_guess_when_the_counter_is_unreadable(exporter, counter):
    mfe, state = exporter
    state["counter"] = counter
    with pytest.raises(mfe.ExportError):
        mfe.build()


def test_facts_read_the_uncached_counter():
    """/api/v1/* is edge-cached with override_origin; the counter must be the
    same un-prefixed endpoint the checkout-integrity shell reads."""
    mfe = pytest.importorskip("mcp_facts_export")
    from routes import checkout_integrity_master_shell as ci
    assert mfe.FOUNDING_COUNTER_PATH == ci._FOUNDING_COUNTER
    assert not mfe.FOUNDING_COUNTER_PATH.startswith("/api/v1/")
