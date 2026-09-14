"""paid_attributed v2 — a payment joins the relayed click that sold it.

r-paid-join (2026-09-14). Through v1 the stage summed two session-bound
tables, and a purchase made through a durable-key ref (pk- pack, k-
subscription) writes a session to neither, so a keyed caller paying from an
agent unlock could not move it. v2 unions those tables with the relayed-
checkout lane: a paid Checkout Session whose client_reference_id equals the
ref of the /go/c/ click that sold it, counted as that click's session.

These pin the COMPOSITION, and the wiring into the webhook and the funnel.
What the SQL counts is executed against a real Postgres by
tests/test_paid_attributed_relayed_checkout_sql.py. Every check binds to a
value a builder produces or to a module's AST (flask_mcp_endpoints and main
open DB pools at import, so they are parsed, never imported).
"""
from __future__ import annotations

import ast
import os

import pytest

from mcp_calls_deloop import external_session_predicate
from routes import checkout_click_tracker as T
from routes import checkout_payment_refs as CPR
from routes import handoff_definition as H

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IV = "30 days"


def _split(sql: str):
    head, sep, rest = sql.partition(" from (")
    assert sep and head == "select count(distinct u.sid)", sql[:120]
    body, sep, tail = rest.rpartition(") u where ")
    assert sep, sql[-160:]
    return body.split(" union "), tail


def _anchor(lane: str) -> str:
    """The table a lane reads FROM at its own top level (not a subquery's)."""
    depth, i, low = 0, 0, lane.lower()
    while i < len(lane):
        ch = lane[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and low.startswith(" from ", i):
            return lane[i + len(" from "):].split(" ", 1)[0]
        i += 1
    raise AssertionError("no top-level FROM in: " + lane[:120])


# ── composition ──────────────────────────────────────────────────────────
def test_the_headline_is_the_two_v1_tables_union_the_relayed_checkout_lane():
    lanes, _ = _split(H.paid_attributed_count_sql(IV))
    assert len(lanes) == 3
    assert [_anchor(x) for x in lanes[:2]] == ["mcp_session_upgrades", "mcp_topups"]
    payments = H._paid_payments_sql(IV)
    assert lanes[2].endswith(" from " + payments)
    assert _anchor(payments[1:]) == "mcp_checkout_payments"


def test_the_payments_read_is_one_literal_the_dataset_inventory_can_see():
    """CI's dataset inventory reads a table only where ONE string literal holds
    both the SELECT and the FROM; the first push split them and failed it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dataset_inventory", os.path.join(REPO, "scripts", "dataset_inventory.py"))
    inv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inv)
    reads = set()
    for node in ast.walk(_parse("routes/handoff_definition.py")):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            reads |= inv.sql_tables(node.value)[0]
    assert "mcp_checkout_payments" in reads


def test_the_relayed_lane_reuses_the_human_acted_click_definition():
    """The click must qualify exactly as human_acted's /go/c/ lane counts it,
    so the lane is built from that lane's own strings, never a copy."""
    lane = _split(H.paid_attributed_count_sql(IV))[0][2]
    for part in (H.RELAYED_CHECKOUT_SESSION_ID, H.relayed_checkout_session_filters(),
                 "from mcp_checkout_clicks cc"):
        assert part in lane, part[:80]
    for clause in ("cc.ref = pay.client_reference_id",
                   "cc.clicked_at <= pay.paid_at",
                   "cc.clicked_at > pay.paid_at - interval '"
                   + H.PAID_RELAYED_CHECKOUT_LOOKBACK + "'",
                   "order by cc.clicked_at desc, cc.id desc limit 1",
                   "p.paid_at > now() - interval '" + IV + "'",
                   "p.livemode IS NOT FALSE"):
        assert clause in lane, clause


def test_the_exclusion_binds_once_on_the_unions_identity():
    ext = external_session_predicate("u.sid")
    lanes, tail = _split(H.paid_attributed_count_sql(IV))
    assert tail.count(ext) == 1
    assert all(ext not in lane for lane in lanes)
    for col in ("cc.ref", "pay.client_reference_id", "p.client_reference_id",
                "su.mcp_session_id", "tp.mcp_session_id"):
        assert external_session_predicate(col) not in H.paid_attributed_count_sql(IV), col
    incl_lanes, incl_tail = _split(H.paid_attributed_count_sql(IV, include_self_traffic=True))
    assert incl_lanes == lanes and ext not in incl_tail


def test_the_lane_published_alone_is_the_headlines_lane():
    lane = _split(H.paid_attributed_count_sql(IV))[0][2]
    alone, tail = _split(H.paid_relayed_checkout_count_sql(IV))
    assert alone == [lane]
    assert tail == _split(H.paid_attributed_count_sql(IV))[1]


def test_the_ceiling_counts_every_matching_click_not_only_session_bound_ones():
    sql = H.relayed_checkout_payments_sql(IV)
    matched = sql[sql.index("exists ("):]
    matched = matched[:matched.index(") as matched")]
    assert H.relayed_checkout_signed() in matched
    assert H.relayed_checkout_real_ua() in matched
    assert H.RELAYED_CHECKOUT_SESSION_ID not in matched
    assert H.paid_relayed_click_session_sql() + " as sid" in sql


def test_every_version_carries_a_changelog_entry():
    assert set(H.PAID_ATTRIBUTED_DEFINITION_CHANGELOG) == set(
        range(1, H.PAID_ATTRIBUTED_DEFINITION_VERSION + 1))
    d = H.paid_attributed_definition()
    assert d["definition_version"] == H.PAID_ATTRIBUTED_DEFINITION_VERSION
    assert d["relayed_checkout_lookback"] == H.PAID_RELAYED_CHECKOUT_LOOKBACK


# ── the writer ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("ref", [
    "pk-" + "a" * 64, "k-" + "b" * 64, "a-" + "c" * 24,
    "5e550001-0000-4000-8000-000000000001"])
