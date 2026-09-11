#!/usr/bin/env python3
"""A paid legacy top-up never expires, and the top-up is no longer sold (2026-09-11).

topup_start() never wrote mcp_topups.expires_at, so every tu- row carried the
column DEFAULT — NOW() + 30 minutes, an unpaid token's checkout window — and
redeem_topup_token() set only paid_at. get_credit_balance, get_credit_status and
consume_credits all require expires_at > NOW(), so the credits a buyer had just
paid for left the balance 30 minutes after checkout STARTED. Owner decision
2026-09-11: a paid top-up follows the pack rule (PACK_NEVER_EXPIRES), and no new
top-up is minted — POST /api/v1/mcp/topup/start answers 410 and points at the
one-time pack, while tokens minted before that still redeem.

NO NETWORK, NO DB in this file: the connection is faked and the SQL captured, the
harness of tests/test_pack_credits_never_expire.py. What only a real Postgres can
prove — a paid row outliving its checkout window, a late payment worth its
credits, a webhook retry changing nothing — is in
tests/test_topup_paid_credits_never_expire_sql.py, which the db-parity job runs.
"""
import ast
import datetime as dt
import pathlib
import re
import sys

import pytest
from flask import Flask

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402


class _Cur:
    """Shaped like the real psycopg2 cursor this code uses. Nothing more capable."""
    def __init__(self, row=None):
        self.calls, self._row = [], row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._row


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


def _app():
    app = Flask(__name__)
    app.register_blueprint(mcp.conversion_bp)
    return app.test_client()


def _no_db():
    raise AssertionError("the retired top-up opened a database connection")


def _set_binding(sql, params, column):
    """(value expression, bound parameter) for ONE column of an UPDATE's SET
    list, derived from the SQL and never from a parameter index — reordering the
    SET list cannot move the assertion onto a different column."""
    flat = " ".join(sql.split())
    m = re.search(r"\bSET (.*?) WHERE ", flat)
    assert m, f"no SET ... WHERE in: {flat}"
    parts, depth, cur = [], 0, ""
    for ch in m.group(1):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    seen = flat[:m.start(1)].count("%s")
    for part in parts:
        col, expr = (s.strip() for s in part.split("=", 1))
        n = expr.count("%s")
        if col == column:
            assert n <= 1, f"{column} is bound to {n} placeholders"
            return expr, (params[seen] if n else None)
        seen += n
    raise AssertionError(f"no {column} in the SET list: {m.group(1)}")


# ── payment writes the pack rule ──────────────────────────────────────────────

def test_payment_writes_never_expires(monkeypatch):
    cur = _Cur(row=(7, 50))
    monkeypatch.setattr(mcp, "_conn", lambda: _Conn(cur))
    out = mcp.redeem_topup_token("tu-abc123", stripe_session_id="cs_test_1")
    assert out == {"ok": True, "token": "tu-abc123", "topup_id": 7, "credits": 50}, out
    updates = [c for c in cur.calls if "UPDATE mcp_topups" in c[0]]
    assert len(updates) == 1, cur.calls
    sql, params = updates[0]
    expr, value = _set_binding(sql, params, "expires_at")
    assert value == mcp.PACK_NEVER_EXPIRES, (expr, value)
    assert "interval" not in expr.lower() and "now()" not in expr.lower(), expr


# ── the top-up is not sold any more ───────────────────────────────────────────

@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "dch_live_testkey"}], ids=["no_key", "keyed"])
def test_topup_start_is_retired_and_mints_nothing(monkeypatch, headers):
    monkeypatch.setattr(mcp, "_conn", _no_db)
    r = _app().post("/api/v1/mcp/topup/start", headers=headers, json={})
    assert r.status_code == 410, (r.status_code, r.get_data(as_text=True))
    body = r.get_json()
    assert body["ok"] is False and body["error"] == "topup_retired", body
    assert "topup_token" not in body and "stripe_url" not in body, body
    assert body["pack_url"] == mcp.PACK5_URL, body


@pytest.mark.parametrize("credits, cents, quoted", [
    (2500, 1250, "2,500 API calls for $12.50,"),
    (1000, 2000, "1,000 API calls for $20,"),
], ids=["cents", "whole_dollars"])
def test_the_retired_start_quotes_the_pack_as_it_is_now(monkeypatch, credits, cents, quoted):
    """Read from the pack constants on every call, never frozen into the copy:
    move them and the message moves with them."""
    monkeypatch.setattr(mcp, "_conn", _no_db)
    monkeypatch.setattr(mcp, "PACK5_URL", "https://buy.stripe.com/test_pack_link")
    monkeypatch.setattr(mcp, "PACK10_CREDITS", credits)
    monkeypatch.setattr(mcp, "PACK10_PRICE_CENTS", cents)
    body = _app().post("/api/v1/mcp/topup/start", json={}).get_json()
    assert body["pack_url"] == "https://buy.stripe.com/test_pack_link", body
    assert body["credits"] == credits and body["price_usd"] == cents / 100, body
    assert quoted in body["message"], body["message"]


def _landing(monkeypatch, row):
    cur = _Cur(row=row)
    monkeypatch.setattr(mcp, "_conn", lambda: _Conn(cur))
    return _app().get("/topup/tu-abc123")


def test_an_unpaid_token_page_sells_the_pack_not_the_topup(monkeypatch):
    r = _landing(monkeypatch, (50, None))
    html = r.get_data(as_text=True)
    assert r.status_code == 410, html
    assert f'href="{mcp.PACK5_URL}"' in html, html
    assert "client_reference_id" not in html, "the retired page still builds a checkout for the token"


def test_a_paid_token_page_no_longer_says_today(monkeypatch):
    r = _landing(monkeypatch, (50, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Top-up complete" in html, html
    assert "today" not in html.lower(), html


# ── a token minted before the retirement still pays ──────────────────────────

def test_the_webhook_still_redeems_tu_tokens():
    """Retiring the top-up must not strand a token minted before it. main.py's
    checkout webhook must still route a tu- client_reference_id to
    redeem_topup_token — read from the AST, so a comment naming the function
    cannot satisfy it."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    hooks = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "stripe_webhook"]
    assert len(hooks) == 1, f"{len(hooks)} stripe_webhook definitions"
    branches = [n for n in ast.walk(hooks[0]) if isinstance(n, ast.If)
                and re.fullmatch(r"\w+\.lower\(\)\.startswith\('tu-'\)", ast.unparse(n.test))]
    assert len(branches) == 1, f"expected one tu- branch in stripe_webhook, found {len(branches)}"
    called = {ast.unparse(node.func) for stmt in branches[0].body
              for node in ast.walk(stmt) if isinstance(node, ast.Call)}
    assert "redeem_topup_token" in called, sorted(called)
