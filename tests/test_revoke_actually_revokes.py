"""tests/test_revoke_actually_revokes.py — `gen_dev_key.py revoke` must be right in BOTH directions.

This file was written on 2026-08-16 for a revoke that reported success and did
nothing. It is kept — and CORRECTED — on 2026-08-31 for the opposite defect the
2026-08-16 fix introduced.

★★★ WHAT THIS FILE GOT WRONG (the correction).

The 2026-08-16 version asserted, in prose and in test names, that "api_keys is
the only table consulted for auth". That is false. TWO DISJOINT tables
authenticate, and WHICH ONE depends on the key's ORIGIN:

  * `api_keys`     — dashboard / partner / paid keys. util/tier_gate.resolve_tier
                     step 1b: key_hash IN (sha256(key), rawkey) AND (is_active
                     IS NULL OR is_active = 1). key_hash is sha256(key) for
                     customer keys and the RAW string for partner/admin keys.
  * `mcp_dev_keys` — MCP-minted keys (claim_free_key, OAuth, pair-code; 648 rows
                     as of 2026-08-31). flask_mcp_endpoints POST
                     /api/v1/keys/validate — the hop the Node MCP server relays —
                     matches `api_key = %s` and requires `status = 'active'`.
                     There is no key_hash column and no api_keys row.

resolve_tier step 1a looks like a third path but is dead code: it queries
`mcp_dev_keys WHERE key_hash = %s`, a column that table does not have, so it
always raises UndefinedColumn into a bare except.

★★★ THE TWO DEFECTS, one per direction, both pinned below.

  2026-08-16 (under-revoke): cmd_revoke wrote mcp_dev_keys ONLY. An
  api_keys-backed key stayed FULLY LIVE while the tool printed success.

  2026-08-31 (over-correct): the fix made `api_keys matched 0 rows` the failure
  condition. For an MCP-minted key that is exactly backwards — it has no
  api_keys row BY CONSTRUCTION, and revoked_in_mcp_dev_keys: 1 is already a
  complete revoke. Confirmed live on 2026-08-31 revoking a leaked free key: the
  tool printed "REVOKE DID NOT TAKE … Do NOT treat this as a successful
  rotation" and exited 1, while the key was fully dead — mcp_dev_keys.status
  ='revoked', the /api/v1/keys/validate query returned accept=false, and a live
  tools/call on https://dchub.cloud/mcp with the key returned byte-identical
  output to a bogus key and to no key at all.

  A revoke tool that cries failure on a real revoke is not safe either: the
  operator stops trusting the exit code, or re-runs a rotation that succeeded.

Ways it can regress, each asserted below:
  (1) api_keys not touched at all — the original bug.
  (2) Only the sha256 convention matched. Partner/admin keys (incl. the owner's
      own enterprise key) store key_hash = the RAW string; a hash-only lookup
      misses them entirely.
  (3) is_active written as FALSE instead of 0. It is an INTEGER column —
      `= FALSE` throws `operator does not exist: integer = boolean`, which the
      callers' bare excepts swallow into a silent anon fall-through.
  (4) A no-match-anywhere revoke exits 0, so a rotation script reads "nothing
      matched" as "successfully revoked".
  (5) ★ NEW — an mcp_dev_keys-only revoke (the 648-row majority class) exits
      NONZERO and prints the failure banner, so a rotation script reads a
      COMPLETE revoke as a failure.
  (6) ★ NEW — the result is inferred from rowcount instead of measured, so an
      UPDATE that reports rows but leaves the key live still reports success.

No DB and no psycopg needed: the module is loaded with a stubbed connection
that simulates the two tables, and the SQL it issues is captured.

Run:  python3 -m pytest tests/test_revoke_actually_revokes.py -v
"""
from __future__ import annotations

import ast
import hashlib
import json as _json
import pathlib
import re
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "gen_dev_key.py"
_KEY = "dch_live_deadbeefdeadbeefdeadbeefdeadbeef"
_HASH = hashlib.sha256(_KEY.encode()).hexdigest()


class _FakeCursor:
    """A two-table Postgres stand-in.

    State is (rows_present, rows_live) per table. A SELECT COUNT returns the
    live census; an UPDATE reports the rows it flipped via .rowcount and clears
    liveness — unless the table is listed in `stuck`, which models the class of
    bug where the UPDATE reports rows but the key keeps working (e.g. writing
    `is_active = FALSE` to an INTEGER column). Unrecognised SQL raises, so a
    reworded query fails this file loudly instead of passing vacuously.
    """

    def __init__(self, db, calls):
        self.db = db
        self.calls = calls
        self.rowcount = 0
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        sql = " ".join(str(sql).split())
        self.calls.append((sql, params))
        db = self.db
        if sql.startswith("UPDATE api_keys"):
            self.rowcount = db["api"][1]
            if "api_keys" not in db["stuck"]:
                db["api"] = (db["api"][0], 0)
        elif sql.startswith("UPDATE mcp_dev_keys"):
            self.rowcount = db["mcp"][1]
            if "mcp_dev_keys" not in db["stuck"]:
                db["mcp"] = (db["mcp"][0], 0)
        elif "FROM api_keys" in sql:
            self._row = db["api"]
        elif "FROM mcp_dev_keys" in sql:
            self._row = db["mcp"]
        else:
            raise AssertionError(f"fake DB got SQL it cannot model: {sql}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, db, calls):
        self._db, self._calls = db, calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self._db, self._calls)


