#!/usr/bin/env python3
"""A key-bound $10 pack sale (a 'pk-' checkout reference) is recorded as $10.

NO NETWORK, NO DB. This runs main.py's own pk- branch, taken from its AST, with
grant_credit_pack replaced by a recorder, so the branch's real arithmetic
decides what is recorded.

WHY (found 2026-09-21, fixed 2026-09-22, frontend#1534). The keyless pack
branch threads price_cents into grant_credit_pack
(test_pack10_records_its_own_price.py). The key-bound branch did not, so the
$5 default applied and every key-bound $10 sale went into
mcp_topups.price_cents as 500. The two packs grant the same 1,000 credits, so
the buyer got what they paid for and only the money column was wrong.
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402

REF = "pk-" + "ab" * 32
PK_TEST = "ref.lower().startswith('pk-')"


def _pk_branch():
    """main.py's pk- branch body, compiled. Exactly one such branch exists."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    hits = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.If) and ast.unparse(n.test) == PK_TEST]
    assert len(hits) == 1, f"expected one pk- branch in main.py, found {len(hits)}"
    return compile(ast.Module(body=hits[0].body, type_ignores=[]),
                   "main.py:pk-branch", "exec")


def _run(monkeypatch, **data):
    calls = []

    def recorder(api_key, mcp_session_id, credits, **kw):
        calls.append({"api_key": api_key, "credits": credits, **kw})
        return {"ok": True}

    monkeypatch.setattr(mcp, "grant_credit_pack", recorder)
    import mcp_signal_canonical
    monkeypatch.setattr(mcp_signal_canonical, "attribute_keybound_conversion",
                        lambda **kw: {"ok": True})
    env = {"data": {"id": "cs_test_pk", **data}, "ref": REF,
           "print": lambda *a, **k: None,
           "_pg_execute": lambda *a, **k: (1, None)}
    exec(_pk_branch(), env)
    return calls


def test_a_ten_dollar_keybound_pack_records_ten_dollars(monkeypatch):
    calls = _run(monkeypatch, mode="payment", amount_subtotal=1000, amount_total=1000)
    assert len(calls) == 1
    assert calls[0]["price_cents"] == mcp.PACK10_PRICE_CENTS == 1000
    assert calls[0]["source"] == "pack10_keybound"
    assert calls[0]["api_key_hash"] == REF[3:]


def test_tax_does_not_change_the_recorded_pack_price(monkeypatch):
    """The ledger records the pack's price, as the keyless branch does."""
    calls = _run(monkeypatch, mode="payment", amount_subtotal=1000, amount_total=1087)
    assert calls[0]["price_cents"] == 1000


def test_a_five_dollar_keybound_pack_records_five_dollars(monkeypatch):
    calls = _run(monkeypatch, mode="payment", amount_subtotal=500, amount_total=500)
    assert calls[0]["price_cents"] == mcp.PACK5_PRICE_CENTS == 500
    assert calls[0]["source"] == "pack5_keybound"


@pytest.mark.parametrize("data", [
    {"mode": "payment", "amount_subtotal": 4900, "amount_total": 4900},
    {"mode": "subscription", "amount_subtotal": 1000, "amount_total": 1000},
])
def test_a_non_pack_payment_grants_nothing(monkeypatch, data):
    assert _run(monkeypatch, **data) == []


def test_the_two_pack_prices_differ():
    """NON-VACUITY. The packs grant the same credits; only the price tells
    them apart. Equal prices would let a hardcoded price pass every test."""
    assert mcp.PACK10_PRICE_CENTS != mcp.PACK5_PRICE_CENTS
