"""
click→pay per plan and per path (frontend#1534 (c)): the definition's static
guards. The SQL itself is proved against a real Postgres in
tests/test_click_to_pay_by_path_sql.py (the db-parity lane); these run anywhere.
"""
import ast
import pathlib

from routes import handoff_definition as H
from routes import pricing_click_tracker as P

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_the_plans_reported_are_the_plans_pricing_sells():
    """A /pricing plan missing here would be clicked and never reported."""
    assert tuple(H.CLICK_TO_PAY_PLANS) == tuple(P.COLD_PLANS)


def test_no_builder_emits_a_literal_percent():
    """A `%` beside `sql % iv` or bound params took a funnel endpoint down once
    (external_session_predicate). Every builder here stays regex-only."""
    for sql in (H.click_to_pay_by_plan_sql("30 days"),
                H.click_to_pay_by_plan_sql("30 days", include_cold=False),
                H.chatgpt_relay_stages_sql("30 days"),
                H.chatgpt_session_predicate("x.sid")):
        assert "%" not in sql


def test_the_leakage_endpoint_calls_the_block():
    """Defined but never called is a column nobody sees: read funnel_leakage's
    AST for the call, so a comment naming it cannot pass."""
    tree = ast.parse((REPO / "routes" / "schema_repair.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "funnel_leakage")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_click_to_pay"]
    assert len(calls) == 1