def test_a_payment_and_the_click_that_sold_it_carry_one_label(ref):
    assert CPR.payment_ref_kind(ref) == T._ref_kind(ref)


@pytest.mark.parametrize("ref,kind", [
    ("mcp:tool=rank_markets:ref=x:sess=abc", "mcp_ref"),
    ("web__pricing__pro", "web"),
    ("ref_claim__tool_rank_markets", "legacy_ref"),
    ("DCM-ABCD", "pair_code"),
    ("tu-0123", "topup_token"),
    ("has spaces and = signs", "other"),
])
def test_refs_the_webhooks_special_case_are_not_labelled_sessions(ref, kind):
    assert CPR.payment_ref_kind(ref) == kind


@pytest.mark.parametrize("session,reason", [
    (None, "no_session_or_ref"),
    ({"id": "cs_1", "payment_status": "paid"}, "no_session_or_ref"),
    ({"client_reference_id": "k-" + "b" * 64, "payment_status": "paid"}, "no_session_or_ref"),
    ({"id": "cs_1", "client_reference_id": "k-" + "b" * 64, "payment_status": "unpaid"}, "not_paid"),
    ({"id": "cs_1", "client_reference_id": "k-" + "b" * 64,
      "payment_status": "no_payment_required"}, "not_paid"),
])
def test_the_writer_refuses_before_it_touches_the_database(monkeypatch, session, reason):
    def _no_db():
        raise AssertionError("ensure_schema was reached for a session it must skip")
    monkeypatch.setattr(CPR, "ensure_schema", _no_db)
    assert CPR.record_checkout_payment(session) == {"ok": False, "skipped": reason}


# ── wiring ───────────────────────────────────────────────────────────────
def _parse(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return ast.parse(f.read())


def _calls(node, name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def test_the_webhook_records_every_completed_checkout():
    tree = _parse("main.py")
    branches = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name) and n.test.left.id == "event_type"
        and any(isinstance(c, ast.Constant) and c.value == "checkout.session.completed"
                for c in n.test.comparators)]
    assert branches, "no `event_type == 'checkout.session.completed'` branch in main.py"
    found = []
    for br in branches:
        imports = [n for n in ast.walk(br) if isinstance(n, ast.ImportFrom)
                   and n.module == "routes.checkout_payment_refs"
                   and any(a.name == "record_checkout_payment" for a in n.names)]
        for imp in imports:
            alias = next(a.asname or a.name for a in imp.names
                         if a.name == "record_checkout_payment")
            found += [c for c in _calls(br, alias)
                      if len(c.args) == 1 and isinstance(c.args[0], ast.Name)
                      and c.args[0].id == "data"]
    assert len(found) == 1, "record_checkout_payment(data) must run once per completed checkout"


def _win():
    tree = _parse("flask_mcp_endpoints.py")
    funnel = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "handoff_funnel")
    return next(n for n in funnel.body if isinstance(n, ast.FunctionDef) and n.name == "_win")


def test_the_funnel_counts_the_canonical_union():
    win = _win()
    assigns = {t.id: n.value for n in ast.walk(win) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    assert _calls(assigns["paid_v2"], "_paid_attributed_count_sql")
    assert _calls(assigns["paid_v1"], "_paid_attributed_v1_sql")
    paid = assigns["paid"]
    assert isinstance(paid, ast.IfExp)
    assert {paid.body.id, paid.orelse.id} == {"paid_v2", "paid_v1"}


def test_the_funnel_publishes_the_stage_and_what_it_is_built_from():
    win = _win()
    keys = {}
    for d in ast.walk(win):
        if isinstance(d, ast.Dict):
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant):
                    keys.setdefault(k.value, v)
    assert isinstance(keys["paid_attributed"], ast.Name) and keys["paid_attributed"].id == "paid"
    for name, value in (("paid_attributed_v1_session_rows", "paid_v1"),
                        ("paid_attributed_from_relayed_checkout", "paid_relayed"),
                        ("paid_attributed_including_self_traffic", "paid_incl_self"),
                        ("relayed_checkout_payments", "paid_relayed_payments")):
        assert isinstance(keys[name], ast.Name) and keys[name].id == value, name
    assert _calls(keys["paid_attributed"] if False else win, "_paid_attributed_definition")
    defs = keys["definitions"]
    assert isinstance(defs, ast.Dict)
    published = {k.value: v for k, v in zip(defs.keys, defs.values) if isinstance(k, ast.Constant)}
    assert _calls(published["paid_attributed"], "_paid_attributed_definition")
    assert "paid_attributed_removed" in keys
