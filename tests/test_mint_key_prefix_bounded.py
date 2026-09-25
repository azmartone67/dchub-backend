"""The tier-key mint paths store key_prefix as the first 16 chars of the key.

api_keys.key_prefix is read as LEFT(key, 16): upgrade_nudger and
brain_layer13_upgrade_nudge join on it, and api_tier_gating.generate_api_key
stores raw_key[:16].
Two more mint paths write the column and are covered here:

  * main.handle_checkout_completed, new-user branch (Stripe checkout) --
    executed from main.py's AST, since main.py cannot be imported in a test;
  * routes/paid_account_health.mint_key (admin) -- driven through Flask.

secrets.token_urlsafe is pinned to fixed 43-char bodies (the length
token_urlsafe(32) returns), so every case is deterministic.
"""
import ast
import hashlib
import pathlib
import re
import secrets
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PREFIX_LEN = 16
PLANS = ("developer", "pro", "enterprise", "founding")
BODIES = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij_klmnop",
    "A_BCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq",
)


def test_bodies_have_token_urlsafe_32_length():
    assert {len(secrets.token_urlsafe(32))} == {len(b) for b in BODIES} == {43}


def _insert_param(calls, column):
    """Value bound to `column` by the one INSERT INTO api_keys in `calls`."""
    inserts = [(sql, params) for sql, params in calls
               if "INSERT INTO api_keys" in sql]
    assert len(inserts) == 1, f"expected one INSERT INTO api_keys, saw {len(inserts)}"
    sql, params = inserts[0]
    m = re.search(r"INSERT INTO api_keys\s*\(([^)]*)\)\s*VALUES\s*\(([^()]*)\)", sql)
    assert m, "INSERT INTO api_keys has no (columns) VALUES (...) list"
    cols = [c.strip() for c in m.group(1).split(",")]
    vals = [v.strip() for v in re.findall(r"\s*('[^']*'|[^,]+)", m.group(2))]
    assert len(cols) == len(vals), (cols, vals)
    i = cols.index(column)
    assert vals[i] == "%s", f"{column} is not a bound parameter"
    return params[vals[:i].count("%s")]


def _assert_bounded(key, stored):
    assert len(stored) <= PREFIX_LEN, (
        f"key_prefix is {len(stored)} chars; at most {PREFIX_LEN} may be stored")
    assert key.startswith(stored), "key_prefix is not a prefix of the key"
    assert stored == key[:PREFIX_LEN], (
        "key_prefix must equal LEFT(key, 16), the value its readers match on")


# -- main.handle_checkout_completed, new-user branch -------------------------

def _checkout_mint_statements():
    """The new-user block of handle_checkout_completed, from the tier prefix
    map to the end of the block (key mint, api_keys INSERT, log lines and the
    welcome email). Anchors are asserted, never assumed."""
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name == "handle_checkout_completed"]
    assert len(fns) == 1, "main.handle_checkout_completed not found exactly once"

    def assigns(stmt, name):
        return isinstance(stmt, ast.Assign) and any(
            isinstance(n, ast.Name) and n.id == name
            for t in stmt.targets for n in ast.walk(t))

    # The existing-user branch mints too (raw_key with no tier map); this
    # test covers the new-user block, the one that builds _tier_prefix_map.
    blocks = [body for node in ast.walk(fns[0])
              for body in (getattr(node, f, None) for f in ("body", "orelse", "finalbody"))
              if isinstance(body, list)
              and any(assigns(s, "raw_key") for s in body)
              and any(assigns(s, "_tier_prefix_map") for s in body)]
    assert len(blocks) == 1, f"found {len(blocks)} tier-key mint blocks, expected 1"
    body = blocks[0]
    start = [i for i, s in enumerate(body) if assigns(s, "_tier_prefix_map")]
    assert len(start) == 1, "the _tier_prefix_map anchor is missing from the mint block"
    return ast.Module(body=body[start[0]:], type_ignores=[])


@pytest.mark.parametrize("plan", PLANS)
@pytest.mark.parametrize("body", BODIES)
def test_checkout_new_user_stores_and_logs_a_bounded_prefix(monkeypatch, plan, body):
    monkeypatch.setattr(secrets, "token_urlsafe", lambda nbytes=None: body)
    reset_mod = types.ModuleType("routes._password_reset_link")
    reset_mod.mint_reset_url = lambda email: "https://example.invalid/reset"
    monkeypatch.setitem(sys.modules, "routes._password_reset_link", reset_mod)

    calls, printed, emailed = [], [], []
    ns = {
        "plan_name": plan, "api_tier": plan, "sec": secrets, "hashlib": hashlib,
        "new_user_id": "u_1", "customer_email": "buyer@example.com",
        "now": "2026-01-01T00:00:00Z", "temp_password": "tmp",
        "_pg_execute": lambda sql, params=None, **kw: calls.append((sql, params)) or (1, []),
        "send_welcome_email_sendgrid": lambda *a, **kw: emailed.append(a),
        "print": lambda *a, **kw: printed.append(" ".join(map(str, a))),
        # r-trial-copy: the welcome call passes the checkout's trial end.
        "session": {}, "_checkout_trial_end": lambda s: None,
    }
    exec(compile(_checkout_mint_statements(), "main.py", "exec"), ns)

    assert len(emailed) == 1, "the welcome email did not go out exactly once"
    key = emailed[0][1]
    assert key.endswith(body)
    assert _insert_param(calls, "key_hash") == hashlib.sha256(key.encode()).hexdigest()
    _assert_bounded(key, _insert_param(calls, "key_prefix"))

    logged = "\n".join(printed)
    assert key[:PREFIX_LEN] in logged, "the key log line no longer names the prefix"
    leaked = [key[i:i + 8] for i in range(PREFIX_LEN - 7, len(key) - 7)
              if key[i:i + 8] in logged]
    assert not leaked, f"a log line carries key characters past the first {PREFIX_LEN}"


# -- routes/paid_account_health.mint_key -------------------------------------

class _Cursor:
    def __init__(self, calls, plan):
        self.calls, self.plan, self.last = calls, plan, ""

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.last = sql

    def fetchone(self):
        if "FROM users" in self.last:
            return (7, self.plan)
        return None                      # no active key yet


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cur

    def commit(self):
        pass


@pytest.mark.parametrize("plan", PLANS)
@pytest.mark.parametrize("body", BODIES)
def test_admin_mint_key_stores_a_bounded_prefix(monkeypatch, plan, body):
    from flask import Flask
    import routes.paid_account_health as pah

    monkeypatch.setattr(secrets, "token_urlsafe", lambda nbytes=None: body)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    calls = []
    monkeypatch.setattr(pah, "_get_pg", lambda: _Conn(_Cursor(calls, plan)))
    app = Flask(__name__)
    app.register_blueprint(pah.paid_health_bp)

    resp = app.test_client().post(
        "/api/v1/admin/paid-account-health/mint-key",
        json={"email": "buyer@example.com"},
        headers={"X-Admin-Key": "test-admin-key"})

    assert resp.status_code == 200, resp.get_json()
    key = resp.get_json()["api_key"]
    assert key.endswith(body)
    assert _insert_param(calls, "key_hash") == hashlib.sha256(key.encode()).hexdigest()
    _assert_bounded(key, _insert_param(calls, "key_prefix"))
