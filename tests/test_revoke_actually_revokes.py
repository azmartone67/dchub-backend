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
import json
import pathlib
import re
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "gen_dev_key.py"
_KEY = "dch_live_deadbeefdeadbeefdeadbeefdeadbeef"


class _Exit(Exception):
    """Raised in place of sys.exit so a test can assert the CODE and the STOP.

    ★ The previous stub was `exit=lambda *a: None`, which let execution run on
    past the exit. A test written against that stub cannot tell exit(1) from
    exit(0) — nor from no exit at all.
    """
    def __init__(self, code):
        self.code = code


class _FakeCursor:
    def __init__(self, calls, rowcounts, fetches):
        self.calls = calls
        self._rowcounts = list(rowcounts)
        self._fetches = list(fetches)
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))
        if self._rowcounts:
            self.rowcount = self._rowcounts.pop(0)

    def fetchall(self):
        return []

    def fetchone(self):
        assert self._fetches, (
            "cmd_revoke called fetchone() more times than the scenario supplies "
            "— the post-state re-read changed shape"
        )
        return self._fetches.pop(0)


class _FakeConn:
    def __init__(self, calls, rowcounts, fetches):
        self._calls = calls
        self._rowcounts = rowcounts
        self._fetches = fetches

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self._calls, self._rowcounts, self._fetches)


# Scenario = the two UPDATE rowcounts, then the two post-state rows the
# re-read returns: (live_rows, total_rows) for api_keys then mcp_dev_keys.
# Default: a paid key revoked cleanly, nothing left live anywhere.
_CLEAN = dict(rowcounts=[1, 1], fetches=[(0, 1), (0, 0)])
# ★ THE REGRESSION CASE: a claim_free_key key. No api_keys row has ever
# existed; the mcp_dev_keys row was just flipped to 'revoked'.
_FREE_KEY = dict(rowcounts=[0, 1], fetches=[(0, 0), (0, 1)])
# Key matched nothing at all — a typo, or the wrong database.
_UNKNOWN = dict(rowcounts=[0, 0], fetches=[(0, 0), (0, 0)])
# ★ A THIRD id space this command does not manage: dch_trial_ keys live in
# auto_trial_keys, so they match nothing here and land in the UNKNOWN branch.
_TRIAL_KEY = "dch_trial_deadbeefdeadbeefdeadbeefdeadbeef"
# The write did not stick: a row is still accepted after the UPDATEs.
_STILL_LIVE = dict(rowcounts=[0, 0], fetches=[(1, 1), (0, 1)])
# ★ Already dead before this run: the rows are still there, none is live, so
# both UPDATEs match ZERO. Note the rowcounts are IDENTICAL to _UNKNOWN's and
# to _STILL_LIVE's — only the post-state separates success from failure here.
_ALREADY_REVOKED = dict(rowcounts=[0, 0], fetches=[(0, 1), (0, 1)])


def _load_cmd_revoke(rowcounts=(1, 1), fetches=((0, 1), (0, 0))):
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

    def _exit(code=0):
        raise _Exit(code)

    ns = {"_connect": lambda: _FakeConn(calls, list(rowcounts), list(fetches)),
          "json": __import__("json"),
          "sys": types.SimpleNamespace(stderr=sys.stderr, exit=_exit)}
    exec(compile(ast.get_source_segment(src, fn), "<cmd_revoke>", "exec"), ns)
    return ns["cmd_revoke"], calls


def _run(**scenario):
    """Run cmd_revoke; return the SQL it issued. Fails the test on a non-zero
    exit, so a scenario that is supposed to succeed cannot pass by accident."""
    fn, calls = _load_cmd_revoke(**(scenario or _CLEAN))
    fn(types.SimpleNamespace(key=_KEY))
    return calls


def _exit_code(key=_KEY, **scenario):
    """Run cmd_revoke and return its exit code (0 if it returned normally)."""
    fn, _ = _load_cmd_revoke(**scenario)
    try:
        fn(types.SimpleNamespace(key=key))
    except _Exit as e:
        return e.code
    return 0


def _stderr(capsys, key=_KEY, **scenario):
    """The operator-facing text. cmd_revoke writes guidance to stderr, and
    guidance that omits a live kill switch is the defect being pinned here."""
    _exit_code(key=key, **scenario)
    return capsys.readouterr().err


