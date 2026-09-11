#!/usr/bin/env python3
"""A $10 credit-pack sale must not be recorded as $5.

NO NETWORK, NO DB — the connection is faked and the SQL captured.

WHY THIS EXISTS (2026-09-10). grant_credit_pack() already took `credits` and
`expires_days` per pack, so r-pack10 could add a second SKU without touching
it. PRICE was the one member of that set left hardcoded:

    VALUES (%s, %s, %s, %s, NOW(), ...)
                      ^ PACK5_PRICE_CENTS, always

So every $10 pack10 sale wrote 500 into mcp_topups.price_cents and revenue
read back at HALF what Stripe charged. Nothing failed: credits were granted
correctly, the buyer got what they paid for, and only the money column lied
— which is why it survived. The bug is invisible from the product and only
visible in the ledger.

★ THE ASSERTION BINDS TO THE COLUMN, NOT TO AN INDEX. price_cents is the 4th
placeholder today. A test asserting params[3] would keep passing if someone
reordered the INSERT and silently moved the money into another column, so
this parses the column list and the VALUES clause and maps them — NOW() has
no placeholder, so the mapping is not positional.
"""
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402


class _Cur:
    """Shaped like the real psycopg cursor this code uses: a context manager
    with execute() and fetchone(). Nothing more capable than the real one."""
    def __init__(self):
        self.calls = []
        self._n = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        self._n += 1
        # 1st = the idempotency probe (no prior row); 2nd = INSERT … RETURNING
        return None if self._n == 1 else (4242,)


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _capture(monkeypatch, **kw):
    cur = _Cur()
    monkeypatch.setattr(mcp, "_conn", lambda: _Conn(cur))
    out = mcp.grant_credit_pack("dch_live_testkey", "sess-1", kw.pop("credits", 1000),
                                stripe_session_id="cs_test_1", **kw)
    assert out.get("ok"), out
    insert = [c for c in cur.calls if "INSERT INTO mcp_topups" in c[0]]
    assert len(insert) == 1, f"expected one INSERT, got {len(insert)}"
    return insert[0]


def _price_param(sql, params):
    """The parameter bound to the price_cents COLUMN, derived from the SQL."""
    cols = re.search(r"INSERT INTO mcp_topups\s*\(([^)]*)\)", sql, re.S).group(1)
    cols = [c.strip() for c in cols.split(",")]
    vals = re.search(r"VALUES\s*\((.*?)\)\s*RETURNING", sql, re.S).group(1)
    # split the VALUES list on top-level commas
    parts, depth, cur_ = [], 0, ""
    for ch in vals:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur_); cur_ = ""
        else:
            cur_ += ch
    parts.append(cur_)
    assert len(parts) == len(cols), (
        f"{len(cols)} columns but {len(parts)} value expressions — the INSERT "
        "was edited and this mapping is no longer trustworthy")

    idx, seen = None, 0
    for col, expr in zip(cols, parts):
        n = expr.count("%s")
        if col == "price_cents":
            assert n == 1, f"price_cents is bound to {n} placeholders"
            idx = seen
        seen += n
    assert idx is not None, "no price_cents column in the INSERT"
    return params[idx]


def test_a_ten_dollar_pack_records_ten_dollars(monkeypatch):
    sql, params = _capture(monkeypatch,
                           credits=mcp.PACK10_CREDITS,
                           source="pack10",
                           price_cents=mcp.PACK10_PRICE_CENTS)
    assert _price_param(sql, params) == mcp.PACK10_PRICE_CENTS == 1000


def test_the_five_dollar_pack_is_unchanged(monkeypatch):
    """The agentic caller in routes/stripe_metered.py passes no price at all."""
    sql, params = _capture(monkeypatch, credits=mcp.PACK5_CREDITS,
                           source="agentic_pack5")
    assert _price_param(sql, params) == mcp.PACK5_PRICE_CENTS == 500


def test_the_two_packs_actually_differ(monkeypatch):
    """NON-VACUITY. If PACK5 and PACK10 were the same number, both assertions
    above would pass against a still-hardcoded price and prove nothing."""
    assert mcp.PACK10_PRICE_CENTS != mcp.PACK5_PRICE_CENTS, (
        "the two packs cost the same — this suite cannot detect the bug it "
        "was written for")


def test_the_webhook_passes_the_pack_price(monkeypatch):
    """The fix is only real if the CALLER threads it. main.py already derives
    _pack_credits/_pack_expiry per pack; price must ride along."""
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="ignore")
    assert "_pack_price = PACK10_PRICE_CENTS if _p10 else PACK5_PRICE_CENTS" in src
    assert "price_cents=_pack_price" in src, (
        "grant_credit_pack now ACCEPTS a price the webhook never sends — the "
        "signature changed and the defect did not")