class _Result(types.SimpleNamespace):
    pass


def _load_cmd_revoke(db, calls, out, err):
    """Exec just cmd_revoke against stubs — importing the module exits(2) with
    no NEON_DATABASE_URL, and psycopg may be absent.

    ★ A silently-empty extraction would pass every assertion below, so the parse
    asserts a real FunctionDef body.

    ★ sys.exit is stubbed to RAISE SystemExit, not to return. The 2026-08-16
    version of this harness used `exit=lambda *a: None`, which let execution run
    on past the failure branch — a stub that cannot observe control flow cannot
    pin an exit code.
    """
    src = _SRC.read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "cmd_revoke"), None)
    assert fn is not None and fn.body, "cmd_revoke parsed with an EMPTY body"

    def _exit(code=0):
        raise SystemExit(code)

    ns = {
        "_connect": lambda: _FakeConn(db, calls),
        "json": _json,
        "print": lambda *a, **k: out.append(" ".join(str(x) for x in a)),
        "sys": types.SimpleNamespace(stderr=types.SimpleNamespace(write=err.append),
                                     exit=_exit),
    }
    exec(compile(ast.get_source_segment(src, fn), "<cmd_revoke>", "exec"), ns)
    return ns["cmd_revoke"]


def _run(api=(1, 1), mcp=(1, 1), stuck=()):
    """Drive cmd_revoke against a simulated DB.

    api / mcp are (rows_present, rows_live). Returns a _Result carrying the
    exit code, the parsed JSON payload, stderr text, and the SQL issued.
    """
    db = {"api": tuple(api), "mcp": tuple(mcp), "stuck": set(stuck)}
    calls: list = []
    out: list = []
    err: list = []
    fn = _load_cmd_revoke(db, calls, out, err)
    code = 0
    try:
        fn(types.SimpleNamespace(key=_KEY))
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    payload = _json.loads(out[0]) if out else {}
    return _Result(code=code, payload=payload, stderr="".join(err), calls=calls)


# ── (1) the authenticating tables must both be written ─────────────────────

def test_revoke_updates_api_keys():
    """THE 2026-08-16 PIN: mcp_dev_keys alone is not a revoke for an
    api_keys-backed key."""
    r = _run()
    assert any("UPDATE api_keys" in sql for sql, _ in r.calls), (
        "revoke never touched api_keys — where dashboard/partner/paid keys "
        f"authenticate. issued: {[s[:60] for s, _ in r.calls]}"
    )


def test_revoke_updates_the_mcp_dev_key_table():
    """Not 'for tidiness' — POST /api/v1/keys/validate reads status here, so
    this write is what kills an MCP-minted key."""
    r = _run()
    assert any("UPDATE mcp_dev_keys" in sql for sql, _ in r.calls)


# ── (2) both key-hash storage conventions ──────────────────────────────────

def test_revoke_matches_sha256_and_raw_conventions():
    """Partner/admin keys store key_hash = the RAW string, not the digest."""
    r = _run()
    sql, params = next((c for c in r.calls if c[0].startswith("UPDATE api_keys")),
                       (None, None))
    assert sql, "no api_keys UPDATE issued"
    # ★ The params check ALONE is not enough: narrowing the SQL to
    # `key_hash = %s` while still passing two params survives it (the fake
    # cursor does not validate placeholder arity, though psycopg would). Assert
    # the DUAL-MATCH SHAPE in the SQL too — that is the actual contract.
    assert re.search(r"key_hash\s+IN\s*\(\s*%s\s*,\s*%s\s*\)", sql), (
        f"api_keys UPDATE must match BOTH conventions via `key_hash IN (%s, %s)`; got: {sql}"
    )
    assert set(params or ()) == {_HASH, _KEY}, (
        f"api_keys UPDATE must be given BOTH sha256 and raw; got params={params}"
    )


# ── (3) is_active is an INTEGER column ─────────────────────────────────────

def test_is_active_is_written_as_integer_zero():
    """`is_active = FALSE` throws on an INTEGER column and gets swallowed."""
    r = _run()
    sql = next(s for s, _ in r.calls if s.startswith("UPDATE api_keys"))
    assert re.search(r"is_active\s*=\s*0\b", sql), sql
    assert not re.search(r"is_active\s*=\s*(FALSE|false|True|False)\b", sql), sql


# ── (4) a no-match-anywhere revoke must still fail loudly ──────────────────