def _stdout(capsys, **scenario):
    fn, _ = _load_cmd_revoke(**scenario)
    try:
        fn(types.SimpleNamespace(key=_KEY))
    except _Exit:
        pass
    return capsys.readouterr().out


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
    assert "NOT ON REST" in body, "cmd_mint must scope where the key works"
    assert "ANONYMOUS" in body, "cmd_mint must name the REST resolution"
    # ★ 2026-08-31: this assertion used to demand the flat claim "THIS KEY DOES
    # NOT AUTHENTICATE". That is false — mcp_dev_keys IS the gate on the MCP
    # path (/api/v1/keys/validate), which is how every claim_free_key key works.
    # Pinning the flat claim would re-import the bug this file now guards.
    assert "DOES NOT AUTHENTICATE" not in body, (
        "cmd_mint must not claim the key is inert — it authenticates on /mcp"
    )


# ── (5) 2026-08-31: the 08-16 fix's mirror-image bug ────────────────────────
# It concluded api_keys was the ONLY authenticator and failed loudly whenever
# api_keys matched nothing. For a claim_free_key key that is backwards: those
# keys have no api_keys row at all, and mcp_dev_keys IS their gate (the
# /api/v1/keys/validate hop the Node MCP server relays on every call).
# Revoking a leaked free key on 08-31 printed the full "REVOKE DID NOT TAKE …
# Do NOT treat this as a successful rotation" banner and exited 1 — for a
# revoke that had entirely succeeded, verified dead three ways.

def test_free_key_revoke_reports_success():
    """★ THE PIN. mcp_dev_keys-only revoke is a COMPLETE revoke, not a failure."""
    assert _exit_code(**_FREE_KEY) == 0, (
        "a claim_free_key key with no api_keys row was revoked in mcp_dev_keys "
        "(its real gate) — that is a successful rotation and must exit 0"
    )


def test_unknown_key_exits_nonzero():
    """The 08-16 protection must survive: nothing matched is still a failure."""
    assert _exit_code(**_UNKNOWN) == 1


def test_key_still_accepted_after_write_exits_nonzero():
    """If a row is STILL live after the UPDATEs, that is the real failure."""
    assert _exit_code(**_STILL_LIVE) == 1


def test_clean_paid_key_revoke_reports_success():
    assert _exit_code(**_CLEAN) == 0


# ── (6) the exit code must come from the POST-STATE, not the rowcounts ─────

def test_post_state_is_reread_from_both_id_spaces():
    """Rowcounts say what CHANGED; only a re-read says what is still accepted.

    Without this, "already revoked" (success) is indistinguishable from
    "no such key" (failure) — the old code failed loudly on both.
    """
    calls = _run(**_CLEAN)
    selects = [sql for sql, _ in calls if sql.upper().startswith("SELECT")]
    assert any("FROM api_keys" in s for s in selects), (
        f"no post-state re-read of api_keys; issued: {selects}")
    assert any("FROM mcp_dev_keys" in s for s in selects), (
        f"no post-state re-read of mcp_dev_keys; issued: {selects}")


def test_already_revoked_key_is_not_a_failure(capsys):
    """★ The distinction the test above ARGUES for, actually exercised.

    Its docstring says "already revoked" (success) must be distinguishable from
    "no such key" (failure). Nothing pinned that: _ALREADY_REVOKED and _UNKNOWN
    issue IDENTICAL rowcounts — [0, 0], nothing flipped either way — and differ
    only in the post-state. Here the rows exist and none is live; there, there
    is no row at all. A rowcount cannot tell those apart.

    It matters because rotations re-run this command. Failing on the second run
    teaches the operator that the exit code lies, which is the habit that made
    the 08-31 false alarm expensive.
    """
    # NB read stdout FIRST: capsys accumulates until it is read, so running the
    # command twice before parsing yields two concatenated JSON documents.
    payload = json.loads(_stdout(capsys, **_ALREADY_REVOKED))
    assert payload["rows_found"] > 0, payload
    assert payload["still_accepted_anywhere"] is False, payload
    # ★ and it reached success with BOTH rowcounts at zero — the same numbers
    # that make _UNKNOWN exit 1.
    assert payload["revoked_in_api_keys"] == 0, payload
    assert payload["revoked_in_mcp_dev_keys"] == 0, payload

    assert _exit_code(**_ALREADY_REVOKED) == 0, (
        "rows exist and nothing is live — the credential is dead, which is "
        "exactly what a rotation needs to know")
    assert _exit_code(**_UNKNOWN) == 1, (
        "guard: _UNKNOWN must still FAIL on those same rowcounts, or this test "
        "proves nothing about the post-state being what decides")


