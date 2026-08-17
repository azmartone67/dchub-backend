"""tests/test_revoke_actually_revokes.py — a revoke that reports success and does nothing (2026-08-16).

Written while working the carried-over SECRET ROTATION item. The rotation runbook
says "generate new → update → verify → revoke old". `gen_dev_key.py revoke` was
the tool for the last step, and it did not perform it.

★★★ THE DEFECT. cmd_revoke ran only:

    UPDATE mcp_dev_keys SET status='revoked' WHERE api_key=%s

But `mcp_dev_keys` is NOT what authenticates. util/tier_gate.resolve_tier step 1a
queries `mcp_dev_keys WHERE key_hash = %s`, and that table HAS NO key_hash column
(dchub-mcp-v2.1/migration_001_api_keys.sql: api_key is the PK; no migration adds
key_hash). So 1a raises UndefinedColumn, a bare `except` swallows it, and every
key resolves through step 1b:

    api_keys WHERE key_hash IN (sha256(key), rawkey)
               AND (is_active IS NULL OR is_active = 1)

So the operator ran `revoke`, saw `"revoked": true`, and the key stayed FULLY
LIVE. In a credential rotation that is the worst failure available: it reports
success while leaving the credential you are retiring in service.

Ways it can regress, each asserted below:
  (1) api_keys not touched at all — the original bug.
  (2) Only the sha256 convention matched. Partner/admin keys (incl. the owner's
      own enterprise key) store key_hash = the RAW string; a hash-only lookup
      misses them entirely.
  (3) is_active written as FALSE instead of 0. It is an INTEGER column —
      `= FALSE` throws `operator does not exist: integer = boolean`, which the
      callers' bare excepts swallow into a silent anon fall-through.
  (4) A no-op revoke exits 0, so a rotation script treats "nothing matched" as
      "successfully revoked".

No DB and no psycopg needed: the module is loaded with a stubbed connection and
the SQL it issues is captured.

Run:  python3 -m pytest tests/test_revoke_actually_revokes.py -v
"""
from __future__ import annotations

import ast
import hashlib
import pathlib
import re
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "gen_dev_key.py"
_KEY = "dch_live_deadbeefdeadbeefdeadbeefdeadbeef"


class _FakeCursor:
    def __init__(self, calls):
        self.calls = calls
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self._calls)


def _load_cmd_revoke():
    """Exec just cmd_revoke against stubs — importing the module exits(2) with
    no NEON_DATABASE_URL, and psycopg may be absent.

    ★ A silently-empty extraction would pass every assertion below, so the parse
    asserts a real FunctionDef body.
    """
    src = _SRC.read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "cmd_revoke"), None)
    assert fn is not None and fn.body, "cmd_revoke parsed with an EMPTY body"
    calls: list = []
    ns = {"_connect": lambda: _FakeConn(calls), "json": __import__("json"),
          "sys": types.SimpleNamespace(stderr=sys.stderr, exit=lambda *a: None)}
    exec(compile(ast.get_source_segment(src, fn), "<cmd_revoke>", "exec"), ns)
    return ns["cmd_revoke"], calls


def _run():
    fn, calls = _load_cmd_revoke()
    fn(types.SimpleNamespace(key=_KEY))
    return calls


# ── (1) the authenticating table must be written ───────────────────────────

def test_revoke_updates_api_keys():
    """THE PIN: mcp_dev_keys alone is not a revoke."""
    calls = _run()
    assert any("UPDATE api_keys" in sql for sql, _ in calls), (
        "revoke never touched api_keys — the only table consulted for auth. "
        f"issued: {[s[:60] for s, _ in calls]}"
    )


def test_revoke_still_marks_the_dev_key_ledger():
    calls = _run()
    assert any("UPDATE mcp_dev_keys" in sql for sql, _ in calls)


# ── (2) both key-hash storage conventions ──────────────────────────────────

def test_revoke_matches_sha256_and_raw_conventions():
    """Partner/admin keys store key_hash = the RAW string, not the digest."""
    calls = _run()
    sql, params = next((c for c in calls if "UPDATE api_keys" in c[0]), (None, None))
    assert sql, "no api_keys UPDATE issued"
    # ★ The params check ALONE is not enough: narrowing the SQL to
    # `key_hash = %s` while still passing two params survives it (the fake
    # cursor does not validate placeholder arity, though psycopg would). Assert
    # the DUAL-MATCH SHAPE in the SQL too — that is the actual contract.
    assert re.search(r"key_hash\s+IN\s*\(\s*%s\s*,\s*%s\s*\)", sql), (
        f"api_keys UPDATE must match BOTH conventions via `key_hash IN (%s, %s)`; got: {sql}"
    )
    assert set(params or ()) == {hashlib.sha256(_KEY.encode()).hexdigest(), _KEY}, (
        f"api_keys UPDATE must be given BOTH sha256 and raw; got params={params}"
    )


# ── (3) is_active is an INTEGER column ─────────────────────────────────────

def test_is_active_is_written_as_integer_zero():
    """`is_active = FALSE` throws on an INTEGER column and gets swallowed."""
    calls = _run()
    sql = next(s for s, _ in calls if "UPDATE api_keys" in s)
    assert re.search(r"is_active\s*=\s*0\b", sql), sql
    assert not re.search(r"is_active\s*=\s*(FALSE|false|True|False)\b", sql), sql


# ── (4) a no-op revoke must not look like success ──────────────────────────

def test_zero_matches_exits_nonzero():
    """A rotation script must not read 'nothing matched' as 'revoked'."""
    src = _SRC.read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_revoke")
    body = ast.get_source_segment(src, fn)
    assert "sys.exit(1)" in body, (
        "cmd_revoke must exit non-zero when no authenticating row was disabled"
    )


def test_mint_warns_that_the_key_does_not_authenticate():
    """mint writes mcp_dev_keys only, so the key it prints resolves as ANONYMOUS.
    It must say so rather than imply a working credential."""
    src = _SRC.read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_mint")
    body = ast.get_source_segment(src, fn)
    assert "DOES NOT AUTHENTICATE" in body, "cmd_mint must not imply a live key"