def test_no_row_in_either_table_exits_nonzero():
    """A rotation script must not read 'nothing matched' as 'revoked'.

    This is the case the failure banner is FOR: the key was never issued by
    this tool (e.g. a dch_trial_ key, which lives in auto_trial_keys).
    """
    r = _run(api=(0, 0), mcp=(0, 0))
    assert r.code != 0, (
        "no row in api_keys OR mcp_dev_keys is a genuine non-revoke and must "
        f"exit non-zero; got exit {r.code} with {r.payload}"
    )
    assert "REVOKE DID NOT TAKE" in r.stderr, r.stderr
    assert r.payload.get("found_in") == [], r.payload


# ── (5) ★ THE 2026-08-31 PIN — an mcp_dev_keys-only revoke is COMPLETE ─────

def test_mcp_dev_keys_only_revoke_succeeds():
    """The majority class (648 rows): MCP-minted keys have NO api_keys row.

    Revoking one gives revoked_in_api_keys: 0 / revoked_in_mcp_dev_keys: 1.
    That is a complete revoke — /api/v1/keys/validate requires status='active'
    and now sees 'revoked'. The tool must exit 0 and must NOT print the failure
    banner. The 2026-08-16 build failed exactly here.
    """
    r = _run(api=(0, 0), mcp=(1, 1))
    assert r.code == 0, (
        "an mcp_dev_keys-only revoke IS a complete revoke — /api/v1/keys/validate "
        "gates on status='active' — but the tool exited "
        f"{r.code}. stderr: {r.stderr!r}"
    )
    assert "REVOKE DID NOT TAKE" not in r.stderr, (
        "a successful mcp_dev_keys revoke must not print the failure banner; "
        f"got: {r.stderr!r}"
    )
    assert r.payload.get("revoked_in_mcp_dev_keys") == 1, r.payload
    assert r.payload.get("still_live_in") == [], r.payload
    assert r.payload.get("found_in") == ["mcp_dev_keys"], r.payload


def test_api_keys_only_revoke_succeeds():
    """The mirror case: a dashboard/partner key has no mcp_dev_keys row, and
    revoked_in_mcp_dev_keys: 0 must not be read as a failure either."""
    r = _run(api=(1, 1), mcp=(0, 0))
    assert r.code == 0, f"exit {r.code}; stderr: {r.stderr!r}"
    assert r.payload.get("found_in") == ["api_keys"], r.payload


def test_already_revoked_key_is_not_a_failure():
    """Rows exist but nothing was live: the credential is dead, which is what a
    rotation actually needs to know. Report it, don't fail on it."""
    r = _run(api=(0, 0), mcp=(1, 0))
    assert r.code == 0, f"exit {r.code}; stderr: {r.stderr!r}"
    assert r.payload.get("already_revoked") is True, r.payload
    assert r.payload.get("revoked_in_mcp_dev_keys") == 0, r.payload


# ── (6) ★ the verdict must be MEASURED, not inferred from rowcount ─────────

@pytest.mark.parametrize("table,api,mcp", [
    ("api_keys",     (1, 1), (0, 0)),
    ("mcp_dev_keys", (0, 0), (1, 1)),
])
def test_update_that_does_not_take_is_reported_as_failure(table, api, mcp):
    """An UPDATE reporting rows is not proof the key is dead — that is the
    is_active=FALSE class of bug. cmd_revoke must re-read the post-state."""
    r = _run(api=api, mcp=mcp, stuck=(table,))
    assert r.code != 0, (
        f"{table} still has a live row after the UPDATE, so the key may still "
        f"authenticate; exit must be non-zero. got {r.code} with {r.payload}"
    )
    assert r.payload.get("still_live_in") == [table], r.payload
    assert "STILL LIVE" in r.stderr, r.stderr


# ── the operator-facing note must not restate the wrong model ──────────────

def test_note_does_not_claim_api_keys_is_the_only_authenticator():
    """The JSON note is what an operator reads mid-rotation. Until 2026-08-31 it
    said "api_keys is the ONLY table consulted for auth" and that a non-zero
    mcp_dev_keys count with api_keys 0 means the key is STILL LIVE — false for
    every MCP-minted key."""
    note = _run(api=(0, 0), mcp=(1, 1)).payload.get("note", "")
    assert note, "revoke output must carry an explanatory note"
    assert not re.search(r"ONLY table consulted", note, re.I), note
    assert not re.search(r"STILL LIVE", note), (
        "the note must not tell operators that an mcp_dev_keys-only revoke "
        f"leaves the key live; got: {note}"
    )
    assert "mcp_dev_keys" in note and "api_keys" in note, note


def test_mint_warns_that_the_key_does_not_authenticate():
    """mint writes mcp_dev_keys only, so the key it prints is not usable on the
    api_keys-gated REST routes. It must say so rather than imply otherwise."""
    src = _SRC.read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_mint")
    body = ast.get_source_segment(src, fn)
    assert "DOES NOT AUTHENTICATE" in body, "cmd_mint must not imply a live key"