def test_already_revoked_scenario_actually_exercises_that_path():
    """★ Guard the guard, matching _FREE_KEY's. If the scenario ever drifted to
    a non-zero rowcount it would pass through the ordinary success path and stop
    testing 'already revoked' at all."""
    assert _ALREADY_REVOKED["rowcounts"] == [0, 0], "both UPDATEs must match 0 rows"
    assert _ALREADY_REVOKED["rowcounts"] == _UNKNOWN["rowcounts"], (
        "the whole point is that these two are indistinguishable by rowcount")
    assert all(live == 0 for live, _ in _ALREADY_REVOKED["fetches"]), "nothing may be live"
    assert any(total > 0 for _, total in _ALREADY_REVOKED["fetches"]), "a row must exist"


def test_free_key_scenario_actually_exercises_the_free_path():
    """★ Guard the guard: _FREE_KEY must really mean 'api_keys matched nothing'.

    If the scenario ever drifted to a non-zero api_keys rowcount, the pin above
    would pass for the wrong reason — it would be testing the paid path.
    """
    assert _FREE_KEY["rowcounts"][0] == 0, "api_keys UPDATE must match 0 rows"
    assert _FREE_KEY["rowcounts"][1] == 1, "mcp_dev_keys UPDATE must match 1 row"
    assert _FREE_KEY["fetches"][0] == (0, 0), "api_keys must hold NO row at all"


# ── (7) the operator-facing text must not re-assert the false claim ────────

def test_note_does_not_claim_api_keys_is_the_only_authenticator(capsys):
    out = _stdout(capsys, **_FREE_KEY)
    assert "ONLY table consulted for auth" not in out, (
        "the JSON note still claims api_keys is the only authenticator — that "
        "is the false premise this fix removes"
    )
    assert "keys/validate" in out, (
        "the note must name the gate that DOES authenticate an MCP-minted key"
    )


def test_revoke_does_not_echo_the_full_key(capsys):
    """It echoed the credential back; that is how a live key reached a
    transcript on 08-31. Match resolve_tier's truncation convention."""
    out = _stdout(capsys, **_CLEAN)
    assert _KEY not in out, "cmd_revoke echoed the full API key to stdout"
    assert _KEY[:12] in out, "the prefix should still be shown for identification"


# ── (7) UNKNOWN must not read as "retired" for a key class we cannot revoke ─

def test_unknown_trial_key_says_this_command_cannot_revoke_it(capsys):
    """★ dch_trial_ keys are a THIRD id space (auto_trial_keys), so they match
    nothing in api_keys or mcp_dev_keys and land in the UNKNOWN branch.

    UNKNOWN's guidance is "check for a typo and confirm NEON_DATABASE_URL" —
    correct for a mistyped dev key, actively misleading here: the key exists,
    is LIVE, and this command cannot touch it. An operator who follows that
    text concludes a live trial credential is retired.

    auto_trial_keys has no status column, so naming the table is not enough —
    expiry is the kill switch and the message must say so.
    """
    err = _stderr(capsys, key=_TRIAL_KEY, **_UNKNOWN)
    assert "auto_trial_keys" in err, (
        "an unknown dch_trial_ key must name the table that DOES hold it; "
        f"got: {err!r}")
    assert "expires_at" in err, (
        "auto_trial_keys has no status column — the message must name expiry "
        f"as the kill switch, not just the table; got: {err!r}")
    assert "STILL LIVE" in err, (
        f"the message must say the key is not retired; got: {err!r}")


def test_unknown_trial_key_still_exits_nonzero():
    """The pointer is guidance, not absolution — nothing was revoked."""
    assert _exit_code(key=_TRIAL_KEY, **_UNKNOWN) == 1


def test_unknown_non_trial_key_does_not_get_the_trial_pointer(capsys):
    """★ The branch must be keyed on the prefix, not printed unconditionally.
    Telling someone with a mistyped dch_live_ key to go edit auto_trial_keys
    sends them to the wrong table."""
    err = _stderr(capsys, key=_KEY, **_UNKNOWN)
    assert "auto_trial_keys" not in err, (
        f"a dch_live_ key must not be pointed at the trial table; got: {err!r}")
